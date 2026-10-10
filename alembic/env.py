"""Alembic 环境：连接与目标元数据来自项目配置与领域模型。"""

from sqlalchemy import engine_from_config, pool

from alembic import context
from packages.domain.models import Base
from packages.platform.config import load_settings

config = context.config
settings = load_settings()
# URL 含密码，禁止出现在日志；注入属性而非覆盖 sqlalchemy.url。
config.attributes["connection"] = None
target_metadata = Base.metadata


def _url_escaped():
    return settings.database_url.render_as_string(hide_password=True)


def run_migrations_offline() -> None:
    context.configure(
        url=_url_escaped(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        {"sqlalchemy.url": settings.database_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
