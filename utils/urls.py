"""URL validation shared by every download entry point.

The bot fetches user-supplied links, so anything that could reach the host,
the container network or a cloud metadata endpoint has to be rejected before
work is queued.
"""

from __future__ import annotations

import asyncio
import socket
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import urlparse

_Address = IPv4Address | IPv6Address

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}


def _is_private_address(address: _Address) -> bool:
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


async def validate_public_url(raw_url: str) -> str:
    """Validate a URL before queueing and reject common SSRF targets."""
    candidate = raw_url.strip()
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP(S) URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not supported")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith((".local", ".internal")):
        raise ValueError("Private or local destinations are not supported")

    try:
        literal = ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError("The destination host could not be resolved") from exc
        addresses = [ip_address(info[4][0]) for info in infos]

    if not addresses or any(_is_private_address(address) for address in addresses):
        raise ValueError("Private or local destinations are not supported")
    return candidate


def is_obviously_private_request(raw_url: str) -> bool:
    """Reject private literal hosts in browser subrequests and redirects."""
    try:
        parsed = urlparse(raw_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return parsed.scheme.lower() not in {"", "about", "data", "blob"}
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname in _BLOCKED_HOSTNAMES or hostname.endswith((".local", ".internal")):
            return True
        try:
            address = ip_address(hostname)
        except ValueError:
            return False
        return _is_private_address(address)
    except ValueError:
        return True
