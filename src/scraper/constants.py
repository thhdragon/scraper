from __future__ import annotations

import re

BROWSER_PROFILES: list[dict[str, str]] = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
    },
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Linux"',
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    },
]

USER_AGENTS = [p["User-Agent"] for p in BROWSER_PROFILES]


MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
}

MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".heic",
    ".heif",
    ".bmp",
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".mp3",
    ".ogg",
    ".wav",
    ".flac",
    ".m4a",
    ".opus",
    ".aac",
}

TELEGRAM_DOMAINS = {"t.me", "telegram.me", "telegram.dog", "telegram.space", "tx.me"}
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
TELEGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog|telegram\.space|tx\.me)/(?:s/)?([A-Za-z0-9_]{3,32})(?:/(\d+))?",
    re.IGNORECASE,
)
TELEGRAM_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9_]{3,32})\b")
TELEGRAPH_DOMAINS = {"telegra.ph", "graph.org"}
TELEGRAPH_RESERVED = {"file", "upload", "check", "a", "edit", "api"}
TELEGRAPH_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:telegra\.ph|graph\.org)/(?!(?:file|upload|check|a|edit|api)(?:[/?#]|$))((?:%[0-9A-Fa-f]{2}|[A-Za-z0-9_\-])+)",
    re.IGNORECASE,
)
TELEGRAPH_SLUG_DATE_RE = re.compile(
    r"^(?P<base>.+)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?:-(?P<page>\d+))?$"
)
TELEGRAPH_URL_SUFFIX_RE = re.compile(
    r"^(?P<base_dated>https?://(?:www\.)?(?:telegra\.ph|graph\.org)/.+-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))(?:-(\d+))?$"
)

TELEGRAM_RESERVED = {
    "joinchat",
    "addstickers",
    "addtheme",
    "share",
    "addemoji",
    "setlanguage",
    "proxy",
    "socks",
    "socks5",
    "login",
    "s",
    "c",
    "m",
    "v",
    "d",
    "bg",
    "invoice",
    "boost",
    "contact",
    "about",
    "blog",
    "apps",
    "press",
    "legal",
    "privacy",
    "security",
    "faq",
    "terms",
    "jobs",
    "tour",
    "premium",
    "stickers",
}
