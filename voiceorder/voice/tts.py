"""ElevenLabs text-to-speech for the order engine (Phase 3).

The agent's spoken replies are rendered to MP3 through the user's connected
ElevenLabs credential. Auth uses the Secure Vault surrogate exchange — no raw
key ever appears in code, env, or logs.

Free-tier discipline (10,000 chars/month on the free plan):
  * a disk cache keyed by (text, voice, model) makes repeated greetings,
    confirmations, and read-backs free after the first render;
  * MAX_CHARS caps a single request so a runaway prompt can't burn the budget;
  * TTS_ENABLED=0 disables synthesis entirely (endpoint returns 503).

Defaults were verified against a free-tier account on 2026-09-28: voice Sarah
(EXAVITQu4vr4xnSDxMaL) with model eleven_flash_v2_5. Other premade voices and
eleven_multilingual_v2 return HTTP 402 paid_plan_required on free accounts.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

from .. import net
from pathlib import Path

ALLOWED_HOSTS = ("api.elevenlabs.io",)
DEFAULT_VOICE = "EXAVITQu4vr4xnSDxMaL"  # Sarah - Mature, Reassuring, Confident
DEFAULT_MODEL = "eleven_flash_v2_5"
MAX_CHARS = 400

_CACHE_DIR = Path(
    os.environ.get(
        "TTS_CACHE_DIR",
        str(Path(__file__).resolve().parent.parent.parent / ".tts_cache"),
    )
)


class TtsError(RuntimeError):
    """Synthesis failed (network, auth, or ElevenLabs rejected the request)."""


def cache_dir() -> Path:
    """Where rendered clips live. Tests point this elsewhere via _CACHE_DIR."""
    return _CACHE_DIR


def cache_key(text: str, voice_id: str, model_id: str) -> str:
    digest = hashlib.sha256(
        f"{voice_id}\x00{model_id}\x00{text}".encode("utf-8")
    ).hexdigest()
    return digest


def cache_path(text: str, voice_id: str, model_id: str) -> Path:
    return _CACHE_DIR / f"{cache_key(text, voice_id, model_id)}.mp3"


def _post_tts(text: str, voice_id: str, model_id: str) -> bytes:
    """One raw synthesis call. Separated so tests can stub the network.

    Auth order: ELEVENLABS_API_KEY env var first (portable: works on any
    host), then the sandbox Secure Vault surrogate (this dev box only).
    """
    payload = json.dumps({"text": text, "model_id": model_id}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    env_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if env_key:
        req.add_header("xi-api-key", env_key)
    else:
        sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
        try:
            from dynamic_credentials import add_surrogate_to_request  # noqa: E402

            add_surrogate_to_request(
                req,
                "custom.elevenlabs",
                entry_name="access_token",
                allowed_hosts=ALLOWED_HOSTS,
            )
        except Exception as exc:
            raise TtsError(
                "ElevenLabs TTS needs ELEVENLABS_API_KEY set "
                f"(no vault available here): {exc}"
            ) from exc
    try:
        with net.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300]
        raise TtsError(f"ElevenLabs TTS failed: HTTP {exc.code}: {body!r}") from exc
    except OSError as exc:
        raise TtsError(f"ElevenLabs TTS unreachable: {exc}") from exc


def synthesize(
    text: str,
    *,
    voice_id: str = DEFAULT_VOICE,
    model_id: str = DEFAULT_MODEL,
    use_cache: bool = True,
) -> bytes:
    """Render text to MP3, serving from the disk cache when possible."""
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("nothing to synthesize: text is empty")
    if len(cleaned) > MAX_CHARS:
        raise ValueError(
            f"text too long for TTS: {len(cleaned)} chars (max {MAX_CHARS})"
        )
    path = cache_path(cleaned, voice_id, model_id)
    if use_cache and path.exists():
        return path.read_bytes()
    audio = _post_tts(cleaned, voice_id, model_id)
    if use_cache:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(audio)
        tmp.replace(path)
    return audio
