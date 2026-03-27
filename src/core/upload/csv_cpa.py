"""
CSV 账号数据转 CPA JSON 的辅助函数。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ...database.models import Account
from ..openai.token_refresh import TokenRefreshManager
from ...core.register import RegistrationEngine
from ...services.outlook.service import OutlookService


CPA_TIMEZONE = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


@dataclass
class CsvAccountRecord:
    email: str
    password: str = ""
    client_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    email_service: str = ""
    account_id: str = ""
    outlook_email: str = ""
    outlook_password: str = ""
    outlook_client_id: str = ""
    outlook_refresh_token: str = ""
    expires_at: Optional[datetime] = None
    last_refresh: Optional[datetime] = None
    raw: Optional[Dict[str, Any]] = None


def _normalize_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def format_cpa_datetime(value: Optional[datetime]) -> str:
    if not value:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(CPA_TIMEZONE)
    return value.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def build_cpa_token_payload(
    *,
    email: str,
    id_token: str = "",
    account_id: str = "",
    access_token: str = "",
    refresh_token: str = "",
    expires_at: Optional[datetime] = None,
    last_refresh: Optional[datetime] = None,
) -> dict:
    return {
        "type": "codex",
        "email": email,
        "expired": format_cpa_datetime(expires_at),
        "id_token": id_token or "",
        "account_id": account_id or "",
        "access_token": access_token or "",
        "last_refresh": format_cpa_datetime(last_refresh),
        "refresh_token": refresh_token or "",
    }


def parse_csv_accounts(csv_bytes: bytes) -> List[CsvAccountRecord]:
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

    normalized_headers = {
        _normalize_header(header): header
        for header in reader.fieldnames
        if header is not None
    }

    def pick(row: Dict[str, Any], *keys: str) -> str:
        for key in keys:
            raw_key = normalized_headers.get(key)
            if not raw_key:
                continue
            value = str(row.get(raw_key) or "").strip()
            if value:
                return value
        return ""

    records: List[CsvAccountRecord] = []
    for row in reader:
        email = pick(row, "email")
        if not email:
            continue

        records.append(
            CsvAccountRecord(
                email=email,
                password=pick(row, "password"),
                client_id=pick(row, "clientid"),
                access_token=pick(row, "accesstoken", "token"),
                refresh_token=pick(row, "refreshtoken"),
                id_token=pick(row, "idtoken"),
                session_token=pick(row, "sessiontoken"),
                email_service=pick(row, "emailservice"),
                account_id=pick(row, "accountid"),
                outlook_email=pick(row, "outlookemail"),
                outlook_password=pick(row, "outlookpassword"),
                outlook_client_id=pick(row, "outlookclientid"),
                outlook_refresh_token=pick(row, "outlookrefreshtoken"),
                expires_at=_parse_datetime(pick(row, "expiresat", "expired")),
                last_refresh=_parse_datetime(pick(row, "lastrefresh", "lastrefreshed")),
                raw=dict(row),
            )
        )

    return records


def csv_records_to_cpa_payloads(records: Iterable[CsvAccountRecord]) -> List[dict]:
    return [
        build_cpa_token_payload(
            email=record.email,
            id_token=record.id_token,
            account_id=record.account_id,
            access_token=record.access_token,
            refresh_token=record.refresh_token,
            expires_at=record.expires_at,
            last_refresh=record.last_refresh,
        )
        for record in records
    ]


def refresh_csv_records_for_cpa(
    records: Iterable[CsvAccountRecord],
    proxy_url: Optional[str] = None,
) -> Tuple[List[dict], dict]:
    manager = TokenRefreshManager(proxy_url=proxy_url)
    payloads: List[dict] = []
    details: List[dict] = []

    for record in records:
        account = Account(
            email=record.email,
            password=record.password or None,
            client_id=record.client_id or None,
            access_token=record.access_token or None,
            refresh_token=record.refresh_token or None,
            id_token=record.id_token or None,
            session_token=record.session_token or None,
            email_service=record.email_service or "manual",
            account_id=record.account_id or None,
            expires_at=record.expires_at,
            last_refresh=record.last_refresh,
        )

        step = "validated_existing"
        refresh_error = ""
        relogin_error = ""

        if account.session_token or account.refresh_token:
            refreshed = manager.refresh_account(account)
            if refreshed.success:
                account.access_token = refreshed.access_token or account.access_token
                if refreshed.refresh_token:
                    account.refresh_token = refreshed.refresh_token
                if refreshed.expires_at:
                    account.expires_at = refreshed.expires_at
                account.last_refresh = datetime.utcnow()
                step = "refreshed"
            else:
                refresh_error = refreshed.error_message
                step = "refresh_failed"

        access_token = str(account.access_token or "").strip()
        is_valid = False
        validate_error = ""
        if not access_token:
            validate_error = refresh_error or "缺少可用 access_token"
        else:
            is_valid, validate_error = manager.validate_token(access_token)

        if (not is_valid) and _can_relogin_with_outlook(record):
            relogin_account = _relogin_csv_record_by_outlook(record, proxy_url=proxy_url)
            if relogin_account:
                account = relogin_account
                access_token = str(account.access_token or "").strip()
                if access_token:
                    is_valid, validate_error = manager.validate_token(access_token)
                else:
                    is_valid, validate_error = False, "重登录成功但未获取到 access_token"
                step = "relogin" if is_valid else "relogin_failed"
            else:
                relogin_error = "Outlook 重登录未获取到有效 token"
                step = "relogin_failed"

        if not is_valid:
            details.append({
                "email": record.email,
                "success": False,
                "step": step,
                "error": relogin_error or validate_error or refresh_error or "access_token 验证失败",
                "has_session_token": bool(record.session_token),
                "has_refresh_token": bool(record.refresh_token),
                "has_outlook_password": bool(record.outlook_password),
            })
            continue

        payloads.append(
            build_cpa_token_payload(
                email=account.email,
                id_token=account.id_token or "",
                account_id=account.account_id or "",
                access_token=access_token,
                refresh_token=account.refresh_token or "",
                expires_at=account.expires_at,
                last_refresh=account.last_refresh,
            )
        )
        details.append({
            "email": record.email,
            "success": True,
            "step": step,
            "error": "",
            "has_session_token": bool(record.session_token),
            "has_refresh_token": bool(record.refresh_token),
            "has_outlook_password": bool(record.outlook_password),
        })

    report = {
        "total": len(details),
        "success_count": sum(1 for item in details if item["success"]),
        "failed_count": sum(1 for item in details if not item["success"]),
        "details": details,
    }
    return payloads, report


def _can_relogin_with_outlook(record: CsvAccountRecord) -> bool:
    service_type = str(record.email_service or "").strip().lower()
    return (
        service_type in {"", "outlook"}
        and bool(str(record.password or "").strip())
        and bool(str(record.outlook_password or "").strip())
    )


def _relogin_csv_record_by_outlook(
    record: CsvAccountRecord,
    proxy_url: Optional[str] = None,
) -> Optional[Account]:
    mailbox_email = str(record.outlook_email or record.email or "").strip()
    mailbox_password = str(record.outlook_password or "").strip()
    openai_password = str(record.password or "").strip()
    if not mailbox_email or not mailbox_password or not openai_password:
        return None

    mailbox_config: Dict[str, Any] = {
        "email": mailbox_email,
        "password": mailbox_password,
    }
    if record.outlook_client_id and record.outlook_refresh_token:
        mailbox_config["client_id"] = record.outlook_client_id
        mailbox_config["refresh_token"] = record.outlook_refresh_token

    email_service = OutlookService(mailbox_config, name=f"csv_relogin_{mailbox_email}")
    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url,
        callback_logger=lambda msg: logger.info("CSV Outlook 重登录: %s", msg),
        task_uuid=None,
    )
    engine.password = openai_password

    result = engine.run()
    if not result.success or not str(result.access_token or "").strip():
        return None

    return Account(
        email=result.email or record.email,
        password=openai_password,
        client_id=record.client_id or None,
        access_token=result.access_token or None,
        refresh_token=result.refresh_token or None,
        id_token=result.id_token or None,
        session_token=result.session_token or None,
        email_service="outlook",
        account_id=result.account_id or record.account_id or None,
        workspace_id=result.workspace_id or None,
        expires_at=None,
        last_refresh=datetime.utcnow(),
    )


def build_cpa_export_content(payloads: List[dict], report: Optional[dict] = None) -> tuple[str, bytes, str]:
    if not payloads:
        raise ValueError("没有可导出的 CSV 账号")

    include_report = False
    if report:
        details = report.get("details") or []
        include_report = bool(
            report.get("failed_count")
            or any(str(item.get("step") or "").strip().lower() != "validated_existing" for item in details)
        )

    if len(payloads) == 1 and not include_report:
        payload = payloads[0]
        filename = f"{payload['email']}.json"
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return filename, content, "application/json"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    buffer = io.BytesIO()
    used_filenames: Dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for payload in payloads:
            base_name = f"{payload['email']}.json"
            filename = base_name
            used_filenames.setdefault(base_name, 0)
            if used_filenames[base_name]:
                stem = base_name[:-5]
                filename = f"{stem}_{used_filenames[base_name] + 1}.json"
            used_filenames[base_name] += 1
            zf.writestr(
                filename,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        if report:
            zf.writestr(
                "_export_report.json",
                json.dumps(report, ensure_ascii=False, indent=2),
            )
    buffer.seek(0)
    return f"cpa_tokens_{timestamp}.zip", buffer.getvalue(), "application/zip"
