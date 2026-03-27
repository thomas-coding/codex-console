"""
轻量浏览器画像生成器。

默认关闭，仅在注册链路显式启用时使用。
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Optional


_CHROME_MAJOR_VERSIONS = (131, 132, 133)
_LANGUAGE_PACKS = (
    {
        "accept_language": "en-US,en;q=0.9",
        "languages": ("en-US", "en"),
        "locale": "en-US",
    },
    {
        "accept_language": "en-US,es-US;q=0.9,en;q=0.8",
        "languages": ("en-US", "es-US", "en"),
        "locale": "en-US",
    },
)
_TIMEZONES = (
    ("America/New_York", "GMT-0500 (Eastern Standard Time)"),
    ("America/Chicago", "GMT-0600 (Central Standard Time)"),
    ("America/Denver", "GMT-0700 (Mountain Standard Time)"),
    ("America/Los_Angeles", "GMT-0800 (Pacific Standard Time)"),
    ("America/Phoenix", "GMT-0700 (Mountain Standard Time)"),
)
_SCREEN_PROFILES = (
    {"screen_width": 1920, "screen_height": 1080, "viewport_width": 1920, "viewport_height": 947},
    {"screen_width": 1920, "screen_height": 1080, "viewport_width": 1536, "viewport_height": 864},
    {"screen_width": 1536, "screen_height": 864, "viewport_width": 1536, "viewport_height": 730},
    {"screen_width": 1366, "screen_height": 768, "viewport_width": 1366, "viewport_height": 629},
    {"screen_width": 1600, "screen_height": 900, "viewport_width": 1600, "viewport_height": 761},
)
_HARDWARE_PROFILES = (
    {"hardware_concurrency": 4, "device_memory": 4},
    {"hardware_concurrency": 8, "device_memory": 8},
    {"hardware_concurrency": 12, "device_memory": 8},
    {"hardware_concurrency": 16, "device_memory": 16},
)
_WEBGL_PROFILES = (
    {
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0)",
    },
    {
        "webgl_vendor": "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, Radeon RX 580 Direct3D11 vs_5_0 ps_5_0)",
    },
    {
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0)",
    },
)


@dataclass(frozen=True)
class BrowserProfile:
    profile_id: str
    family: str
    proxy_ip: Optional[str]
    proxy_country: Optional[str]
    chrome_major_version: int
    user_agent: str
    accept_language: str
    languages: tuple[str, ...]
    locale: str
    timezone: str
    timezone_display: str
    platform: str
    viewport_width: int
    viewport_height: int
    screen_width: int
    screen_height: int
    color_depth: int
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str

    def to_log_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "family": self.family,
            "proxy_ip": self.proxy_ip,
            "proxy_country": self.proxy_country,
            "user_agent": self.user_agent,
            "accept_language": self.accept_language,
            "locale": self.locale,
            "timezone": self.timezone,
            "viewport": f"{self.viewport_width}x{self.viewport_height}",
            "screen": f"{self.screen_width}x{self.screen_height}",
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
        }


def _build_user_agent(chrome_major_version: int) -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_major_version}.0.0.0 Safari/537.36"
    )


def build_browser_profile(
    *,
    proxy_ip: Optional[str] = None,
    proxy_country: Optional[str] = None,
) -> BrowserProfile:
    """
    构造一份轻量、统一的浏览器画像。

    当前首版固定在美区 Windows Chrome 桌面模板池内随机。
    """
    chrome_major_version = random.choice(_CHROME_MAJOR_VERSIONS)
    language_pack = random.choices(_LANGUAGE_PACKS, weights=(80, 20), k=1)[0]
    timezone_name, timezone_display = random.choices(_TIMEZONES, weights=(35, 25, 10, 25, 5), k=1)[0]
    screen_profile = random.choice(_SCREEN_PROFILES)
    hardware_profile = random.choices(_HARDWARE_PROFILES, weights=(20, 45, 20, 15), k=1)[0]
    webgl_profile = random.choice(_WEBGL_PROFILES)

    return BrowserProfile(
        profile_id=uuid.uuid4().hex[:12],
        family="us_windows_chrome_desktop",
        proxy_ip=str(proxy_ip or "").strip() or None,
        proxy_country=str(proxy_country or "").strip().upper() or "US",
        chrome_major_version=chrome_major_version,
        user_agent=_build_user_agent(chrome_major_version),
        accept_language=language_pack["accept_language"],
        languages=tuple(language_pack["languages"]),
        locale=language_pack["locale"],
        timezone=timezone_name,
        timezone_display=timezone_display,
        platform="Win32",
        viewport_width=screen_profile["viewport_width"],
        viewport_height=screen_profile["viewport_height"],
        screen_width=screen_profile["screen_width"],
        screen_height=screen_profile["screen_height"],
        color_depth=24,
        hardware_concurrency=hardware_profile["hardware_concurrency"],
        device_memory=hardware_profile["device_memory"],
        webgl_vendor=webgl_profile["webgl_vendor"],
        webgl_renderer=webgl_profile["webgl_renderer"],
    )
