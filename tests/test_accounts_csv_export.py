from types import SimpleNamespace

from src.web.routes.accounts import _resolve_account_outlook_export_payload


def test_resolve_account_outlook_export_payload_prefers_account_stored_recovery():
    account = SimpleNamespace(
        email="tester@outlook.com",
        email_service="outlook",
        email_service_id="service-1",
        extra_data={
            "outlook_recovery": {
                "email": "tester@outlook.com",
                "password": "stored-mail-pwd",
                "client_id": "stored-client",
                "refresh_token": "stored-refresh",
            }
        },
    )
    lookup = {
        "service-1": {
            "email": "tester@outlook.com",
            "password": "lookup-mail-pwd",
            "client_id": "lookup-client",
            "refresh_token": "lookup-refresh",
        }
    }

    payload = _resolve_account_outlook_export_payload(account, lookup)

    assert payload == {
        "email": "tester@outlook.com",
        "password": "stored-mail-pwd",
        "client_id": "stored-client",
        "refresh_token": "stored-refresh",
    }


def test_resolve_account_outlook_export_payload_falls_back_to_lookup_when_account_has_no_backup():
    account = SimpleNamespace(
        email="tester@outlook.com",
        email_service="outlook",
        email_service_id="service-1",
        extra_data={},
    )
    lookup = {
        "service-1": {
            "email": "tester@outlook.com",
            "password": "lookup-mail-pwd",
            "client_id": "lookup-client",
            "refresh_token": "lookup-refresh",
        }
    }

    payload = _resolve_account_outlook_export_payload(account, lookup)

    assert payload == {
        "email": "tester@outlook.com",
        "password": "lookup-mail-pwd",
        "client_id": "lookup-client",
        "refresh_token": "lookup-refresh",
    }
