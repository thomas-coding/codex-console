import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.config.constants import EmailServiceType
from src.core.register import RegistrationResult
from src.database import crud
from src.database.session import DatabaseSessionManager
from src.web.routes import registration


def _create_test_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db_path = tmp_path / "registration_routes.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    manager.create_tables()
    return manager


def _route_db_context(manager):
    @contextmanager
    def _ctx():
        db = manager.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    return _ctx


def test_start_batch_registration_prebinds_unique_outlook_services(tmp_path, monkeypatch):
    manager = _create_test_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(registration, "get_db", _route_db_context(manager))

    with manager.SessionLocal() as db:
        svc1 = crud.create_email_service(
            db,
            service_type="outlook",
            name="fresh-1",
            config={"email": "fresh1@outlook.com", "password": "mail-1"},
            priority=1,
        )
        svc2 = crud.create_email_service(
            db,
            service_type="outlook",
            name="used",
            config={"email": "used@outlook.com", "password": "mail-2"},
            priority=2,
        )
        svc3 = crud.create_email_service(
            db,
            service_type="outlook",
            name="fresh-2",
            config={"email": "fresh2@outlook.com", "password": "mail-3"},
            priority=3,
        )
        crud.create_account(
            db,
            email="used@outlook.com",
            email_service="outlook",
            email_service_id=str(svc2.id),
            access_token="access-token",
            refresh_token="refresh-token",
            account_id="acct-used",
            workspace_id="ws-used",
        )

    request = registration.BatchRegistrationRequest(
        count=2,
        email_service_type="outlook",
        concurrency=2,
        mode="parallel",
    )
    background_tasks = BackgroundTasks()

    response = asyncio.run(registration.start_batch_registration(request, background_tasks))

    assert response.count == 2
    assert len(response.tasks) == 2
    assert [task.email_service_id for task in response.tasks] == [svc1.id, svc3.id]
    assert len(background_tasks.tasks) == 1

    with manager.SessionLocal() as db:
        tasks = crud.get_registration_tasks(db)

    assert [task.email_service_id for task in reversed(tasks)] == [svc1.id, svc3.id]


def test_start_batch_registration_rejects_outlook_when_available_accounts_are_insufficient(tmp_path, monkeypatch):
    manager = _create_test_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(registration, "get_db", _route_db_context(manager))

    with manager.SessionLocal() as db:
        crud.create_email_service(
            db,
            service_type="outlook",
            name="fresh-only",
            config={"email": "fresh-only@outlook.com", "password": "mail-1"},
            priority=1,
        )

    request = registration.BatchRegistrationRequest(
        count=2,
        email_service_type="outlook",
        concurrency=2,
        mode="parallel",
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(registration.start_batch_registration(request, BackgroundTasks()))

    assert exc_info.value.status_code == 400
    assert "可用的未注册 Outlook 账户不足" in exc_info.value.detail

    with manager.SessionLocal() as db:
        assert crud.get_registration_tasks(db) == []


def test_run_sync_registration_task_prefers_task_bound_outlook_service_id(tmp_path, monkeypatch):
    manager = _create_test_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(registration, "get_db", _route_db_context(manager))

    class DummyTaskManager:
        def is_cancelled(self, task_uuid):
            return False

        def update_status(self, task_uuid, status, **kwargs):
            return None

        def add_log(self, task_uuid, log_message):
            return None

        def create_log_callback(self, task_uuid, prefix="", batch_id=""):
            return lambda message: None

    monkeypatch.setattr(registration, "task_manager", DummyTaskManager())
    monkeypatch.setattr(registration, "get_settings", lambda: SimpleNamespace(registration_browser_profile_enabled=False))
    monkeypatch.setattr(
        registration,
        "_prepare_registration_proxy_with_ip_check",
        lambda proxy_url, log_callback=None: (proxy_url, None, None, 1, False),
    )
    monkeypatch.setattr(registration, "get_proxy_for_registration", lambda db: (None, None))
    monkeypatch.setattr(registration, "update_proxy_usage", lambda db, proxy_id: None)

    captured = {}

    def fake_create(service_type, config, name=None):
        captured["service_type"] = service_type
        captured["config"] = config
        return SimpleNamespace(service_type=service_type, config=config)

    class FakeEngine:
        def __init__(self, email_service, **kwargs):
            self.email_service = email_service

        def run(self):
            return RegistrationResult(success=False, error_message="simulated stop")

    monkeypatch.setattr(registration.EmailServiceFactory, "create", fake_create)
    monkeypatch.setattr(registration, "RegistrationEngine", FakeEngine)

    task_uuid = "task-bound-outlook"
    with manager.SessionLocal() as db:
        crud.create_email_service(
            db,
            service_type="outlook",
            name="other",
            config={"email": "other@outlook.com", "password": "mail-1"},
            priority=1,
        )
        bound_service = crud.create_email_service(
            db,
            service_type="outlook",
            name="bound",
            config={"email": "bound@outlook.com", "password": "mail-2"},
            priority=2,
        )
        bound_service_id = bound_service.id
        crud.create_registration_task(
            db,
            task_uuid=task_uuid,
            email_service_id=bound_service_id,
        )

    registration._run_sync_registration_task(
        task_uuid=task_uuid,
        email_service_type="outlook",
        proxy=None,
        email_service_config=None,
        email_service_id=None,
    )

    assert captured["service_type"] == EmailServiceType.OUTLOOK
    assert captured["config"]["email"] == "bound@outlook.com"

    with manager.SessionLocal() as db:
        task = crud.get_registration_task_by_uuid(db, task_uuid)

    assert task is not None
    assert task.email_service_id == bound_service_id
    assert task.status == "failed"
    assert task.error_message == "simulated stop"
