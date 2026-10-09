import socket

from apps.worker.main import app, settings
from packages.platform.connections import Connections


def main() -> int:
    connections = Connections(settings)
    try:
        if not connections.ready():
            return 1
        destination = f"defectguard@{socket.gethostname()}"
        replies = app.control.inspect(destination=[destination], timeout=3).ping()
        return 0 if replies and replies.get(destination, {}).get("ok") == "pong" else 1
    except Exception:
        return 1
    finally:
        connections.close()


if __name__ == "__main__":
    raise SystemExit(main())
