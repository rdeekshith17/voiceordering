"""Should the AI answer this call? Pure decision logic, no I/O.

Inputs are a restaurant's routing config, its weekly windows, its pauses /
date exceptions, and the platform emergency stop. Precedence, highest first:

  1. platform emergency stop        -> OFF
  2. restaurant emergency stop      -> OFF
  3. active pause / exception       -> as set (newest wins)
  4. mode: always_on / always_off / scheduled (weekly windows)

Windows are local wall-clock times in the restaurant's IANA time zone, so a
9:00-21:00 window stays 9:00-21:00 across daylight-saving changes. A window
whose end is at or before its start runs past midnight (Fri 18:00 -> Sat 01:00).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

MODES = ("always_on", "always_off", "scheduled")
OFF_ACTIONS = ("transfer", "voicemail", "message")
NO_ANSWER_ACTIONS = ("voicemail", "message")
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEK = 7 * 1440


@dataclass(frozen=True)
class Window:
    day: int        # 0 = Monday
    start_min: int  # minutes after local midnight, 0-1439
    end_min: int    # end <= start means it ends the next day


@dataclass(frozen=True)
class Override:
    id: str
    starts_at: float
    ends_at: float | None  # None = until someone resumes
    ai_on: bool
    kind: str = "pause"    # pause | exception
    reason: str = ""
    created_at: float = 0.0


@dataclass
class RoutingConfig:
    mode: str = "always_on"
    off_action: str = "transfer"
    no_answer_action: str = "voicemail"
    closed_message: str = ""
    emergency_off: bool = False
    windows: list[Window] = field(default_factory=list)
    overrides: list[Override] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    ai: bool
    reason: str
    next_change_at: float | None = None  # when `ai` next flips, if within a week


def _local(ts: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def _in_windows(windows: list[Window], local: datetime) -> Window | None:
    minute = local.weekday() * 1440 + local.hour * 60 + local.minute
    for w in windows:
        start = w.day * 1440 + w.start_min
        length = (w.end_min - w.start_min) % 1440 or 1440  # end<=start wraps; equal = 24 h
        if (minute - start) % _WEEK < length:
            return w
    return None


def _active_override(overrides: list[Override], ts: float) -> Override | None:
    live = [o for o in overrides
            if o.starts_at <= ts and (o.ends_at is None or ts < o.ends_at)]
    return max(live, key=lambda o: (o.created_at, o.starts_at)) if live else None


def _fmt(minute: int) -> str:
    h, m = divmod(minute % 1440, 60)
    return datetime(2000, 1, 1, h, m).strftime("%I:%M %p").lstrip("0")


def _state(cfg: RoutingConfig, ts: float, tz: tzinfo | None,
           platform_emergency: bool) -> tuple[bool, str]:
    if platform_emergency:
        return False, "Platform emergency stop"
    if cfg.emergency_off:
        return False, "AI turned off now (until resumed)"
    o = _active_override(cfg.overrides, ts)
    if o is not None:
        what = "Paused" if o.kind == "pause" and not o.ai_on else (
            "Date exception" if o.kind == "exception" else "Override")
        return o.ai_on, f"{what}{': ' + o.reason if o.reason else ''}"
    if cfg.mode == "always_on":
        return True, "Always on"
    if cfg.mode == "always_off":
        return False, "Always off"
    w = _in_windows(cfg.windows, _local(ts, tz))
    if w is None:
        return False, "Outside scheduled hours"
    return True, f"Scheduled hours ({DAY_NAMES[w.day]} {_fmt(w.start_min)}-{_fmt(w.end_min)})"


def _boundaries(cfg: RoutingConfig, now: float, tz: tzinfo | None) -> list[float]:
    """Moments in the next 8 days where the answer could change."""
    out: list[float] = []
    for o in cfg.overrides:
        out += [t for t in (o.starts_at, o.ends_at) if t is not None and t > now]
    if cfg.mode == "scheduled":
        today = _local(now, tz).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        for offset in range(-1, 9):
            day = today + timedelta(days=offset)
            for w in cfg.windows:
                if w.day != day.weekday():
                    continue
                for minute in (w.start_min, w.start_min + ((w.end_min - w.start_min) % 1440 or 1440)):
                    wall = day + timedelta(minutes=minute)
                    ts = (wall.replace(tzinfo=tz) if tz else wall).timestamp()
                    if ts > now:
                        out.append(ts)
    return sorted(set(out))


def evaluate(cfg: RoutingConfig, now: float, tz: tzinfo | None = None,
             platform_emergency: bool = False) -> Decision:
    ai, reason = _state(cfg, now, tz, platform_emergency)
    next_change = None
    if not platform_emergency and not cfg.emergency_off:
        for t in _boundaries(cfg, now, tz):
            if t - now > 8 * 86400:
                break
            if _state(cfg, t + 1, tz, platform_emergency)[0] != ai:
                next_change = t
                break
    return Decision(ai=ai, reason=reason, next_change_at=next_change)
