from redis import Redis
from sqlalchemy import create_engine, text

from packages.persistence.models import SCHEMA_HEAD
from packages.platform.config import Settings


class Connections:
    def __init__(self, settings: Settings):
        self.engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=0,
            connect_args={"connect_timeout": 2, "read_timeout": 2, "write_timeout": 2},
        )
        self.redis = Redis.from_url(
            settings.broker_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )

    def ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                if connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars().all() != [SCHEMA_HEAD]:
                    return False
            return bool(self.redis.ping())
        except Exception:
            # Do not expose driver exceptions (URLs, SQL, hostnames, passwords).
            return False

    def require_schema(self) -> None:
        from packages.platform.config import ConfigurationError

        try:
            with self.engine.connect() as connection:
                current = (
                    connection.execute(text("SELECT version_num FROM alembic_version"))
                    .scalars()
                    .all()
                )
        except Exception:
            raise ConfigurationError(
                "SYSTEM_MIGRATION_REQUIRED: check database and run alembic upgrade head"
            ) from None
        if current != [SCHEMA_HEAD]:
            raise ConfigurationError("SYSTEM_MIGRATION_REQUIRED: incompatible schema revision")

    def close(self) -> None:
        self.redis.close()
        self.engine.dispose()
