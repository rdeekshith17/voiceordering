"""Keep customer phone numbers out of the logs.

A logging filter that masks anything shaped like a phone number in every log
line to its last four digits (+1 415 555 0123 -> ***0123). It matches whole
numbers only, so Twilio call SIDs (letters and digits) and order numbers
stay readable for debugging.
"""
from __future__ import annotations

import logging
import re

_PHONE = re.compile(
    r"(?<![\w+])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w])"
    r"|(?<![\w+])\+\d{8,15}(?![\w])")


def mask_phones(text: str) -> str:
    return _PHONE.sub(lambda m: "***" + re.sub(r"\D", "", m.group(0))[-4:], text)


class PhoneRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = mask_phones(message)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


def install() -> None:
    """Mask phone numbers in everything the root handlers write."""
    flt = PhoneRedactingFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, PhoneRedactingFilter) for f in handler.filters):
            handler.addFilter(flt)
