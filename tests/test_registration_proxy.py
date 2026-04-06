from contextlib import contextmanager
from types import SimpleNamespace

from src.core.proxy_runtime import _generate_runtime_session_id, prepare_runtime_proxy
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.web.routes import registration as registration_routes


def test_generate_runtime_session_id_preserves_template_shape():
    session_id = _generate_runtime_session_id("fa7HXGXm")

    assert len(session_id) == 8
    assert session_id[0].islower()
    assert session_id[1].islower()
    assert session_id[2].isdigit()
    assert session_id[3].isupper()
    assert session_id[4].isupper()
    assert session_id[5].isupper()
    assert session_id[6].isupper()
    assert session_id[7].islower()


def test_prepare_runtime_proxy_rewrites_iproyal_session_in_password(monkeypatch):
    monkeypatch.setattr(
        "src.core.proxy_runtime._generate_runtime_session_id",
        lambda template: "fa7HXGXm",
    )

    proxy_url = (
        "socks5://user-1:"
        "secret_country-us_session-oldsess99_lifetime-30m@geo.iproyal.com:12321"
    )

    rewritten, session_id = prepare_runtime_proxy(proxy_url)

    assert session_id == "fa7HXGXm"
    assert rewritten == (
        "socks5://user-1:"
        "secret_country-us_session-fa7HXGXm_lifetime-30m@geo.iproyal.com:12321"
    )


def test_prepare_runtime_proxy_rewrites_iproyal_session_in_username(monkeypatch):
    monkeypatch.setattr(
        "src.core.proxy_runtime._generate_runtime_session_id",
        lambda template: "Abcde1234",
    )

    proxy_url = (
        "http://customer_country-us_session-oldsess99_lifetime-30m:"
        "password@geo.iproyal.com:12321"
    )

    rewritten, session_id = prepare_runtime_proxy(proxy_url)

    assert session_id == "Abcde1234"
    assert rewritten == (
        "http://customer_country-us_session-Abcde1234_lifetime-30m:"
        "password@geo.iproyal.com:12321"
    )


def test_prepare_runtime_proxy_keeps_non_iproyal_proxy_unchanged():
    proxy_url = "http://user:pass@127.0.0.1:7890"

    rewritten, session_id = prepare_runtime_proxy(proxy_url)

    assert rewritten == proxy_url
    assert session_id is None


def test_prepare_registration_proxy_with_ip_check_retries_same_public_ip(monkeypatch):
    monkeypatch.setattr(
        registration_routes,
        "_prepare_registration_proxy",
        lambda proxy_url: (
            f"{proxy_url}-retry"
            if proxy_url.endswith("retry-seed")
            else f"{proxy_url}-seed",
            "sess-bbbb" if proxy_url.endswith("retry-seed") else "sess-aaaa",
        ),
    )

    attempts = iter(["1.1.1.1", "2.2.2.2"])
    monkeypatch.setattr(registration_routes, "_resolve_public_ip", lambda proxy_url: next(attempts))
    monkeypatch.setattr(registration_routes, "_last_registration_public_ip", "1.1.1.1")

    logs = []
    final_proxy, session_id, public_ip, attempt_count, forced_reuse = (
        registration_routes._prepare_registration_proxy_with_ip_check(
            "http://proxy.local/retry-seed",
            max_attempts=2,
            log_callback=logs.append,
        )
    )

    assert final_proxy.endswith("retry")
    assert session_id == "sess-bbbb"
    assert public_ip == "2.2.2.2"
    assert attempt_count == 2
    assert forced_reuse is False
    assert any("上一任务相同" in item for item in logs)


def test_run_sync_registration_task_marks_failed_when_save_to_database_fails(monkeypatch):
    task_updates = []

    class DummyQuery:
        def __init__(self, result):
            self._result = result

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return self._result

    class DummyDb:
        def __init__(self, service):
            self._service = service

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def query(self, model):
            return DummyQuery(self._service)

    class DummyTaskManager:
        def __init__(self):
            self.statuses = []
            self.logs = []

        def is_cancelled(self, task_uuid):
            return False

        def update_status(self, task_uuid, status, **kwargs):
            self.statuses.append((task_uuid, status, kwargs))

        def add_log(self, task_uuid, message):
            self.logs.append((task_uuid, message))

        def create_log_callback(self, task_uuid, prefix="", batch_id=""):
            def _callback(message):
                self.logs.append((task_uuid, message))
            return _callback

    class FakeEmailService:
        def __init__(self):
            self.service_type = SimpleNamespace(value="luckmail")
            self.marker_calls = []

        def mark_registration_outcome(self, **kwargs):
            self.marker_calls.append(kwargs)

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.callback_logger = callback_logger
            self.task_uuid = task_uuid
            self.email_info = {
                "service_id": "tok_test",
                "token": "tok_test",
            }

        def run(self):
            return registration_routes.RegistrationResult(
                success=True,
                email="persistfail@hotmail.com",
                password="Passw0rd!",
                metadata={},
                source="register",
            )

        def save_to_database(self, result, account_label=None, role_tag=None):
            return False

    fake_db_service = SimpleNamespace(
        id=2,
        service_type="luckmail",
        name="LuckMail",
        config={
            "base_url": "https://mails.luckyous.com/",
            "project_code": "openai",
            "preset_mailboxes": "persistfail@hotmail.com----tok_test",
        },
    )
    fake_email_service = FakeEmailService()
    fake_task_manager = DummyTaskManager()
    fake_db = DummyDb(fake_db_service)

    monkeypatch.setattr(registration_routes, "get_db", lambda: fake_db)
    monkeypatch.setattr(registration_routes, "task_manager", fake_task_manager)
    monkeypatch.setattr(
        registration_routes,
        "_prepare_registration_proxy_with_ip_check",
        lambda proxy_url, log_callback=None: (proxy_url, None, None, 1, False),
    )
    monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda db, proxy_id: None)
    monkeypatch.setattr(registration_routes.EmailServiceFactory, "create", lambda service_type, config: fake_email_service)
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeEngine)

    def fake_update_registration_task(db, task_uuid, **kwargs):
        task_updates.append(kwargs)
        return SimpleNamespace(task_uuid=task_uuid, status=kwargs.get("status", "pending"))

    monkeypatch.setattr(registration_routes.crud, "update_registration_task", fake_update_registration_task)

    registration_routes._run_sync_registration_task(
        "task-persist-fail",
        "luckmail",
        "http://proxy.local:8080",
        None,
        email_service_id=2,
    )

    assert any(item.get("status") == "failed" for item in task_updates)
    assert not any(item.get("status") == "completed" for item in task_updates)
    assert fake_email_service.marker_calls
    assert fake_email_service.marker_calls[0]["success"] is False
    assert fake_email_service.marker_calls[0]["reason"] == "保存到数据库失败"
    assert any(status == "failed" for _, status, _ in fake_task_manager.statuses)
    assert not any(status == "completed" for _, status, _ in fake_task_manager.statuses)


def test_sqlite_session_manager_enables_wal_and_busy_timeout(tmp_path):
    db_path = tmp_path / "concurrency.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")

    with manager.engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) >= 30000
    assert int(foreign_keys) == 1


def test_run_sync_registration_task_ignores_proxy_usage_failure(monkeypatch):
    task_updates = []

    class DummyQuery:
        def __init__(self, result):
            self._result = result

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return self._result

    class DummyDb:
        def __init__(self, service):
            self._service = service

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def query(self, model):
            return DummyQuery(self._service)

    class DummyTaskManager:
        def __init__(self):
            self.statuses = []
            self.logs = []

        def is_cancelled(self, task_uuid):
            return False

        def update_status(self, task_uuid, status, **kwargs):
            self.statuses.append((task_uuid, status, kwargs))

        def add_log(self, task_uuid, message):
            self.logs.append((task_uuid, message))

        def create_log_callback(self, task_uuid, prefix="", batch_id=""):
            def _callback(message):
                self.logs.append((task_uuid, message))
            return _callback

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.callback_logger = callback_logger
            self.task_uuid = task_uuid
            self.email_info = {"token": "tok_ok"}

        def run(self):
            return registration_routes.RegistrationResult(
                success=True,
                email="proxywarn@hotmail.com",
                password="Passw0rd!",
                metadata={},
                source="register",
            )

        def save_to_database(self, result, account_label=None, role_tag=None):
            return True

    fake_db_service = SimpleNamespace(
        id=2,
        service_type="luckmail",
        name="LuckMail",
        config={
            "base_url": "https://mails.luckyous.com/",
            "project_code": "openai",
            "preset_mailboxes": "proxywarn@hotmail.com----tok_ok",
        },
    )
    fake_task_manager = DummyTaskManager()
    fake_db = DummyDb(fake_db_service)

    monkeypatch.setattr(registration_routes, "get_db", lambda: fake_db)
    monkeypatch.setattr(registration_routes, "task_manager", fake_task_manager)
    monkeypatch.setattr(
        registration_routes,
        "_prepare_registration_proxy_with_ip_check",
        lambda proxy_url, log_callback=None: (proxy_url, None, None, 1, False),
    )
    monkeypatch.setattr(
        registration_routes,
        "get_proxy_for_registration",
        lambda db: ("http://proxy.local:8080", 1),
    )
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config: SimpleNamespace(service_type=SimpleNamespace(value="luckmail")),
    )
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeEngine)
    monkeypatch.setattr(
        registration_routes.crud,
        "update_proxy_last_used",
        lambda db, proxy_id: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )

    def fake_update_registration_task(db, task_uuid, **kwargs):
        task_updates.append(kwargs)
        return SimpleNamespace(task_uuid=task_uuid, status=kwargs.get("status", "pending"))

    monkeypatch.setattr(registration_routes.crud, "update_registration_task", fake_update_registration_task)

    registration_routes._run_sync_registration_task(
        "task-proxy-warning",
        "luckmail",
        None,
        None,
        email_service_id=2,
    )

    assert any(item.get("status") == "completed" for item in task_updates)
    assert not any(item.get("status") == "failed" for item in task_updates)
    assert any(status == "completed" for _, status, _ in fake_task_manager.statuses)
    assert not any(status == "failed" for _, status, _ in fake_task_manager.statuses)


def test_run_sync_registration_task_recovers_completed_status_after_bookkeeping_failure(monkeypatch):
    update_calls = []
    completed_attempts = {"count": 0}

    class DummyQuery:
        def __init__(self, result):
            self._result = result

        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return self._result

    class DummyDb:
        def __init__(self, service):
            self._service = service

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def query(self, model):
            return DummyQuery(self._service)

    class DummyTaskManager:
        def __init__(self):
            self.statuses = []
            self.logs = []

        def is_cancelled(self, task_uuid):
            return False

        def update_status(self, task_uuid, status, **kwargs):
            self.statuses.append((task_uuid, status, kwargs))

        def add_log(self, task_uuid, message):
            self.logs.append((task_uuid, message))

        def create_log_callback(self, task_uuid, prefix="", batch_id=""):
            def _callback(message):
                self.logs.append((task_uuid, message))
            return _callback

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.callback_logger = callback_logger
            self.task_uuid = task_uuid
            self.email_info = {"token": "tok_ok"}

        def run(self):
            return registration_routes.RegistrationResult(
                success=True,
                email="recovery@hotmail.com",
                password="Passw0rd!",
                metadata={},
                source="register",
            )

        def save_to_database(self, result, account_label=None, role_tag=None):
            return True

    fake_db_service = SimpleNamespace(
        id=2,
        service_type="luckmail",
        name="LuckMail",
        config={
            "base_url": "https://mails.luckyous.com/",
            "project_code": "openai",
            "preset_mailboxes": "recovery@hotmail.com----tok_ok",
        },
    )
    fake_task_manager = DummyTaskManager()
    fake_db = DummyDb(fake_db_service)

    monkeypatch.setattr(registration_routes, "get_db", lambda: fake_db)
    monkeypatch.setattr(registration_routes, "task_manager", fake_task_manager)
    monkeypatch.setattr(
        registration_routes,
        "_prepare_registration_proxy_with_ip_check",
        lambda proxy_url, log_callback=None: (proxy_url, None, None, 1, False),
    )
    monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda db, proxy_id: True)
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config: SimpleNamespace(service_type=SimpleNamespace(value="luckmail")),
    )
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeEngine)

    def fake_update_registration_task(db, task_uuid, **kwargs):
        update_calls.append(kwargs)
        if kwargs.get("status") == "completed":
            completed_attempts["count"] += 1
            if completed_attempts["count"] == 1:
                raise RuntimeError("database is locked")
        return SimpleNamespace(task_uuid=task_uuid, status=kwargs.get("status", "pending"))

    monkeypatch.setattr(registration_routes.crud, "update_registration_task", fake_update_registration_task)

    registration_routes._run_sync_registration_task(
        "task-complete-recovery",
        "luckmail",
        "http://proxy.local:8080",
        None,
        email_service_id=2,
    )

    assert completed_attempts["count"] == 2
    assert any(item.get("status") == "completed" for item in update_calls)
    assert not any(item.get("status") == "failed" for item in update_calls)
    assert any(status == "completed" for _, status, _ in fake_task_manager.statuses)
    assert not any(status == "failed" for _, status, _ in fake_task_manager.statuses)


def test_run_sync_registration_task_uses_task_bound_email_service_id_when_argument_missing(monkeypatch, tmp_path):
    db_path = tmp_path / "outlook_task_binding.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add_all([
            EmailService(
                service_type="outlook",
                name="Outlook 1",
                config={"email": "first@example.com", "password": "pw-1"},
                enabled=True,
                priority=0,
            ),
            EmailService(
                service_type="outlook",
                name="Outlook 2",
                config={"email": "second@example.com", "password": "pw-2"},
                enabled=True,
                priority=1,
            ),
        ])
        session.flush()
        first_service_id = session.query(EmailService).filter_by(name="Outlook 1").first().id
        second_service_id = session.query(EmailService).filter_by(name="Outlook 2").first().id
        registration_routes.crud.create_registration_task(
            session,
            task_uuid="task-bound-outlook",
            email_service_id=second_service_id,
            proxy="http://proxy.local:8080",
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class DummyTaskManager:
        def __init__(self):
            self.statuses = []
            self.logs = []

        def is_cancelled(self, task_uuid):
            return False

        def update_status(self, task_uuid, status, **kwargs):
            self.statuses.append((task_uuid, status, kwargs))

        def add_log(self, task_uuid, message):
            self.logs.append((task_uuid, message))

        def create_log_callback(self, task_uuid, prefix="", batch_id=""):
            def _callback(message):
                self.logs.append((task_uuid, message))
            return _callback

    class FakeEmailService:
        def __init__(self, service_type, config):
            self.service_type = SimpleNamespace(value=service_type.value)
            self.config = config

    class FakeEngine:
        def __init__(self, email_service, proxy_url=None, callback_logger=None, task_uuid=None):
            self.email_service = email_service
            self.email_info = {}

        def run(self):
            return registration_routes.RegistrationResult(
                success=True,
                email=self.email_service.config["email"],
                password="Passw0rd!",
                metadata={},
                source="register",
            )

        def save_to_database(self, result, account_label=None, role_tag=None):
            return True

    created_configs = []

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "task_manager", DummyTaskManager())
    monkeypatch.setattr(
        registration_routes,
        "_prepare_registration_proxy_with_ip_check",
        lambda proxy_url, log_callback=None: (proxy_url, None, None, 1, False),
    )
    monkeypatch.setattr(registration_routes, "update_proxy_usage", lambda db, proxy_id: None)
    monkeypatch.setattr(
        registration_routes.EmailServiceFactory,
        "create",
        lambda service_type, config: created_configs.append((service_type.value, config.copy())) or FakeEmailService(service_type, config),
    )
    monkeypatch.setattr(registration_routes, "RegistrationEngine", FakeEngine)

    registration_routes._run_sync_registration_task(
        "task-bound-outlook",
        "outlook",
        "http://proxy.local:8080",
        None,
        email_service_id=None,
    )

    assert created_configs == [
        (
            "outlook",
            {
                "email": "second@example.com",
                "password": "pw-2",
                "proxy_url": "http://proxy.local:8080",
            },
        )
    ]

    with manager.session_scope() as session:
        task = session.query(registration_routes.RegistrationTask).filter_by(task_uuid="task-bound-outlook").first()
        assert task is not None
        assert task.email_service_id == second_service_id
        assert task.email_service_id != first_service_id
        assert task.status == "completed"
