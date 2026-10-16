"""Security posture checks: which protective settings are missing or unsafe.

Pure env inspection, no I/O beyond os.environ. Shown on the Super Admin
overview and logged at startup. SECURITY_STRICT=1 additionally makes the app
reject requests that would otherwise be accepted without protection.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    ok: bool
    severity: str  # critical | high | medium | low (what it means when not ok)
    detail: str
    fix: str

    def to_dict(self) -> dict:
        return asdict(self)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def strict_mode() -> bool:
    """SECURITY_STRICT=1: unprotected requests are refused instead of allowed."""
    return _env("SECURITY_STRICT") == "1"


def chat_demo_enabled() -> bool:
    """Public /chat demo: off unless explicitly turned on (CHAT_DEMO_ENABLED=1)."""
    return _env("CHAT_DEMO_ENABLED") == "1"


def signup_enabled() -> bool:
    """Public restaurant self-signup: off unless explicitly turned on (PORTAL_SIGNUP_ENABLED=1)."""
    return _env("PORTAL_SIGNUP_ENABLED") == "1"


def run_checks() -> list[Check]:
    base = _env("PUBLIC_BASE_URL")
    tts_on = os.environ.get("TTS_ENABLED", "1") == "1"
    checks = [
        Check("twilio_auth_token", "Twilio call signature check", bool(_env("TWILIO_AUTH_TOKEN")),
              "critical",
              "Without TWILIO_AUTH_TOKEN anyone can send fake calls to /twilio/* and place real orders.",
              "Set TWILIO_AUTH_TOKEN (Twilio console > Account > API keys & tokens)."),
        Check("public_base_url", "Public https address", base.startswith("https://"), "critical",
              "Twilio signatures are checked against PUBLIC_BASE_URL; missing or non-https turns the check off.",
              "Set PUBLIC_BASE_URL to the exact https URL Twilio calls, e.g. https://voiceorderai.onrender.com."),
        Check("voice_secret", "API secret (VOICE_SECRET)", bool(_env("VOICE_SECRET")), "high",
              "Without it /call/start, /tools/*, /orders/recent and /voice/speak are open to anyone.",
              "Set VOICE_SECRET to a long random value."),
        Check("tenant_master_key", "POS credential encryption key", bool(_env("TENANT_MASTER_KEY")), "high",
              "Without it a key file on local disk is used; losing it makes saved POS logins unreadable.",
              "Set TENANT_MASTER_KEY and keep an offline copy. Never change it once set."),
        Check("database_url", "Permanent database", bool(_env("DATABASE_URL")), "high",
              "Without DATABASE_URL data is kept in a local SQLite file that Render wipes on every deploy.",
              "Set DATABASE_URL to your Postgres (e.g. Neon) connection string."),
        Check("owner_secret", "Owner script secret (OWNER_SECRET)", bool(_env("OWNER_SECRET")), "low",
              "Only affects scripted access to /owner; signed-in restaurant admins still work without it.",
              "Set OWNER_SECRET to a long random value."),
        Check("chat_demo", "Public text demo is off", not chat_demo_enabled(), "high",
              "When on, anyone on the internet can place real orders through /chat and spend AI credits.",
              "Remove CHAT_DEMO_ENABLED or set it to 0."),
        Check("signup", "Public restaurant signup is off", not signup_enabled(), "medium",
              "When on, anyone can create a restaurant account at /portal/signup.",
              "Remove PORTAL_SIGNUP_ENABLED or set it to 0; add restaurants from /admin."),
        Check("anthropic", "AI key (Anthropic)", bool(_env("ANTHROPIC_API_KEY")), "high",
              "Without it phone calls can't be answered by the AI.",
              "Set ANTHROPIC_API_KEY."),
        Check("twilio_sid", "Twilio account (SMS receipts)", bool(_env("TWILIO_ACCOUNT_SID")), "low",
              "Without it SMS receipts are not sent.",
              "Set TWILIO_ACCOUNT_SID."),
        Check("strict", "Strict mode (refuse unprotected requests)", strict_mode(), "low",
              "Off: if a secret above goes missing, requests are still accepted (with a warning).",
              "Once every check above is green, set SECURITY_STRICT=1."),
    ]
    if tts_on:
        checks.append(Check("elevenlabs", "Voice key (ElevenLabs)", bool(_env("ELEVENLABS_API_KEY")), "medium",
                            "Without it callers hear the plain Twilio voice instead of ElevenLabs.",
                            "Set ELEVENLABS_API_KEY."))
    return sorted(checks, key=lambda c: (c.ok, SEVERITY_ORDER[c.severity]))


def problems(checks: list[Check] | None = None) -> list[Check]:
    return [c for c in (checks if checks is not None else run_checks()) if not c.ok]
