"""Deterministic TLS smart-HTTP Git fixture. No production safety bypass flag."""

import ipaddress
import os
import shutil
import socket
import ssl
import subprocess
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from packages.repositories.safety import URLPolicy
from packages.repositories.transport import RepositoryProbe


def git(*args, cwd=None, data=None, env=None):
    result = subprocess.run(
        [shutil.which("git"), *args],
        cwd=cwd,
        input=data,
        env=env,
        capture_output=True,
        timeout=20,
        check=True,
    )
    return result.stdout


class PinnedTestSocket:
    """Only fixture connector translates a test public IP to its local TLS server."""

    def __init__(self, connection, address):
        self.connection, self.address = connection, address

    def getpeername(self):
        return self.address, 443

    def __getattr__(self, name):
        return getattr(self.connection, name)


@contextmanager
def https_git(root, empty=False):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source"
    source.mkdir()
    git("init", "-b", "main", str(source))
    git("-C", str(source), "config", "user.email", "fixture@example.invalid")
    git("-C", str(source), "config", "user.name", "Fixture")
    if not empty:
        (source / "file.txt").write_text("deterministic source\n", encoding="utf-8")
        git("-C", str(source), "add", ".")
        git("-C", str(source), "commit", "-m", "fixture commit")
    project = root / "served" / "team"
    project.mkdir(parents=True)
    git("clone", "--bare", str(source), str(project / "demo.git"))
    head = None if empty else git("-C", str(source), "rev-parse", "HEAD").decode().strip()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "repo.example")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("repo.example")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = root / "cert.pem", root / "fixture-key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    state = {"redirect": False, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["requests"].append(self.path)
            if state["redirect"]:
                self.send_response(302)
                self.send_header("Location", "https://127.0.0.1/private.git")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024**2:
                self.send_error(413)
                return
            environment = os.environ.copy()
            environment.update(
                GIT_PROJECT_ROOT=str(root / "served"),
                GIT_HTTP_EXPORT_ALL="1",
                PATH_INFO=path,
                QUERY_STRING=query,
                REQUEST_METHOD=self.command,
                CONTENT_TYPE=self.headers.get("Content-Type", ""),
                CONTENT_LENGTH=str(length),
            )
            data = git("http-backend", data=self.rfile.read(length), env=environment)
            header, body = data.split(b"\r\n\r\n", 1)
            headers = [line.decode().split(":", 1) for line in header.split(b"\r\n")]
            status = next((int(v.strip().split()[0]) for k, v in headers if k == "Status"), 200)
            self.send_response(status)
            for k, v in headers:
                if k != "Status":
                    self.send_header(k, v.strip())
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def connector(address):
        assert ipaddress.ip_address(address).is_global
        connection = socket.create_connection(server.server_address, timeout=3)
        return PinnedTestSocket(connection, address)

    policy = URLPolicy(lambda host: ["93.184.216.34"])
    probe = RepositoryProbe(policy, connector, lambda: ssl.create_default_context(cafile=certfile))
    try:
        yield {
            "policy": policy,
            "connector": connector,
            "probe": probe,
            "certfile": certfile,
            "head": head,
            "state": state,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
