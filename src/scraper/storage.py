from __future__ import annotations

import contextlib
import logging
import sqlite3
import typing
from pathlib import Path

from scraper.urls import (
    canonicalize_seed,
    extract_base_slug,
    extract_chat_username,
    normalize_telegram_url,
    normalize_telegraph_url,
)

logger = logging.getLogger("scraper")


class Storage:
    """SQLite persistence layer for crawl state, downloads, and discovered seeds."""

    def __init__(self, path: Path, discovered_seeds_file: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.discovered_seeds_file = Path(discovered_seeds_file)
        self.discovered_seeds_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()
        self.clean_discovered_seeds()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn = conn
        return self._conn

    def __enter__(self) -> typing.Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                self._conn.execute("PRAGMA optimize;")
                self._conn.close()
            self._conn = None

    def _init_db(self) -> None:
        conn = self._get_conn()
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawled_urls (
                    url TEXT PRIMARY KEY COLLATE NOCASE,
                    crawled_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS downloaded_hashes (
                    file_hash TEXT PRIMARY KEY,
                    file_path TEXT,
                    downloaded_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS downloaded_media_urls (
                    url TEXT PRIMARY KEY COLLATE NOCASE,
                    file_hash TEXT,
                    downloaded_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS api_channel_progress (
                    channel_username TEXT PRIMARY KEY COLLATE NOCASE,
                    last_message_id INTEGER,
                    updated_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS probed_base_slugs (
                    base_slug TEXT PRIMARY KEY COLLATE NOCASE,
                    probed_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS discovered_seeds (
                    seed TEXT PRIMARY KEY COLLATE NOCASE,
                    discovered_at TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS crawl_queue (
                    url TEXT PRIMARY KEY COLLATE NOCASE,
                    url_type TEXT NOT NULL,
                    depth INTEGER NOT NULL DEFAULT 0,
                    queued_at TEXT DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_crawl_queue_depth
                ON crawl_queue(depth, queued_at);
                """
            )

    # ---------- Crawled URLs ----------

    def has_url_crawled(self, url: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM crawled_urls WHERE url = ?",
            (url,),
        ).fetchone()
        return row is not None

    def mark_url_crawled(self, url: str) -> None:
        if not url:
            return
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO crawled_urls (url) VALUES (?)",
                (url,),
            )

    def uncrawl_seed(self, seed: str) -> None:
        """Remove a seed URL or username from crawled_urls to allow re-crawling."""
        if not seed:
            return
        clean = seed.strip().lstrip("@")
        urls_to_delete = {seed, clean}

        norm_tg = normalize_telegram_url(seed)
        if norm_tg:
            urls_to_delete.add(norm_tg)
            urls_to_delete.add(norm_tg.lower())

        norm_ph = normalize_telegraph_url(seed)
        if norm_ph:
            urls_to_delete.add(norm_ph)
            urls_to_delete.add(norm_ph.lower())

        chat_user = extract_chat_username(seed)
        if chat_user:
            urls_to_delete.add(f"https://t.me/s/{chat_user}")
            urls_to_delete.add(f"https://t.me/s/{chat_user.lower()}")
            urls_to_delete.add(f"https://telegra.ph/{chat_user}")
            urls_to_delete.add(f"https://telegra.ph/{chat_user.lower()}")

        if "://" not in seed:
            urls_to_delete.add(f"https://t.me/s/{clean}")
            urls_to_delete.add(f"https://t.me/s/{clean.lower()}")
            urls_to_delete.add(f"https://telegra.ph/{clean}")
            urls_to_delete.add(f"https://telegra.ph/{clean.lower()}")

        base_slug = extract_base_slug(seed)

        conn = self._get_conn()
        with conn:
            for u in urls_to_delete:
                conn.execute("DELETE FROM crawled_urls WHERE url = ?", (u,))
                conn.execute("DELETE FROM crawl_queue WHERE url = ?", (u,))
            if chat_user:
                escaped_chat = (
                    chat_user.lower()
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                target_chat_base = f"https://t.me/s/{chat_user.lower()}"
                conn.execute(
                    "DELETE FROM crawled_urls WHERE url = ? OR url LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\'",
                    (
                        target_chat_base,
                        f"https://t.me/s/{escaped_chat}/%",
                        f"https://t.me/s/{escaped_chat}?%",
                    ),
                )
                conn.execute(
                    "DELETE FROM crawl_queue WHERE url = ? OR url LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\'",
                    (
                        target_chat_base,
                        f"https://t.me/s/{escaped_chat}/%",
                        f"https://t.me/s/{escaped_chat}?%",
                    ),
                )
                conn.execute(
                    "DELETE FROM api_channel_progress WHERE channel_username = ?",
                    (chat_user.lower(),),
                )
            if base_slug:
                escaped_base = (
                    base_slug.lower()
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                target_ph_base = f"https://telegra.ph/{base_slug.lower()}"
                conn.execute(
                    "DELETE FROM crawled_urls WHERE url = ? OR url LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\'",
                    (
                        target_ph_base,
                        f"https://telegra.ph/{escaped_base}-%",
                        f"https://telegra.ph/{escaped_base}/%",
                        f"https://telegra.ph/{escaped_base}?%",
                    ),
                )
                conn.execute(
                    "DELETE FROM crawl_queue WHERE url = ? OR url LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\'",
                    (
                        target_ph_base,
                        f"https://telegra.ph/{escaped_base}-%",
                        f"https://telegra.ph/{escaped_base}/%",
                        f"https://telegra.ph/{escaped_base}?%",
                    ),
                )
                conn.execute(
                    "DELETE FROM probed_base_slugs WHERE base_slug = ?",
                    (base_slug.strip().lower(),),
                )

    def clear_crawled_urls(self) -> None:
        """Clear all crawled URL records, API channel progress, and probed base slugs from database."""
        conn = self._get_conn()
        with conn:
            conn.execute("DELETE FROM crawled_urls")
            conn.execute("DELETE FROM api_channel_progress")
            conn.execute("DELETE FROM probed_base_slugs")

    # ---------- Downloaded hashes & URLs ----------

    def has_hash_downloaded(self, file_hash: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM downloaded_hashes WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()
        return row is not None

    def record_downloaded_hash(self, file_hash: str, file_path: Path) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO downloaded_hashes (file_hash, file_path) VALUES (?, ?)",
                (file_hash, str(file_path)),
            )

    def count_downloaded_hashes(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) AS c FROM downloaded_hashes").fetchone()
        return int(row["c"]) if row else 0

    def has_media_url_downloaded(self, url: str) -> bool:
        if not url:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM downloaded_media_urls WHERE url = ?",
            (url,),
        ).fetchone()
        return row is not None

    def record_downloaded_media_url(self, url: str, file_hash: str) -> None:
        if not url:
            return
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO downloaded_media_urls (url, file_hash) VALUES (?, ?)",
                (url, file_hash),
            )

    # ---------- Queue ----------

    def enqueue(self, url: str, url_type: str, depth: int) -> bool:
        if not url or not url_type:
            return False
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                """
                INSERT INTO crawl_queue (url, url_type, depth)
                VALUES (?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET depth = excluded.depth
                WHERE excluded.depth < crawl_queue.depth
                """,
                (url, url_type, depth),
            )
            return cur.rowcount > 0

    def remove_from_queue(self, url: str) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "DELETE FROM crawl_queue WHERE url = ?",
                (url,),
            )

    def get_min_pending_depth(self, max_depth: float = float("inf")) -> int | None:
        conn = self._get_conn()
        if max_depth < float("inf"):
            row = conn.execute(
                "SELECT MIN(depth) AS min_depth FROM crawl_queue WHERE depth <= ?",
                (int(max_depth),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT MIN(depth) AS min_depth FROM crawl_queue"
            ).fetchone()
        if row and row["min_depth"] is not None:
            return int(row["min_depth"])
        return None

    def get_pending_batch(self, depth: int, limit: int = 200) -> list[tuple[str, str]]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT url, url_type
            FROM crawl_queue
            WHERE depth = ?
            ORDER BY queued_at, rowid
            LIMIT ?
            """,
            (depth, limit),
        ).fetchall()
        return [(row["url"], row["url_type"]) for row in rows]

    def clear_queue(self) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute("DELETE FROM crawl_queue")

    def clear_queue_at_depth(self, depth: int) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute(
                "DELETE FROM crawl_queue WHERE depth = ?",
                (depth,),
            )

    # ---------- Base slug probing ----------

    def has_base_slug_probed(self, base_slug: str) -> bool:
        if not base_slug:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM probed_base_slugs WHERE base_slug = ?",
            (base_slug.strip().lower(),),
        ).fetchone()
        return row is not None

    def mark_base_slug_probed(self, base_slug: str) -> None:
        if not base_slug:
            return
        conn = self._get_conn()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO probed_base_slugs (base_slug) VALUES (?)",
                (base_slug.strip().lower(),),
            )

    # ---------- Discovered seeds ----------

    def add_discovered_seed(self, seed: str) -> None:
        if not seed:
            return

        canonical = canonicalize_seed(seed)
        if not canonical:
            return

        conn = self._get_conn()
        try:
            with conn:
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

    def get_discovered_seeds(self) -> list[str]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT seed FROM discovered_seeds ORDER BY discovered_at ASC"
            ).fetchall()
            return [r["seed"] for r in rows]
        except Exception:
            return []

    def clean_discovered_seeds(self) -> int:
        """Deduplicate and canonicalize discovered_seeds table and file."""
        seeds_from_db: list[str] = []
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT seed FROM discovered_seeds").fetchall()
            seeds_from_db = [r["seed"] for r in rows]
        except Exception:
            pass

        seeds_from_file: list[str] = []
        if self.discovered_seeds_file.exists():
            try:
                seeds_from_file = self.discovered_seeds_file.read_text(
                    encoding="utf-8"
                ).splitlines()
            except Exception as exc:
                logger.warning(
                    f"Could not read discovered seeds file for cleanup: {exc}"
                )

        all_raw = seeds_from_file + seeds_from_db
        if not all_raw:
            return 0

        canonical_seeds: list[str] = []
        seen_lower: set[str] = set()

        for line in all_raw:
            c = canonicalize_seed(line)
            if not c:
                continue

            c_lower = c.lower()
            if c_lower not in seen_lower:
                seen_lower.add(c_lower)
                canonical_seeds.append(c)

        try:
            with open(self.discovered_seeds_file, "w", encoding="utf-8") as f:
                f.writelines(s_item + "\n" for s_item in canonical_seeds)
        except Exception as exc:
            logger.warning(f"Could not write cleaned discovered seeds file: {exc}")

        try:
            with conn:
                conn.execute("DELETE FROM discovered_seeds")
                conn.executemany(
                    "INSERT OR IGNORE INTO discovered_seeds (seed) VALUES (?)",
                    [(s_item,) for s_item in canonical_seeds],
                )
        except Exception as exc:
            logger.warning(f"Could not update discovered_seeds database table: {exc}")

        return len(canonical_seeds)

    # ---------- Telegram API progress ----------

    def get_channel_last_message_id(self, channel: str) -> int:
        conn = self._get_conn()
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
        conn = self._get_conn()
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_channel_progress (channel_username, last_message_id, updated_at)
                VALUES (?, ?, datetime('now'))
                """,
                (channel.lower(), message_id),
            )
