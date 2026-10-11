"""Load caller-supplied reference images (``reference_images``) for the upstream models.

The service fetches arbitrary caller URLs, so every hop is checked to point at a
public address (no loopback / private / link-local targets), size and type are
capped, and images this service generated itself are read from local storage.

Behind a fake-IP DNS proxy (Clash on the dev server) the local resolver answers
every external host with an address in 198.18.0.0/15 and the proxy resolves the
real one. Such hosts are re-resolved over DNS-over-HTTPS and judged by the real
addresses; if that lookup fails the URL is refused, never let through.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from src.config import Settings

# Per image. Images go upstream base64-encoded inline (~4/3 size): 3 x 4 MB stays
# under Gemini's 20 MB inline request limit and Ark's 10 MB per-image limit.
MAX_REFERENCE_BYTES = 4 * 1024 * 1024
DOWNLOAD_TIMEOUT = 20.0
MAX_REDIRECTS = 3
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DOH_URL = "https://cloudflare-dns.com/dns-query"
DOH_TIMEOUT = 5.0
_DOH_RECORD_TYPES = {"A": 1, "AAAA": 28}
USER_AGENT = "Mozilla/5.0 (compatible; image-service/1.0)"


@dataclass(frozen=True)
class ReferenceImage:
    mime: str
    data: bytes

    @property
    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"


class ReferenceImageError(Exception):
    """A reference image could not be used; ``message`` is safe to return to the caller."""

    def __init__(self, index: int, reason: str):
        self.index = index
        self.message = f"reference_images[{index}] {reason}"
        super().__init__(self.message)


async def _resolve_host(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _resolve_doh(host: str, client: httpx.AsyncClient) -> list[str]:
    """Real A/AAAA addresses of ``host`` via DNS-over-HTTPS. Raises ValueError on any failure."""

    async def query(rtype: str) -> list[str]:
        try:
            resp = await client.get(
                DOH_URL,
                params={"name": host, "type": rtype},
                headers={"Accept": "application/dns-json"},
                timeout=DOH_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError):
            raise ValueError("host could not be verified (DNS-over-HTTPS lookup failed)") from None
        if not isinstance(body, dict) or not isinstance(body.get("Answer", []), list):
            raise ValueError("host could not be verified (DNS-over-HTTPS lookup failed)")
        if body.get("Status") != 0:
            raise ValueError("host could not be resolved")
        code = _DOH_RECORD_TYPES[rtype]
        addresses = [a.get("data") for a in body.get("Answer") or [] if isinstance(a, dict) and a.get("type") == code]
        try:
            # Normalise and reject anything that is not an IP literal.
            return [str(ipaddress.ip_address(a)) for a in addresses]
        except (TypeError, ValueError):
            raise ValueError("host could not be verified (DNS-over-HTTPS lookup failed)") from None

    a, aaaa = await asyncio.gather(query("A"), query("AAAA"))
    return a + aaaa


def _is_fake_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return isinstance(ip, ipaddress.IPv4Address) and ip in FAKE_IP_NETWORK


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


async def _check_public(url: str, client: httpx.AsyncClient) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http(s) URL")
    host = parts.hostname
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("points to a private or local address")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await _resolve_host(host)
        except OSError:
            raise ValueError("host could not be resolved") from None
        # Only fake-IP answers trigger the extra lookup; real answers are judged as-is.
        if addresses and any(_is_fake_ip(a) for a in addresses):
            addresses = await _resolve_doh(host, client)
    if not addresses or not all(_is_public(a) for a in addresses):
        raise ValueError("points to a private or local address")


async def _download(url: str, client: httpx.AsyncClient) -> bytes:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        # Checked on every hop: a public URL may redirect to an internal one.
        await _check_public(current, client)
        async with client.stream(
            "GET", current, timeout=DOWNLOAD_TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        ) as resp:
            if resp.is_redirect and resp.headers.get("location"):
                current = urljoin(current, resp.headers["location"])
                continue
            if resp.status_code != 200:
                raise ValueError(f"could not be downloaded: HTTP {resp.status_code}")
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > MAX_REFERENCE_BYTES:
                    raise ValueError(f"is larger than {MAX_REFERENCE_BYTES // (1024 * 1024)} MB")
            return bytes(data)
    raise ValueError("was redirected too many times")


def _local_file(url: str, settings: Settings) -> Path | None:
    """Path of an image this service generated, if ``url`` points at its static files."""
    prefix = f"{settings.service_base_url.rstrip('/')}{settings.static_url_prefix}/"
    if not url.startswith(prefix):
        return None
    name = url[len(prefix) :]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("is not a valid image path")
    path = Path(settings.storage_dir) / name
    if not path.is_file():
        raise ValueError("was not found")
    return path


def _sniff_mime(data: bytes) -> str | None:
    """Detect the type from the bytes; Content-Type headers are often wrong or generic."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def load_reference_images(urls: list[str], settings: Settings, client: httpx.AsyncClient) -> list[ReferenceImage]:
    """Fetch all reference images concurrently. Raises ReferenceImageError on the first unusable one."""

    async def load(index: int, url: str) -> ReferenceImage:
        try:
            local = _local_file(url, settings)
            data = local.read_bytes() if local else await _download(url, client)
            if len(data) > MAX_REFERENCE_BYTES:
                raise ValueError(f"is larger than {MAX_REFERENCE_BYTES // (1024 * 1024)} MB")
            mime = _sniff_mime(data)
            if mime is None:
                raise ValueError("is not a supported image (PNG, JPEG or WebP)")
            return ReferenceImage(mime, data)
        except ValueError as e:
            raise ReferenceImageError(index, str(e)) from None
        except httpx.TimeoutException:
            raise ReferenceImageError(index, "timed out while downloading") from None
        except httpx.HTTPError as e:
            raise ReferenceImageError(index, f"could not be downloaded ({type(e).__name__})") from None

    return list(await asyncio.gather(*(load(i, url) for i, url in enumerate(urls))))
