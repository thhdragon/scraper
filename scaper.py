# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx",
#   "beautifulsoup4",
#   "telethon",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import random
import re
import sqlite3
import sys
import urllib.parse
import uuid
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scraper")


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}

MEDIA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
}

TELEGRAM_DOMAINS = {"t.me", "telegram.me", "telegram.dog", "telegram.space"}
TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
TELEGRAM_URL_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me|telegram\.dog|telegram\.space)/(?:s/)?([A-Za-z0-9_]{5,32})(?:/(\d+))?",
    re.IGNORECASE,
)
TELEGRAM_MENTION_RE = re.compile(r"(?:^|[\s(])@([A-Za-z0-9_]{5,32})\b")
TELEGRAPH_URL_RE = re.compile(r"https?://telegra\.ph/[A-Za-z0-9_\-]+", re.IGNORECASE)
TELEGRAPH_SLUG_DATE_RE = re.compile(
    r"^(?P<base>.+?)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?:-(?P<page>\d+))?$"
)
TELEGRAPH_SLUG_MONTH_RE = re.compile(r"^(?P<base>.+?)-(?:0[1-9]|1[0-2])$")
TELEGRAPH_URL_SUFFIX_RE = re.compile(
    r"^(?P<base_dated>https?://telegra\.ph/.+?-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))(?:-(\d+))?$"
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
    "login",
    "s",
    "c",
    "m",
    "v",
    "d",
    "bg",
    "invoice",
    "boost",
}


@dataclass
class ScraperConfig:
    config_path: Path = Path("config.json")
    db_path: Path = Path("scraper_cache.db")
    download_dir: Path = Path("downloads")

    min_size_kb: int = 100

    # -1 means infinite depth
    max_web_depth: int = -1

    web_delay_min: float = 1.0
    web_delay_max: float = 3.0

    max_connections: int = 5

    # 0 means unlimited downloads per page
    max_downloads_per_page: int = 0

    request_timeout: float = 20.0

    probe_telegraph_dates: bool = False
    probe_date_concurrency: int = 10

    # 0 means unlimited suffix probing
    max_suffix_probes: int = 0

    discovery_enabled: bool = False
    keywords: tuple[str, ...] = ("wallpaper",)
    search_engines: tuple[str, ...] = ("duckduckgo",)

    include_discovered: bool = True
    discovered_seeds_file: Path = Path("discovered_seeds.txt")

    telegram_api: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ScraperConfig:
        path = Path(path)
        data = {}

        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    logger.error(
                        "config.json must contain a JSON object. Using defaults."
                    )
                    data = {}
            except Exception as exc:
                logger.error(f"Error loading config file: {exc}. Using defaults.")
                data = {}

        delay_range = data.get("web_delay_range", [1.0, 3.0])
        if not isinstance(delay_range, (list, tuple)) or len(delay_range) != 2:
            delay_range = (1.0, 3.0)

        discovery = data.get("discovery", {})
        if not isinstance(discovery, dict):
            discovery = {}

        max_web_depth = data.get("max_web_depth", -1)
        if max_web_depth is None:
            max_web_depth = -1

        try:
            max_web_depth = int(max_web_depth)
        except Exception:
            max_web_depth = -1

        return cls(
            config_path=path,
            db_path=Path(data.get("db_path", "scraper_cache.db")),
            download_dir=Path(data.get("download_dir", "./downloads")),
            min_size_kb=int(data.get("min_size_kb", 100)),
            max_web_depth=max_web_depth,
            web_delay_min=float(delay_range[0]),
            web_delay_max=float(delay_range[1]),
            max_connections=int(data.get("max_connections", 5)),
            max_downloads_per_page=int(data.get("max_downloads_per_page", 0)),
            request_timeout=float(data.get("request_timeout", 20.0)),
            probe_telegraph_dates=bool(data.get("probe_telegraph_dates", False)),
            probe_date_concurrency=int(data.get("probe_date_concurrency", 10)),
            max_suffix_probes=int(data.get("max_suffix_probes", 0)),
            discovery_enabled=bool(discovery.get("enabled", False)),
            keywords=tuple(str(k) for k in discovery.get("keywords", ["wallpaper"])),
            search_engines=tuple(
                str(e).lower() for e in discovery.get("search_engines", ["duckduckgo"])
            ),
            include_discovered=bool(data.get("include_discovered", True)),
            discovered_seeds_file=Path(
                data.get("discovered_seeds_file", "discovered_seeds.txt")
            ),
            telegram_api=data.get("telegram_api", {}),
            raw=data,
        )

    @property
    def min_size_bytes(self) -> int:
        return self.min_size_kb * 1024

    @property
    def max_depth(self) -> float:
        return float("inf") if self.max_web_depth < 0 else float(self.max_web_depth)

    def delay(self) -> float:
        return random.uniform(self.web_delay_min, self.web_delay_max)


class Storage:
    """SQLite persistence layer for crawl state, downloads, and discovered seeds."""

    def __init__(self, path: Path, discovered_seeds_file: Path):
        self.path = Path(path)
        self.discovered_seeds_file = Path(discovered_seeds_file)
        self.discovered_seeds_file.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.clean_discovered_seeds()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawled_urls (
                    url TEXT PRIMARY KEY,
                    crawled_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS downloaded_hashes (
                    file_hash TEXT PRIMARY KEY,
                    file_path TEXT,
                    downloaded_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS api_channel_progress (
                    channel_username TEXT PRIMARY KEY,
                    last_message_id INTEGER,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS probed_base_slugs (
                    base_slug TEXT PRIMARY KEY,
                    probed_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS discovered_seeds (
                    seed TEXT PRIMARY KEY,
                    discovered_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS crawl_queue (
                    url TEXT PRIMARY KEY,
                    url_type TEXT NOT NULL,
                    depth INTEGER NOT NULL DEFAULT 0,
                    queued_at TEXT DEFAULT (datetime('now'))
                );
                """
            )

    # ---------- Crawled URLs ----------

    def has_url_crawled(self, url: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM crawled_urls WHERE url = ?",
                (url,),
            ).fetchone()
            return row is not None

    def mark_url_crawled(self, url: str) -> None:
        if not url:
            return

        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO crawled_urls (url) VALUES (?)",
                (url,),
            )

    # ---------- Downloaded hashes ----------

    def has_hash_downloaded(self, file_hash: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM downloaded_hashes WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()
            return row is not None

    def record_downloaded_hash(self, file_hash: str, file_path: Path) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO downloaded_hashes (file_hash, file_path) VALUES (?, ?)",
                (file_hash, str(file_path)),
            )

    def count_downloaded_hashes(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM downloaded_hashes").fetchone()
            return int(row["c"]) if row else 0

    # ---------- Queue ----------

    def enqueue(self, url: str, url_type: str, depth: int) -> bool:
        if not url or not url_type:
            return False

        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO crawl_queue (url, url_type, depth)
                VALUES (?, ?, ?)
                """,
                (url, url_type, int(depth)),
            )
            return cur.rowcount > 0

    def remove_from_queue(self, url: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM crawl_queue WHERE url = ?",
                (url,),
            )

    def get_min_pending_depth(self) -> int | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MIN(depth) AS min_depth FROM crawl_queue"
            ).fetchone()
            if row and row["min_depth"] is not None:
                return int(row["min_depth"])
            return None

    def get_pending_batch(self, depth: int, limit: int = 200) -> list[tuple[str, str]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT url, url_type
                FROM crawl_queue
                WHERE depth = ?
                ORDER BY queued_at, rowid
                LIMIT ?
                """,
                (int(depth), int(limit)),
            ).fetchall()
            return [(row["url"], row["url_type"]) for row in rows]

    def clear_queue(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM crawl_queue")

    def clear_queue_at_depth(self, depth: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM crawl_queue WHERE depth = ?",
                (int(depth),),
            )

    # ---------- Base slug probing ----------

    def has_base_slug_probed(self, base_slug: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM probed_base_slugs WHERE base_slug = ?",
                (base_slug,),
            ).fetchone()
            return row is not None

    def mark_base_slug_probed(self, base_slug: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO probed_base_slugs (base_slug) VALUES (?)",
                (base_slug,),
            )

    # ---------- Discovered seeds ----------

    def add_discovered_seed(self, seed: str) -> None:
        if not seed:
            return

        seed = seed.strip().lstrip("@")

        if not seed or seed.startswith("#") or len(seed) < 3:
            return

        canonical: str | None = None

        if "t.me" in seed or "telegram.me" in seed:
            m = TELEGRAM_URL_RE.match(seed)
            if m:
                canonical = m.group(1)
        elif "telegra.ph" in seed:
            canonical = MediaScraper.extract_base_slug(seed)

        if not canonical:
            canonical = MediaScraper.extract_base_slug(seed) or seed

        canonical = canonical.strip().lstrip("@")

        if not canonical or len(canonical) < 3:
            return

        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO discovered_seeds (seed) VALUES (?)",
                    (canonical,),
                )
        except sqlite3.IntegrityError:
            return

        try:
            with open(self.discovered_seeds_file, "a", encoding="utf-8") as f:
                f.write(canonical + "\n")
            logger.info(f"Discovered new seed: {canonical}")
        except Exception as exc:
            logger.warning(f"Could not write discovered seed to file: {exc}")

    def clean_discovered_seeds(self) -> int:
        """Deduplicate and canonicalize discovered_seeds table and file."""
        if not self.discovered_seeds_file.exists():
            return 0

        try:
            lines = self.discovered_seeds_file.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            logger.warning(f"Could not read discovered seeds file for cleanup: {exc}")
            return 0

        canonical_seeds: list[str] = []
        seen: set[str] = set()

        for line in lines:
            s = line.strip().lstrip("@")
            if not s or s.startswith("#") or len(s) < 3:
                continue

            c: str | None = None
            if "t.me" in s or "telegram.me" in s:
                m = TELEGRAM_URL_RE.match(s)
                if m:
                    c = m.group(1)
            elif "telegra.ph" in s:
                c = MediaScraper.extract_base_slug(s)

            if not c:
                c = MediaScraper.extract_base_slug(s) or s

            c = c.strip().lstrip("@")
            if c and len(c) >= 3 and c not in seen:
                seen.add(c)
                canonical_seeds.append(c)

        try:
            with open(self.discovered_seeds_file, "w", encoding="utf-8") as f:
                f.writelines(s_item + "\n" for s_item in canonical_seeds)
        except Exception as exc:
            logger.warning(f"Could not write cleaned discovered seeds file: {exc}")

        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("DELETE FROM discovered_seeds")
                for s_item in canonical_seeds:
                    conn.execute(
                        "INSERT OR IGNORE INTO discovered_seeds (seed) VALUES (?)",
                        (s_item,),
                    )
        except Exception as exc:
            logger.warning(f"Could not update discovered_seeds database table: {exc}")

        logger.info(
            f"Cleaned discovered seeds: reduced to {len(canonical_seeds)} unique canonical seeds."
        )
        return len(canonical_seeds)

    # ---------- Telegram API progress ----------

    def get_channel_last_message_id(self, channel: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT last_message_id FROM api_channel_progress WHERE channel_username = ?",
                (channel.lower(),),
            ).fetchone()
            return (
                int(row["last_message_id"])
                if row and row["last_message_id"] is not None
                else 0
            )

    def update_channel_progress(self, channel: str, message_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_channel_progress (channel_username, last_message_id, updated_at)
                VALUES (?, ?, datetime('now'))
                """,
                (channel.lower(), int(message_id)),
            )


class MediaScraper:
    def __init__(self, config: ScraperConfig):
        self.cfg = config
        self.storage = Storage(
            path=config.db_path,
            discovered_seeds_file=config.discovered_seeds_file,
        )
        self.cfg.download_dir.mkdir(parents=True, exist_ok=True)

    # ------------------ HTTP helpers ------------------

    def make_client(self) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_connections=self.cfg.max_connections,
            max_keepalive_connections=self.cfg.max_connections,
        )
        timeout = httpx.Timeout(self.cfg.request_timeout, connect=10.0)

        return httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )

    def _headers(self) -> dict:
        return {"User-Agent": random.choice(USER_AGENTS)}

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        url: str,
        method: str = "GET",
        retries: int = 3,
        **kwargs,
    ) -> httpx.Response | None:
        last_exc: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    **kwargs,
                )

                if resp.status_code in {429, 500, 502, 503, 504}:
                    if attempt == retries:
                        return resp

                    await asyncio.sleep(min(30, 2**attempt + random.random()))
                    continue

                return resp

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc

                if attempt == retries:
                    raise

                await asyncio.sleep(min(30, 2**attempt + random.random()))

        if last_exc:
            raise last_exc

        return None

    async def _probe_url_exists(self, client: httpx.AsyncClient, url: str) -> bool:
        try:
            resp = await client.head(
                url,
                headers=self._headers(),
                timeout=self.cfg.request_timeout,
                follow_redirects=True,
            )

            # Some endpoints dislike HEAD; fall back to GET if needed.
            if resp.status_code == 405:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    timeout=self.cfg.request_timeout,
                    follow_redirects=True,
                )

            return resp.status_code == 200

        except Exception:
            return False

    # ------------------ File downloads ------------------

    def _extension_from_content_type(self, content_type: str) -> str | None:
        if not content_type:
            return None

        ct = content_type.split(";")[0].strip().lower()
        return MIME_EXTENSIONS.get(ct)

    def _extension_from_url(self, url: str) -> str:
        try:
            path = urllib.parse.urlparse(url).path
            ext = Path(path).suffix.lower()

            if ext and len(ext) <= 6:
                return ext
        except Exception:
            pass

        return ""

    def _looks_like_media(self, content_type: str, ext: str) -> bool:
        content_type = (content_type or "").lower()
        ext = (ext or "").lower()

        if content_type.startswith("text/html"):
            return False

        if content_type.startswith(("image/", "video/")):
            return True

        return ext in MEDIA_EXTENSIONS

    async def download_file(self, client: httpx.AsyncClient, url: str) -> Path | None:
        if not url or not str(url).lower().startswith(("http://", "https://")):
            return None

        temp_path = self.cfg.download_dir / f"temp_{uuid.uuid4().hex}"

        try:
            async with client.stream(
                "GET",
                url,
                headers=self._headers(),
                follow_redirects=True,
                timeout=self.cfg.request_timeout,
            ) as resp:
                if resp.status_code != 200:
                    logger.debug(f"Skipping {url}: HTTP {resp.status_code}")
                    return None

                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) < self.cfg.min_size_bytes:
                            return None
                    except ValueError:
                        pass

                content_type = (
                    resp.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                ext = self._extension_from_content_type(
                    content_type
                ) or self._extension_from_url(url)

                if not self._looks_like_media(content_type, ext):
                    return None

                sha256 = hashlib.sha256()
                bytes_written = 0

                with temp_path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=32768):
                        fh.write(chunk)
                        sha256.update(chunk)
                        bytes_written += len(chunk)

            if bytes_written < self.cfg.min_size_bytes:
                return None

            file_hash = sha256.hexdigest()

            if self.storage.has_hash_downloaded(file_hash):
                return None

            final_path = self.cfg.download_dir / f"{file_hash}{ext or '.bin'}"

            if final_path.exists():
                self.storage.record_downloaded_hash(file_hash, final_path)
                return final_path

            temp_path.replace(final_path)
            self.storage.record_downloaded_hash(file_hash, final_path)

            logger.info(f"Downloaded: {final_path.name} ({bytes_written // 1024} KB)")
            return final_path

        except Exception as exc:
            logger.debug(f"Download failed for {url}: {exc}")
            return None

        finally:
            temp_path.unlink(missing_ok=True)

    async def download_many(
        self, client: httpx.AsyncClient, urls: Iterable[str]
    ) -> None:
        unique_urls: list[str] = []
        seen: set[str] = set()

        for url in urls:
            if url and url not in seen:
                seen.add(url)
                unique_urls.append(url)

        if not unique_urls:
            return

        if (
            self.cfg.max_downloads_per_page > 0
            and len(unique_urls) > self.cfg.max_downloads_per_page
        ):
            logger.warning(
                f"Limiting downloads from page to {self.cfg.max_downloads_per_page} "
                f"of {len(unique_urls)} candidates."
            )
            unique_urls = unique_urls[: self.cfg.max_downloads_per_page]

        sem = asyncio.Semaphore(self.cfg.max_connections)

        async def _download(url: str):
            async with sem:
                await self.download_file(client, url)

        results = await asyncio.gather(
            *(_download(u) for u in unique_urls),
            return_exceptions=True,
        )

        for url, result in zip(unique_urls, results):
            if isinstance(result, Exception):
                logger.debug(f"Download task failed for {url}: {result}")

    # ------------------ Slug / URL helpers ------------------

    @staticmethod
    def ensure_scheme(url: str) -> str:
        url = url.strip()

        if not url:
            return ""

        if "://" not in url:
            return "https://" + url

        return url

    @staticmethod
    def normalize_telegraph_url(url: str) -> str | None:
        url = MediaScraper.ensure_scheme(url)

        if not url:
            return None

        parsed = urllib.parse.urlparse(url)

        if not parsed.netloc.lower().endswith("telegra.ph"):
            return None

        path = parsed.path.rstrip("/")

        if not path or path == "/":
            return None

        if path.lower() in {"/a", "/edit", "/api"}:
            return None

        if not re.fullmatch(r"/[A-Za-z0-9_\-]+", path):
            return None

        return f"https://telegra.ph{path}"

    @staticmethod
    def normalize_telegram_url(url: str) -> str | None:
        url = MediaScraper.ensure_scheme(url)

        if not url:
            return None

        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower().split(":")[0]

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

        username = parts[0].split("?")[0].strip().lstrip("@")

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
                if val:
                    q_pairs.append(f"{param}={val}")
            if q_pairs:
                return f"{base}?{'&'.join(q_pairs)}"

        return base

    @staticmethod
    def extract_base_slug(url: str) -> str | None:
        if not url:
            return None

        url = url.strip()

        if "telegra.ph" in url:
            normalized = MediaScraper.normalize_telegraph_url(url)
            if not normalized:
                return None
            path = urllib.parse.urlparse(normalized).path.strip("/")
        else:
            path = url.strip("/")

        if not path:
            return None

        m = TELEGRAPH_SLUG_DATE_RE.match(path)
        if m:
            return m.group("base")

        m_month = TELEGRAPH_SLUG_MONTH_RE.match(path)
        if m_month:
            return m_month.group("base")

        return path

    @staticmethod
    def slugify_telegraph_candidates(text: str) -> list[str]:
        if not text:
            return []
        text = text.strip().lstrip("@")
        candidates: list[str] = []

        # 1. Hyphenated slug (converting underscores and spaces to hyphens)
        ph_hyphen = re.sub(
            r"-+",
            "-",
            re.sub(r"[^A-Za-z0-9_\-]+", "-", text).replace("_", "-").strip("-"),
        )
        if ph_hyphen and len(ph_hyphen) >= 3 and ph_hyphen not in candidates:
            candidates.append(ph_hyphen)

        # 2. Preserving underscores if present
        ph_underscore = re.sub(
            r"_+", "_", re.sub(r"[^A-Za-z0-9_\-]+", "_", text).strip("-_")
        )
        if (
            ph_underscore
            and len(ph_underscore) >= 3
            and ph_underscore not in candidates
        ):
            candidates.append(ph_underscore)

        # 3. Compact without separators
        ph_compact = re.sub(r"[^A-Za-z0-9]+", "", text)
        if ph_compact and len(ph_compact) >= 3 and ph_compact not in candidates:
            candidates.append(ph_compact)

        return candidates

    @staticmethod
    def slugify_telegram_candidates(text: str) -> list[str]:
        if not text:
            return []
        text = text.strip().lstrip("@")
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

        return [c for c in candidates if c.lower() not in TELEGRAM_RESERVED]

    @staticmethod
    def slugify_telegraph(text: str) -> str | None:
        candidates = MediaScraper.slugify_telegraph_candidates(text)
        return candidates[0] if candidates else None

    @staticmethod
    def slugify_telegram(text: str) -> str | None:
        candidates = MediaScraper.slugify_telegram_candidates(text)
        return candidates[0] if candidates else None

    # ------------------ Queue helpers ------------------

    def enqueue_url(self, url: str, url_type: str, depth: int) -> bool:
        if not url:
            return False

        if self.storage.has_url_crawled(url):
            return False

        added = self.storage.enqueue(url, url_type, depth)

        if added:
            logger.debug(f"Queued {url_type} URL at depth {depth}: {url}")

        return added

    async def add_seed(
        self, client: httpx.AsyncClient, seed: str, depth: int = 0
    ) -> None:
        seed = seed.strip()

        if not seed or seed.startswith("#"):
            return

        # Direct Telegraph URL
        if "telegra.ph" in seed:
            url = self.normalize_telegraph_url(seed)

            if url:
                self.enqueue_url(url, "telegraph", depth)
                base = self.extract_base_slug(url)

                if base:
                    # Cross-pollinate to Telegram
                    for tg_cand in self.slugify_telegram_candidates(base):
                        self.enqueue_url(f"https://t.me/s/{tg_cand}", "telegram", depth)
                        self.storage.add_discovered_seed(tg_cand)

                    if self.cfg.probe_telegraph_dates:
                        discovered = await self.probe_slug_all_dates(client, base)

                        for discovered_url in discovered:
                            self.enqueue_url(discovered_url, "telegraph", depth)

            return

        # Direct Telegram URL
        if any(dom in seed for dom in TELEGRAM_DOMAINS):
            url = self.normalize_telegram_url(seed)

            if url:
                self.enqueue_url(url, "telegram", depth)
                m = TELEGRAM_URL_RE.match(url)

                if m:
                    username = m.group(1)

                    # Cross-pollinate to Telegraph
                    for ph_cand in self.slugify_telegraph_candidates(username):
                        self.enqueue_url(
                            f"https://telegra.ph/{ph_cand}", "telegraph", depth
                        )
                        self.storage.add_discovered_seed(ph_cand)

                        if self.cfg.probe_telegraph_dates:
                            discovered = await self.probe_slug_all_dates(
                                client, ph_cand
                            )

                            for discovered_url in discovered:
                                self.enqueue_url(discovered_url, "telegraph", depth)

            return

        # Raw username or phrase: Cross-pollinate both Telegram and Telegraph candidates
        raw = seed.lstrip("@").strip()

        if not raw:
            return

        # 1. Telegram candidates
        for tg_slug in self.slugify_telegram_candidates(raw):
            self.enqueue_url(f"https://t.me/s/{tg_slug}", "telegram", depth)
            self.storage.add_discovered_seed(tg_slug)

        # 2. Telegraph candidates
        for ph_slug in self.slugify_telegraph_candidates(raw):
            self.enqueue_url(f"https://telegra.ph/{ph_slug}", "telegraph", depth)
            self.storage.add_discovered_seed(ph_slug)

            if self.cfg.probe_telegraph_dates:
                discovered = await self.probe_slug_all_dates(client, ph_slug)

                for discovered_url in discovered:
                    self.enqueue_url(discovered_url, "telegraph", depth)

    # ------------------ Discovery ------------------

    @staticmethod
    def _unwrap_ddg(href: str) -> str:
        if href.startswith("//"):
            href = "https:" + href

        parsed = urllib.parse.urlparse(href)

        if parsed.netloc.endswith("duckduckgo.com") and "uddg=" in parsed.query:
            qs = urllib.parse.parse_qs(parsed.query)
            return qs.get("uddg", [href])[0]

        return href

    async def discover_urls_duckduckgo(
        self,
        client: httpx.AsyncClient,
        keyword: str,
    ) -> tuple[set[str], set[str]]:
        telegraph_urls: set[str] = set()
        telegram_urls: set[str] = set()

        queries = [
            f"site:telegra.ph {keyword}",
            f"site:t.me/s/ {keyword}",
        ]

        for query in queries:
            try:
                logger.info(f"DuckDuckGo search: {query}")

                resp = await self._fetch(
                    client,
                    "https://html.duckduckgo.com/html/",
                    method="POST",
                    data={"q": query},
                )

                if resp is None or resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                for a in soup.find_all("a", href=True):
                    target = self._unwrap_ddg(a["href"])

                    tg = self.normalize_telegraph_url(target)

                    if tg:
                        telegraph_urls.add(tg)
                        continue

                    tel = self.normalize_telegram_url(target)

                    if tel:
                        telegram_urls.add(tel)

            except Exception as exc:
                logger.warning(f"DuckDuckGo discovery failed for '{query}': {exc}")

            await asyncio.sleep(self.cfg.delay())

        return telegraph_urls, telegram_urls

    async def discover_urls_yandex(
        self,
        client: httpx.AsyncClient,
        keyword: str,
    ) -> tuple[set[str], set[str]]:
        telegraph_urls: set[str] = set()
        telegram_urls: set[str] = set()

        queries = [
            f"site:telegra.ph {keyword}",
            f"site:t.me/s/ {keyword}",
        ]

        for query in queries:
            url = f"https://yandex.com/search/?text={urllib.parse.quote_plus(query)}"

            try:
                logger.info(f"Yandex search: {query}")

                resp = await self._fetch(client, url)

                if resp is None or resp.status_code != 200:
                    continue

                text = resp.text.lower()

                if any(
                    marker in text for marker in ("smartcaptcha", "captcha", "robot")
                ):
                    logger.warning("Yandex captcha/bot check triggered. Skipping.")
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                for a in soup.find_all("a", href=True):
                    target = a["href"]

                    tg = self.normalize_telegraph_url(target)

                    if tg:
                        telegraph_urls.add(tg)
                        continue

                    tel = self.normalize_telegram_url(target)

                    if tel:
                        telegram_urls.add(tel)

            except Exception as exc:
                logger.warning(f"Yandex discovery failed for '{query}': {exc}")

            await asyncio.sleep(self.cfg.delay())

        return telegraph_urls, telegram_urls

    async def run_discovery(self, keywords: list[str]) -> tuple[set[str], set[str]]:
        discovered_telegraph: set[str] = set()
        discovered_telegram: set[str] = set()

        engines = set(self.cfg.search_engines)

        async with self.make_client() as client:
            for keyword in keywords:
                if "duckduckgo" in engines:
                    tg, tel = await self.discover_urls_duckduckgo(client, keyword)
                    discovered_telegraph.update(tg)
                    discovered_telegram.update(tel)

                if "yandex" in engines:
                    tg, tel = await self.discover_urls_yandex(client, keyword)
                    discovered_telegraph.update(tg)
                    discovered_telegram.update(tel)

        for url in discovered_telegraph:
            base = self.extract_base_slug(url)

            if base:
                self.storage.add_discovered_seed(base)

        for url in discovered_telegram:
            m = TELEGRAM_URL_RE.match(url)

            if m:
                self.storage.add_discovered_seed(m.group(1))
            else:
                self.storage.add_discovered_seed(url)

        return discovered_telegraph, discovered_telegram

    # ------------------ Telegraph crawling ------------------

    async def scrape_telegraph_page(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[list[str], set[str], set[str]]:
        media_urls: list[str] = []
        telegram_urls: set[str] = set()
        telegraph_urls: set[str] = set()

        try:
            resp = await self._fetch(client, url)

            if resp is None or resp.status_code != 200:
                logger.warning(f"Failed to fetch Telegraph page: {url}")
                return media_urls, telegram_urls, telegraph_urls

            soup = BeautifulSoup(resp.text, "html.parser")

            # 1. Media tags
            for tag in soup.find_all(["img", "video", "source", "audio"]):
                src = tag.get("src") or tag.get("data-src")
                if src:
                    media_urls.append(urllib.parse.urljoin(url, src))

            # 2. Links in <a> tags
            for a in soup.find_all("a", href=True):
                href = urllib.parse.urljoin(url, a["href"])

                tg = self.normalize_telegraph_url(href)
                if tg and tg != url:
                    telegraph_urls.add(tg)
                    continue

                tel = self.normalize_telegram_url(href)
                if tel:
                    telegram_urls.add(tel)

            # 3. Telegram mentions and URLs in article text
            article_text = soup.get_text()
            for mention in TELEGRAM_MENTION_RE.findall(article_text):
                tel = self.normalize_telegram_url(f"https://t.me/{mention}")
                if tel:
                    telegram_urls.add(tel)

            for tg_match in TELEGRAM_URL_RE.finditer(article_text):
                tel = self.normalize_telegram_url(tg_match.group(0))
                if tel:
                    telegram_urls.add(tel)

            for ph_match in TELEGRAPH_URL_RE.finditer(article_text):
                tg = self.normalize_telegraph_url(ph_match.group(0))
                if tg and tg != url:
                    telegraph_urls.add(tg)

            # 4. Author handle in <address> or header
            for author_tag in soup.find_all(["address", "header"]):
                author_text = author_tag.get_text().strip()
                for cand in self.slugify_telegram_candidates(author_text):
                    tel = self.normalize_telegram_url(f"https://t.me/{cand}")
                    if tel:
                        telegram_urls.add(tel)

            # 5. Cross-pollinate Telegram candidates from this Telegraph page's own slug
            base_slug = self.extract_base_slug(url)
            if base_slug:
                for tg_cand in self.slugify_telegram_candidates(base_slug):
                    tel = self.normalize_telegram_url(f"https://t.me/{tg_cand}")
                    if tel:
                        telegram_urls.add(tel)

        except Exception as exc:
            logger.error(f"Error scraping Telegraph page {url}: {exc}")

        return media_urls, telegram_urls, telegraph_urls

    async def probe_telegraph_suffixes(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> list[str]:
        m = TELEGRAPH_URL_SUFFIX_RE.match(url)

        if not m:
            return []

        base = m.group("base_dated")
        start = int(m.group(2) or 1) + 1
        found: list[str] = []
        attempts = 0

        while True:
            if (
                self.cfg.max_suffix_probes > 0
                and attempts >= self.cfg.max_suffix_probes
            ):
                break

            probe_url = f"{base}-{start + attempts}"

            if await self._probe_url_exists(client, probe_url):
                found.append(probe_url)
                attempts += 1
                await asyncio.sleep(random.uniform(0.2, 0.8))
            else:
                break

        return found

    async def probe_slug_all_dates(
        self,
        client: httpx.AsyncClient,
        base_slug: str,
    ) -> list[str]:
        if not self.cfg.probe_telegraph_dates:
            return []

        if self.storage.has_base_slug_probed(base_slug):
            return []

        self.storage.mark_base_slug_probed(base_slug)

        logger.info(f"Probing all 366 date suffixes for base slug: {base_slug}")

        days_per_month = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        probe_urls: list[str] = []

        for month, days in enumerate(days_per_month, start=1):
            for day in range(1, days + 1):
                probe_urls.append(
                    f"https://telegra.ph/{base_slug}-{month:02d}-{day:02d}"
                )

        sem = asyncio.Semaphore(self.cfg.probe_date_concurrency)
        found: list[str] = []

        async def probe_one(probe_url: str) -> None:
            async with sem:
                if await self._probe_url_exists(client, probe_url):
                    found.append(probe_url)

        await asyncio.gather(*(probe_one(u) for u in probe_urls))

        return found

    async def process_telegraph_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        depth: int,
    ) -> None:
        logger.info(f"Scraping Telegraph: {url}")

        media_urls, telegram_urls, telegraph_urls = await self.scrape_telegraph_page(
            client,
            url,
        )

        for tel_url in telegram_urls:
            self.enqueue_url(tel_url, "telegram", depth + 1)

            m = TELEGRAM_URL_RE.match(tel_url)

            if m:
                self.storage.add_discovered_seed(m.group(1))

        for tg_url in telegraph_urls:
            self.enqueue_url(tg_url, "telegraph", depth + 1)

            base = self.extract_base_slug(tg_url)

            if base:
                self.storage.add_discovered_seed(base)

        if media_urls:
            logger.info(f"Found {len(media_urls)} media candidates on Telegraph page.")
            await self.download_many(client, media_urls)

        suffix_urls = await self.probe_telegraph_suffixes(client, url)

        for suffix_url in suffix_urls:
            self.enqueue_url(suffix_url, "telegraph", depth + 1)

        base = self.extract_base_slug(url)

        if base:
            # Cross-pollinate Telegram candidates from Telegraph slug
            for tg_cand in self.slugify_telegram_candidates(base):
                self.enqueue_url(f"https://t.me/s/{tg_cand}", "telegram", depth + 1)
                self.storage.add_discovered_seed(tg_cand)

            if self.cfg.probe_telegraph_dates:
                date_urls = await self.probe_slug_all_dates(client, base)

                for date_url in date_urls:
                    self.enqueue_url(date_url, "telegraph", depth + 1)

    # ------------------ Telegram web preview crawling ------------------

    async def scrape_telegram_web_preview(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> tuple[list[str], set[str], set[str], str | None]:
        media_urls: list[str] = []
        telegraph_urls: set[str] = set()
        discovered_telegram: set[str] = set()
        before_url: str | None = None

        try:
            resp = await self._fetch(client, url)

            if resp is None or resp.status_code != 200:
                logger.warning(f"Failed to fetch Telegram preview: {url}")
                return media_urls, telegraph_urls, discovered_telegram, before_url

            soup = BeautifulSoup(resp.text, "html.parser")
            messages = soup.select(".tgme_widget_message")

            # 1. Photos in message wraps and albums
            for wrap in soup.find_all(
                class_=re.compile(r"tgme_widget_message_photo_wrap")
            ):
                style = wrap.get("style", "")
                m = re.search(r"url\((['\"]?)(?P<url>[^'\")]+)\1\)", style)
                if m:
                    media_urls.append(urllib.parse.urljoin(url, m.group("url")))

            # 2. Photos in link previews
            for preview in soup.select(
                ".link_preview_image, .link_preview_right_image, .tgme_widget_message_link_preview"
            ):
                style = preview.get("style", "")
                m = re.search(r"url\((['\"]?)(?P<url>[^'\")]+)\1\)", style)
                if m:
                    media_urls.append(urllib.parse.urljoin(url, m.group("url")))

            # 3. Videos, round videos, and video sources
            for tag in soup.find_all(["video", "source"]):
                src = tag.get("src")
                if src:
                    media_urls.append(urllib.parse.urljoin(url, src))
                poster = tag.get("poster")
                if poster and not src:
                    media_urls.append(urllib.parse.urljoin(url, poster))

            # 4. Audios and voice clips
            for tag in soup.find_all(["audio"]):
                src = tag.get("src")
                if src:
                    media_urls.append(urllib.parse.urljoin(url, src))

            # 5. Documents / downloadable attachments
            for doc_wrap in soup.select("a.tgme_widget_message_document_wrap"):
                doc_href = doc_wrap.get("href")
                if doc_href:
                    media_urls.append(urllib.parse.urljoin(url, doc_href))

            # 6. Direct img tags (skipping avatar/profile icons)
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src")
                if not src:
                    continue

                parent_classes = (
                    " ".join(img.parent.get("class", [])) if img.parent else ""
                )
                classes = " ".join(img.get("class", []))
                if any(
                    avatar_cls in parent_classes or avatar_cls in classes
                    for avatar_cls in (
                        "tgme_widget_message_user_photo",
                        "tgme_page_photo_image",
                        "tgme_page_icon",
                    )
                ):
                    continue

                if (
                    "tgme_widget_message_photo" in classes
                    or "tgme_widget_message_sticker" in classes
                ):
                    media_urls.append(urllib.parse.urljoin(url, src))

            # 7. Telegraph & Telegram links inside messages / buttons / forwards
            for a in soup.find_all("a", href=True):
                href = urllib.parse.urljoin(url, a["href"])

                tg = self.normalize_telegraph_url(href)
                if tg:
                    telegraph_urls.add(tg)

                tel = self.normalize_telegram_url(href)
                if tel and tel != url:
                    discovered_telegram.add(tel)

            # 8. Text mentions (@username) and Telegraph links in message bodies
            for msg_text_elem in soup.select(".tgme_widget_message_text"):
                msg_text = msg_text_elem.get_text()
                for mention in TELEGRAM_MENTION_RE.findall(msg_text):
                    m_norm = self.normalize_telegram_url(f"https://t.me/{mention}")
                    if m_norm and m_norm != url:
                        discovered_telegram.add(m_norm)

                for ph_match in TELEGRAPH_URL_RE.finditer(msg_text):
                    ph_norm = self.normalize_telegraph_url(ph_match.group(0))
                    if ph_norm:
                        telegraph_urls.add(ph_norm)

            # 9. Cross-pollinate Telegraph candidate slugs from this Telegram channel username
            m_self = TELEGRAM_URL_RE.match(url)
            if m_self:
                ch_name = m_self.group(1)
                for ph_cand in self.slugify_telegraph_candidates(ch_name):
                    ph_norm = self.normalize_telegraph_url(
                        f"https://telegra.ph/{ph_cand}"
                    )
                    if ph_norm:
                        telegraph_urls.add(ph_norm)

            # 10. Pagination for older messages
            next_link = soup.find("a", class_=re.compile(r"(?:tgme|tme)_messages_more"))
            if next_link is None:
                next_link = soup.find("a", href=re.compile(r"[?&]before="))

            if next_link and next_link.get("href"):
                before_url = urllib.parse.urljoin(url, next_link["href"])
            elif messages:
                # Fallback: calculate minimum message ID on page to step back
                msg_ids: list[int] = []
                channel_name: str | None = None
                for msg in messages:
                    post_attr = msg.get("data-post", "")
                    if "/" in post_attr:
                        ch, mid = post_attr.split("/", 1)
                        if mid.isdigit():
                            msg_ids.append(int(mid))
                            channel_name = ch
                if msg_ids and channel_name and min(msg_ids) > 1:
                    before_url = f"https://t.me/s/{channel_name}?before={min(msg_ids)}"

        except Exception as exc:
            logger.error(f"Error scraping Telegram preview {url}: {exc}")

        return media_urls, telegraph_urls, discovered_telegram, before_url

    async def process_telegram_preview(
        self,
        client: httpx.AsyncClient,
        url: str,
        depth: int,
    ) -> None:
        logger.info(f"Scraping Telegram public preview: {url}")

        (
            media_urls,
            telegraph_urls,
            discovered_telegram,
            before_url,
        ) = await self.scrape_telegram_web_preview(
            client,
            url,
        )

        for tg_url in telegraph_urls:
            self.enqueue_url(tg_url, "telegraph", depth + 1)

            base = self.extract_base_slug(tg_url)

            if base:
                self.storage.add_discovered_seed(base)

        for tel_url in discovered_telegram:
            self.enqueue_url(tel_url, "telegram", depth + 1)

            m = TELEGRAM_URL_RE.match(tel_url)

            if m:
                self.storage.add_discovered_seed(m.group(1))

        # Cross-pollinate: If date probing is enabled, probe dates for discovered telegraph slugs and this channel
        m_self = TELEGRAM_URL_RE.match(url)
        if m_self and self.cfg.probe_telegraph_dates:
            ch_name = m_self.group(1)
            for ph_cand in self.slugify_telegraph_candidates(ch_name):
                date_urls = await self.probe_slug_all_dates(client, ph_cand)
                for date_url in date_urls:
                    self.enqueue_url(date_url, "telegraph", depth + 1)

        if media_urls:
            logger.info(
                f"Found {len(media_urls)} media candidates in Telegram preview."
            )
            await self.download_many(client, media_urls)

        if before_url:
            normalized_before = self.normalize_telegram_url(before_url)

            if normalized_before:
                # Keep same depth for channel pagination so max-depth limits inter-channel hops
                self.enqueue_url(normalized_before, "telegram", depth)

    # ------------------ Telegram API scraping ------------------

    def _extract_chat_username(self, target: str) -> str | None:
        target = target.strip()

        if not target:
            return None

        if "t.me" in target or "telegram.me" in target:
            normalized = self.normalize_telegram_url(target)

            if normalized:
                m = TELEGRAM_URL_RE.match(normalized)

                if m:
                    return m.group(1)

        raw = target.lstrip("@").split("/")[-1].split("?")[0].strip()

        if TELEGRAM_USERNAME_RE.match(raw):
            return raw

        return self.slugify_telegram(target)

    def _message_has_media(self, message) -> bool:
        try:
            if getattr(message, "photo", None):
                return True

            if getattr(message, "video", None):
                return True

            doc = getattr(message, "document", None)

            if doc and getattr(doc, "mime_type", ""):
                return doc.mime_type.startswith(("image/", "video/"))
        except Exception:
            return False

        return False

    async def download_telethon_media(self, client, message) -> Path | None:
        doc = getattr(message, "document", None)

        if doc and getattr(doc, "size", 0) and int(doc.size) < self.cfg.min_size_bytes:
            return None

        temp_path = self.cfg.download_dir / f"temp_api_{uuid.uuid4().hex}"

        try:
            downloaded = await client.download_media(message, file=str(temp_path))

            if not downloaded:
                return None

            downloaded_path = not (Path(downloaded))

            if not (not downloaded_path).exists():
                return None

            actual_size = (not downloaded_path).stat().st_size

            if actual_size < self.cfg.min_size_bytes:
                (not downloaded_path).unlink(missing_ok=True)
                return None

            sha256 = hashlib.sha256()

            with (not downloaded_path).open("rb") as fh:
                while True:
                    chunk = fh.read(32768)

                    if not chunk:
                        break

                    sha256.update(chunk)

            file_hash = sha256.hexdigest()

            if self.storage.has_hash_downloaded(file_hash):
                (not downloaded_path).unlink(missing_ok=True)
                return None

            ext = (not downloaded_path).suffix.lower()

            if not ext:
                mime = getattr(doc, "mime_type", "") or ""
                ext = self._extension_from_content_type(mime) or ".bin"

            final_path = self.cfg.download_dir / f"{file_hash}{ext}"

            if final_path.exists():
                (not downloaded_path).unlink(missing_ok=True)
                self.storage.record_downloaded_hash(file_hash, final_path)
                return final_path

            (not downloaded_path).replace(final_path)
            self.storage.record_downloaded_hash(file_hash, final_path)

            logger.info(
                f"Downloaded via API: {final_path.name} ({actual_size // 1024} KB)"
            )
            return final_path

        except Exception as exc:
            logger.error(f"Error downloading Telethon media: {exc}")
            return None

        finally:
            temp_path.unlink(missing_ok=True)

    async def run_api_scraper(self, target_chats: list[str]) -> bool:
        """
        Run Telegram API scraping via Telethon if configured.
        Returns True if successful, or False if skipped/failed (triggering fallback).
        """
        api_cfg = self.cfg.telegram_api or {}

        api_id = api_cfg.get("api_id")
        api_hash = api_cfg.get("api_hash")
        phone = api_cfg.get("phone")
        session_name = api_cfg.get("session_name", "telegram_media_scraper")

        if not api_id or not api_hash:
            logger.info(
                "Telegram API credentials not configured. Using public web scraping."
            )
            return False

        try:
            api_id = int(api_id)
        except Exception:
            logger.warning(
                "telegram_api.api_id must be an integer. Falling back to public web scraping."
            )
            return False

        try:
            from telethon import TelegramClient
        except ImportError:
            logger.warning(
                "Telethon package is not installed. Falling back to public web scraping."
            )
            return False

        client: TelegramClient | None = None

        try:
            client = TelegramClient(session_name, api_id, str(api_hash))
            await client.start(
                phone=phone
                if phone
                else lambda: input("Please enter your phone number: "),
                code_callback=lambda: input("Please enter the Telegram login code: "),
                password=lambda: getpass.getpass(
                    "Please enter your Telegram 2FA password: "
                ),
            )

            if not await client.is_user_authorized():
                logger.warning(
                    "Telegram API client is not authorized. Falling back to public web scraping."
                )
                return False

            logger.info("Telegram API authorized successfully.")

            discovered_telegraphs: set[str] = set()

            for target in target_chats:
                username = self._extract_chat_username(target)

                if not username:
                    logger.warning(f"Could not parse Telegram target: {target}")
                    continue

                logger.info(f"Scraping channel via API: {username}")

                try:
                    entity = await client.get_entity(username)
                    last_scraped_msg_id = self.storage.get_channel_last_message_id(
                        username
                    )
                    max_msg_id = last_scraped_msg_id

                    logger.info(f"Starting from message ID > {last_scraped_msg_id}")

                    async for message in client.iter_messages(
                        entity,
                        min_id=last_scraped_msg_id,
                        reverse=True,
                    ):
                        max_msg_id = max(max_msg_id, message.id)

                        if self._message_has_media(message):
                            await self.download_telethon_media(client, message)

                        text = getattr(message, "message", "") or ""

                        for link in TELEGRAPH_URL_RE.findall(text):
                            normalized = self.normalize_telegraph_url(link)

                            if not normalized:
                                continue

                            discovered_telegraphs.add(normalized)

                            base = self.extract_base_slug(normalized)

                            if base:
                                self.storage.add_discovered_seed(base)

                            self.enqueue_url(normalized, "telegraph", 0)

                    self.storage.update_channel_progress(username, max_msg_id)

                    logger.info(
                        f"Finished API scrape for {username}. "
                        f"Saved progress to message ID {max_msg_id}."
                    )

                except Exception as exc:
                    logger.error(f"Error scraping channel {username} via API: {exc}")

            if discovered_telegraphs:
                logger.info(
                    f"Discovered {len(discovered_telegraphs)} Telegraph links via API. "
                    "They have been saved and queued for the web crawler."
                )

            return True

        except Exception as exc:
            logger.warning(
                f"Telegram API scraping failed ({exc}). Falling back to public web scraping."
            )
            return False

        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    # ------------------ Main crawler loop ------------------

    async def run_web_crawler(self, seeds: list[str]) -> None:
        async with self.make_client() as client:
            for seed in seeds:
                await self.add_seed(client, seed, depth=0)

            while True:
                depth = self.storage.get_min_pending_depth()

                if depth is None:
                    break

                if depth >= self.cfg.max_depth:
                    logger.info(
                        f"Reached max crawl depth ({self.cfg.max_web_depth}). "
                        "Remaining queue is preserved."
                    )
                    break

                batch = self.storage.get_pending_batch(depth, limit=200)

                if not batch:
                    self.storage.clear_queue_at_depth(depth)
                    continue

                logger.info(
                    f"--- Depth {depth} | Processing {len(batch)} queued URLs ---"
                )

                for url, url_type in batch:
                    if self.storage.has_url_crawled(url):
                        self.storage.remove_from_queue(url)
                        continue

                    try:
                        if url_type == "telegram":
                            await self.process_telegram_preview(client, url, depth)
                        else:
                            await self.process_telegraph_page(client, url, depth)

                    except asyncio.CancelledError:
                        logger.warning(
                            f"Interrupted while processing {url}. "
                            "Leaving it pending for next run."
                        )
                        raise

                    except Exception:
                        logger.exception(f"Failed processing {url_type} URL: {url}")

                        # This prevents a bad URL from poisoning the queue forever.
                        # If you want failed URLs to retry next run, comment these two lines.
                        self.storage.mark_url_crawled(url)
                        self.storage.remove_from_queue(url)

                    else:
                        self.storage.mark_url_crawled(url)
                        self.storage.remove_from_queue(url)
                        await asyncio.sleep(self.cfg.delay())

        logger.info("Web crawler run finished.")

    async def run(self, seeds: list[str], use_api: bool = True) -> None:
        seeds = [s.strip() for s in seeds if s and s.strip()]

        api_configured = bool(
            self.cfg.telegram_api.get("api_id")
            and self.cfg.telegram_api.get("api_hash")
        )

        if use_api and api_configured:
            api_targets = [s for s in seeds if "telegra.ph" not in s.lower()]

            if api_targets:
                logger.info(
                    "Telegram API credentials detected. Attempting API scraper..."
                )
                api_success = await self.run_api_scraper(api_targets)
                if not api_success:
                    logger.info(
                        "Proceeding with public web scraper fallback for all seeds."
                    )
        else:
            logger.info(
                "Telegram API credentials not configured. Falling back to public web scraping (no login required)."
            )

        logger.info("Starting web crawler engine...")
        await self.run_web_crawler(seeds)


def load_seed_files(paths: Iterable[Path]) -> list[str]:
    seeds: list[str] = []

    for path in paths:
        path = Path(path)

        if not path.exists():
            continue

        try:
            lines = path.read_text(encoding="utf-8").splitlines()

            loaded = [
                line.strip()
                for line in lines
                if line.strip() and not line.strip().startswith("#")
            ]

            seeds.extend(loaded)

            logger.info(f"Loaded {len(loaded)} seeds from {path}")
        except Exception as exc:
            logger.error(f"Error reading seed file {path}: {exc}")

    return seeds


async def async_main() -> None:
    parser = argparse.ArgumentParser(
        description="Telegram & Telegra.ph media scraper with unlimited depth and resume support"
    )

    parser.add_argument(
        "extra_seeds",
        nargs="*",
        help="Direct URLs, usernames, or phrases to crawl.",
    )

    parser.add_argument(
        "--seeds",
        default="seeds.txt",
        type=Path,
        help="Path to seeds file.",
    )

    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="Path to config file.",
    )

    parser.add_argument(
        "--db",
        type=Path,
        help="Override SQLite database path.",
    )

    parser.add_argument(
        "--download-dir",
        type=Path,
        help="Override download directory.",
    )

    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip Telegram API mode.",
    )

    parser.add_argument(
        "--no-discovered",
        action="store_true",
        help="Do not load discovered_seeds.txt on startup.",
    )

    parser.add_argument(
        "--min-size",
        type=int,
        help="Minimum file size in KB.",
    )

    parser.add_argument(
        "--max-depth",
        type=int,
        help="Override max crawl depth. Use -1 for infinite.",
    )

    parser.add_argument(
        "--discover",
        action="store_true",
        help="Run search-engine discovery.",
    )

    parser.add_argument(
        "--keywords",
        help="Comma-separated keywords for discovery.",
    )

    parser.add_argument(
        "--reset-queue",
        action="store_true",
        help="Clear crawl queue before starting.",
    )

    parser.add_argument(
        "--probe-dates",
        action="store_true",
        help="Enable aggressive Telegraph date probing.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    cfg = ScraperConfig.load(args.config)

    if args.db:
        cfg.db_path = args.db

    if args.download_dir:
        cfg.download_dir = args.download_dir

    if args.min_size is not None:
        cfg.min_size_kb = max(0, args.min_size)
        logger.info(f"Overridden min size filter: {cfg.min_size_kb} KB")

    if args.max_depth is not None:
        cfg.max_web_depth = args.max_depth

        if cfg.max_web_depth < 0:
            logger.info("Overridden max depth: infinite")
        else:
            logger.info(f"Overridden max depth: {cfg.max_web_depth}")

    if args.probe_dates:
        cfg.probe_telegraph_dates = True
        logger.info("Enabled aggressive Telegraph date probing.")

    scraper = MediaScraper(cfg)

    if args.reset_queue:
        scraper.storage.clear_queue()
        logger.info("Cleared crawl queue.")

    seed_paths = [args.seeds]

    if cfg.include_discovered and not args.no_discovered:
        seed_paths.append(cfg.discovered_seeds_file)

    seeds = load_seed_files(seed_paths)

    if args.extra_seeds:
        seeds.extend(args.extra_seeds)

    if args.discover or cfg.discovery_enabled:
        keywords = list(cfg.keywords)

        if args.keywords:
            keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

        logger.info(f"Running discovery for keywords: {keywords}")

        discovered_telegraph, discovered_telegram = await scraper.run_discovery(
            keywords
        )

        discovered = sorted(discovered_telegraph | discovered_telegram)
        seeds.extend(discovered)

        logger.info(f"Discovery added {len(discovered)} seeds.")

    # Deduplicate while preserving order
    unique_seeds: list[str] = []
    seen: set[str] = set()

    for seed in seeds:
        if seed not in seen:
            seen.add(seed)
            unique_seeds.append(seed)

    pending_depth = scraper.storage.get_min_pending_depth()

    if not unique_seeds and pending_depth is None:
        logger.warning(
            "No seeds found and no pending crawl queue. "
            "Provide seeds via file, CLI args, discovery, or previous discovered seeds."
        )
        sys.exit(0)

    if not unique_seeds and pending_depth is not None:
        logger.info("No new seeds provided, but pending crawl queue found. Resuming...")

    await scraper.run(unique_seeds, use_api=not args.no_api)

    total = scraper.storage.count_downloaded_hashes()

    logger.info("=" * 60)
    logger.info("Scraping run completed.")
    logger.info(f"Total unique media records in database: {total}")
    logger.info(f"Downloads directory: {scraper.cfg.download_dir.resolve()}")
    logger.info(f"Discovered seeds file: {scraper.cfg.discovered_seeds_file.resolve()}")
    logger.info("=" * 60)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Scraper interrupted by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
