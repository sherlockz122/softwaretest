"""Controlled deadline fault on a task in the disposable browser acceptance DB."""

import argparse
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from packages.platform.config import load_settings
from packages.platform.connections import Connections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    config = Path(args.config).resolve()
    runtime = Path("runtime/fresh").resolve()
    if not config.is_relative_to(runtime):
        raise RuntimeError("Browser fault requires this run disposable config")
    task = str(UUID(args.task))
    settings = load_settings(_env_file=config)
    if settings.mysql_host != "127.0.0.1" or settings.environment != "development":
        raise RuntimeError("Browser fault requires local development")
    connections = Connections(settings)
    try:
        with connections.engine.begin() as db:
            result = db.execute(
                text(
                    "UPDATE async_task SET queued_deadline=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND "
                    "WHERE id=:id AND status='queued'"
                ),
                {"id": task},
            )
            if result.rowcount != 1:
                raise RuntimeError("Only the newly created queued acceptance task may be changed")
    finally:
        connections.close()
    print("PASS acceptance task deadline adjusted; no secrets recorded")


if __name__ == "__main__":
    main()
