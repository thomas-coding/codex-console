import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

from src.core.upload.csv_cpa import (
    build_cpa_export_content,
    csv_records_to_cpa_payloads,
    format_cpa_datetime,
    parse_csv_accounts,
    refresh_csv_records_for_cpa,
)


def test_parse_sample_csv_file_supports_existing_export_header():
    csv_bytes = (
        b"ID,Email,Password,Client ID,Account ID,Workspace ID,Access Token,Refresh Token,ID Token,Session Token,Email Service,Status,Registered At,Last Refresh,Expires At\r\n"
        b"40,zongzhengken818503@outlook.com,kAv8Y5AaO5UI,app_EMoamEEZ73f0CkXaXp7hrann,cc08e7f3-4130-4681-b8e3-67eaf34dad98,cc08e7f3-4130-4681-b8e3-67eaf34dad98,eyJ-sample-access,rt_sample_refresh,eyJ-sample-id,,outlook,active,2026-03-27T09:48:15.143202,,\r\n"
    )

    records = parse_csv_accounts(csv_bytes)

    assert len(records) == 1
    assert records[0].email == "zongzhengken818503@outlook.com"
    assert records[0].account_id == "cc08e7f3-4130-4681-b8e3-67eaf34dad98"
    assert records[0].access_token.startswith("eyJ")
    assert records[0].refresh_token.startswith("rt_")


def test_parse_full_csv_extracts_outlook_credentials():
    csv_bytes = (
        b"Email,Password,Email Service,Outlook Email,Outlook Password,Outlook Client ID,Outlook Refresh Token\r\n"
        b"tester@example.com,openai-pwd,outlook,tester@outlook.com,mail-pwd,client-1,refresh-1\r\n"
    )

    records = parse_csv_accounts(csv_bytes)

    assert len(records) == 1
    assert records[0].password == "openai-pwd"
    assert records[0].outlook_email == "tester@outlook.com"
    assert records[0].outlook_password == "mail-pwd"
    assert records[0].outlook_client_id == "client-1"
    assert records[0].outlook_refresh_token == "refresh-1"


def test_build_cpa_export_content_returns_json_for_single_csv_record():
    csv_bytes = (
        b"Email,Account ID,Access Token,Refresh Token,ID Token,Expires At,Last Refresh\r\n"
        b"tester@example.com,acct-1,access-1,refresh-1,id-1,2026-03-27T09:48:15,2026-03-27T10:00:00\r\n"
    )

    payloads = csv_records_to_cpa_payloads(parse_csv_accounts(csv_bytes))
    filename, content, media_type = build_cpa_export_content(payloads)
    payload = json.loads(content.decode("utf-8"))

    assert media_type == "application/json"
    assert filename == "tester@example.com.json"
    assert payload == {
        "type": "codex",
        "email": "tester@example.com",
        "expired": "2026-03-27T09:48:15+08:00",
        "id_token": "id-1",
        "account_id": "acct-1",
        "access_token": "access-1",
        "last_refresh": "2026-03-27T10:00:00+08:00",
        "refresh_token": "refresh-1",
    }


def test_build_cpa_export_content_returns_zip_for_multiple_records():
    payloads = [
        {
            "type": "codex",
            "email": "alpha@example.com",
            "expired": "",
            "id_token": "id-a",
            "account_id": "acct-a",
            "access_token": "access-a",
            "last_refresh": "",
            "refresh_token": "refresh-a",
        },
        {
            "type": "codex",
            "email": "beta@example.com",
            "expired": "",
            "id_token": "id-b",
            "account_id": "acct-b",
            "access_token": "access-b",
            "last_refresh": "",
            "refresh_token": "refresh-b",
        },
    ]

    filename, content, media_type = build_cpa_export_content(payloads)

    assert media_type == "application/zip"
    assert filename.startswith("cpa_tokens_")
    with zipfile.ZipFile(BytesIO(content), "r") as zf:
        assert sorted(zf.namelist()) == ["alpha@example.com.json", "beta@example.com.json"]
        alpha_payload = json.loads(zf.read("alpha@example.com.json").decode("utf-8"))
        assert alpha_payload["account_id"] == "acct-a"


def test_build_cpa_export_content_renames_duplicate_emails_inside_zip():
    payloads = [
        {
            "type": "codex",
            "email": "dup@example.com",
            "expired": "",
            "id_token": "id-a",
            "account_id": "acct-a",
            "access_token": "access-a",
            "last_refresh": "",
            "refresh_token": "refresh-a",
        },
        {
            "type": "codex",
            "email": "dup@example.com",
            "expired": "",
            "id_token": "id-b",
            "account_id": "acct-b",
            "access_token": "access-b",
            "last_refresh": "",
            "refresh_token": "refresh-b",
        },
    ]

    _, content, _ = build_cpa_export_content(payloads)

    with zipfile.ZipFile(BytesIO(content), "r") as zf:
        assert sorted(zf.namelist()) == ["dup@example.com.json", "dup@example.com_2.json"]


def test_format_cpa_datetime_converts_timezone_aware_values_to_utc_plus_8():
    value = datetime(2026, 3, 27, 1, 30, tzinfo=timezone.utc)

    formatted = format_cpa_datetime(value)

    assert formatted == "2026-03-27T09:30:00+08:00"


class _FakeRefreshResult:
    def __init__(self, success, access_token="", refresh_token="", expires_at=None, error_message=""):
        self.success = success
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.error_message = error_message


def test_refresh_csv_records_for_cpa_prefers_refresh_then_export(monkeypatch):
    csv_bytes = (
        b"Email,Client ID,Refresh Token,Account ID,ID Token\r\n"
        b"tester@example.com,client-1,refresh-1,acct-1,id-1\r\n"
    )

    class FakeManager:
        def __init__(self, proxy_url=None):
            self.proxy_url = proxy_url

        def refresh_account(self, account):
            assert account.refresh_token == "refresh-1"
            return _FakeRefreshResult(
                success=True,
                access_token="fresh-access",
                refresh_token="fresh-refresh",
                expires_at=datetime(2026, 3, 27, 12, 0, 0),
            )

        def validate_token(self, access_token):
            assert access_token == "fresh-access"
            return True, None

    monkeypatch.setattr("src.core.upload.csv_cpa.TokenRefreshManager", FakeManager)

    payloads, report = refresh_csv_records_for_cpa(parse_csv_accounts(csv_bytes), proxy_url="http://proxy")

    assert report["success_count"] == 1
    assert report["failed_count"] == 0
    assert report["details"][0]["step"] == "refreshed"
    assert payloads[0]["access_token"] == "fresh-access"
    assert payloads[0]["refresh_token"] == "fresh-refresh"


def test_refresh_csv_records_for_cpa_falls_back_to_existing_valid_access_token(monkeypatch):
    csv_bytes = (
        b"Email,Access Token,Session Token,Account ID\r\n"
        b"tester@example.com,existing-access,session-1,acct-1\r\n"
    )

    class FakeManager:
        def __init__(self, proxy_url=None):
            self.proxy_url = proxy_url

        def refresh_account(self, account):
            return _FakeRefreshResult(success=False, error_message="refresh failed")

        def validate_token(self, access_token):
            assert access_token == "existing-access"
            return True, None

    monkeypatch.setattr("src.core.upload.csv_cpa.TokenRefreshManager", FakeManager)

    payloads, report = refresh_csv_records_for_cpa(parse_csv_accounts(csv_bytes))

    assert report["success_count"] == 1
    assert report["details"][0]["step"] == "refresh_failed"
    assert payloads[0]["access_token"] == "existing-access"


def test_refresh_csv_records_for_cpa_retries_with_outlook_relogin(monkeypatch):
    csv_bytes = (
        b"Email,Password,Email Service,Account ID,Outlook Email,Outlook Password,Outlook Client ID,Outlook Refresh Token\r\n"
        b"tester@example.com,openai-pwd,outlook,acct-1,tester@outlook.com,mail-pwd,client-1,mail-refresh\r\n"
    )

    class FakeManager:
        def __init__(self, proxy_url=None):
            self.proxy_url = proxy_url

        def refresh_account(self, account):
            return _FakeRefreshResult(success=False, error_message="refresh failed")

        def validate_token(self, access_token):
            assert access_token == "relogin-access"
            return True, None

    class FakeResult:
        success = True
        email = "tester@example.com"
        access_token = "relogin-access"
        refresh_token = "relogin-refresh"
        id_token = "relogin-id"
        session_token = "relogin-session"
        account_id = "acct-1"
        workspace_id = "ws-1"

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.password = ""

        def run(self):
            assert self.password == "openai-pwd"
            return FakeResult()

    monkeypatch.setattr("src.core.upload.csv_cpa.TokenRefreshManager", FakeManager)
    monkeypatch.setattr("src.core.upload.csv_cpa.RegistrationEngine", FakeEngine)

    payloads, report = refresh_csv_records_for_cpa(parse_csv_accounts(csv_bytes))

    assert report["success_count"] == 1
    assert report["details"][0]["step"] == "relogin"
    assert payloads[0]["access_token"] == "relogin-access"
    assert payloads[0]["refresh_token"] == "relogin-refresh"


def test_build_cpa_export_content_includes_report_when_failures_exist():
    payloads = [
        {
            "type": "codex",
            "email": "ok@example.com",
            "expired": "",
            "id_token": "id-ok",
            "account_id": "acct-ok",
            "access_token": "access-ok",
            "last_refresh": "",
            "refresh_token": "refresh-ok",
        }
    ]
    report = {
        "total": 2,
        "success_count": 1,
        "failed_count": 1,
        "details": [
            {"email": "ok@example.com", "success": True},
            {"email": "bad@example.com", "success": False, "error": "expired"},
        ],
    }

    filename, content, media_type = build_cpa_export_content(payloads, report=report)

    assert media_type == "application/zip"
    assert filename.startswith("cpa_tokens_")
    with zipfile.ZipFile(BytesIO(content), "r") as zf:
        assert "_export_report.json" in zf.namelist()


def test_build_cpa_export_content_includes_report_when_single_record_uses_relogin():
    payloads = [
        {
            "type": "codex",
            "email": "ok@example.com",
            "expired": "",
            "id_token": "id-ok",
            "account_id": "acct-ok",
            "access_token": "access-ok",
            "last_refresh": "",
            "refresh_token": "refresh-ok",
        }
    ]
    report = {
        "total": 1,
        "success_count": 1,
        "failed_count": 0,
        "details": [
            {"email": "ok@example.com", "success": True, "step": "relogin"},
        ],
    }

    filename, content, media_type = build_cpa_export_content(payloads, report=report)

    assert media_type == "application/zip"
    assert filename.startswith("cpa_tokens_")
    with zipfile.ZipFile(BytesIO(content), "r") as zf:
        assert sorted(zf.namelist()) == ["_export_report.json", "ok@example.com.json"]
        export_report = json.loads(zf.read("_export_report.json").decode("utf-8"))
        assert export_report["details"][0]["step"] == "relogin"
