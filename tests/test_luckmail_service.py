import json

from src.services import luckmail_mail
from src.services.luckmail_mail import LuckMailService


class _FakeLuckMailUser:
    def __init__(self):
        self.purchase_called = False

    def purchase_emails(self, **kwargs):
        self.purchase_called = True
        raise AssertionError("preset_mailboxes 已配置时不应再调用 purchase_emails")


class _FakeLuckMailClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key
        self.user = _FakeLuckMailUser()


def _bind_temp_state(service, tmp_path):
    service._data_dir = tmp_path
    service._registered_file = tmp_path / "luckmail_registered_emails.json"
    service._failed_file = tmp_path / "luckmail_failed_emails.json"


def test_luckmail_create_email_prefers_preset_mailbox(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.services.luckmail_mail._load_luckmail_client_class",
        lambda: _FakeLuckMailClient,
    )
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()

    service = LuckMailService(
        {
            "base_url": "https://mails.luckyous.com/",
            "api_key": "lm_test_key",
            "project_code": "openai",
            "email_type": "ms_graph",
            "preferred_domain": "hotmail.com",
            "preset_mailboxes": "brynnsiofra6506@hotmail.com----tok_36283b4eb7ccc93a2eeaf45738825c7b",
        }
    )
    _bind_temp_state(service, tmp_path)

    info = service.create_email()

    assert info["email"] == "brynnsiofra6506@hotmail.com"
    assert info["service_id"] == "tok_36283b4eb7ccc93a2eeaf45738825c7b"
    assert info["token"] == "tok_36283b4eb7ccc93a2eeaf45738825c7b"
    assert info["inbox_mode"] == "purchase"
    assert info["source"] == "preset_purchase"
    assert service.client.user.purchase_called is False
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()


def test_luckmail_create_email_prefers_preset_mailbox_without_sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.services.luckmail_mail._load_luckmail_client_class",
        lambda: None,
    )
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()

    service = LuckMailService(
        {
            "base_url": "https://mails.luckyous.com/",
            "project_code": "openai",
            "email_type": "ms_graph",
            "preset_mailboxes": "brynnsiofra6506@hotmail.com----tok_36283b4eb7ccc93a2eeaf45738825c7b",
        }
    )
    _bind_temp_state(service, tmp_path)

    info = service.create_email()

    assert service.client is None
    assert info["email"] == "brynnsiofra6506@hotmail.com"
    assert info["token"] == "tok_36283b4eb7ccc93a2eeaf45738825c7b"
    assert info["source"] == "preset_purchase"
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()


def test_luckmail_create_email_reserves_distinct_preset_mailboxes_across_instances(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.services.luckmail_mail._load_luckmail_client_class",
        lambda: None,
    )
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()

    config = {
        "base_url": "https://mails.luckyous.com/",
        "project_code": "openai",
        "email_type": "ms_graph",
        "preset_mailboxes": (
            "firstbox@hotmail.com----tok_first\n"
            "secondbox@hotmail.com----tok_second"
        ),
    }

    service_one = LuckMailService(config)
    service_two = LuckMailService(config)
    _bind_temp_state(service_one, tmp_path)
    _bind_temp_state(service_two, tmp_path)

    first = service_one.create_email()
    second = service_two.create_email()

    assert first["email"] == "firstbox@hotmail.com"
    assert second["email"] == "secondbox@hotmail.com"
    assert first["email"] != second["email"]
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()


def test_luckmail_preset_mailbox_skips_existing_db_account(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.services.luckmail_mail._load_luckmail_client_class",
        lambda: None,
    )
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()

    service = LuckMailService(
        {
            "base_url": "https://mails.luckyous.com/",
            "project_code": "openai",
            "email_type": "ms_graph",
            "preset_mailboxes": (
                "usedbox@hotmail.com----tok_used\n"
                "freshbox@hotmail.com----tok_fresh"
            ),
        }
    )
    _bind_temp_state(service, tmp_path)
    monkeypatch.setattr(service, "_query_existing_account_emails", lambda emails: {"usedbox@hotmail.com"})

    info = service.create_email()

    assert info["email"] == "freshbox@hotmail.com"
    registered_payload = json.loads(service._registered_file.read_text(encoding="utf-8"))
    assert "usedbox@hotmail.com" in registered_payload["emails"]
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()


def test_luckmail_get_verification_code_uses_token_api_without_sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.services.luckmail_mail._load_luckmail_client_class",
        lambda: None,
    )
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()

    service = LuckMailService(
        {
            "base_url": "https://mails.luckyous.com/",
            "project_code": "openai",
            "email_type": "ms_graph",
            "preset_mailboxes": "brynnsiofra6506@hotmail.com----tok_36283b4eb7ccc93a2eeaf45738825c7b",
        }
    )
    _bind_temp_state(service, tmp_path)
    info = service.create_email()

    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {
            "has_new_mail": True,
            "verification_code": "482910",
        }

    monkeypatch.setattr(service, "_request_openapi", fake_request)

    code = service.get_verification_code(info["email"], info["service_id"], timeout=1)

    assert code == "482910"
    assert calls
    assert calls[0][0] == "GET"
    assert calls[0][1].endswith("/email/token/tok_36283b4eb7ccc93a2eeaf45738825c7b/code")
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()


def test_luckmail_check_health_uses_token_alive_without_sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.services.luckmail_mail._load_luckmail_client_class",
        lambda: None,
    )
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()

    service = LuckMailService(
        {
            "base_url": "https://mails.luckyous.com/",
            "project_code": "openai",
            "email_type": "ms_graph",
            "preset_mailboxes": "brynnsiofra6506@hotmail.com----tok_36283b4eb7ccc93a2eeaf45738825c7b",
        }
    )
    _bind_temp_state(service, tmp_path)

    def fake_request(method, path, **kwargs):
        assert method == "GET"
        assert path.endswith("/email/token/tok_36283b4eb7ccc93a2eeaf45738825c7b/alive")
        return {
            "alive": True,
            "status": "success",
        }

    monkeypatch.setattr(service, "_request_openapi", fake_request)

    assert service.check_health() is True
    luckmail_mail._RUNTIME_RESERVED_EMAILS.clear()


def test_luckmail_rejects_invalid_preset_mailbox_format(monkeypatch):
    monkeypatch.setattr(
        "src.services.luckmail_mail._load_luckmail_client_class",
        lambda: _FakeLuckMailClient,
    )

    try:
        LuckMailService(
            {
                "base_url": "https://mails.luckyous.com/",
                "api_key": "lm_test_key",
                "preset_mailboxes": "invalid-format",
            }
        )
    except ValueError as exc:
        assert "邮箱----tok_xxx" in str(exc)
    else:
        raise AssertionError("expected invalid preset mailbox format to fail")
