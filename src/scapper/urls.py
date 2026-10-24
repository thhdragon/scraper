from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path

from scapper.constants import (
    MEDIA_EXTENSIONS,
    TELEGRAM_DOMAINS,
    TELEGRAM_RESERVED,
    TELEGRAM_URL_RE,
    TELEGRAM_USERNAME_RE,
    TELEGRAPH_RESERVED,
    TELEGRAPH_SLUG_DATE_RE,
)

logger = logging.getLogger("scraper")


def ensure_scheme(url: str) -> str:
    """Ensure a URL string has an HTTP/HTTPS scheme."""
    url = url.strip()

    if not url:
        return ""

    if "://" not in url:
        return "https://" + url

    return url


def normalize_telegraph_url(url: str) -> str | None:
    """Validate and normalize a Telegra.ph article URL."""
    url = ensure_scheme(url)

    if not url:
        return None

    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower().split(":")[0].removeprefix("www.")

    if netloc != "telegra.ph" and not netloc.endswith(".telegra.ph"):
        return None

    path = parsed.path.rstrip("/")

    if not path or path == "/":
        return None

    slug = path.lstrip("/").lower()
    if slug in TELEGRAPH_RESERVED or slug.startswith("file/"):
        return None

    if Path(path).suffix.lower() in MEDIA_EXTENSIONS:
        return None

    if not re.fullmatch(r"/[A-Za-z0-9_\-]+", path):
        return None

    return f"https://telegra.ph{path}"


def normalize_telegram_url(url: str) -> str | None:
    """Validate and normalize a Telegram web preview URL (https://t.me/s/channel)."""
    url = ensure_scheme(url)

    if not url:
        return None

    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower().split(":")[0].removeprefix("www.")

    if netloc not in TELEGRAM_DOMAINS:
        return None

    path_clean = parsed.path.strip("/")
    if not path_clean:
        return None

    # Check for private channel/group invite patterns
    if path_clean.startswith(("+", "joinchat/", "c/")):
        logger.warning(
            f"Skipping private Telegram URL '{url}'. "
            "Private channels/groups cannot be viewed publicly and require an authorized Telegram account."
        )
        return None

    parts = [p for p in path_clean.split("/") if p]

    if not parts:
        return None

    if parts[0].lower() == "s":
        parts = parts[1:]

    if not parts:
        return None

    username = parts[0].split("?")[0].strip().lstrip("@").lower()

    if username.lower() in TELEGRAM_RESERVED:
        return None

    if not TELEGRAM_USERNAME_RE.match(username):
        return None

    msg_id: str | None = None
    if len(parts) > 1 and parts[1].isdigit():
        msg_id = parts[1]

    if msg_id:
        base = f"https://t.me/s/{username}/{msg_id}"
    else:
        base = f"https://t.me/s/{username}"

    if parsed.query:
        qs = urllib.parse.parse_qs(parsed.query)
        q_pairs = []
        for param in ("before", "after"):
            val = qs.get(param, [None])[0]
            if val and val.isdigit() and int(val) > 0:
                q_pairs.append(f"{param}={val}")
        if q_pairs:
            return f"{base}?{'&'.join(q_pairs)}"

    return base


def extract_base_slug(url: str) -> str | None:
    """Extract the base slug from a Telegra.ph URL, stripping date suffixes."""
    if not url:
        return None

    url = url.strip()

    if "telegra.ph" in url:
        normalized = normalize_telegraph_url(url)
        if not normalized:
            return None
        path = urllib.parse.urlparse(normalized).path.strip("/")
    elif "://" in url or "/" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            path = parsed.path.strip("/")
            if not path:
                return None
        except Exception:
            path = url.strip("/")
    else:
        path = url.strip("/")

    if not path:
        return None

    m = TELEGRAPH_SLUG_DATE_RE.match(path)
    if m:
        return m.group("base")

    return path


def slugify_telegraph_candidates(text: str) -> list[str]:
    """Generate candidate slug variants for Telegra.ph."""
    if not text:
        return []
    if "://" in text or "/" in text:
        try:
            parsed = urllib.parse.urlparse(text)
            text = parsed.path.rstrip("/").split("/")[-1]
        except Exception:
            pass
    text = text.split("?")[0].strip().lstrip("@")
    candidates: list[str] = []

    # 1. Hyphenated slug (converting underscores and spaces to hyphens)
    ph_hyphen = re.sub(
        r"-+",
        "-",
        re.sub(r"[^A-Za-z0-9_\-]+", "-", text).replace("_", "-").strip("-"),
    )
    if (
        ph_hyphen
        and len(ph_hyphen) >= 3
        and ph_hyphen not in candidates
        and ph_hyphen.lower() not in TELEGRAPH_RESERVED
    ):
        candidates.append(ph_hyphen)

    # 2. Preserving underscores if present
    ph_underscore = re.sub(
        r"_+", "_", re.sub(r"[^A-Za-z0-9_\-]+", "_", text).strip("-_")
    )
    if (
        ph_underscore
        and len(ph_underscore) >= 3
        and ph_underscore not in candidates
        and ph_underscore.lower() not in TELEGRAPH_RESERVED
    ):
        candidates.append(ph_underscore)

    # 3. Compact without separators
    ph_compact = re.sub(r"[^A-Za-z0-9]+", "", text)
    if (
        ph_compact
        and len(ph_compact) >= 3
        and ph_compact not in candidates
        and ph_compact.lower() not in TELEGRAPH_RESERVED
    ):
        candidates.append(ph_compact)

    return candidates


def slugify_telegram_candidates(text: str) -> list[str]:
    """Generate candidate Telegram usernames from raw text or URLs."""
    if not text:
        return []
    if any(dom in text for dom in TELEGRAM_DOMAINS) or "://" in text:
        norm = normalize_telegram_url(text)
        if norm:
            m = TELEGRAM_URL_RE.match(norm)
            if m:
                text = m.group(1)
        else:
            try:
                parsed = urllib.parse.urlparse(text)
                parts = [
                    p for p in parsed.path.strip("/").split("/") if p and p != "s"
                ]
                if parts:
                    text = parts[0]
            except Exception:
                pass
    elif "/" in text:
        parts = [p for p in text.strip("/").split("/") if p and p != "s"]
        if parts:
            text = parts[0]

    text = text.split("?")[0].strip().lstrip("@")
    candidates: list[str] = []

    # 1. Exact match if valid
    if TELEGRAM_USERNAME_RE.match(text):
        candidates.append(text)

    # 2. Replace hyphens and separators with underscores
    tg_underscore = re.sub(
        r"_+", "_", re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    )
    if (
        TELEGRAM_USERNAME_RE.match(tg_underscore)
        and tg_underscore not in candidates
    ):
        candidates.append(tg_underscore)

    # 3. Compact alphanumeric without separators
    tg_compact = re.sub(r"[^A-Za-z0-9]+", "", text)
    if TELEGRAM_USERNAME_RE.match(tg_compact) and tg_compact not in candidates:
        candidates.append(tg_compact)

    return [
        c
        for c in candidates
        if c.lower() not in TELEGRAM_RESERVED
        and c.lower() not in TELEGRAPH_RESERVED
    ]


def slugify_telegraph(text: str) -> str | None:
    candidates = slugify_telegraph_candidates(text)
    return candidates[0] if candidates else None


def slugify_telegram(text: str) -> str | None:
    candidates = slugify_telegram_candidates(text)
    return candidates[0] if candidates else None


def extract_chat_username(target: str) -> str | None:
    """Extract a clean Telegram channel/chat username from a URL or seed."""
    target = target.strip()

    if not target:
        return None

    if any(dom in target for dom in TELEGRAM_DOMAINS) or "://" in target:
        normalized = normalize_telegram_url(target)

        if normalized:
            m = TELEGRAM_URL_RE.match(normalized)

            if m:
                cand = m.group(1)
                if cand.lower() not in TELEGRAM_RESERVED:
                    return cand

    raw = target.lstrip("@").split("?")[0].strip()
    if "/" in raw:
        parts = [p for p in raw.split("/") if p and p != "s"]
        if parts:
            raw = parts[0]

    if (
        TELEGRAM_USERNAME_RE.match(raw)
        and raw.lower() not in TELEGRAM_RESERVED
        and raw.lower() not in TELEGRAPH_RESERVED
    ):
        return raw

    return slugify_telegram(target)
