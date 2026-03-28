import base64
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.config.constants import EmailServiceType, OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.database import crud
from src.database.session import DatabaseSessionManager
from src.core.http_client import OpenAIHTTPClient
from src.core.openai.oauth import OAuthStart
from src.core.register import RegistrationEngine, RegistrationResult
from src.services.base import BaseEmailService
from src.services.outlook.service import OutlookService


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, on_return=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.on_return = on_return

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class QueueSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.cookies = {}

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._request(method.upper(), url, **kwargs)

    def close(self):
        return None

    def _request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.steps:
            raise AssertionError(f"unexpected request: {method} {url}")
        expected_method, expected_url, response = self.steps.pop(0)
        assert method == expected_method
        assert url == expected_url
        if callable(response):
            response = response(self)
        if response.on_return:
            response.on_return(self)
        return response


class FakeEmailService(BaseEmailService):
    def __init__(self, codes):
        super().__init__(EmailServiceType.TEMPMAIL)
        self.codes = list(codes)
        self.otp_requests = []

    def create_email(self, config=None):
        return {
            "email": "tester@example.com",
            "service_id": "mailbox-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        self.otp_requests.append({
            "email": email,
            "email_id": email_id,
            "otp_sent_at": otp_sent_at,
        })
        if not self.codes:
            raise AssertionError("no verification code queued")
        return self.codes.pop(0)

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


class FakeOAuthManager:
    def __init__(self):
        self.start_calls = 0
        self.callback_calls = []

    def start_oauth(self):
        self.start_calls += 1
        return OAuthStart(
            auth_url=f"https://auth.example.test/flow/{self.start_calls}",
            state=f"state-{self.start_calls}",
            code_verifier=f"verifier-{self.start_calls}",
            redirect_uri="http://localhost:1455/auth/callback",
        )

    def handle_callback(self, callback_url, expected_state, code_verifier):
        self.callback_calls.append({
            "callback_url": callback_url,
            "expected_state": expected_state,
            "code_verifier": code_verifier,
        })
        return {
            "account_id": "acct-1",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "id_token": "id-1",
        }


class FakeOpenAIClient:
    def __init__(self, sessions, sentinel_tokens):
        self._sessions = list(sessions)
        self._session_index = 0
        self._session = self._sessions[0]
        self._sentinel_tokens = list(sentinel_tokens)

    @property
    def session(self):
        return self._session

    def check_ip_location(self):
        return True, "US"

    def check_sentinel(self, did):
        if not self._sentinel_tokens:
            raise AssertionError("no sentinel token queued")
        return self._sentinel_tokens.pop(0)

    def close(self):
        if self._session_index + 1 < len(self._sessions):
            self._session_index += 1
            self._session = self._sessions[self._session_index]


def _workspace_cookie(workspace_id):
    payload = base64.urlsafe_b64encode(
        json.dumps({"workspaces": [{"id": workspace_id}]}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload}.sig"


def _response_with_did(did):
    return DummyResponse(
        status_code=200,
        text="ok",
        on_return=lambda session: session.cookies.__setitem__("oai-did", did),
    )


def _response_with_login_cookies(workspace_id="ws-1", session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie(workspace_id)
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def test_check_sentinel_sends_non_empty_pow(monkeypatch):
    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["sentinel"], DummyResponse(payload={"token": "sentinel-token"})),
    ])
    client = OpenAIHTTPClient()
    client._session = session

    monkeypatch.setattr(
        "src.core.http_client.build_sentinel_pow_token",
        lambda user_agent: "gAAAAACpow-token",
    )

    token = client.check_sentinel("device-1")

    assert token == "sentinel-token"
    body = json.loads(session.calls[0]["kwargs"]["data"])
    assert body["id"] == "device-1"
    assert body["flow"] == "authorize_continue"
    assert body["p"] == "gAAAAACpow-token"


def test_run_registers_then_relogs_to_fetch_token():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "register"
    assert result.workspace_id == "ws-1"
    assert result.session_token == "session-1"
    assert fake_oauth.start_calls == 2
    assert len(email_service.otp_requests) == 2
    assert all(item["otp_sent_at"] is not None for item in email_service.otp_requests)
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 1
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 0
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 1
    relogin_start_body = json.loads(session_two.calls[1]["kwargs"]["data"])
    assert relogin_start_body["screen_hint"] == "login"
    assert relogin_start_body["username"]["value"] == "tester@example.com"
    password_verify_body = json.loads(session_two.calls[2]["kwargs"]["data"])
    assert password_verify_body == {"password": result.password}
    assert result.metadata["token_acquired_via_relogin"] is True


def test_existing_account_login_uses_auto_sent_otp_without_manual_send():
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-existing", "session-existing")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-existing"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-existing",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["246810"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert fake_oauth.start_calls == 1
    assert sum(1 for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert len(email_service.otp_requests) == 1
    assert email_service.otp_requests[0]["otp_sent_at"] is not None
    assert result.metadata["token_acquired_via_relogin"] is False


def test_run_switches_to_existing_account_login_when_register_username_is_rejected():
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["register"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "Failed to register username. Please try again.",
                        "code": "bad_request",
                    }
                },
                text='{"error":{"message":"Failed to register username. Please try again.","code":"bad_request"}}',
            ),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-existing", "session-existing")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-existing"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-existing",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["135790"])
    engine = RegistrationEngine(email_service)
    engine.password = "existing-pass"
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert result.password == "existing-pass"
    assert result.session_token == "session-existing"
    assert sum(1 for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    password_verify_body = json.loads(session.calls[4]["kwargs"]["data"])
    assert password_verify_body == {"password": "existing-pass"}


def test_save_to_database_persists_outlook_recovery_payload(monkeypatch):
    email_service = OutlookService(
        {
            "email": "tester@outlook.com",
            "password": "mail-pwd",
            "client_id": "mail-client",
            "refresh_token": "mail-refresh",
        },
        name="test-outlook",
    )
    engine = RegistrationEngine(email_service)
    engine.email_info = {"email": "tester@outlook.com", "service_id": "tester@outlook.com"}
    engine._dump_session_cookies = lambda: "cookie-1"

    captured = {}

    def fake_create_account(db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=123)

    @contextmanager
    def fake_get_db():
        yield object()

    monkeypatch.setattr("src.core.register.crud.create_account", fake_create_account)
    monkeypatch.setattr("src.core.register.get_db", fake_get_db)
    monkeypatch.setattr("src.core.register.get_settings", lambda: SimpleNamespace(openai_client_id="app-client"))

    result = RegistrationResult(
        success=True,
        email="tester@outlook.com",
        password="openai-pass",
        account_id="acct-1",
        workspace_id="ws-1",
        access_token="access-1",
        refresh_token="refresh-1",
        id_token="id-1",
        session_token="session-1",
        metadata={"foo": "bar"},
        source="register",
    )

    assert engine.save_to_database(result) is True
    assert captured["extra_data"]["foo"] == "bar"
    assert captured["extra_data"]["outlook_recovery"] == {
        "email": "tester@outlook.com",
        "password": "mail-pwd",
        "client_id": "mail-client",
        "refresh_token": "mail-refresh",
    }


def test_outlook_finalize_partial_success_after_account_creation():
    email_service = FakeEmailService(["654321"])
    engine = RegistrationEngine(email_service)
    engine.password = "openai-pass"
    engine.device_id = "did-1"
    engine._create_account_completed = True
    engine._create_account_account_id = "acct-created"
    engine._create_account_workspace_id = "ws-created"
    engine._create_account_refresh_token = "refresh-created"
    engine._last_validate_otp_workspace_id = "ws-from-otp"
    engine._verify_email_otp_with_retry = lambda **kwargs: True
    engine._get_workspace_id = lambda: ""
    engine._select_workspace = lambda workspace_id: ""
    engine._capture_auth_session_tokens = lambda result, access_hint=None: setattr(result, "session_token", "session-from-cookie")

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._complete_token_exchange_outlook(result) is True
    assert result.account_id == "acct-created"
    assert result.workspace_id == "ws-from-otp"
    assert result.refresh_token == "refresh-created"
    assert result.password == "openai-pass"
    assert result.session_token == "session-from-cookie"
    assert result.source == "register"
    assert result.error_message == ""


def test_save_to_database_updates_existing_account_without_clobbering_tokens(monkeypatch, tmp_path):
    db_path = Path(tmp_path) / "registration.db"
    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    manager.create_tables()
    manager.migrate_tables()

    with manager.session_scope() as db:
        existing = crud.create_account(
            db,
            email="tester@outlook.com",
            password="old-openai-pass",
            client_id="old-client",
            session_token="old-session",
            cookies="old-cookie",
            email_service="outlook",
            email_service_id="tester@outlook.com",
            account_id="acct-old",
            workspace_id="ws-old",
            access_token="old-access",
            refresh_token="old-refresh",
            id_token="old-id",
            proxy_used="old-proxy",
            extra_data={"keep": "old", "old_only": True},
            source="register",
        )
        existing_id = existing.id

    @contextmanager
    def fake_get_db():
        db = manager.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    email_service = OutlookService(
        {
            "email": "tester@outlook.com",
            "password": "mail-pwd",
            "client_id": "mail-client",
            "refresh_token": "mail-refresh",
        },
        name="test-outlook",
    )
    engine = RegistrationEngine(email_service, proxy_url="socks5://proxy.example:1234")
    engine.email_info = {"email": "tester@outlook.com", "service_id": "tester@outlook.com"}
    engine._dump_session_cookies = lambda: "new-cookie"

    monkeypatch.setattr("src.core.register.get_db", fake_get_db)
    monkeypatch.setattr("src.core.register.get_settings", lambda: SimpleNamespace(openai_client_id="new-client"))

    result = RegistrationResult(
        success=True,
        email="tester@outlook.com",
        password="new-openai-pass",
        account_id="",
        workspace_id="ws-new",
        access_token="",
        refresh_token="refresh-new",
        id_token="",
        session_token="",
        metadata={"new_flag": True},
        source="register",
    )

    assert engine.save_to_database(result) is True

    with manager.session_scope() as db:
        saved = crud.get_account_by_email(db, "tester@outlook.com")
        registered, record, _account = crud.get_registered_email_state(db, "tester@outlook.com")

        assert saved is not None
        assert saved.id == existing_id
        assert saved.password == "new-openai-pass"
        assert saved.client_id == "new-client"
        assert saved.workspace_id == "ws-new"
        assert saved.access_token == "old-access"
        assert saved.refresh_token == "refresh-new"
        assert saved.session_token == "old-session"
        assert saved.cookies == "new-cookie"
        assert saved.proxy_used == "socks5://proxy.example:1234"
        assert saved.extra_data["keep"] == "old"
        assert saved.extra_data["old_only"] is True
        assert saved.extra_data["new_flag"] is True
        assert saved.extra_data["outlook_recovery"] == {
            "email": "tester@outlook.com",
            "password": "mail-pwd",
            "client_id": "mail-client",
            "refresh_token": "mail-refresh",
        }
        assert registered is True
        assert record is not None
        assert record.status == "registered_success"


def test_handle_oauth_callback_uses_dedicated_retry_setting(monkeypatch):
    class RetryableOAuthManager:
        def __init__(self):
            self.calls = 0

        def handle_callback(self, callback_url, expected_state, code_verifier):
            self.calls += 1
            if self.calls < 5:
                raise RuntimeError("token exchange failed: network error: timeout")
            return {
                "account_id": "acct-retry",
                "access_token": "access-retry",
                "refresh_token": "refresh-retry",
                "id_token": "id-retry",
            }

    email_service = FakeEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.example.test/flow/1",
        state="state-1",
        code_verifier="verifier-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    retry_oauth = RetryableOAuthManager()
    engine.oauth_manager = retry_oauth

    monkeypatch.setattr("src.core.register.get_settings", lambda: SimpleNamespace(registration_token_exchange_max_retries=5))
    monkeypatch.setattr("src.core.register.time.sleep", lambda *_args, **_kwargs: None)

    token_info = engine._handle_oauth_callback("http://localhost:1455/auth/callback?code=abc&state=state-1")

    assert token_info is not None
    assert token_info["access_token"] == "access-retry"
    assert retry_oauth.calls == 5
