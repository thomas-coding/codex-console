import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.database.models import Base, EmailService, RegistrationTask
from src.database.session import DatabaseSessionManager
from src.web.routes import email as email_routes


def _build_manager(db_name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / db_name
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_delete_email_service_clears_historical_registration_task_references(monkeypatch):
    manager = _build_manager("email_delete_historical.db")

    with manager.session_scope() as session:
        service = EmailService(
            service_type="outlook",
            name="historical@example.com",
            config={"email": "historical@example.com", "password": "secret"},
            enabled=True,
            priority=0,
        )
        session.add(service)
        session.flush()
        session.add(
            RegistrationTask(
                task_uuid="task-historical-1",
                status="completed",
                email_service_id=service.id,
            )
        )
        service_id = service.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    result = asyncio.run(email_routes.delete_email_service(service_id))

    assert result["success"] is True

    with manager.session_scope() as session:
        assert session.query(EmailService).filter(EmailService.id == service_id).first() is None
        task = session.query(RegistrationTask).filter(RegistrationTask.task_uuid == "task-historical-1").first()
        assert task is not None
        assert task.email_service_id is None


def test_delete_email_service_rejects_active_registration_tasks(monkeypatch):
    manager = _build_manager("email_delete_active.db")

    with manager.session_scope() as session:
        service = EmailService(
            service_type="outlook",
            name="active@example.com",
            config={"email": "active@example.com", "password": "secret"},
            enabled=True,
            priority=0,
        )
        session.add(service)
        session.flush()
        session.add(
            RegistrationTask(
                task_uuid="task-active-1",
                status="running",
                email_service_id=service.id,
            )
        )
        service_id = service.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    try:
        asyncio.run(email_routes.delete_email_service(service_id))
    except email_routes.HTTPException as exc:
        assert exc.status_code == 409
        assert "进行中的注册任务" in exc.detail
    else:
        raise AssertionError("expected HTTPException for active registration task")

    with manager.session_scope() as session:
        assert session.query(EmailService).filter(EmailService.id == service_id).first() is not None
        task = session.query(RegistrationTask).filter(RegistrationTask.task_uuid == "task-active-1").first()
        assert task is not None
        assert task.email_service_id == service_id


def test_batch_delete_outlook_preserves_all_when_any_service_has_active_tasks(monkeypatch):
    manager = _build_manager("email_batch_delete_active.db")

    with manager.session_scope() as session:
        historical = EmailService(
            service_type="outlook",
            name="historical-batch@example.com",
            config={"email": "historical-batch@example.com", "password": "secret"},
            enabled=True,
            priority=0,
        )
        active = EmailService(
            service_type="outlook",
            name="active-batch@example.com",
            config={"email": "active-batch@example.com", "password": "secret"},
            enabled=True,
            priority=1,
        )
        session.add(historical)
        session.add(active)
        session.flush()
        session.add(
            RegistrationTask(
                task_uuid="task-batch-completed-1",
                status="completed",
                email_service_id=historical.id,
            )
        )
        session.add(
            RegistrationTask(
                task_uuid="task-batch-running-1",
                status="pending",
                email_service_id=active.id,
            )
        )
        historical_id = historical.id
        active_id = active.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    try:
        asyncio.run(email_routes.batch_delete_outlook([historical_id, active_id]))
    except email_routes.HTTPException as exc:
        assert exc.status_code == 409
        assert "进行中的注册任务" in exc.detail
    else:
        raise AssertionError("expected HTTPException for active registration task")

    with manager.session_scope() as session:
        assert session.query(EmailService).filter(EmailService.id == historical_id).first() is not None
        assert session.query(EmailService).filter(EmailService.id == active_id).first() is not None
        completed_task = (
            session.query(RegistrationTask)
            .filter(RegistrationTask.task_uuid == "task-batch-completed-1")
            .first()
        )
        active_task = (
            session.query(RegistrationTask)
            .filter(RegistrationTask.task_uuid == "task-batch-running-1")
            .first()
        )
        assert completed_task is not None
        assert completed_task.email_service_id == historical_id
        assert active_task is not None
        assert active_task.email_service_id == active_id
