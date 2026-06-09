from __future__ import annotations

from urllib.parse import unquote, urlparse


ODRANID_HOSTS = {"odranid.com.ar", "www.odranid.com.ar", "odranid.com", "www.odranid.com"}


def extract_product_slugs(text: str) -> list[str]:
    slugs: list[str] = []
    seen: set[str] = set()
    for raw_url in str(text or "").split():
        slug = product_slug_from_url(raw_url.rstrip(".,)"))
        if slug and slug not in seen:
            slugs.append(slug)
            seen.add(slug)
    return slugs


def product_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.lower() not in ODRANID_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "producto":
        return None
    return normalize_product_slug(parts[1])


def normalize_product_slug(slug: str | None) -> str | None:
    value = unquote(str(slug or "")).strip().strip("/").lower()
    return value or None


def canonical_product_urls_for_slug(slug: str) -> list[str]:
    clean_slug = normalize_product_slug(slug)
    if not clean_slug:
        return []
    return [
        f"https://odranid.com.ar/producto/{clean_slug}",
        f"https://www.odranid.com.ar/producto/{clean_slug}",
        f"https://odranid.com/producto/{clean_slug}",
        f"https://www.odranid.com/producto/{clean_slug}",
    ]


def product_link_matches_slug(link: str | None, slug: str | None) -> bool:
    target = normalize_product_slug(slug)
    if not target:
        return False
    return product_slug_from_url(str(link or "").rstrip("/")) == target
