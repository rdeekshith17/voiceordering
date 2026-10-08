"""AI ON/OFF decision logic: modes, weekly windows, overnight spans, DST,
pauses / exceptions, emergency stops, and the next-change time."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from voiceorder.routing import Override, RoutingConfig, Window, evaluate

CHI = ZoneInfo("America/Chicago")


def at(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=CHI).timestamp()


def local(ts: float) -> str:
    return datetime.fromtimestamp(ts, CHI).strftime("%a %Y-%m-%d %H:%M")


# Oct 5 2026 is a Monday.
LUNCH_DINNER = [Window(d, 11 * 60, 14 * 60) for d in range(7)] + \
               [Window(d, 17 * 60, 21 * 60) for d in range(7)]


def sched(windows, **kw) -> RoutingConfig:
    return RoutingConfig(mode="scheduled", windows=windows, **kw)


def test_always_on_and_always_off():
    assert evaluate(RoutingConfig(mode="always_on"), at(2026, 10, 5, 3), CHI).ai is True
    d = evaluate(RoutingConfig(mode="always_off"), at(2026, 10, 5, 12), CHI)
    assert d.ai is False and d.reason == "Always off" and d.next_change_at is None


def test_two_windows_a_day_and_the_gap_between():
    cfg = sched(LUNCH_DINNER)
    assert evaluate(cfg, at(2026, 10, 5, 11), CHI).ai is True          # opens on the minute
    assert evaluate(cfg, at(2026, 10, 5, 13, 59), CHI).ai is True
    gap = evaluate(cfg, at(2026, 10, 5, 14), CHI)                      # closes on the minute
    assert gap.ai is False and gap.reason == "Outside scheduled hours"
    assert local(gap.next_change_at) == "Mon 2026-10-05 17:00"
    on = evaluate(cfg, at(2026, 10, 5, 18), CHI)
    assert on.reason == "Scheduled hours (Monday 5:00 PM-9:00 PM)"
    assert local(on.next_change_at) == "Mon 2026-10-05 21:00"


def test_weekdays_only_skips_the_weekend():
    cfg = sched([Window(d, 9 * 60, 17 * 60) for d in range(5)])
    assert evaluate(cfg, at(2026, 10, 9, 12), CHI).ai is True    # Friday
    sat = evaluate(cfg, at(2026, 10, 10, 12), CHI)                # Saturday
    assert sat.ai is False and local(sat.next_change_at) == "Mon 2026-10-12 09:00"


def test_overnight_window_crosses_midnight_and_the_week_boundary():
    cfg = sched([Window(4, 18 * 60, 1 * 60),    # Fri 18:00 -> Sat 01:00
                 Window(6, 22 * 60, 2 * 60)])   # Sun 22:00 -> Mon 02:00 (wraps the week)
    assert evaluate(cfg, at(2026, 10, 9, 23, 30), CHI).ai is True   # Fri night
    assert evaluate(cfg, at(2026, 10, 10, 0, 30), CHI).ai is True   # Sat 00:30
    assert evaluate(cfg, at(2026, 10, 10, 1, 0), CHI).ai is False   # Sat 01:00
    assert evaluate(cfg, at(2026, 10, 12, 1, 30), CHI).ai is True   # Mon 01:30, from Sunday
    assert evaluate(cfg, at(2026, 10, 12, 2, 0), CHI).ai is False


def test_windows_follow_local_time_across_dst_changes():
    cfg = sched([Window(d, 9 * 60, 17 * 60) for d in range(7)])
    # 2026: DST starts Sun Mar 8, ends Sun Nov 1 in Chicago.
    for day in ((2026, 3, 7), (2026, 3, 8), (2026, 3, 9), (2026, 10, 31), (2026, 11, 1), (2026, 11, 2)):
        assert evaluate(cfg, at(*day, 9, 0), CHI).ai is True, day
        assert evaluate(cfg, at(*day, 8, 59), CHI).ai is False, day
        assert evaluate(cfg, at(*day, 17, 0), CHI).ai is False, day
    # The next-change time lands on 9:00 local even when the clocks change overnight.
    d = evaluate(cfg, at(2026, 3, 7, 20), CHI)
    assert local(d.next_change_at) == "Sun 2026-03-08 09:00"
    d = evaluate(cfg, at(2026, 10, 31, 20), CHI)
    assert local(d.next_change_at) == "Sun 2026-11-01 09:00"


def test_pause_beats_schedule_and_newest_override_wins():
    now = at(2026, 10, 5, 12)
    pause = Override("p1", starts_at=now - 60, ends_at=now + 3600, ai_on=False,
                     reason="short staffed", created_at=now - 60)
    cfg = sched(LUNCH_DINNER, overrides=[pause])
    d = evaluate(cfg, now, CHI)
    assert d.ai is False and d.reason == "Paused: short staffed"
    assert d.next_change_at == now + 3600            # back on when the pause ends
    open_late = Override("e1", starts_at=now - 30, ends_at=None, ai_on=True, kind="exception",
                         reason="festival", created_at=now - 30)
    cfg.overrides.append(open_late)
    assert evaluate(cfg, now, CHI).ai is True            # newer override wins
    holiday = Override("h1", at(2026, 12, 25, 0), at(2026, 12, 26, 0), ai_on=False,
                       kind="exception", reason="Christmas", created_at=now)
    cfg2 = sched(LUNCH_DINNER, overrides=[holiday])
    assert evaluate(cfg2, at(2026, 12, 25, 12), CHI).reason == "Date exception: Christmas"
    assert evaluate(cfg2, at(2026, 12, 26, 12), CHI).ai is True
    expired = Override("x", now - 7200, now - 3600, ai_on=False, created_at=now - 7200)
    assert evaluate(sched(LUNCH_DINNER, overrides=[expired]), now, CHI).ai is True


def test_emergency_stops_beat_everything():
    now = at(2026, 10, 5, 12)
    always = RoutingConfig(mode="always_on")
    assert evaluate(always, now, CHI, platform_emergency=True).reason == "Platform emergency stop"
    stopped = RoutingConfig(mode="always_on", emergency_off=True,
                            overrides=[Override("o", now - 1, None, ai_on=True, created_at=now)])
    d = evaluate(stopped, now, CHI)
    assert d.ai is False and d.next_change_at is None


def test_scheduled_with_no_windows_is_off_not_an_error():
    d = evaluate(sched([]), at(2026, 10, 5, 12), CHI)
    assert d.ai is False and d.next_change_at is None
