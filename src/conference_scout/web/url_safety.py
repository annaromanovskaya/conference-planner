import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]


class UnsafeURLError(ValueError):
    """Raised when an untrusted URL may reach a non-public resource."""


async def _default_resolver(host: str, port: int) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(ipaddress.ip_address(record[4][0]) for record in records))


class URLSafetyPolicy:
    def __init__(self, resolver: Resolver = _default_resolver) -> None:
        self.resolver = resolver

    async def validate(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise UnsafeURLError("only HTTP and HTTPS URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeURLError("URL credentials are not allowed")
        host = parsed.hostname
        if not host:
            raise UnsafeURLError("URL must include a hostname")
        try:
            literal = ipaddress.ip_address(host)
            addresses = [literal]
        except ValueError:
            addresses = await self.resolver(
                host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            )
        if not addresses or any(not address.is_global for address in addresses):
            raise UnsafeURLError("URL resolves to a non-public address")
        scheme = parsed.scheme.lower()
        port = parsed.port
        default_port = 443 if scheme == "https" else 80
        netloc = host.lower() if port in {None, default_port} else f"{host.lower()}:{port}"
        return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))
