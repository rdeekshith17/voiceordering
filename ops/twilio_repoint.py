#!/usr/bin/env python3
"""Repoint the Twilio trial number's webhooks at a new public base URL.

Why: the number's VoiceUrl still points at the dead localtunnel URL, so
inbound calls hear "application error". After a working tunnel is up
(see ops/ngrok_tunnel.sh), this moves VoiceUrl + StatusCallbackUrl to it.

Auth: TWILIO_AUTH_TOKEN env var (transient paste; never printed or logged).
Only the account SID below is embedded (an identifier, not a secret).

Usage:
    TWILIO_AUTH_TOKEN=<paste> ./twilio_repoint.py --url https://xxxx.ngrok.io [--dry-run]

--dry-run prints the exact API calls without sending them.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.expanduser("~/workspace/voiceorder"))
from voiceorder.net import urlopen  # proxy-aware opener for this sandbox

ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
NUMBER = "+15622680097"
API = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}"


def _auth(token: str) -> str:
    return "Basic " + base64.b64encode(f"{ACCOUNT_SID}:{token}".encode()).decode()


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        API + path, headers={"Authorization": _auth(token)}
    )
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def _post(path: str, token: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={"Authorization": _auth(token),
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="new public base URL, e.g. https://xxxx.ngrok.io")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not token and not args.dry_run:
        print("error: TWILIO_AUTH_TOKEN env var is required", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"would GET  {API}/IncomingPhoneNumbers.json?PhoneNumber=%2B15622680097")
        print(f"would POST <number-sid>.json VoiceUrl={base}/twilio/voice "
              f"StatusCallback={base}/twilio/status (VoiceMethod/StatusCallbackMethod=POST)")
        return 0

    nums = _get("/IncomingPhoneNumbers.json?PhoneNumber=%2B15622680097", token)
    matches = [n for n in nums.get("incoming_phone_numbers", [])
               if n.get("phone_number") == NUMBER]
    if not matches:
        print(f"error: {NUMBER} not found on this account", file=sys.stderr)
        return 1
    pn = matches[0]
    sid = pn["sid"]
    print(f"found {NUMBER} sid={sid}")
    print(f"  current VoiceUrl:       {pn.get('voice_url')}")
    print(f"  current StatusCallback: {pn.get('status_callback')}")

    updated = _post(f"/IncomingPhoneNumbers/{sid}.json", token, {
        "VoiceUrl": f"{base}/twilio/voice",
        "VoiceMethod": "POST",
        "StatusCallback": f"{base}/twilio/status",
        "StatusCallbackMethod": "POST",
    })
    print("updated:")
    print(f"  VoiceUrl:       {updated.get('voice_url')}")
    print(f"  StatusCallback: {updated.get('status_callback')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
