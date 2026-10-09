import re
import subprocess
from contextlib import contextmanager
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from packages.platform.config import load_settings


def root_sql(sql):
    import os

    docker = os.environ.get("DG_DOCKER_EXE", "docker")
    result = subprocess.run(
        [
            docker,
            "compose",
            "exec",
            "-T",
            "mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -e "$1"',
            "--",
            sql,
        ],
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Dedicated test database setup failed; inspect MySQL container locally")


def migrate(engine, direction="upgrade", target="head"):
    config = Config("alembic.ini")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        if direction == "check":
            command.check(config)
        else:
            getattr(command, direction)(config, target)


@contextmanager
def scratch_database():
    settings = load_settings()
    if settings.mysql_host != "127.0.0.1" or settings.environment not in {"development", "test"}:
        raise RuntimeError("Auth acceptance requires local isolated development services")
    if not re.fullmatch(r"[A-Za-z0-9_]+", settings.mysql_user):
        raise RuntimeError("Unsafe database username for acceptance setup")
    name = "dg_stage3_test_" + uuid4().hex
    root_sql(
        f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4; "
        f"GRANT ALL PRIVILEGES ON `{name}`.* TO '{settings.mysql_user}'@'%';"
    )
    engine = None
    try:
        test_settings = settings.model_copy(
            update={
                "mysql_database": name,
                "environment": "test",
                "login_rate_limit": 1000,
                "login_rate_prefix": "defectguard:auth:test:" + uuid4().hex,
            }
        )
        engine = create_engine(test_settings.database_url, pool_pre_ping=True)
        yield test_settings, engine
    finally:
        if engine:
            engine.dispose()
        if not re.fullmatch(r"dg_stage3_test_[0-9a-f]{32}", name):
            raise RuntimeError("Test database cleanup guard failed")
        root_sql(
            f"REVOKE ALL PRIVILEGES ON `{name}`.* FROM '{settings.mysql_user}'@'%'; "
            f"DROP DATABASE `{name}`;"
        )
