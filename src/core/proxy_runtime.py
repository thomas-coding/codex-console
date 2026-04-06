"""
运行时代理辅助函数。
"""

from __future__ import annotations

import logging
import re
import secrets
import string
import uuid
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)

_IPROYAL_HOST_MARKER = "iproyal.com"
_IPROYAL_SESSION_PATTERN = re.compile(r"(?P<prefix>_session-)(?P<token>[^_@]+)", re.IGNORECASE)


def _generate_runtime_session_id(template: str) -> str:
    """
    基于现有 session token 形态生成新的 runtime session。

    IPRoyal 现在的 session 既可能是旧的纯小写/数字，也可能是
    面板里展示的大小写混合字母数字。这里按原 token 的长度和每一位
    的字符类型生成新值，避免把新格式硬改回旧格式。
    """
    sample = str(template or "").strip()
    if not sample:
        sample = uuid.uuid4().hex[:8]

    generated = []
    for char in sample:
        if char.isdigit():
            alphabet = string.digits
        elif char.islower():
            alphabet = string.ascii_lowercase
        elif char.isupper():
            alphabet = string.ascii_uppercase
        elif char.isalpha():
            alphabet = string.ascii_letters
        else:
            alphabet = string.ascii_lowercase + string.digits
        generated.append(secrets.choice(alphabet))

    return "".join(generated)


def prepare_runtime_proxy(proxy_url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    为运行中的任务准备最终代理 URL。

    目前仅对 IPRoyal 粘性代理做运行时 session 改写：
    - 同一任务内保持一个 sticky session / IP
    - 每次新任务生成新的 sticky session / IP
    """
    raw_proxy = str(proxy_url or "").strip()
    if not raw_proxy:
        return proxy_url, None

    try:
        parsed = urlsplit(raw_proxy)
        hostname = str(parsed.hostname or "").strip().lower()
        if _IPROYAL_HOST_MARKER not in hostname:
            return raw_proxy, None

        netloc = str(parsed.netloc or "").strip()
        if not netloc or "@" not in netloc:
            return raw_proxy, None

        auth, host_part = netloc.rsplit("@", 1)
        if ":" not in auth:
            return raw_proxy, None

        username, password = auth.split(":", 1)
        session_match = _IPROYAL_SESSION_PATTERN.search(username) or _IPROYAL_SESSION_PATTERN.search(password)
        if not session_match:
            return raw_proxy, None

        session_id = _generate_runtime_session_id(session_match.group("token"))
        replacement = lambda match: f"{match.group('prefix')}{session_id}"
        username, username_count = _IPROYAL_SESSION_PATTERN.subn(replacement, username, count=1)
        password, password_count = _IPROYAL_SESSION_PATTERN.subn(replacement, password, count=1)
        rewritten = bool(username_count or password_count)

        if not rewritten:
            return raw_proxy, None

        updated_proxy = urlunsplit((
            parsed.scheme,
            f"{username}:{password}@{host_part}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        ))
        return updated_proxy, session_id
    except Exception as exc:
        logger.warning("改写 IPRoyal session 失败，回退原代理: %s", exc)
        return raw_proxy, None
