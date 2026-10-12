"""Local read-only feasibility audit. Never creates or repairs a dataset/source."""

import argparse
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from packages.mining.dataset import DatasetError
from packages.mining.dataset_source import DatasetSource
from packages.platform.config import load_settings
from packages.platform.connections import Connections
from packages.repositories.safety import RepositoryError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-id", type=UUID, required=True)
    parser.add_argument("--feature-run-id", type=UUID, required=True)
    parser.add_argument("--szz-run-id", type=UUID, action="append", required=True)
    parser.add_argument("--window-days", type=int, default=90)
    parser.add_argument("--snapshot-at", default=None)
    args = parser.parse_args()
    settings = load_settings()
    connections = Connections(settings)
    try:
        date = (
            datetime.fromisoformat(args.snapshot_at.replace("Z", "+00:00"))
            if args.snapshot_at
            else None
        )
        if date is not None and date.tzinfo is None:
            raise DatasetError("DATA_UTC_OFFSET_REQUIRED")
        snapshot = date.astimezone(UTC).replace(tzinfo=None) if date else None
        result = DatasetSource(settings, connections).audit(
            str(args.repository_id),
            str(args.feature_run_id),
            [str(x) for x in args.szz_run_id],
            snapshot,
            args.window_days,
        )
        # Public aggregate only: no private storage, identity, code or row payload.
        public = {key: value for key, value in result.items() if key not in ("labels", "split")}
        public["split_counts"] = {key: len(value) for key, value in result["split"].items()}
        print(json.dumps(public, indent=2, default=lambda d: d.isoformat() + "Z"))
        return 0 if result["ready"] else 2
    except DatasetError as error:
        print(json.dumps({"ready": False, "code": error.code}))
        return 3
    except RepositoryError as error:
        print(json.dumps({"ready": False, "code": error.code}))
        return 3
    except SQLAlchemyError:
        print(json.dumps({"ready": False, "code": "SYSTEM_DEPENDENCY_UNAVAILABLE"}))
        return 3
    except ValueError:
        print(json.dumps({"ready": False, "code": "DATA_INPUT_INVALID"}))
        return 3
    finally:
        connections.close()


if __name__ == "__main__":
    raise SystemExit(main())
