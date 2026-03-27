"""
运行时代理辅助函数。
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)

_IPROYAL_HOST_MARKER = "iproyal.com"
_IPROYAL_SESSION_PATTERN = re.compile(r"_session-[^_@]+", re.IGNORECASE)


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
        session_id = uuid.uuid4().hex[:8]
        rewritten = False

        if _IPROYAL_SESSION_PATTERN.search(username):
            username = _IPROYAL_SESSION_PATTERN.sub(f"_session-{session_id}", username, count=1)
            rewritten = True
        if _IPROYAL_SESSION_PATTERN.search(password):
            password = _IPROYAL_SESSION_PATTERN.sub(f"_session-{session_id}", password, count=1)
            rewritten = True

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
