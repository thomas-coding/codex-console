import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from src.web.routes import settings as settings_routes


class DummyResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class DummyRequestClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_proxy_connectivity_falls_back_to_next_target():
    client = DummyRequestClient([
        TimeoutError("dns timeout"),
        DummyResponse(200, {"ip": "1.2.3.4"}),
    ])

    result = settings_routes._test_proxy_connectivity(
        "http://proxy.local:8080",
        request_client=client,
        timeout=5,
    )

    assert result["success"] is True
    assert result["ip"] == "1.2.3.4"
    assert result["target"] == "ipify64"
    assert len(client.calls) == 2
    assert client.calls[0][1]["timeout"] == 5


def test_proxy_connectivity_returns_combined_failure_details():
    client = DummyRequestClient([
        TimeoutError("dns timeout"),
        DummyResponse(502, {}),
        RuntimeError("connection reset"),
    ])

    result = settings_routes._test_proxy_connectivity(
        "http://proxy.local:8080",
        request_client=client,
        timeout=5,
    )

    assert result["success"] is False
    assert "ipify" in result["message"]
    assert "ipify64: HTTP 502" in result["message"]
    assert "httpbin" in result["message"]


def test_test_proxy_item_route_uses_connectivity_helper(monkeypatch):
    @contextmanager
    def fake_get_db():
        yield object()

    monkeypatch.setattr(settings_routes, "get_db", fake_get_db)
    monkeypatch.setattr(
        settings_routes.crud,
        "get_proxy_by_id",
        lambda db, proxy_id: SimpleNamespace(proxy_url="http://proxy.local:8080"),
    )
    monkeypatch.setattr(
        settings_routes,
        "_test_proxy_connectivity",
        lambda proxy_url: {
            "success": True,
            "ip": "8.8.8.8",
            "response_time": 1234,
            "target": "httpbin",
            "message": "ok",
        },
    )

    result = asyncio.run(settings_routes.test_proxy_item(1))

    assert result["success"] is True
    assert result["target"] == "httpbin"


def test_test_all_proxies_aggregates_helper_results(monkeypatch):
    @contextmanager
    def fake_get_db():
        yield object()

    proxies = [
        SimpleNamespace(id=1, name="A", proxy_url="http://proxy-a"),
        SimpleNamespace(id=2, name="B", proxy_url="http://proxy-b"),
    ]

    monkeypatch.setattr(settings_routes, "get_db", fake_get_db)
    monkeypatch.setattr(settings_routes.crud, "get_enabled_proxies", lambda db: proxies)

    def fake_probe(proxy_url):
        if proxy_url.endswith("proxy-a"):
            return {"success": True, "ip": "1.1.1.1", "response_time": 100, "message": "ok"}
        return {"success": False, "message": "timeout"}

    monkeypatch.setattr(settings_routes, "_test_proxy_connectivity", fake_probe)

    result = asyncio.run(settings_routes.test_all_proxies())

    assert result["total"] == 2
    assert result["success"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["name"] == "A"
    assert result["results"][1]["message"] == "timeout"
