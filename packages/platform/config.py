"""Shared configuration. Validation messages never include secret values."""

from urllib.parse import quote

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class ConfigurationError(RuntimeError):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DG_", env_file=".env", extra="ignore")

    environment: str = "development"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=13306, ge=1, le=65535)
    mysql_database: str = "defectguard"
    mysql_user: str = "defectguard"
    mysql_password: SecretStr
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(default=16379, ge=1, le=65535)
    redis_password: SecretStr
    signing_key: SecretStr
    frontend_origin: str = "http://127.0.0.1:5173"
    access_seconds: int = Field(default=900, ge=60, le=900)
    session_seconds: int = Field(default=604800, ge=900, le=604800)
    login_rate_limit: int = Field(default=5, ge=1, le=1000)
    login_rate_prefix: str = "defectguard:auth:rate"

    @field_validator("environment")
    @classmethod
    def environment_known(cls, value: str) -> str:
        if value not in {"development", "test", "production"}:
            raise ValueError("unsupported environment")
        return value

    @field_validator("mysql_password", "redis_password", "signing_key")
    @classmethod
    def secret_required(cls, value: SecretStr, info) -> SecretStr:
        raw = value.get_secret_value()
        minimum = 32 if info.field_name == "signing_key" else 12
        if len(raw) < minimum or raw.lower() in {"change_me", "changeme", "password"}:
            raise ValueError("explicit strong secret required")
        return value

    @field_validator("frontend_origin")
    @classmethod
    def origin_allowed(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or "*" in value
        ):
            raise ValueError("explicit frontend origin required")
        return value.rstrip("/")

    @property
    def database_url(self) -> URL:
        return URL.create(
            "mysql+pymysql",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
            query={"charset": "utf8mb4"},
        )

    @property
    def broker_url(self) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/0"


def load_settings(**overrides) -> Settings:
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise ConfigurationError("SYSTEM_CONFIGURATION_INVALID: " + ", ".join(fields)) from None
