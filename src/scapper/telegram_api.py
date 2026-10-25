from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import logging
import typing
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scapper.constants import (
    MEDIA_EXTENSIONS,
    TELEGRAM_MENTION_RE,
    TELEGRAM_RESERVED,
    TELEGRAM_URL_RE,
    TELEGRAPH_URL_RE,
)
from scapper.downloader import extension_from_content_type
from scapper.urls import (
    extract_base_slug,
    extract_chat_username,
    normalize_telegram_url,
    normalize_telegraph_url,
)
from scapper.utils import detect_media_type_from_bytes

if TYPE_CHECKING:
    from scapper.config import ScraperConfig
    from scapper.storage import Storage

logger = logging.getLogger("scraper")


def message_has_media(message: typing.Any) -> bool:
    """Check if a Telethon message contains downloadable media."""
    try:
        if getattr(message, "photo", None):
            return True

        if getattr(message, "video", None):
            return True

        doc = getattr(message, "document", None)

        if doc:
            mime = (getattr(doc, "mime_type", "") or "").lower()
            if mime.startswith(("image/", "video/", "audio/")):
                return True

            # Check uncompressed files sent as documents
            for attr in getattr(doc, "attributes", []):
                fn = getattr(attr, "file_name", "")
                if fn and Path(fn).suffix.lower() in MEDIA_EXTENSIONS:
                    return True
    except Exception:
        return False

    return False


async def download_telethon_media(
    client: typing.Any,
    message: typing.Any,
    config: ScraperConfig,
    storage: Storage,
) -> Path | None:
    """Download media from a Telethon Message object with FloodWait retry and deduplication."""
    doc = getattr(message, "document", None)

    if doc and getattr(doc, "size", 0) and int(doc.size) < config.min_size_bytes:
        return None

    config.download_dir.mkdir(parents=True, exist_ok=True)
    temp_path = config.download_dir / f"temp_api_{uuid.uuid4().hex}"
    downloaded: str | None = None

    try:
        for attempt in range(2):
            try:
                downloaded = await client.download_media(
                    message, file=str(temp_path)
                )
                break
            except Exception as exc:
                seconds = getattr(exc, "seconds", None)
                if seconds is not None and attempt == 0:
                    if seconds > 120:
                        logger.warning(
                            f"Telegram FloodWait too long ({seconds}s). Skipping media."
                        )
                        return None
                    logger.warning(
                        f"Telegram FloodWait encountered: waiting {seconds} seconds."
                    )
                    await asyncio.sleep(seconds + 1)
                    continue
                logger.error(f"Error downloading Telethon media: {exc}")
                return None

        if not downloaded:
            return None

        downloaded_path = Path(downloaded)

        if not downloaded_path.exists():
            return None

        actual_size = downloaded_path.stat().st_size

        if actual_size < config.min_size_bytes:
            downloaded_path.unlink(missing_ok=True)
            return None

        def _calc_sha256_and_header(p: Path) -> tuple[str, bytes]:
            s = hashlib.sha256()
            header = b""
            with p.open("rb") as fh:
                while chunk := fh.read(65536):
                    if not header:
                        header = chunk[:4096]
                    s.update(chunk)
            return s.hexdigest(), header

        file_hash, header_bytes = await asyncio.to_thread(
            _calc_sha256_and_header, downloaded_path
        )

        if storage.has_hash_downloaded(file_hash):
            downloaded_path.unlink(missing_ok=True)
            return None

        ext = downloaded_path.suffix.lower()

        if not ext:
            if getattr(message, "photo", None):
                ext = ".jpg"
            else:
                mime = getattr(doc, "mime_type", "") or ""
                ext = extension_from_content_type(mime) or ""

        if (not ext or ext == ".bin") and header_bytes:
            detected = detect_media_type_from_bytes(header_bytes)
            if detected:
                ext = detected[1]

        if not ext:
            ext = ".bin"

        final_path = config.download_dir / f"{file_hash}{ext}"

        if final_path.exists():
            downloaded_path.unlink(missing_ok=True)
            storage.record_downloaded_hash(file_hash, final_path)
            return final_path

        downloaded_path.replace(final_path)
        storage.record_downloaded_hash(file_hash, final_path)

        size_display = max(1, actual_size // 1024)
        logger.info(f"Downloaded via API: {final_path.name} ({size_display} KB)")
        return final_path

    except Exception as exc:
        logger.error(f"Error processing Telethon media: {exc}")
        return None

    finally:
        temp_path.unlink(missing_ok=True)
        if downloaded:
            Path(downloaded).unlink(missing_ok=True)


async def run_api_scraper(
    target_chats: list[str],
    config: ScraperConfig,
    storage: Storage,
    enqueue_fn: Callable[[str, str, int], bool],
    download_media_fn: Callable | None = None,
) -> bool:
    """
    Run Telegram API scraping via Telethon if configured.
    Returns True if successful, or False if skipped/failed (triggering fallback).
    """
    api_cfg = config.telegram_api or {}

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
    media_downloader = download_media_fn or (
        lambda cl, msg: download_telethon_media(cl, msg, config, storage)
    )

    try:
        client = TelegramClient(session_name, api_id, str(api_hash))
        api_client: typing.Any = client
        await api_client.start(
            phone=phone
            if phone
            else lambda: input("Please enter your phone number: "),
            code_callback=lambda: input("Please enter the Telegram login code: "),
            password=lambda: getpass.getpass(
                "Please enter your Telegram 2FA password: "
            ),
        )

        assert client is not None
        if not await client.is_user_authorized():
            logger.warning(
                "Telegram API client is not authorized. Falling back to public web scraping."
            )
            return False

        logger.info("Telegram API authorized successfully.")

        discovered_telegraphs: set[str] = set()

        for target in target_chats:
            username = extract_chat_username(target)

            if not username:
                logger.warning(f"Could not parse Telegram target: {target}")
                continue

            logger.info(f"Scraping channel via API: {username}")
            last_scraped_msg_id = storage.get_channel_last_message_id(username)
            max_msg_id = last_scraped_msg_id
            msg_count = 0

            try:
                entity = await api_client.get_entity(username)
                if isinstance(entity, list):
                    entity = entity[0]

                logger.info(f"Starting from message ID > {last_scraped_msg_id}")

                async for message in client.iter_messages(
                    entity,
                    min_id=last_scraped_msg_id,
                    reverse=True,
                ):
                    max_msg_id = max(max_msg_id, message.id)
                    msg_count += 1

                    if message_has_media(message):
                        await media_downloader(client, message)

                    text = getattr(message, "message", "") or ""

                    # Discover Telegraph links
                    for match in TELEGRAPH_URL_RE.finditer(text):
                        normalized = normalize_telegraph_url(match.group(0))
                        if not normalized:
                            continue

                        discovered_telegraphs.add(normalized)
                        base = extract_base_slug(normalized)
                        if base:
                            storage.add_discovered_seed(base)
                        enqueue_fn(normalized, "telegraph", 1)

                    # Discover Telegram channel mentions & URLs
                    for mention in TELEGRAM_MENTION_RE.findall(text):
                        m_lower = mention.lower()
                        if (
                            m_lower != username.lower()
                            and m_lower not in TELEGRAM_RESERVED
                        ):
                            storage.add_discovered_seed(mention)
                            enqueue_fn(f"https://t.me/s/{mention}", "telegram", 1)

                    for tg_match in TELEGRAM_URL_RE.finditer(text):
                        cand_url = normalize_telegram_url(tg_match.group(0))
                        if cand_url:
                            m_cand = TELEGRAM_URL_RE.match(cand_url)
                            if (
                                m_cand
                                and m_cand.group(1).lower() != username.lower()
                                and m_cand.group(1).lower() not in TELEGRAM_RESERVED
                            ):
                                storage.add_discovered_seed(m_cand.group(1))
                                enqueue_fn(
                                    f"https://t.me/s/{m_cand.group(1)}",
                                    "telegram",
                                    1,
                                )

                    if msg_count % 50 == 0:
                        storage.update_channel_progress(username, max_msg_id)

                logger.info(
                    f"Finished API scrape for {username}. "
                    f"Saved progress to message ID {max_msg_id}."
                )
                storage.mark_url_crawled(f"https://t.me/s/{username.lower()}")

            except Exception as exc:
                logger.error(f"Error scraping channel {username} via API: {exc}")

            finally:
                if max_msg_id > last_scraped_msg_id:
                    storage.update_channel_progress(username, max_msg_id)

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
            with contextlib.suppress(Exception):
                await typing.cast(typing.Any, client).disconnect()
