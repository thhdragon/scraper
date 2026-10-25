from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import Path

from scraper.constants import (
    MEDIA_EXTENSIONS,
    TELEGRAM_DOMAINS,
    TELEGRAM_RESERVED,
    TELEGRAM_URL_RE,
    TELEGRAM_USERNAME_RE,
    TELEGRAPH_DOMAINS,
    TELEGRAPH_RESERVED,
    TELEGRAPH_SLUG_DATE_RE,
)

logger = logging.getLogger("scraper")


def ensure_scheme(url: str) -> str:
    """Ensure a URL string has an HTTP/HTTPS scheme."""
    url = url.strip()

    if not url:
        return ""

    if url.startswith("//"):
        return "https:" + url

    if "://" not in url:
        return "https://" + url

    return url


def normalize_telegraph_url(url: str) -> str | None:
    """Validate and normalize a Telegra.ph / Graph.org article URL."""
    url = ensure_scheme(url)

    if not url:
        return None

    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower().split(":")[0].removeprefix("www.")

    if netloc not in TELEGRAPH_DOMAINS and not any(
        netloc.endswith(f".{d}") for d in TELEGRAPH_DOMAINS
    ):
        return None

    path = parsed.path.rstrip("/")

    if not path or path == "/":
        return None

    slug = path.lstrip("/").lower()
    if slug in TELEGRAPH_RESERVED or slug.startswith("file/"):
        return None

    if Path(path).suffix.lower() in MEDIA_EXTENSIONS:
        return None

    # Percent-encode unencoded unicode characters
    quoted_path = urllib.parse.quote(urllib.parse.unquote(path), safe="/%-_")

    if not re.fullmatch(r"/(?:%[0-9A-Fa-f]{2}|[A-Za-z0-9_\-])+", quoted_path):
        return None

    return f"https://telegra.ph{quoted_path}"


def is_telegraph_media_url(url: str) -> bool:
    """Check if a URL points to a media asset on a Telegra.ph / Graph.org domain (/file/...)."""
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower().split(":")[0].removeprefix("www.")
        is_telegraph = netloc in TELEGRAPH_DOMAINS or any(
            netloc.endswith(f".{d}") for d in TELEGRAPH_DOMAINS
        )
        return is_telegraph and parsed.path.startswith("/file/")
    except Exception:
        return False


def normalize_telegram_url(url: str) -> str | None:
    """Validate and normalize a Telegram web preview URL (https://t.me/s/channel)."""
    url = ensure_scheme(url)

    if not url:
        return None

    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc.lower().split(":")[0].removeprefix("www.")

    subdomain_channel: str | None = None
    if netloc not in TELEGRAM_DOMAINS:
        for d in TELEGRAM_DOMAINS:
            if netloc.endswith(f".{d}"):
                cand_sub = netloc[: -len(d) - 1].strip(".")
                if cand_sub and cand_sub != "www" and cand_sub not in TELEGRAM_RESERVED:
                    subdomain_channel = cand_sub
                break
        if not subdomain_channel and netloc not in TELEGRAM_DOMAINS:
            return None

    path_clean = parsed.path.strip("/")
    parts = [p for p in path_clean.split("/") if p] if path_clean else []

    if parts and parts[0].lower() == "s":
        parts = parts[1:]

    msg_id: str | None = None
    if subdomain_channel:
        username = subdomain_channel.lower()
        if parts and parts[0].isdigit():
            msg_id = parts[0]
    else:
        if not parts:
            return None

        first_part = parts[0].lower()
        # Check for private channel/group invite patterns (both raw and after /s/)
        if (
            first_part.startswith("+")
            or first_part in {"joinchat", "c"}
            or path_clean.startswith(("+", "joinchat/", "c/"))
        ):
            logger.warning(
                f"Skipping private Telegram URL '{url}'. "
                "Private channels/groups cannot be viewed publicly and require an authorized Telegram account."
            )
            return None

        username = parts[0].split("?")[0].split("#")[0].strip().lstrip("@").lower()

        if len(parts) > 1 and parts[1].isdigit():
            msg_id = parts[1]

    if username in TELEGRAM_RESERVED:
        return None

    if not TELEGRAM_USERNAME_RE.match(username):
        return None

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
    """Extract the base slug from a Telegra.ph URL or slug string, stripping date suffixes."""
    if not url:
        return None

    url = url.strip()
    url_lower = url.lower()

    if any(dom in url_lower for dom in TELEGRAPH_DOMAINS):
        normalized = normalize_telegraph_url(url)
        if not normalized:
            return None
        path = urllib.parse.urlparse(normalized).path.strip("/")
    elif "://" in url:
        return None
    elif "/" in url:
        parts = [p for p in url.strip("/").split("/") if p]
        if len(parts) > 1:
            return None
        path = parts[0] if parts else ""
    else:
        path = url.strip("/")

    if not path or path.startswith("+"):
        return None

    m = TELEGRAPH_SLUG_DATE_RE.match(path)
    if m:
        return m.group("base")

    return path


def slugify_telegraph_candidates(text: str) -> list[str]:
    """Generate candidate slug variants for Telegra.ph."""
    if not text:
        return []

    # Skip private Telegram links or media files
    if text.startswith("+") or text.lower().startswith(("joinchat/", "c/")):
        return []
    if "/file/" in text or any(text.lower().endswith(ext) for ext in MEDIA_EXTENSIONS):
        return []

    text_lower = text.lower()
    if any(dom in text_lower for dom in TELEGRAM_DOMAINS):
        norm = normalize_telegram_url(text)
        if norm:
            m = TELEGRAM_URL_RE.match(norm)
            if m:
                text = m.group(1)
        else:
            return []

    if "://" in text or "/" in text:
        try:
            parsed = urllib.parse.urlparse(text)
            text = parsed.path.rstrip("/").split("/")[-1]
        except Exception:
            pass
    text = text.split("?")[0].split("#")[0].strip().lstrip("@")
    candidates: list[str] = []

    def _add_variants(slug_text: str) -> None:
        # 1. Hyphenated slug (converting underscores and spaces to hyphens)
        ph_hyphen = re.sub(
            r"-+",
            "-",
            re.sub(r"[^A-Za-z0-9_\-]+", "-", slug_text).replace("_", "-").strip("-"),
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
            r"_+", "_", re.sub(r"[^A-Za-z0-9_\-]+", "_", slug_text).strip("-_")
        )
        if (
            ph_underscore
            and len(ph_underscore) >= 3
            and ph_underscore not in candidates
            and ph_underscore.lower() not in TELEGRAPH_RESERVED
        ):
            candidates.append(ph_underscore)

        # 3. Compact without separators
        ph_compact = re.sub(r"[^A-Za-z0-9]+", "", slug_text)
        if (
            ph_compact
            and len(ph_compact) >= 3
            and ph_compact not in candidates
            and ph_compact.lower() not in TELEGRAPH_RESERVED
        ):
            candidates.append(ph_compact)

    _add_variants(text)
    base = extract_base_slug(text)
    if base and base != text:
        _add_variants(base)

    return candidates


def slugify_telegram_candidates(text: str) -> list[str]:
    """Generate candidate Telegram usernames from raw text or URLs."""
    if not text:
        return []

    # Skip private Telegram links
    if text.startswith("+") or text.lower().startswith(("joinchat/", "c/")):
        return []

    text_lower = text.lower()
    if any(dom in text_lower for dom in TELEGRAM_DOMAINS) or "://" in text:
        norm = normalize_telegram_url(text)
        if norm:
            m = TELEGRAM_URL_RE.match(norm)
            if m:
                text = m.group(1)
        else:
            if any(dom in text_lower for dom in TELEGRAM_DOMAINS):
                return []
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

    text = text.split("?")[0].split("#")[0].strip().lstrip("@")
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

    # Reject private Telegram invite links
    if target.startswith("+") or target.lower().startswith(("joinchat/", "c/")):
        return None

    target_lower = target.lower()
    if any(dom in target_lower for dom in TELEGRAM_DOMAINS) or "://" in target:
        normalized = normalize_telegram_url(target)

        if normalized:
            m = TELEGRAM_URL_RE.match(normalized)

            if m:
                cand = m.group(1)
                if cand.lower() not in TELEGRAM_RESERVED:
                    return cand

        # If it was a Telegram domain or URL and could not be normalized to a public channel,
        # it is either private, invalid, or an external URL. Do not fall back!
        return None

    raw = target.lstrip("@").split("?")[0].split("#")[0].strip()
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


def canonicalize_seed(seed: str) -> str | None:
    """
    Validate and return canonical representation of a seed.
    Returns:
      - Lowercase Telegram username for public channels (e.g. 'durov')
      - Clean Telegra.ph base slug for articles (e.g. 'my-article-title')
      - None if invalid, private, reserved, or not a supported channel/article.
    """
    if not seed:
        return None
    s = seed.strip()
    if not s or s.startswith("#") or len(s) < 3:
        return None

    # Reject private Telegram invite codes
    if s.startswith("+") or s.lower().startswith(("joinchat/", "c/")):
        return None

    s_lower = s.lower()

    # 1. Telegram domain or full URL
    if any(dom in s_lower for dom in TELEGRAM_DOMAINS):
        norm = normalize_telegram_url(s)
        if not norm:
            return None
        m = TELEGRAM_URL_RE.match(norm)
        if m:
            username = m.group(1).lower()
            if username not in TELEGRAM_RESERVED and username not in TELEGRAPH_RESERVED:
                return username
        return None

    # 2. Telegra.ph / Graph.org domain or full URL
    if any(dom in s_lower for dom in TELEGRAPH_DOMAINS):
        norm_ph = normalize_telegraph_url(s)
        if not norm_ph:
            return None
        base = extract_base_slug(norm_ph)
        if base and base.lower() not in TELEGRAPH_RESERVED and base.lower() not in TELEGRAM_RESERVED:
            return base
        return None

    # 3. If there is a scheme '://' but neither Telegram nor Telegraph, reject
    if "://" in s:
        return None

    # 4. Raw identifier / slug without domain
    raw = s.lstrip("@").split("?")[0].split("#")[0].strip()
    if not raw or "/" in raw:
        return None

    # Check if valid Telegram username
    if TELEGRAM_USERNAME_RE.match(raw):
        cand = raw.lower()
        if cand not in TELEGRAM_RESERVED and cand not in TELEGRAPH_RESERVED:
            return cand

    # Check if valid Telegraph slug
    base = extract_base_slug(raw)
    if base:
        quoted_base = urllib.parse.quote(urllib.parse.unquote(base), safe="%-_")
        if re.fullmatch(r"(?:%[0-9A-Fa-f]{2}|[A-Za-z0-9_\-])+", quoted_base):
            cand = quoted_base.lower()
            if cand not in TELEGRAPH_RESERVED and cand not in TELEGRAM_RESERVED:
                return quoted_base

    return None
