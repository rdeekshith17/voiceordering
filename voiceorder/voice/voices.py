"""ElevenLabs voice + model catalog for the tenant portal's voice picker.

The voice list was fetched live from GET /v1/voices on 2026-09-29 against the
connected free-tier account (21 premade voices). Voice IDs are stable
ElevenLabs identifiers; the list only needs a refresh if ElevenLabs adds or
retires premade voices.

Free-tier note: not every premade voice is usable via the API on a free
account (some return 402 paid_plan_required). The portal's Preview button
synthesizes a short sample on demand, so owners discover what works without
guessing -- and without us hardcoding availability claims that go stale.
"""
from __future__ import annotations

import re

# (voice_id, display name, gender, age, accent)
ELEVENLABS_VOICES: tuple[tuple[str, str, str, str, str], ...] = (
    ("EXAVITQu4vr4xnSDxMaL", "Sarah", "female", "young", "american"),
    ("CwhRBWXzGAHq8TQ4Fs17", "Roger", "male", "middle_aged", "american"),
    ("FGY2WhTYpPnrIDTdsKH5", "Laura", "female", "young", "american"),
    ("IKne3meq5aSn9XLyUdCD", "Charlie", "male", "young", "australian"),
    ("JBFqnCBsd6RMkjVDRZzb", "George", "male", "middle_aged", "british"),
    ("N2lVS1w4EtoT3dr4eOWO", "Callum", "male", "middle_aged", "american"),
    ("SAz9YHcvj6GT2YYXdXww", "River", "neutral", "middle_aged", "american"),
    ("SOYHLrjzK2X1ezoPC6cr", "Harry", "male", "young", "american"),
    ("TX3LPaxmHKxFdv7VOQHJ", "Liam", "male", "young", "american"),
    ("Xb7hH8MSUJpSbSDYk0k2", "Alice", "female", "middle_aged", "british"),
    ("XrExE9yKIg1WjnnlVkGX", "Matilda", "female", "middle_aged", "american"),
    ("bIHbv24MWmeRgasZH58o", "Will", "male", "young", "american"),
    ("cgSgspJ2msm6clMCkdW9", "Jessica", "female", "young", "american"),
    ("cjVigY5qzO86Huf0OWal", "Eric", "male", "middle_aged", "american"),
    ("hpp4J3VqNfWAUOO0d1Us", "Bella", "female", "middle_aged", "american"),
    ("iP95p4xoKVk53GoZ742B", "Chris", "male", "middle_aged", "american"),
    ("nPczCjzI2devNBz1zQrb", "Brian", "male", "middle_aged", "american"),
    ("onwK4e9ZLuTAKqWW03F9", "Daniel", "male", "middle_aged", "british"),
    ("pFZP5JQG7iQjIQuC4Bku", "Lily", "female", "middle_aged", "british"),
    ("pNInz6obpgDQGcFmaJgB", "Adam", "male", "middle_aged", "american"),
    ("pqHfZKP75CvOlQylNhV4", "Bill", "male", "old", "american"),
)

# (model_id, label). Only models known to work on free-tier accounts.
ELEVENLABS_MODELS: tuple[tuple[str, str], ...] = (
    ("eleven_flash_v2_5", "Flash v2.5 — fastest, lowest latency"),
    ("eleven_turbo_v2_5", "Turbo v2.5 — high quality, still fast"),
)

DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah
DEFAULT_MODEL_ID = "eleven_flash_v2_5"

_KNOWN_VOICE_IDS = frozenset(v[0] for v in ELEVENLABS_VOICES)
_KNOWN_MODEL_IDS = frozenset(m[0] for m in ELEVENLABS_MODELS)

# Cloned / custom voices have the same opaque-ID shape; allow them so owners
# aren't limited to the premade list.
_CUSTOM_VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")


def is_known_voice(voice_id: str) -> bool:
    """True for a catalog voice or a plausible custom/cloned voice ID."""
    return voice_id in _KNOWN_VOICE_IDS or bool(_CUSTOM_VOICE_RE.match(voice_id or ""))


def is_known_model(model_id: str) -> bool:
    return model_id in _KNOWN_MODEL_IDS


def voice_label(voice_id: str) -> str:
    for vid, name, gender, age, accent in ELEVENLABS_VOICES:
        if vid == voice_id:
            return f"{name} ({gender}, {age}, {accent})"
    return "Custom voice"


def model_label(model_id: str) -> str:
    for mid, label in ELEVENLABS_MODELS:
        if mid == model_id:
            return label
    return model_id
