from apps.worker.main import settings
from packages.platform.connections import Connections


def main():
    connections = Connections(settings)
    try:
        return (
            0
            if connections.ready()
            and connections.redis.get("defectguard:scheduler:heartbeat") == b"ok"
            else 1
        )
    except Exception:
        return 1
    finally:
        connections.close()


if __name__ == "__main__":
    raise SystemExit(main())
