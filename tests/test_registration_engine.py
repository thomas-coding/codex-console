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
from src.core.register import RegistrationEngine, RegistrationResult, SignupFormResult
from src.services.base import BaseEmailService
from src.services.outlook.service import OutlookService
from http_client import _build_playwright_cookie_items


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


def _auth_session_cookie(payload):
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{encoded}.sig"


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
        lambda user_agent, browser_profile=None: "gAAAAACpow-token",
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
    assert "last_validate_otp_page_type" in result.metadata
    assert "auth_cookie_has_workspace" in result.metadata


def test_run_prefers_create_account_continue_before_relogin():
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
        (
            "POST",
            OPENAI_API_ENDPOINTS["create_account"],
            DummyResponse(
                payload={
                    "continue_url": "http://localhost:1455/auth/callback?code=code-1&state=state-1",
                    "account_id": "acct-created",
                    "workspace_id": "ws-created",
                    "refresh_token": "refresh-created",
                }
            ),
        ),
        (
            "GET",
            "http://localhost:1455/auth/callback?code=code-1&state=state-1",
            DummyResponse(
                payload={"session": {"access_token": "access-1"}},
                on_return=lambda session: session.cookies.__setitem__("__Secure-next-auth.session-token", "session-1"),
            ),
        ),
        (
            "GET",
            "https://chatgpt.com/api/auth/session",
            DummyResponse(
                payload={"session": {"access_token": "access-1"}},
                on_return=lambda session: session.cookies.__setitem__("__Secure-next-auth.session-token", "session-1"),
            ),
        ),
        (
            "GET",
            "https://chatgpt.com/",
            DummyResponse(
                payload={},
                on_return=lambda session: session.cookies.__setitem__("__Secure-next-auth.session-token", "session-1"),
            ),
        ),
        (
            "GET",
            "https://chatgpt.com/api/auth/session",
            DummyResponse(
                payload={"session": {"access_token": "access-1"}},
                on_return=lambda session: session.cookies.__setitem__("__Secure-next-auth.session-token", "session-1"),
            ),
        ),
    ])

    email_service = FakeEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.account_id == "acct-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"
    assert result.session_token == "session-1"
    assert fake_oauth.start_calls == 1
    assert result.metadata["token_acquired_via_relogin"] is False
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["password_verify"]) == 0


def test_run_outlook_continues_completion_when_direct_path_only_has_access(monkeypatch):
    monkeypatch.setattr(
        "src.core.register.get_settings",
        lambda: SimpleNamespace(
            openai_client_id="app-client",
            openai_auth_url="https://auth.example.test/authorize",
            openai_token_url="https://auth.example.test/token",
            openai_redirect_uri="http://localhost:1455/auth/callback",
            openai_scope="openid email profile offline_access",
        ),
    )

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.email_service.service_type = SimpleNamespace(value="outlook")
    engine._check_ip_location = lambda: (True, "US")
    engine._create_email = lambda: (
        setattr(engine, "email", "tester@example.com"),
        setattr(engine, "email_info", {"service_id": "mailbox-1"}),
        True,
    )[-1]
    engine._prepare_authorize_flow = lambda label: ("did-1", "sentinel-1")
    engine._submit_signup_form = lambda did, sen_token: SignupFormResult(
        success=True,
        page_type=OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"],
    )
    engine._register_password = lambda did=None, sen_token=None: (setattr(engine, "password", "openai-pass"), (True, "openai-pass"))[-1]
    engine._send_verification_code = lambda referer=None: True
    engine._verify_email_otp_with_retry = lambda **kwargs: True
    engine._create_user_account = lambda: (
        setattr(engine, "_create_account_completed", True),
        setattr(engine, "_create_account_account_id", "acct-1"),
        True,
    )[-1]
    engine._try_complete_created_account_direct_session = (
        lambda result, flow_label: (
            setattr(result, "account_id", "acct-1"),
            setattr(result, "access_token", "access-1"),
            setattr(result, "session_token", "session-1"),
            False,
        )[-1]
    )
    engine._restart_login_flow = lambda: (
        setattr(engine, "_token_acquisition_requires_login", True),
        (True, ""),
    )[-1]
    completion_called = {"value": 0}

    def complete_outlook(result):
        completion_called["value"] += 1
        result.workspace_id = "ws-1"
        result.refresh_token = "refresh-1"
        result.device_id = "did-2"
        return True

    engine._complete_token_exchange_outlook = complete_outlook

    result = engine.run()

    assert result.success is True
    assert completion_called["value"] == 1
    assert result.account_id == "acct-1"
    assert result.workspace_id == "ws-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"
    assert result.metadata["token_acquired_via_relogin"] is True


def test_run_outlook_prefers_browser_reference_flow(monkeypatch):
    monkeypatch.setattr(
        "src.core.register.get_settings",
        lambda: SimpleNamespace(
            openai_client_id="app-client",
            openai_auth_url="https://auth.example.test/authorize",
            openai_token_url="https://auth.example.test/token",
            openai_redirect_uri="http://localhost:1455/auth/callback",
            openai_scope="openid email profile offline_access",
            registration_entry_flow="native",
        ),
    )

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.email_service.service_type = SimpleNamespace(value="outlook")
    engine._check_ip_location = lambda: (True, "US")
    engine._create_email = lambda: (
        setattr(engine, "email", "tester@example.com"),
        setattr(engine, "email_info", {"service_id": "mailbox-1"}),
        True,
    )[-1]
    engine._try_run_outlook_browser_reference = (
        lambda result: (
            setattr(engine, "password", "openai-pass"),
            setattr(result, "email", "tester@example.com"),
            setattr(result, "password", "openai-pass"),
            setattr(result, "account_id", "acct-browser"),
            setattr(result, "workspace_id", "ws-browser"),
            setattr(result, "access_token", "access-browser"),
            setattr(result, "session_token", "session-browser"),
            True,
        )[-1]
    )
    engine._prepare_authorize_flow = lambda label: (_ for _ in ()).throw(
        AssertionError("browser reference flow should skip HTTP authorize bootstrap")
    )
    engine._backfill_identity_from_current_session = lambda result, source_label: True
    engine._backfill_oauth_tokens_from_authenticated_session = lambda result, source_label: False
    engine._finalize_result_with_current_tokens = lambda result, workspace_hint=None, source=None: True

    result = engine.run()

    assert result.success is True
    assert result.email == "tester@example.com"
    assert result.account_id == "acct-browser"
    assert result.workspace_id == "ws-browser"
    assert result.access_token == "access-browser"
    assert result.session_token == "session-browser"
    assert result.metadata["registration_entry_flow_effective"] == "outlook"
    assert result.metadata["token_acquired_via_relogin"] is False


def test_outlook_browser_reference_skips_authenticated_proxy():
    engine = RegistrationEngine(FakeEmailService(["123456"]), proxy_url="http://user:pass@proxy.example:8080")

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._try_run_outlook_browser_reference(result) is False
    assert any("认证代理" in log for log in engine.logs)


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
    monkeypatch.setattr("src.core.register.crud.get_account_by_email", lambda db, email: None)
    monkeypatch.setattr("src.core.register.crud.upsert_registered_email", lambda db, **kwargs: None)
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
    engine._capture_auth_session_tokens = (
        lambda result, access_hint=None, referer=None: setattr(result, "session_token", "session-from-cookie")
    )

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._complete_token_exchange_outlook(result) is True
    assert result.account_id == "acct-created"
    assert result.workspace_id == "ws-from-otp"
    assert result.refresh_token == "refresh-created"
    assert result.password == "openai-pass"
    assert result.session_token == "session-from-cookie"
    assert result.source == "register"
    assert result.error_message == ""


def test_best_effort_retry_outlook_refresh_retries_gate_once():
    engine = RegistrationEngine(FakeEmailService(["654321"]))
    engine._create_account_completed = True
    engine._last_validate_otp_continue_url = "https://auth.openai.com/add-phone"

    restart_calls = []
    completion_calls = []

    def fake_restart():
        restart_calls.append(True)
        return True, ""

    def fake_complete(result):
        completion_calls.append(True)
        engine._last_validate_otp_continue_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        result.refresh_token = "refresh-1"
        return True

    engine._restart_login_flow = fake_restart
    engine._complete_token_exchange_outlook = fake_complete

    result = RegistrationResult(success=False, email="tester@example.com", access_token="access-1", session_token="session-1")

    engine._best_effort_retry_outlook_refresh(result, max_attempts=1)

    assert len(restart_calls) == 1
    assert len(completion_calls) == 1
    assert result.refresh_token == "refresh-1"


def test_validate_verification_code_tracks_add_phone_and_missing_auth_workspace():
    session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "continue_url": "https://auth.openai.com/add-phone",
                    "page": {"type": "add_phone"},
                },
                on_return=lambda queued: queued.cookies.__setitem__(
                    "oai-client-auth-session",
                    _auth_session_cookie(
                        {
                            "email": "tester@example.com",
                            "email_verified": True,
                            "session_id": "authsess-1",
                        }
                    ),
                ),
            ),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = session

    assert engine._validate_verification_code("123456") is True
    assert engine._last_validate_otp_page_type == "add_phone"
    assert engine._last_validate_otp_continue_url == "https://auth.openai.com/add-phone"
    assert engine._last_auth_cookie_has_workspace is False
    assert engine._last_auth_cookie_workspace_id == ""


def test_validate_verification_code_tracks_auth_workspace_from_cookie():
    session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "continue_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "page": {"type": "sign_in_with_chatgpt_codex_consent"},
                },
                on_return=lambda queued: queued.cookies.__setitem__(
                    "oai-client-auth-session",
                    _workspace_cookie("ws-cookie"),
                ),
            ),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = session

    assert engine._validate_verification_code("123456") is True
    assert engine._last_validate_otp_page_type == "sign_in_with_chatgpt_codex_consent"
    assert engine._last_auth_cookie_has_workspace is True
    assert engine._last_auth_cookie_workspace_id == "ws-cookie"


def test_pick_preferred_continue_url_prefers_callback_over_add_phone_gate():
    engine = RegistrationEngine(FakeEmailService(["123456"]))

    picked = engine._pick_preferred_continue_url(
        "https://auth.openai.com/add-phone?state=otp-state",
        "http://localhost:1455/auth/callback?code=code-1&state=state-1",
    )

    assert picked == "http://localhost:1455/auth/callback?code=code-1&state=state-1"


def test_capture_auth_session_tokens_extracts_nested_access_token():
    session = QueueSession([
        (
            "GET",
            "https://chatgpt.com/api/auth/session",
            DummyResponse(
                payload={
                    "user": {
                        "account_id": "acct-session",
                        "session": {
                            "access_token": "jwt.access.token",
                            "current_workspace_id": "ws-session",
                        }
                    }
                },
                on_return=lambda queued: queued.cookies.__setitem__("__Secure-next-auth.session-token", "session-1"),
            ),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = session

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._capture_auth_session_tokens(result, referer="https://auth.openai.com/add-phone") is True
    assert result.account_id == "acct-session"
    assert result.workspace_id == "ws-session"
    assert result.access_token == "jwt.access.token"
    assert result.session_token == "session-1"


def test_try_complete_session_from_registration_gate_uses_browser_client_fallback():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine._capture_auth_session_tokens = lambda result, access_hint=None, referer=None: False
    engine._warmup_chatgpt_session = lambda: None
    engine._backfill_identity_from_current_session = lambda result, source_label: True
    engine._backfill_oauth_tokens_from_authenticated_session = lambda result, source_label: False
    captured = {}

    def fake_browser_capture(result, *, stage_label, auth_url=None, continue_url=None):
        captured["stage_label"] = stage_label
        captured["continue_url"] = continue_url
        result.access_token = "access-browser"
        result.session_token = "session-browser"
        result.workspace_id = "ws-browser"
        result.account_id = "acct-browser"
        return True

    engine._try_capture_with_root_browser_client = fake_browser_capture
    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._try_complete_session_from_registration_gate(
        result,
        "https://auth.openai.com/add-phone?state=gate-1",
    )
    assert captured == {
        "stage_label": "门页桥接",
        "continue_url": "https://auth.openai.com/add-phone?state=gate-1",
    }
    assert result.access_token == "access-browser"
    assert result.session_token == "session-browser"
    assert result.workspace_id == "ws-browser"
    assert result.account_id == "acct-browser"


def test_try_complete_session_from_registration_gate_prefers_browser_first():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.registration_browser_first_enabled = True
    engine.session = QueueSession([])
    engine._capture_auth_session_tokens = lambda result, access_hint=None, referer=None: (_ for _ in ()).throw(
        AssertionError("browser-first gate should not hit auth/session before browser capture")
    )
    engine._warmup_chatgpt_session = lambda: (_ for _ in ()).throw(
        AssertionError("browser-first gate should not warm up before browser capture")
    )
    engine._backfill_identity_from_current_session = lambda result, source_label: True
    engine._backfill_oauth_tokens_from_authenticated_session = lambda result, source_label: False
    engine._try_capture_with_root_browser_client = lambda result, *, stage_label, auth_url=None, continue_url=None: (
        setattr(result, "access_token", "access-browser"),
        setattr(result, "session_token", "session-browser"),
        True,
    )[-1]

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._try_complete_session_from_registration_gate(
        result,
        "https://auth.openai.com/add-phone?state=gate-browser-first",
    ) is True
    assert result.access_token == "access-browser"
    assert result.session_token == "session-browser"


def test_build_playwright_cookie_items_keeps_only_essential_domains():
    items = _build_playwright_cookie_items(
        "__Secure-next-auth.session-token=session-1; "
        "oai-did=did-1; "
        "oai-client-auth-session=auth-session-1; "
        "oai-client-auth-info=auth-info-1; "
        "_puid=ignored"
    )

    by_name = {}
    for item in items:
        by_name.setdefault(item["name"], []).append(item)

    assert len(by_name["__Secure-next-auth.session-token"]) == 1
    assert by_name["__Secure-next-auth.session-token"][0]["domain"] == ".chatgpt.com"
    assert len(by_name["oai-did"]) == 2
    assert sorted(item["domain"] for item in by_name["oai-did"]) == [".auth.openai.com", ".chatgpt.com"]
    assert len(by_name["oai-client-auth-session"]) == 1
    assert by_name["oai-client-auth-session"][0]["domain"] == ".auth.openai.com"
    assert len(by_name["oai-client-auth-info"]) == 1
    assert by_name["oai-client-auth-info"][0]["domain"] == ".auth.openai.com"
    assert "_puid" not in by_name


def test_capture_auth_session_tokens_uses_personal_account_id_as_workspace_id():
    session = QueueSession([
        (
            "GET",
            "https://chatgpt.com/api/auth/session",
            DummyResponse(
                payload={
                    "account": {
                        "id": "acct-personal",
                        "structure": "personal",
                    },
                    "accessToken": "jwt.personal.token",
                },
                on_return=lambda queued: queued.cookies.__setitem__("__Secure-next-auth.session-token", "session-2"),
            ),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = session

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._capture_auth_session_tokens(result, referer="https://chatgpt.com/")
    assert result.account_id == "acct-personal"
    assert result.workspace_id == "acct-personal"
    assert result.access_token == "jwt.personal.token"
    assert result.session_token == "session-2"


def test_backfill_identity_from_current_session_reads_backend_api_me():
    session = QueueSession([
        (
            "GET",
            "https://chatgpt.com/backend-api/me",
            DummyResponse(
                payload={
                    "accounts": {"default": {"account_id": "acct-backend"}},
                    "workspaces": [{"id": "ws-backend"}],
                }
            ),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = session
    engine._get_workspace_id = lambda: ""

    result = RegistrationResult(
        success=False,
        email="tester@example.com",
        access_token="not-a-jwt",
    )

    assert engine._backfill_identity_from_current_session(result, source_label="test") is True
    assert result.account_id == "acct-backend"
    assert result.workspace_id == "ws-backend"


def test_get_workspace_id_falls_back_to_client_auth_session_dump():
    session = QueueSession([
        ("GET", "https://auth.openai.com/sign-in-with-chatgpt/codex/consent", DummyResponse(text="ok")),
        (
            "GET",
            "https://auth.openai.com/api/accounts/client_auth_session_dump",
            DummyResponse(payload={"client_auth_session": {"workspaces": [{"id": "ws-dump"}]}}),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = session
    engine.session.cookies = {}

    assert engine._get_workspace_id() == "ws-dump"


def test_get_workspace_id_reads_base64_auth_info_cookie():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    payload = base64.urlsafe_b64encode(
        json.dumps({"workspace": {"id": "ws-auth-info"}}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    engine.session = SimpleNamespace(cookies={"oai-client-auth-info": payload})

    assert engine._get_workspace_id() == "ws-auth-info"


def test_get_workspace_id_falls_back_to_consent_html_when_dump_missing_workspace():
    consent_html = """
    <html>
      <body>
        <script id="__NEXT_DATA__" type="application/json">
          {"props":{"pageProps":{"clientAuthSession":{"workspaces":[{"id":"ws-html"}]}}}}
        </script>
      </body>
    </html>
    """
    session = QueueSession([
        ("GET", "https://auth.openai.com/sign-in-with-chatgpt/codex/consent", DummyResponse(text=consent_html)),
        (
            "GET",
            "https://auth.openai.com/api/accounts/client_auth_session_dump",
            DummyResponse(payload={"client_auth_session": {"session_id": "sess-1"}}),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = session
    engine.session.cookies = {}

    assert engine._get_workspace_id() == "ws-html"


def test_backfill_oauth_tokens_from_authenticated_session_prefers_authorize_without_prompt():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = SimpleNamespace(cookies={})
    engine.oauth_manager = SimpleNamespace(
        start_oauth=lambda: OAuthStart(
            auth_url=(
                "https://auth.example.test/oauth/authorize?"
                "client_id=app-client&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback"
                "&scope=openid&state=state-1&prompt=login"
            ),
            state="state-1",
            code_verifier="verifier-1",
            redirect_uri="http://localhost:1455/auth/callback",
        )
    )
    engine._get_workspace_id = lambda: ""

    followed_urls = []

    def fake_follow_redirects(url):
        followed_urls.append(url)
        return ("http://localhost:1455/auth/callback?code=code-1&state=state-1", url)

    engine._follow_redirects = fake_follow_redirects
    engine._handle_oauth_callback = lambda callback_url: {
        "account_id": "acct-1",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "id_token": "id-1",
    }

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._backfill_oauth_tokens_from_authenticated_session(result, source_label="test") is True
    assert followed_urls == [
        (
            "https://auth.example.test/oauth/authorize?"
            "client_id=app-client&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback"
            "&scope=openid&state=state-1&prompt=none"
        )
    ]
    assert result.refresh_token == "refresh-1"


def test_backfill_oauth_tokens_from_authenticated_session_reuses_existing_oauth_start():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = SimpleNamespace(cookies={})
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.example.test/oauth/authorize?client_id=app-client&state=state-existing&prompt=login",
        state="state-existing",
        code_verifier="verifier-existing",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    start_oauth_calls = []
    engine.oauth_manager = SimpleNamespace(
        start_oauth=lambda: (
            start_oauth_calls.append("called"),
            OAuthStart(
                auth_url="https://auth.example.test/oauth/authorize?client_id=app-client&state=state-fresh&prompt=login",
                state="state-fresh",
                code_verifier="verifier-fresh",
                redirect_uri="http://localhost:1455/auth/callback",
            ),
        )[-1]
    )
    engine._get_workspace_id = lambda: ""

    followed_urls = []

    def fake_follow_redirects(url):
        followed_urls.append(url)
        return ("http://localhost:1455/auth/callback?code=code-1&state=state-existing", url)

    engine._follow_redirects = fake_follow_redirects
    engine._handle_oauth_callback = lambda callback_url: {
        "account_id": "acct-1",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "id_token": "id-1",
    }

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._backfill_oauth_tokens_from_authenticated_session(result, source_label="test") is True
    assert start_oauth_calls == []
    assert followed_urls == [
        "https://auth.example.test/oauth/authorize?client_id=app-client&state=state-existing&prompt=none"
    ]
    assert result.refresh_token == "refresh-1"


def test_backfill_oauth_tokens_from_authenticated_session_uses_account_id_as_workspace_candidate():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = SimpleNamespace(cookies={})
    engine.oauth_manager = SimpleNamespace(
        start_oauth=lambda: OAuthStart(
            auth_url="https://auth.example.test/oauth/authorize?client_id=app-client&prompt=login",
            state="state-1",
            code_verifier="verifier-1",
            redirect_uri="http://localhost:1455/auth/callback",
        )
    )
    engine._get_workspace_id = lambda: ""

    workspace_select_calls = []

    def fake_select_workspace(workspace_id):
        workspace_select_calls.append(workspace_id)
        if workspace_id == "acct-1":
            return "https://auth.example.test/continue-from-account"
        return ""

    engine._select_workspace = fake_select_workspace
    engine._follow_redirects = lambda url: ("http://localhost:1455/auth/callback?code=code-1&state=state-1", url)
    engine._handle_oauth_callback = lambda callback_url: {
        "account_id": "acct-1",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "id_token": "id-1",
    }

    result = RegistrationResult(success=False, email="tester@example.com", account_id="acct-1")

    assert engine._backfill_oauth_tokens_from_authenticated_session(result, source_label="test") is True
    assert workspace_select_calls == ["acct-1"]
    assert result.workspace_id == "acct-1"
    assert result.refresh_token == "refresh-1"


def test_backfill_oauth_tokens_from_authenticated_session_tries_browser_client_after_http_miss():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = SimpleNamespace(cookies={})
    engine.oauth_manager = SimpleNamespace(
        start_oauth=lambda: OAuthStart(
            auth_url="https://auth.example.test/oauth/authorize?client_id=app-client&prompt=login",
            state="state-1",
            code_verifier="verifier-1",
            redirect_uri="http://localhost:1455/auth/callback",
        )
    )
    engine._get_workspace_id = lambda: ""
    engine._follow_redirects = lambda url: ("https://auth.openai.com/log-in", "https://auth.openai.com/log-in")

    browser_calls = []

    def fake_browser_capture(result, *, stage_label, auth_url=None, continue_url=None):
        browser_calls.append(
            {
                "stage_label": stage_label,
                "auth_url": auth_url,
                "continue_url": continue_url,
            }
        )
        result.workspace_id = "acct-browser"
        return True

    engine._try_capture_with_root_browser_client = fake_browser_capture

    result = RegistrationResult(success=False, email="tester@example.com", account_id="acct-browser")

    assert engine._backfill_oauth_tokens_from_authenticated_session(result, source_label="test") is False
    assert browser_calls == [
        {
            "stage_label": "test",
            "auth_url": (
                "https://auth.example.test/oauth/authorize?"
                "client_id=app-client&prompt=none"
            ),
            "continue_url": "",
        }
    ]
    assert result.workspace_id == "acct-browser"


def test_try_complete_from_otp_callback_reuses_chatgpt_callback_then_backfills_local_oauth(monkeypatch):
    engine = RegistrationEngine(FakeEmailService(["123456"]))

    monkeypatch.setattr(
        "src.core.register.get_settings",
        lambda: SimpleNamespace(
            openai_redirect_uri="http://localhost:1455/auth/callback",
            registration_token_exchange_max_retries=3,
        ),
    )

    engine._last_validate_otp_continue_url = "https://chatgpt.com/api/auth/callback/openai?code=chat-code&state=chat-state"
    engine._consume_oauth_callback_for_session = (
        lambda result, callback_url, stage_label, referer=None: (
            setattr(result, "access_token", "access-1"),
            setattr(result, "session_token", "session-1"),
            True,
        )[-1]
    )
    engine._handle_oauth_callback = lambda callback_url: (_ for _ in ()).throw(
        AssertionError("chatgpt callback should not be exchanged directly")
    )
    engine._warmup_chatgpt_session = lambda: None
    engine._capture_auth_session_tokens = lambda result, access_hint=None, referer=None: True
    engine._backfill_oauth_tokens_from_authenticated_session = (
        lambda result, source_label: (
            setattr(result, "refresh_token", "refresh-1"),
            True,
        )[-1]
    )
    engine._backfill_identity_from_current_session = (
        lambda result, source_label: (
            setattr(result, "account_id", "acct-1"),
            setattr(result, "workspace_id", "ws-1"),
            True,
        )[-1]
    )

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._try_complete_from_otp_callback(result, stage_label="登录 OTP") is True
    assert result.account_id == "acct-1"
    assert result.workspace_id == "ws-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"
    assert result.session_token == "session-1"


def test_validate_verification_code_browser_first_short_circuits_http():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.registration_browser_first_enabled = True
    engine.session = QueueSession([])
    engine._try_validate_verification_code_with_browser = lambda code: True

    assert engine._validate_verification_code("123456") is True
    assert engine._last_otp_validation_outcome == "success"
    assert engine.session.calls == []


def test_validate_verification_code_browser_first_can_fall_back_to_http():
    session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(
                payload={
                    "page": {"type": "add_phone"},
                    "continue_url": "https://auth.openai.com/add-phone?state=otp-1",
                }
            ),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.registration_browser_first_enabled = True
    engine.session = session
    engine._try_validate_verification_code_with_browser = lambda code: None
    engine._refresh_auth_cookie_workspace_diagnostics = lambda source_label="": None

    assert engine._validate_verification_code("123456") is True
    assert len(session.calls) == 1
    assert engine._last_validate_otp_page_type == "add_phone"
    assert engine._last_validate_otp_continue_url == "https://auth.openai.com/add-phone?state=otp-1"


def test_browser_otp_validation_keeps_auth_proxy_and_prefers_live_auth_callback():
    engine = RegistrationEngine(FakeEmailService(["123456"]), proxy_url="http://user:pass@proxy.example:8080")
    engine.session = SimpleNamespace(cookies={})
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.openai.com/oauth/authorize?client_id=app-1&prompt=login",
        state="state-1",
        code_verifier="verifier-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    engine._refresh_auth_cookie_workspace_diagnostics = lambda source_label="": None

    class FakeBrowserClient:
        def __init__(self):
            self.calls = []

        def submit_openai_otp(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "submitted": True,
                "final_url": "https://auth.openai.com/add-phone?state=otp-state",
                "auth_final_url": "http://localhost:1455/auth/callback?code=code-1&state=state-1",
                "consent_final_url": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "page_title": "OpenAI",
                "page_html": "<html></html>",
                "consent_html": "<html></html>",
                "session_payload": {
                    "accessToken": "access-1",
                    "sessionToken": "session-1",
                    "account": {"id": "acct-1", "structure": "personal"},
                },
                "backend_me_payload": None,
                "cookies_text": "__Secure-next-auth.session-token=session-1",
            }

        def close(self):
            return None

    fake_client = FakeBrowserClient()
    engine._create_root_browser_client = lambda proxy_url=None, purpose="otp": fake_client

    assert engine._try_validate_verification_code_with_browser("123456") is True
    assert fake_client.calls[0]["auth_url"] == "https://auth.openai.com/oauth/authorize?client_id=app-1"
    assert engine._last_validate_otp_continue_url == "http://localhost:1455/auth/callback?code=code-1&state=state-1"
    assert engine._last_validate_otp_workspace_id == "acct-1"


def test_browser_otp_validation_prefers_observed_callback_when_auth_final_missing():
    engine = RegistrationEngine(FakeEmailService(["123456"]), proxy_url="http://user:pass@proxy.example:8080")
    engine.session = SimpleNamespace(cookies={})
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.openai.com/oauth/authorize?client_id=app-1&prompt=login",
        state="state-1",
        code_verifier="verifier-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    engine._refresh_auth_cookie_workspace_diagnostics = lambda source_label="": None

    class FakeBrowserClient:
        def submit_openai_otp(self, **kwargs):
            return {
                "submitted": True,
                "final_url": "https://auth.openai.com/add-phone?state=otp-state",
                "auth_final_url": "",
                "consent_final_url": "",
                "observed_callback_urls": [
                    "http://localhost:1455/auth/callback?code=code-observed&state=state-1"
                ],
                "page_title": "OpenAI",
                "page_html": "<html></html>",
                "consent_html": "",
                "session_payload": {
                    "accessToken": "access-1",
                    "sessionToken": "session-1",
                    "account": {"id": "acct-1", "structure": "personal"},
                },
                "backend_me_payload": None,
                "cookies_text": "__Secure-next-auth.session-token=session-1",
            }

        def close(self):
            return None

    engine._create_root_browser_client = lambda proxy_url=None, purpose="otp": FakeBrowserClient()

    assert engine._try_validate_verification_code_with_browser("123456") is True
    assert engine._last_validate_otp_continue_url == "http://localhost:1455/auth/callback?code=code-observed&state=state-1"
    assert engine._last_validate_otp_workspace_id == "acct-1"


def test_browser_otp_validation_does_not_merge_cookies_when_not_submitted():
    engine = RegistrationEngine(FakeEmailService(["123456"]), proxy_url="http://user:pass@proxy.example:8080")
    engine.session = SimpleNamespace(cookies={})
    engine._refresh_auth_cookie_workspace_diagnostics = lambda source_label="": None

    class FakeBrowserClient:
        def submit_openai_otp(self, **kwargs):
            return {
                "submitted": False,
                "final_url": "https://auth.openai.com/email-verification",
                "auth_final_url": "",
                "consent_final_url": "",
                "page_title": "OpenAI",
                "page_html": "<html></html>",
                "consent_html": "",
                "session_payload": None,
                "backend_me_payload": None,
                "cookies_text": "__Secure-next-auth.session-token=session-browser",
            }

        def close(self):
            return None

    engine._create_root_browser_client = lambda proxy_url=None, purpose="otp": FakeBrowserClient()

    assert engine._try_validate_verification_code_with_browser("123456") is None
    assert engine.session.cookies == {}


def test_try_capture_with_root_browser_client_uses_observed_local_callback():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.session = SimpleNamespace(cookies={})
    engine._dump_session_cookies = lambda: "oai-did=device-1"
    engine._handle_oauth_callback = lambda callback_url: {
        "account_id": "acct-observed",
        "access_token": "access-observed",
        "refresh_token": "refresh-observed",
        "id_token": "id-observed",
    }

    class FakeBrowserClient:
        def capture_openai_state(self, **kwargs):
            return {
                "success": True,
                "auth_final_url": "https://auth.openai.com/log-in",
                "consent_html": "",
                "session_payload": None,
                "backend_me_payload": None,
                "observed_callback_urls": [
                    "http://localhost:1455/auth/callback?code=code-observed&state=state-1"
                ],
            }

        def close(self):
            return None

    engine._create_root_browser_client = lambda proxy_url=None, purpose="capture": FakeBrowserClient()

    result = RegistrationResult(success=False)

    assert engine._try_capture_with_root_browser_client(
        result,
        stage_label="observed",
        auth_url="https://auth.openai.com/oauth/authorize?client_id=app-1",
        continue_url="https://chatgpt.com/api/auth/callback/openai?code=chat-code",
    ) is True
    assert result.account_id == "acct-observed"
    assert result.access_token == "access-observed"
    assert result.refresh_token == "refresh-observed"
    assert result.id_token == "id-observed"


def test_created_account_direct_session_uses_chatgpt_callback_then_backfills_tokens(monkeypatch):
    engine = RegistrationEngine(FakeEmailService(["123456"]))

    monkeypatch.setattr(
        "src.core.register.get_settings",
        lambda: SimpleNamespace(
            openai_redirect_uri="http://localhost:1455/auth/callback",
            registration_token_exchange_max_retries=3,
        ),
    )

    engine._create_account_completed = True
    engine._create_account_continue_url = "https://chatgpt.com/api/auth/callback/openai?code=chat-code&state=chat-state"
    engine.password = "openai-pass"
    engine.device_id = "did-1"
    engine.session = SimpleNamespace(cookies={})
    engine._consume_oauth_callback_for_session = (
        lambda result, callback_url, stage_label, referer=None: (
            setattr(result, "access_token", "access-1"),
            setattr(result, "session_token", "session-1"),
            True,
        )[-1]
    )
    engine._handle_oauth_callback = lambda callback_url: (_ for _ in ()).throw(
        AssertionError("chatgpt callback should not be exchanged directly")
    )
    engine._warmup_chatgpt_session = lambda: None
    engine._capture_auth_session_tokens = lambda result, access_hint=None, referer=None: True
    engine._backfill_oauth_tokens_from_authenticated_session = (
        lambda result, source_label: (
            setattr(result, "refresh_token", "refresh-1"),
            True,
        )[-1]
    )
    engine._backfill_identity_from_current_session = (
        lambda result, source_label: (
            setattr(result, "account_id", "acct-1"),
            setattr(result, "workspace_id", "ws-1"),
            True,
        )[-1]
    )

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._try_complete_created_account_direct_session(result, flow_label="Outlook") is True
    assert result.account_id == "acct-1"
    assert result.workspace_id == "ws-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"
    assert result.session_token == "session-1"
    assert result.password == "openai-pass"
    assert result.device_id == "did-1"


def test_created_account_direct_session_prefers_browser_first():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.registration_browser_first_enabled = True
    engine._create_account_completed = True
    engine._create_account_continue_url = "https://auth.openai.com/about-you?state=created-1"
    engine.password = "openai-pass"
    engine.device_id = "did-1"
    engine.session = SimpleNamespace(cookies={})
    engine._try_capture_with_root_browser_client = lambda result, *, stage_label, auth_url=None, continue_url=None: (
        setattr(result, "access_token", "access-1"),
        setattr(result, "session_token", "session-1"),
        setattr(result, "workspace_id", "ws-1"),
        True,
    )[-1]
    engine._backfill_identity_from_current_session = lambda result, source_label: (
        setattr(result, "account_id", "acct-1"),
        True,
    )[-1]
    engine._backfill_oauth_tokens_from_authenticated_session = lambda result, source_label: (
        setattr(result, "refresh_token", "refresh-1"),
        True,
    )[-1]
    engine._follow_redirects = lambda url: (_ for _ in ()).throw(
        AssertionError("browser-first direct session should not follow redirects before browser capture")
    )

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._try_complete_created_account_direct_session(result, flow_label="Outlook") is True
    assert result.account_id == "acct-1"
    assert result.workspace_id == "ws-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"
    assert result.session_token == "session-1"


def test_created_account_direct_session_prefers_direct_auth_continue_before_browser():
    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.registration_browser_first_enabled = True
    engine._create_account_completed = True
    engine._create_account_continue_url = "https://auth.openai.com/api/oauth/oauth2/auth?client_id=app-1"
    engine.password = "openai-pass"
    engine.device_id = "did-1"
    engine.session = SimpleNamespace(cookies={})
    engine._backfill_identity_from_current_session = lambda result, source_label: True

    browser_calls = []
    engine._try_capture_with_root_browser_client = lambda result, *, stage_label, auth_url=None, continue_url=None: (
        browser_calls.append({"stage_label": stage_label, "continue_url": continue_url}),
        True,
    )[-1]

    followed = {}
    engine._follow_redirects = lambda url: (
        "http://localhost:1455/auth/callback?code=code-1&state=state-1",
        followed.setdefault("url", url),
    )
    engine._process_oauth_callback_result = lambda result, callback_url, stage_label, referer=None: (
        setattr(result, "account_id", "acct-1"),
        setattr(result, "access_token", "access-1"),
        setattr(result, "refresh_token", "refresh-1"),
        setattr(result, "session_token", "session-1"),
        True,
    )[-1]

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._try_complete_created_account_direct_session(result, flow_label="Outlook") is True
    assert followed["url"] == "https://auth.openai.com/api/oauth/oauth2/auth?client_id=app-1"
    assert browser_calls == []
    assert result.account_id == "acct-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"


def test_outlook_token_exchange_prefers_create_account_callback_over_otp_gate():
    engine = RegistrationEngine(FakeEmailService(["654321"]))
    engine.password = "openai-pass"
    engine._create_account_refresh_token = "refresh-created"
    engine._create_account_continue_url = "http://localhost:1455/auth/callback?code=code-1&state=state-1"
    engine._last_validate_otp_continue_url = "https://auth.openai.com/add-phone?state=otp-state"
    engine._verify_email_otp_with_retry = lambda **kwargs: True
    engine._try_complete_from_otp_callback = lambda result, stage_label: False
    engine._get_workspace_id = lambda: ""
    engine.session = SimpleNamespace(cookies={"__Secure-next-auth.session-token": "session-1"})
    followed = {}
    engine._follow_redirects = lambda url: (followed.setdefault("url", url), url)
    engine._consume_oauth_callback_for_session = (
        lambda result, callback_url, stage_label, referer=None: (
            setattr(result, "access_token", "access-1"),
            setattr(result, "session_token", "session-1"),
            True,
        )[-1]
    )
    engine._handle_oauth_callback = lambda callback_url: {
        "account_id": "acct-1",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "id_token": "id-1",
    }

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._complete_token_exchange_outlook(result) is True
    assert followed["url"] == "http://localhost:1455/auth/callback?code=code-1&state=state-1"
    assert result.account_id == "acct-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"


def test_outlook_token_exchange_prefers_direct_workspace_continue_before_browser():
    engine = RegistrationEngine(FakeEmailService(["654321"]))
    engine.registration_browser_first_enabled = True
    engine.password = "openai-pass"
    engine.device_id = "did-1"
    engine._create_account_account_id = "acct-created"
    engine._verify_email_otp_with_retry = lambda **kwargs: True
    engine._try_complete_from_otp_callback = lambda result, stage_label: False
    engine._last_validate_otp_workspace_id = "ws-1"
    engine._last_validate_otp_continue_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    engine.session = SimpleNamespace(cookies={"__Secure-next-auth.session-token": "session-1"})

    browser_calls = []
    engine._try_capture_with_root_browser_client = lambda result, *, stage_label, auth_url=None, continue_url=None: (
        browser_calls.append({"stage_label": stage_label, "continue_url": continue_url}),
        True,
    )[-1]

    engine._select_workspace = lambda workspace_id: "https://auth.openai.com/api/oauth/oauth2/auth?client_id=app-1"
    followed = {}
    engine._follow_redirects = lambda url: (
        "http://localhost:1455/auth/callback?code=code-1&state=state-1",
        followed.setdefault("url", url),
    )
    engine._process_oauth_callback_result = lambda result, callback_url, stage_label, referer=None: (
        setattr(result, "account_id", "acct-1"),
        setattr(result, "access_token", "access-1"),
        setattr(result, "refresh_token", "refresh-1"),
        setattr(result, "session_token", "session-1"),
        True,
    )[-1]
    engine._backfill_identity_from_current_session = lambda result, source_label: True

    result = RegistrationResult(success=False, email="tester@example.com")

    assert engine._complete_token_exchange_outlook(result) is True
    assert followed["url"] == "https://auth.openai.com/api/oauth/oauth2/auth?client_id=app-1"
    assert browser_calls == []
    assert result.account_id == "acct-1"
    assert result.access_token == "access-1"
    assert result.refresh_token == "refresh-1"


def test_restart_login_flow_accepts_direct_email_otp_page():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": "email-verification"}}),
        ),
    ])

    engine = RegistrationEngine(FakeEmailService(["123456"]))
    engine.email = "tester@example.com"
    engine.password = "openai-pass"
    engine.http_client = FakeOpenAIClient([session_one], ["sentinel-1"])
    engine.oauth_manager = FakeOAuthManager()

    ok, error = engine._restart_login_flow()

    assert ok is True
    assert error == ""


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
