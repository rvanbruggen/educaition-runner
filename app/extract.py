"""HTTP fetching and article extraction (text, date, og:image)."""
import html as htmllib
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import trafilatura

USER_AGENT = ("Mozilla/5.0 (compatible; educaition-runner/0.2; "
              "+https://www.educaition.today/over/)")
TIMEOUT = 25
_last_hit: dict[str, float] = {}
POLITE_DELAY = 1.0  # seconds between requests to the same host


def get(url: str, client: httpx.Client | None = None) -> httpx.Response:
    host = urlsplit(url).netloc
    wait = POLITE_DELAY - (time.monotonic() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()
    c = client or httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True,
                               timeout=TIMEOUT)
    try:
        r = c.get(url)
        r.raise_for_status()
        return r
    finally:
        if client is None:
            c.close()


@dataclass
class Page:
    url: str           # final URL after redirects
    title: str
    text: str
    date: str          # YYYY-MM-DD or ''
    image: str         # og:image or ''
    sitename: str


def _meta(html: str, prop: str) -> str:
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]*content=["\']([^"\']+)["\']',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{prop}["\']'):
        m = re.search(pat, html, re.I)
        if m:
            return htmllib.unescape(m.group(1)).strip()
    return ""


def fetch_page(url: str) -> Page:
    r = get(url)
    html = r.text
    text = trafilatura.extract(html, url=str(r.url), include_comments=False,
                               include_tables=False, favor_precision=True) or ""
    meta = trafilatura.extract_metadata(html, default_url=str(r.url))
    title = (meta.title if meta and meta.title else "") or _meta(html, "og:title")
    date = (meta.date if meta and meta.date else "") or ""
    # Prefer the explicit article date over htmldate's guess when the page has one.
    published = _meta(html, "article:published_time")
    if re.match(r"\d{4}-\d{2}-\d{2}", published):
        date = published[:10]
    image = _meta(html, "og:image")
    if image and not image.startswith("https://"):
        image = ""
    return Page(url=str(r.url), title=title.strip(), text=text.strip(), date=date[:10],
                image=image, sitename=(meta.sitename if meta and meta.sitename else "") or "")
