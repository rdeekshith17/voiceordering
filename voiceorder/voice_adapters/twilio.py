"""Twilio voice adapter (Phase 5a): TwiML builders, webhook signature validation, SMS.

Stdlib only — no Twilio SDK, so there is nothing new to install and the
voice_adapters layer stays thin. main.py translates between these helpers and
the order engine at the edge.

Call flow (the "Gather loop"):
  POST /twilio/voice   Twilio answers -> Play greeting -> Gather speech
  POST /twilio/gather  SpeechResult -> agent turn -> Play reply -> Gather again
  POST /twilio/status  call ended -> close the cart
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

from .. import net
from xml.sax.saxutils import escape


class TwilioError(RuntimeError):
    """Twilio rejected the request or was unreachable."""


def _response(inner: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>'


def _gather(action_url: str) -> str:
    return (
        f'<Gather input="speech" action="{escape(action_url)}" method="POST" '
        'speechTimeout="auto" actionOnEmptyResult="true" language="en-US" />'
    )


def _redirect_fallback() -> str:
    # If Gather ever completes without hitting the action URL, end politely
    # instead of leaving the caller in silence.
    return "<Say>Thanks for calling. Goodbye.</Say><Hangup />"


def answer_call(
    greeting_audio_url: str | None, greeting_text: str, gather_action_url: str
) -> str:
    """TwiML for an incoming call: play the greeting, then listen.

    greeting_audio_url is None when TTS synthesis failed -- fall back to
    Twilio <Say> so the caller never hears silence (or a 500 from the
    webhook)."""
    if greeting_audio_url:
        spoken = f"<Play>{escape(greeting_audio_url)}</Play>"
    else:
        spoken = f"<Say>{escape(greeting_text)}</Say>"
    return _response(
        f"{spoken}"
        f"{_gather(gather_action_url)}"
        f"{_redirect_fallback()}"
    )


def continue_call(reply_audio_url: str | None, reply_text: str, gather_action_url: str) -> str:
    """Play the agent's reply (MP3 when synthesis worked, Twilio Say as
    fallback), then keep listening."""
    if reply_audio_url:
        spoken = f"<Play>{escape(reply_audio_url)}</Play>"
    else:
        spoken = f"<Say>{escape(reply_text)}</Say>"
    return _response(f"{spoken}{_gather(gather_action_url)}{_redirect_fallback()}")


def end_call(reply_audio_url: str | None, reply_text: str) -> str:
    """Final reply (order confirmed, call transferred announcement, ...),
    then hang up."""
    if reply_audio_url:
        spoken = f"<Play>{escape(reply_audio_url)}</Play>"
    else:
        spoken = f"<Say>{escape(reply_text)}</Say>"
    return _response(f"{spoken}<Hangup />")


def transfer_call(announcement_audio_url: str | None, announcement: str, number: str,
                  action_url: str | None = None, timeout: int = 20) -> str:
    """Play the handoff line, then dial the restaurant for real.

    With action_url, the call rings for `timeout` seconds and Twilio then posts
    the outcome (DialCallStatus) there, so a busy or unanswered line can fall
    back to voicemail or a message instead of just ending."""
    if announcement_audio_url:
        spoken = f"<Play>{escape(announcement_audio_url)}</Play>"
    elif announcement:
        spoken = f"<Say>{escape(announcement)}</Say>"
    else:
        spoken = ""
    if action_url:
        dial = (f'<Dial timeout="{int(timeout)}" action="{escape(action_url)}" method="POST">'
                f"{escape(number)}</Dial>")
    else:
        dial = f"<Dial>{escape(number)}</Dial>"
    return _response(f"{spoken}{dial}")


def voicemail(prompt: str, action_url: str, max_seconds: int = 120) -> str:
    """Invite a message after the beep; Twilio posts the recording to action_url.
    If the caller records nothing, Twilio continues to the goodbye line."""
    return _response(
        f"<Say>{escape(prompt)}</Say>"
        f'<Record maxLength="{int(max_seconds)}" playBeep="true" trim="trim-silence" '
        f'action="{escape(action_url)}" method="POST" />'
        "<Say>We didn't get a message. Goodbye.</Say><Hangup />"
    )


def closed_message(message: str) -> str:
    """Say the restaurant's message and end the call."""
    return _response(f"<Say>{escape(message)}</Say><Hangup />")


def unavailable() -> str:
    """The agent brain (LLM) isn't configured: say so and hang up."""
    return _response(
        "<Say>Sorry, our ordering system is unavailable right now. "
        "Please call back later. Goodbye.</Say><Hangup />"
    )


def validate_signature(
    url: str, params: dict[str, str], signature: str, auth_token: str
) -> bool:
    """Verify Twilio's X-Twilio-Signature header.

    Twilio signs the full public URL with sorted POST params appended,
    HMAC-SHA1 with the account's auth token, base64-encoded. `url` must be the
    public URL Twilio called (PUBLIC_BASE_URL + path), not the internal one.
    """
    data = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def send_sms(
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    body: str,
) -> str:
    """Send an SMS via the Twilio REST API. Returns the message SID."""
    api_url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    )
    payload = urllib.parse.urlencode(
        {"From": from_number, "To": to_number, "Body": body}
    ).encode("utf-8")
    credentials = base64.b64encode(
        f"{account_sid}:{auth_token}".encode("utf-8")
    ).decode("ascii")
    req = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )
    try:
        with net.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300]
        raise TwilioError(f"Twilio SMS failed: HTTP {exc.code}: {detail!r}") from exc
    except OSError as exc:
        raise TwilioError(f"Twilio unreachable: {exc}") from exc
    return str(result.get("sid", ""))
