"""The site's controlled vocabulary (_data/tags.yml) and URL normalisation."""
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

FACETS = ("niveau", "vak", "thema", "regio", "type", "doelgroep")
# Post types the article scan may produce (the rest belong to other tasks).
ARTICLE_TYPES = ["nieuws", "onderzoek", "opinie", "gids-handleiding", "beleidsdocument",
                 "best-practice", "casestudy", "tool-review"]
ONDERWIJSNETTEN = ["go", "katholiek-onderwijs", "ovsg", "pov"]

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|xtor$|at_|ref$|cmp$)")


def load(repo: Path) -> dict[str, list[str]]:
    """{facet: [slug, ...]}, flattening kinderen / finaliteit / onderwijsnet."""
    raw = yaml.safe_load((repo / "_data" / "tags.yml").read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for facet in FACETS:
        slugs: list[str] = []

        def walk(items):
            for it in items or []:
                if isinstance(it, dict):
                    if "slug" in it:
                        slugs.append(it["slug"])
                    for key, val in it.items():
                        if isinstance(val, list):
                            walk(val)
        walk(raw.get(facet))
        out[facet] = slugs
    return out


def normalise_url(url: str) -> str:
    """Dedupe key: https, no www, no fragment, no tracking params, no trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def clean_url(url: str) -> str:
    """For publishing: the URL as the source gave it, minus tracking params and fragment."""
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def existing_bron_urls(repo: Path) -> set[str]:
    """Normalised bron: URLs of every post already on the site."""
    urls = set()
    for p in (repo / "_posts").glob("*.md"):
        m = re.search(r"^bron:\s*[\"']?(\S+?)[\"']?\s*$", p.read_text(encoding="utf-8"), re.M)
        if m:
            urls.add(normalise_url(m.group(1)))
    return urls
