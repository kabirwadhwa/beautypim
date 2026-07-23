import ipaddress
import socket
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image as PILImage


MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def normalize_public_image_url(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or len(text) > 2048:
        return None
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return None
    try:
        if not ipaddress.ip_address(hostname).is_global:
            return None
    except ValueError:
        pass
    return text


def _assert_public_host(url: str) -> None:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("Image URL has no host.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("Image host could not be resolved.") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("Image URL must resolve to a public internet address.")


def fetch_public_image(url: Optional[str]) -> Optional[BytesIO]:
    normalized = normalize_public_image_url(url)
    if not normalized:
        return None
    current = normalized
    for _ in range(4):
        _assert_public_host(current)
        response = requests.get(
            current,
            timeout=(3, 8),
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": "BeautyPIM-PDF/1.0"},
        )
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        if content_type not in ALLOWED_IMAGE_TYPES:
            return None
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                return None
        image = PILImage.open(BytesIO(data))
        image.verify()
        return BytesIO(data)
    return None
