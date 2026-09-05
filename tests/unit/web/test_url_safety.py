import ipaddress

import pytest

from conference_scout.web.url_safety import UnsafeURLError, URLSafetyPolicy


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://user:password@example.org/",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await URLSafetyPolicy().validate(url)


@pytest.mark.asyncio
async def test_hostname_resolving_to_private_address_is_rejected() -> None:
    async def resolver(_host: str, _port: int) -> list[ipaddress.IPv4Address]:
        return [ipaddress.ip_address("10.0.0.2")]

    with pytest.raises(UnsafeURLError):
        await URLSafetyPolicy(resolver=resolver).validate("https://example.org/path")


@pytest.mark.asyncio
async def test_public_url_is_normalized() -> None:
    async def resolver(_host: str, _port: int) -> list[ipaddress.IPv4Address]:
        return [ipaddress.ip_address("93.184.216.34")]

    result = await URLSafetyPolicy(resolver=resolver).validate("HTTPS://Example.ORG:443/a#fragment")
    assert result == "https://example.org/a"
