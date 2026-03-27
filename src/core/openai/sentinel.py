"""Helpers for OpenAI Sentinel proof-of-work tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence

from ..browser_profile import BrowserProfile


DEFAULT_SENTINEL_DIFF = "0fffff"
DEFAULT_MAX_ITERATIONS = 500_000
_SCREEN_SIGNATURES = (3000, 3120, 4000, 4160)
_LANGUAGE_SIGNATURE = "en-US,es-US,en,es"
_NAVIGATOR_KEYS = ("location", "ontransitionend", "onprogress")
_WINDOW_KEYS = ("window", "document", "navigator")


class SentinelPOWError(RuntimeError):
    """Raised when a Sentinel proof-of-work token cannot be solved."""


def _format_browser_time(browser_profile: BrowserProfile | None = None) -> str:
    """Match the browser-style timestamp used by public Sentinel solvers."""
    if browser_profile:
        timezone_map = {
            "America/New_York": timezone(timedelta(hours=-5)),
            "America/Chicago": timezone(timedelta(hours=-6)),
            "America/Denver": timezone(timedelta(hours=-7)),
            "America/Los_Angeles": timezone(timedelta(hours=-8)),
            "America/Phoenix": timezone(timedelta(hours=-7)),
        }
        browser_now = datetime.now(timezone_map.get(browser_profile.timezone, timezone(timedelta(hours=-5))))
        return browser_now.strftime("%a %b %d %Y %H:%M:%S") + f" {browser_profile.timezone_display}"

    browser_now = datetime.now(timezone(timedelta(hours=-5)))
    return browser_now.strftime("%a %b %d %Y %H:%M:%S") + " GMT-0500 (Eastern Standard Time)"


def build_sentinel_config(user_agent: str, browser_profile: BrowserProfile | None = None) -> list:
    """Build a browser-like fingerprint payload for the Sentinel PoW solver."""
    perf_ms = time.perf_counter() * 1000
    epoch_ms = (time.time() * 1000) - perf_ms
    language_signature = _LANGUAGE_SIGNATURE
    locale = "en-US"
    screen_signature = random.choice(_SCREEN_SIGNATURES)
    hardware_concurrency = 8

    if browser_profile:
        if browser_profile.languages:
            language_signature = ",".join(browser_profile.languages)
        locale = browser_profile.locale
        screen_signature = browser_profile.screen_width + browser_profile.screen_height
        hardware_concurrency = browser_profile.hardware_concurrency

    return [
        screen_signature,
        _format_browser_time(browser_profile),
        4294705152,
        0,
        user_agent,
        "",
        "",
        locale,
        language_signature,
        0,
        random.choice(_NAVIGATOR_KEYS),
        "location",
        random.choice(_WINDOW_KEYS),
        perf_ms,
        str(uuid.uuid4()),
        "",
        hardware_concurrency,
        epoch_ms,
    ]


def _encode_pow_payload(config: Sequence[object], nonce: int) -> bytes:
    prefix = (json.dumps(config[:3], separators=(",", ":"), ensure_ascii=False)[:-1] + ",").encode("utf-8")
    middle = (
        "," + json.dumps(config[4:9], separators=(",", ":"), ensure_ascii=False)[1:-1] + ","
    ).encode("utf-8")
    suffix = ("," + json.dumps(config[10:], separators=(",", ":"), ensure_ascii=False)[1:]).encode("utf-8")
    body = prefix + str(nonce).encode("ascii") + middle + str(nonce >> 1).encode("ascii") + suffix
    return base64.b64encode(body)


def solve_sentinel_pow(
    seed: str,
    difficulty: str,
    config: Sequence[object],
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> str:
    """Solve the Sentinel PoW challenge and return the base64 payload."""
    seed_bytes = seed.encode("utf-8")
    target = bytes.fromhex(difficulty)
    prefix_length = len(target)

    for nonce in range(max_iterations):
        encoded = _encode_pow_payload(config, nonce)
        digest = hashlib.sha3_512(seed_bytes + encoded).digest()
        if digest[:prefix_length] <= target:
            return encoded.decode("ascii")

    raise SentinelPOWError(f"failed to solve sentinel pow after {max_iterations} attempts")


def build_sentinel_pow_token(
    user_agent: str,
    difficulty: str = DEFAULT_SENTINEL_DIFF,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    browser_profile: BrowserProfile | None = None,
) -> str:
    """Build the `p` token required by the Sentinel request endpoint."""
    config = build_sentinel_config(user_agent, browser_profile=browser_profile)
    seed = format(random.random())
    solution = solve_sentinel_pow(seed, difficulty, config, max_iterations=max_iterations)
    return f"gAAAAAC{solution}"
