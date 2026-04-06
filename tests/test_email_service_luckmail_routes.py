import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.config.constants import EmailServiceType
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.base import EmailServiceFactory
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    tempmail_enabled = False
    yyds_mail_enabled = False
    yyds_mail_api_key = None
    custom_domain_base_url = ""
    custom_domain_api_key = None


def test_luckmail_service_registered():
    service_type = EmailServiceType("luckmail")
    service_class = EmailServiceFactory.get_service_class(service_type)
    assert service_class is not None
    assert service_class.__name__ == "LuckMailService"


def test_email_service_types_include_luckmail():
    result = asyncio.run(email_routes.get_service_types())
    luckmail_type = next(item for item in result["types"] if item["value"] == "luckmail")

    assert luckmail_type["label"] == "LuckMail"
    field_names = [field["name"] for field in luckmail_type["config_fields"]]
    assert "api_key" in field_names
    assert "project_code" in field_names
    assert "email_type" in field_names
    assert "preferred_domain" in field_names
    assert "preset_mailboxes" in field_names


def test_filter_sensitive_config_marks_luckmail_api_key():
    filtered = email_routes.filter_sensitive_config(
        {
            "base_url": "https://mails.luckyous.com/",
            "api_key": "lm_test_key",
            "project_code": "openai",
            "email_type": "ms_graph",
            "preferred_domain": "hotmail.com",
            "preset_mailboxes": "brynnsiofra6506@hotmail.com----tok_36283b4eb7ccc93a2eeaf45738825c7b",
        }
    )

    assert filtered["base_url"] == "https://mails.luckyous.com/"
    assert filtered["project_code"] == "openai"
    assert filtered["email_type"] == "ms_graph"
    assert filtered["preferred_domain"] == "hotmail.com"
    assert filtered["has_api_key"] is True
    assert filtered["has_preset_mailboxes"] is True
    assert filtered["preset_mailbox_count"] == 1
    assert "api_key" not in filtered
    assert "preset_mailboxes" not in filtered


def test_registration_available_services_include_outlook_and_luckmail(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "outlook_luckmail_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="outlook",
                name="banjooswald1002@hotmail.com",
                config={
                    "email": "banjooswald1002@hotmail.com",
                    "password": "tok_outlook",
                },
                enabled=True,
                priority=0,
            )
        )
        session.add(
            EmailService(
                service_type="luckmail",
                name="LuckMail Hotmail",
                config={
                    "base_url": "https://mails.luckyous.com/",
                    "api_key": "lm_test_key",
                    "project_code": "openai",
                    "email_type": "ms_graph",
                    "preferred_domain": "hotmail.com",
                },
                enabled=True,
                priority=1,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    import src.config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: DummySettings())
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["outlook"]["available"] is True
    assert result["outlook"]["count"] == 1
    assert result["outlook"]["services"][0]["name"] == "banjooswald1002@hotmail.com"
    assert result["outlook"]["services"][0]["type"] == "outlook"
    assert result["outlook"]["services"][0]["has_oauth"] is False

    assert result["luckmail"]["available"] is True
    assert result["luckmail"]["count"] == 1
    assert result["luckmail"]["services"][0]["name"] == "LuckMail Hotmail"
    assert result["luckmail"]["services"][0]["type"] == "luckmail"
    assert result["luckmail"]["services"][0]["email_type"] == "ms_graph"
    assert result["luckmail"]["services"][0]["preferred_domain"] == "hotmail.com"
