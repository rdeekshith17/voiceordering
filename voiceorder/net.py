"""Outbound HTTP helper (stdlib only).

This app's hosting sandbox requires outbound HTTPS to go through an egress
proxy configured via the usual ``*_proxy`` environment variables. urllib's
default opener does not reliably pick those up here (direct connections hang),
so every outbound vendor call -- Square, Toast, Clover, Twilio, ElevenLabs --
goes through this helper, which builds a proxy-aware opener explicitly.

When no proxy is configured (ordinary production hosting) it falls back to a
direct connection. The raised exceptions are the same as urllib's
(``HTTPError``/``URLError``), so call sites keep their existing handling.

This module is also the single seam the test suite patches
(``voiceorder.net.urlopen``) instead of reaching into urllib.
"""
from __future__ import annotations

import os
import urllib.request


def _proxy_map() -> dict[str, str]:
    proxies: dict[str, str] = {}
    for scheme, var in (("http", "http_proxy"), ("https", "https_proxy")):
        val = os.environ.get(var) or os.environ.get(var.upper())
        if val:
            proxies[scheme] = val
    return proxies


def urlopen(req: urllib.request.Request, timeout: float = 30):
    """Drop-in for ``urllib.request.urlopen`` that honors the sandbox proxy."""
    proxies = _proxy_map()
    if proxies:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
