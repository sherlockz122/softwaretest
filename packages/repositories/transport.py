"""A local CONNECT tunnel pins every upstream TCP connection to approved public IPs.

Only the original HTTPS authority is accepted. Git retains end-to-end TLS validation.
The tunnel also prevents alternate Git object URLs from reaching another host or HTTP.
"""

import http.client
import select
import socket
import socketserver
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from packages.repositories.safety import RepositoryError


def connect_ip(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    connection = socket.socket(family, socket.SOCK_STREAM)
    connection.settimeout(3)
    try:
        connection.connect((address, 443))
        return connection
    except OSError:
        connection.close()
        raise


class Tunnel:
    def __init__(self, target, policy, byte_limit, connector=connect_ip):
        self.target, self.policy, self.byte_limit = target, policy, byte_limit
        self.connector = connector
        self.error = None
        self.bytes = 0
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        self.closed = threading.Event()
        self.sockets = set()

    def upstream(self):
        # Resolve again for every CONNECT and pin a numeric address, never a hostname.
        addresses = self.policy.addresses(self.target)
        deadline = time.monotonic() + 3
        for address in addresses:
            if self.closed.is_set() or time.monotonic() >= deadline:
                break
            try:
                connection = self.connector(address)
            except OSError:
                continue
            if self.closed.is_set():
                connection.close()
                break
            if connection.getpeername()[0] != address:
                connection.close()
                raise RepositoryError(422, "REPOSITORY_UNSAFE_ADDRESS")
            return connection
        raise RepositoryError(503, "REPOSITORY_NETWORK_UNAVAILABLE")

    def account(self, count):
        with self.lock:
            self.bytes += count
            if self.bytes > self.byte_limit:
                raise RepositoryError(422, "REPOSITORY_TRANSFER_LIMIT")

    def __enter__(self):
        tunnel = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                if self.path != tunnel.target.host + ":443" or not tunnel.slots.acquire(False):
                    self.send_error(403)
                    return
                upstream = None
                try:
                    upstream = tunnel.upstream()
                    with tunnel.lock:
                        tunnel.sockets.update((self.connection, upstream))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.flush()
                    sockets = (self.connection, upstream)
                    last_activity = time.monotonic()
                    while not tunnel.closed.is_set() and time.monotonic() - last_activity < 30:
                        readable, _, _ = select.select(sockets, [], [], 0.5)
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            tunnel.account(len(data))
                            destination = upstream if source is self.connection else self.connection
                            destination.sendall(data)
                            last_activity = time.monotonic()
                except RepositoryError as error:
                    tunnel.error = error.code
                    if upstream is None:
                        self.send_error(403)
                except (OSError, ValueError):
                    pass
                finally:
                    with tunnel.lock:
                        tunnel.sockets.discard(self.connection)
                        if upstream is not None:
                            tunnel.sockets.discard(upstream)
                    if upstream is not None:
                        upstream.close()
                    tunnel.slots.release()
                    self.close_connection = True

            def do_GET(self):
                self.send_error(403)

            do_POST = do_GET

        class Server(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, *args):
                pass

            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(3)
                return connection, address

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        return self

    def __exit__(self, *args):
        self.closed.set()
        with self.lock:
            for connection in self.sockets:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RepositoryProbe:
    def __init__(self, policy, connector=connect_ip, context_factory=ssl.create_default_context):
        self.policy, self.connector, self.context_factory = policy, connector, context_factory

    def check(self, target):
        self.policy.addresses(target)
        with Tunnel(target, self.policy, 1024**2, self.connector) as tunnel:
            connection = http.client.HTTPSConnection(
                "127.0.0.1", tunnel.port, timeout=3, context=self.context_factory()
            )
            connection.set_tunnel(target.host, 443)
            try:
                connection.request("GET", target.path + "/info/refs?service=git-upload-pack")
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    raise RepositoryError(422, "REPOSITORY_REDIRECT_REJECTED")
                if response.status != 200:
                    raise RepositoryError(422, "REPOSITORY_NOT_PUBLIC_GIT")
                if (
                    response.getheader("Content-Type", "").split(";")[0]
                    != "application/x-git-upload-pack-advertisement"
                    or response.read(34) != b"001e# service=git-upload-pack\n0000"
                ):
                    raise RepositoryError(422, "REPOSITORY_NOT_PUBLIC_GIT")
            except (OSError, http.client.HTTPException):
                raise RepositoryError(
                    503, tunnel.error or "REPOSITORY_NETWORK_UNAVAILABLE"
                ) from None
            finally:
                connection.close()
