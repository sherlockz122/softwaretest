import logging
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.application import create_app
from packages.platform.config import ConfigurationError, load_settings


@pytest.fixture
def settings():
    return load_settings(
        _env_file=None,
        mysql_password="local-test-value-123456",
        redis_password="local-test-value-654321",
        signing_key="s" * 48,
    )


class FakeConnections:
    available = True
    closed = False

    def __init__(self, settings):
        pass

    def ready(self):
        return self.available

    def close(self):
        self.closed = True


@pytest.mark.parametrize("field", ["mysql_password", "redis_password", "signing_key"])
def test_weak_secrets_rejected_without_values(settings, field):
    values = settings.model_dump()
    values[field] = "private-test-secret"
    if field != "signing_key":
        values[field] = "short"
    with pytest.raises(ConfigurationError) as error:
        load_settings(_env_file=None, **values)
    assert field in str(error.value)
    assert values[field] not in str(error.value)


def test_missing_config_fails_without_env(monkeypatch):
    for name in ("DG_MYSQL_PASSWORD", "DG_REDIS_PASSWORD", "DG_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError) as error:
        load_settings(_env_file=None)
    assert "signing_key" in str(error.value)


def test_passwords_encoded_and_repr_hidden(settings):
    # Test URL encoding without relying on repr to conceal broker URLs.
    settings = load_settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            "redis_password": "special:/@#password",
        },
    )
    assert "special:/@#password" not in settings.broker_url
    assert "%3A%2F%40%23" in settings.broker_url
    assert "special:/@#password" not in repr(settings)


def test_health_errors_and_request_context(settings, caplog):
    connection = FakeConnections(settings)
    app = create_app(settings, lambda config: connection)

    @app.get("/test/fail")
    def fail():
        raise RuntimeError("do-not-leak /local/path SQL secret")

    @app.get("/test/validate")
    def validate(count: int):
        return count

    with TestClient(app, raise_server_exceptions=False) as client:
        request_id = str(uuid4())
        response = client.get("/api/v1/health", headers={"X-Request-ID": request_id})
        assert response.json() == {"status": "ok"}
        assert response.headers["X-Request-ID"] == request_id
        assert client.get("/api/v1/health/ready").status_code == 200
        connection.available = False
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "SYSTEM_DEPENDENCY_UNAVAILABLE"
        for path, status in [
            ("/missing", 404),
            ("/test/validate?count=secret", 422),
            ("/test/fail", 500),
        ]:
            with caplog.at_level(logging.ERROR):
                response = client.get(path, headers={"X-Request-ID": "invalid-header"})
            assert response.status_code == status
            body = response.json()
            assert str(UUID(body["request_id"])) == response.headers["X-Request-ID"]
            assert "secret" not in response.text and "/local/path" not in response.text
        assert "do-not-leak" not in caplog.text
    assert connection.closed


def test_production_schema_hidden_and_cors(settings):
    settings.environment = "production"
    with TestClient(create_app(settings, FakeConnections)) as client:
        assert client.get("/api/openapi.json").status_code == 404
        allowed = client.get("/api/v1/health", headers={"Origin": settings.frontend_origin})
        assert allowed.headers["access-control-allow-origin"] == settings.frontend_origin
        denied = client.get("/api/v1/health", headers={"Origin": "http://untrusted.example"})
        assert "access-control-allow-origin" not in denied.headers
