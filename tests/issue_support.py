"""Real TLS API fixture, using production pinning and certificate validation."""

import json
import socket
import ssl
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from packages.mining.issues import GitHubIssues
from packages.repositories.safety import URLPolicy
from tests.repository_support import PinnedTestSocket


@contextmanager
def https_issues(root):
    root.mkdir()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.github.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.github.com")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = root / "cert.pem", root / "fixture-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    state = {
        "status": 200,
        "body": {"number": 18, "labels": [{"name": "Bug"}]},
        "content_type": "application/json",
        "requests": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append((self.path, dict(self.headers)))
            self.send_response(state["status"])
            self.send_header("Content-Type", state["content_type"])
            self.send_header("Location", "https://127.0.0.1/private")
            body = (
                state["body"]
                if isinstance(state["body"], bytes)
                else json.dumps(state["body"]).encode()
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def connector(address):
        return PinnedTestSocket(socket.create_connection(server.server_address, timeout=3), address)

    adapter = GitHubIssues(
        URLPolicy(lambda host: ["93.184.216.34"]),
        connector,
        lambda: ssl.create_default_context(cafile=certfile),
    )
    try:
        yield adapter, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
