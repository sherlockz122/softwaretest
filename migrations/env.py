from alembic import context
from sqlalchemy import create_engine

from packages.persistence.models import Base
from packages.platform.config import load_settings

config = context.config
target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    # Autogeneration must not propose deleting unrelated/pre-existing tables.
    return name in target_metadata.tables if type_ == "table" else True


if context.is_offline_mode():
    raise RuntimeError("Offline migration is not enabled; use a dedicated MySQL database")

connection = config.attributes.get("connection")
if connection is not None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(load_settings().database_url)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_name=include_name,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
