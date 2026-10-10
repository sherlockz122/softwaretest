"""Strict repository identity and public-address validation, with bounded DNS queries."""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import dns.exception
import dns.resolver


class RepositoryError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


@dataclass(frozen=True)
class Target:
    url: str
    host: str
    path: str


def canonicalize(value):
    if not isinstance(value, str) or len(value) > 1024 or re.search(r"[\s\\%]", value):
        raise RepositoryError(422, "REPOSITORY_UNSAFE_URL")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
            or not parts.hostname
        ):
            raise ValueError
        host = parts.hostname.rstrip(".").lower()
        # ASCII names avoid differing IDNA interpretations in DNS, TLS and libcurl.
        if len(host) > 253 or not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError
        labels = host.split(".")
        if len(labels) < 2 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
        ):
            raise ValueError
        if host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        segments = parts.path.rstrip("/").split("/")[1:]
        if not 2 <= len(segments) <= 12 or any(
            segment in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.~-]{1,200}", segment)
            for segment in segments
        ):
            raise ValueError
        if segments[-1].endswith(".git"):
            segments[-1] = segments[-1][:-4]
        if not segments[-1] or segments[-1] in {".", ".."}:
            raise ValueError
        if host == "github.com":
            if len(segments) != 2:
                raise ValueError
            segments = [segment.lower() for segment in segments]
        path = "/" + "/".join(segments) + ".git"
        url = "https://" + host + path
        if len(url) > 1024:
            raise ValueError
        return Target(url, host, path)
    except ValueError:
        raise RepositoryError(422, "REPOSITORY_UNSAFE_URL") from None


def public_address(value):
    try:
        if "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        forbidden = (
            "192.0.0.0/24",
            "64:ff9b::/96",
            "64:ff9b:1::/48",
            "2002::/16",
            "2001::/32",
        )
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or getattr(address, "ipv4_mapped", None) is not None
            or any(address in ipaddress.ip_network(network) for network in forbidden)
        ):
            raise ValueError
        return str(address)
    except ValueError:
        raise RepositoryError(422, "REPOSITORY_UNSAFE_ADDRESS") from None


def resolve_addresses(host):
    resolver = dns.resolver.Resolver()
    resolver.timeout = 1
    resolver.lifetime = 2
    values = []
    try:
        # Failure of either family is fail-closed; NoAnswer simply means no such records.
        for kind in ("A", "AAAA"):
            try:
                values.extend(str(answer) for answer in resolver.resolve(host, kind, search=False))
            except dns.resolver.NoAnswer:
                pass
    except dns.exception.DNSException:
        raise RepositoryError(503, "REPOSITORY_DNS_UNAVAILABLE") from None
    if not values:
        raise RepositoryError(503, "REPOSITORY_DNS_UNAVAILABLE")
    return values


class URLPolicy:
    def __init__(self, resolver=resolve_addresses):
        self.resolver = resolver

    def addresses(self, target):
        try:
            literal = ipaddress.ip_address(target.host)
        except ValueError:
            values = self.resolver(target.host)
        else:
            values = [str(literal)]
        if not values:
            raise RepositoryError(503, "REPOSITORY_DNS_UNAVAILABLE")
        return tuple(dict.fromkeys(public_address(value) for value in values))
