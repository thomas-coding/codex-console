"""
账号独立归档

用于在主表账号/邮箱被清理后，仍保留一份完整的账号恢复快照。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..database.models import Account, EmailService

logger = logging.getLogger(__name__)


CSV_HEADERS = [
    "ID",
    "Email",
    "Password",
    "Client ID",
    "Account ID",
    "Workspace ID",
    "Access Token",
    "Refresh Token",
    "ID Token",
    "Session Token",
    "Email Service",
    "Status",
    "Registered At",
    "Last Refresh",
    "Expires At",
    "Outlook Email",
    "Outlook Password",
    "Outlook Client ID",
    "Outlook Refresh Token",
]

ACCOUNT_OUTLOOK_RECOVERY_KEY = "outlook_recovery"


def _resolve_data_root() -> Path:
    app_data_dir = os.environ.get("APP_DATA_DIR")
    if app_data_dir:
        root = Path(app_data_dir)
    else:
        root = Path(__file__).resolve().parents[2] / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_account_archive_root() -> Path:
    root = _resolve_data_root() / "account_archive"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_outlook_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    source = payload if isinstance(payload, dict) else {}
    return {
        "email": _normalize_text(source.get("email")),
        "password": _normalize_text(source.get("password")),
        "client_id": _normalize_text(source.get("client_id")),
        "refresh_token": _normalize_text(source.get("refresh_token")),
    }


def _build_outlook_lookup(db) -> Dict[str, Dict[str, str]]:
    lookup: Dict[str, Dict[str, str]] = {}
    if db is None:
        return lookup

    services = db.query(EmailService).filter(EmailService.service_type == "outlook").all()
    for service in services:
        config = service.config or {}
        payload = {
            "service_name": _normalize_text(service.name),
            "email": _normalize_text(config.get("email") or service.name),
            "password": _normalize_text(config.get("password")),
            "client_id": _normalize_text(config.get("client_id")),
            "refresh_token": _normalize_text(config.get("refresh_token")),
        }
        for key in (
            payload["service_name"].lower(),
            payload["email"].lower(),
            _normalize_text(service.id),
        ):
            if key:
                lookup[key] = payload
    return lookup


def resolve_account_outlook_archive_payload(account: Account, db=None) -> Dict[str, str]:
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    stored_payload = _normalize_outlook_payload(extra_data.get(ACCOUNT_OUTLOOK_RECOVERY_KEY))

    lookup_payload: Dict[str, str] = {}
    if _normalize_text(account.email_service).lower() == "outlook":
        outlook_lookup = _build_outlook_lookup(db)
        for key in (
            _normalize_text(account.email_service_id).lower(),
            _normalize_text(account.email).lower(),
        ):
            if key and key in outlook_lookup:
                lookup_payload = _normalize_outlook_payload(outlook_lookup[key])
                break

    merged = dict(lookup_payload)
    for key, value in stored_payload.items():
        if value:
            merged[key] = value
    return merged


def build_account_archive_record(account: Account, db=None, reason: str = "snapshot") -> Dict[str, Any]:
    outlook = resolve_account_outlook_archive_payload(account, db=db)
    now = datetime.utcnow().isoformat()

    return {
        "id": getattr(account, "id", None),
        "email": _normalize_text(getattr(account, "email", "")),
        "password": _normalize_text(getattr(account, "password", "")),
        "client_id": _normalize_text(getattr(account, "client_id", "")),
        "account_id": _normalize_text(getattr(account, "account_id", "")),
        "workspace_id": _normalize_text(getattr(account, "workspace_id", "")),
        "access_token": _normalize_text(getattr(account, "access_token", "")),
        "refresh_token": _normalize_text(getattr(account, "refresh_token", "")),
        "id_token": _normalize_text(getattr(account, "id_token", "")),
        "session_token": _normalize_text(getattr(account, "session_token", "")),
        "email_service": _normalize_text(getattr(account, "email_service", "")),
        "email_service_id": _normalize_text(getattr(account, "email_service_id", "")),
        "status": _normalize_text(getattr(account, "status", "")),
        "registered_at": getattr(account, "registered_at", None).isoformat() if getattr(account, "registered_at", None) else "",
        "last_refresh": getattr(account, "last_refresh", None).isoformat() if getattr(account, "last_refresh", None) else "",
        "expires_at": getattr(account, "expires_at", None).isoformat() if getattr(account, "expires_at", None) else "",
        "proxy_used": _normalize_text(getattr(account, "proxy_used", "")),
        "source": _normalize_text(getattr(account, "source", "")),
        "subscription_type": _normalize_text(getattr(account, "subscription_type", "")),
        "cookies": _normalize_text(getattr(account, "cookies", "")),
        "outlook_recovery": outlook,
        "extra_data": account.extra_data if isinstance(account.extra_data, dict) else {},
        "archived_at": now,
        "archive_reason": _normalize_text(reason or "snapshot"),
    }


def build_account_archive_csv_text(record: Dict[str, Any]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADERS)
    writer.writerow([
        record.get("id", ""),
        record.get("email", ""),
        record.get("password", ""),
        record.get("client_id", ""),
        record.get("account_id", ""),
        record.get("workspace_id", ""),
        record.get("access_token", ""),
        record.get("refresh_token", ""),
        record.get("id_token", ""),
        record.get("session_token", ""),
        record.get("email_service", ""),
        record.get("status", ""),
        record.get("registered_at", ""),
        record.get("last_refresh", ""),
        record.get("expires_at", ""),
        (record.get("outlook_recovery") or {}).get("email", ""),
        (record.get("outlook_recovery") or {}).get("password", ""),
        (record.get("outlook_recovery") or {}).get("client_id", ""),
        (record.get("outlook_recovery") or {}).get("refresh_token", ""),
    ])
    return output.getvalue()


def _sanitize_email_filename(email: str) -> str:
    normalized = _normalize_text(email).lower()
    if not normalized:
        normalized = "unknown"
    return re.sub(r"[^a-z0-9._@-]+", "_", normalized)


def write_account_archive_snapshot(account: Account, db=None, reason: str = "snapshot") -> Optional[Dict[str, Any]]:
    email = _normalize_text(getattr(account, "email", ""))
    if not email:
        return None

    try:
        root = get_account_archive_root()
        latest_dir = root / "latest"
        history_dir = root / "history"
        latest_dir.mkdir(parents=True, exist_ok=True)
        history_dir.mkdir(parents=True, exist_ok=True)

        record = build_account_archive_record(account, db=db, reason=reason)
        safe_name = _sanitize_email_filename(email)

        json_path = latest_dir / f"{safe_name}.json"
        csv_path = latest_dir / f"{safe_name}.csv"
        history_path = history_dir / f"{datetime.utcnow().strftime('%Y%m')}.jsonl"

        json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        csv_path.write_text(build_account_archive_csv_text(record), encoding="utf-8", newline="")
        with history_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False))
            fp.write("\n")

        return {
            "json_path": str(json_path),
            "csv_path": str(csv_path),
            "history_path": str(history_path),
        }
    except Exception as exc:
        logger.warning("写入账号归档失败: email=%s err=%s", email, exc)
        return None


def find_latest_account_archive(email: str) -> Optional[Dict[str, Any]]:
    safe_name = _sanitize_email_filename(email)
    json_path = get_account_archive_root() / "latest" / f"{safe_name}.json"
    if not json_path.is_file():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取账号归档失败: email=%s err=%s", email, exc)
        return None
