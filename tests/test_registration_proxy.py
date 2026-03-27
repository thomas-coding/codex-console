from types import SimpleNamespace

from src.web.routes.registration import _prepare_registration_proxy


def test_prepare_registration_proxy_rewrites_iproyal_session_in_password(monkeypatch):
    monkeypatch.setattr(
        "src.web.routes.registration.uuid.uuid4",
        lambda: SimpleNamespace(hex="abcd1234efgh5678"),
    )

    proxy_url = (
        "socks5://user-1:"
        "secret_country-us_session-oldsess99_lifetime-30m@geo.iproyal.com:12321"
    )

    rewritten, session_id = _prepare_registration_proxy(proxy_url)

    assert session_id == "abcd1234"
    assert rewritten == (
        "socks5://user-1:"
        "secret_country-us_session-abcd1234_lifetime-30m@geo.iproyal.com:12321"
    )


def test_prepare_registration_proxy_rewrites_iproyal_session_in_username(monkeypatch):
    monkeypatch.setattr(
        "src.web.routes.registration.uuid.uuid4",
        lambda: SimpleNamespace(hex="11223344aabbccdd"),
    )

    proxy_url = (
        "http://customer_country-us_session-oldsess99_lifetime-30m:"
        "password@geo.iproyal.com:12321"
    )

    rewritten, session_id = _prepare_registration_proxy(proxy_url)

    assert session_id == "11223344"
    assert rewritten == (
        "http://customer_country-us_session-11223344_lifetime-30m:"
        "password@geo.iproyal.com:12321"
    )


def test_prepare_registration_proxy_keeps_non_iproyal_proxy_unchanged():
    proxy_url = "http://user:pass@127.0.0.1:7890"

    rewritten, session_id = _prepare_registration_proxy(proxy_url)

    assert rewritten == proxy_url
    assert session_id is None
