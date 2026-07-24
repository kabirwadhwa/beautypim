import gzip
import re
from xml.etree import ElementTree
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.scraping.url_safety import normalize_url


def parse_sitemap(content: bytes) -> tuple[str, list[str]]:
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    root = ElementTree.fromstring(content)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag not in {"urlset", "sitemapindex"}:
        raise ValueError("Unsupported sitemap document")
    urls = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text:
            urls.append(node.text.strip())
    return ("sitemap_index" if tag == "sitemapindex" else "sitemap"), urls


def discover_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: set[str] = set()
    for tag in soup.select("a[href], link[rel=canonical][href]"):
        href = tag.get("href")
        if href and not href.startswith(("mailto:", "tel:", "javascript:")):
            found.add(normalize_url(urljoin(base_url, href)))
    for match in re.findall(r'https?://[^"\'<>\s]+', html):
        if "/product" in match.lower() or "/p/" in match.lower():
            found.add(normalize_url(match))
    return sorted(found)
