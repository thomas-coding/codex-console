from pathlib import Path
from types import SimpleNamespace

from src.core.account_archive import (
    find_latest_account_archive,
    write_account_archive_snapshot,
)
from src.database import crud
from src.database.session import DatabaseSessionManager


def test_write_account_archive_snapshot_writes_latest_json_and_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    account = SimpleNamespace(
        id=1,
        email="tester@outlook.com",
        password="openai-pwd",
        client_id="openai-client",
        account_id="acct-1",
        workspace_id="ws-1",
        access_token="access-1",
        refresh_token="refresh-1",
        id_token="id-1",
        session_token="session-1",
        email_service="outlook",
        email_service_id="svc-1",
        status="active",
        registered_at=None,
        last_refresh=None,
        expires_at=None,
        proxy_used="socks5://proxy",
        source="register",
        subscription_type="",
        cookies="cookie=value",
        extra_data={
            "outlook_recovery": {
                "email": "tester@outlook.com",
                "password": "mail-pwd",
                "client_id": "mail-client",
                "refresh_token": "mail-refresh",
            }
        },
    )

    result = write_account_archive_snapshot(account, reason="create")

    assert result is not None
    assert Path(result["json_path"]).is_file()
    assert Path(result["csv_path"]).is_file()

    archived = find_latest_account_archive("tester@outlook.com")
    assert archived is not None
    assert archived["email"] == "tester@outlook.com"
    assert archived["password"] == "openai-pwd"
    assert archived["outlook_recovery"]["password"] == "mail-pwd"
    assert archived["archive_reason"] == "create"

    csv_text = Path(result["csv_path"]).read_text(encoding="utf-8")
    assert "Outlook Password" in csv_text
    assert "mail-pwd" in csv_text


def test_delete_account_keeps_archive_snapshot_with_outlook_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "archive_test.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    manager.create_tables()

    db = manager.SessionLocal()
    try:
        service = crud.create_email_service(
            db,
            service_type="outlook",
            name="tester@outlook.com",
            config={
                "email": "tester@outlook.com",
                "password": "mail-pwd",
                "client_id": "mail-client",
                "refresh_token": "mail-refresh",
            },
        )
        account = crud.create_account(
            db,
            email="tester@outlook.com",
            password="openai-pwd",
            email_service="outlook",
            email_service_id=str(service.id),
            client_id="openai-client",
            refresh_token="openai-refresh",
            access_token="openai-access",
            account_id="acct-1",
            workspace_id="ws-1",
            extra_data={},
        )

        created_archive = find_latest_account_archive("tester@outlook.com")
        assert created_archive is not None
        assert created_archive["outlook_recovery"]["password"] == "mail-pwd"

        assert crud.delete_account(db, account.id) is True
        assert crud.get_account_by_email(db, "tester@outlook.com") is None

        deleted_archive = find_latest_account_archive("tester@outlook.com")
        assert deleted_archive is not None
        assert deleted_archive["password"] == "openai-pwd"
        assert deleted_archive["refresh_token"] == "openai-refresh"
        assert deleted_archive["outlook_recovery"]["refresh_token"] == "mail-refresh"
        assert deleted_archive["archive_reason"] == "delete"
    finally:
        db.close()
