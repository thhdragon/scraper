from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from scapper.constants import MEDIA_EXTENSIONS, MIME_EXTENSIONS, USER_AGENTS
from scapper.utils import detect_media_type_from_bytes

if TYPE_CHECKING:
    from scapper.config import ScraperConfig
    from scapper.storage import Storage

logger = logging.getLogger("scraper")


def make_client(config: ScraperConfig) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient configured with connection limits and headers."""
    limits = httpx.Limits(
        max_connections=config.max_connections,
        max_keepalive_connections=config.max_connections,
    )
    timeout = httpx.Timeout(config.request_timeout, connect=10.0)

    return httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-Ua": '"Chromium";v="120", "Not?A_Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    )


async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    method: str = "GET",
    retries: int = 3,
    **kwargs,
) -> httpx.Response | None:
    """Fetch a URL with automatic retries for rate limits and server errors."""
    last_exc: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            resp = await client.request(
                method,
                url,
                **kwargs,
            )

            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                wait_time = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else min(30.0, 2**attempt + random.random())
                )
                if attempt == retries:
                    return resp

                await asyncio.sleep(wait_time)
                continue

            if resp.status_code in {500, 502, 503, 504}:
                if attempt == retries:
                    return resp

                await asyncio.sleep(min(30.0, 2**attempt + random.random()))
                continue

            return resp

        except httpx.TransportError as exc:
            last_exc = exc

            if attempt == retries:
                raise

            await asyncio.sleep(min(30.0, 2**attempt + random.random()))

    if last_exc:
        raise last_exc

    return None


async def probe_url_exists(
    client: httpx.AsyncClient,
    url: str,
    timeout: float = 20.0,
) -> bool:
    """Check if a remote URL exists using HEAD with fallback to GET."""
    try:
        resp = await client.head(
            url,
            timeout=timeout,
            follow_redirects=True,
        )

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            wait_time = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else 1.5 + random.uniform(0.5, 2.0)
            )
            await asyncio.sleep(wait_time)
            resp = await client.head(
                url,
                timeout=timeout,
                follow_redirects=True,
            )

        # Some endpoints dislike HEAD; fall back to GET if needed.
        if resp.status_code == 405:
            resp = await client.get(
                url,
                timeout=timeout,
                follow_redirects=True,
            )

        return resp.status_code == 200

    except Exception:
        return False


def extension_from_content_type(content_type: str) -> str | None:
    """Return standard file extension for a given MIME Content-Type header."""
    if not content_type:
        return None

    ct = content_type.split(";")[0].strip().lower()
    return MIME_EXTENSIONS.get(ct)


def extension_from_url(url: str) -> str:
    """Extract lowercase file extension from URL path."""
    try:
        path = urllib.parse.urlparse(url).path
        ext = Path(path).suffix.lower()

        if ext and len(ext) <= 6:
            return ext
    except Exception:
        pass

    return ""


def looks_like_media(content_type: str, ext: str) -> bool:
    """Check if Content-Type or file extension indicates media."""
    content_type = (content_type or "").lower()
    ext = (ext or "").lower()

    if content_type.startswith("text/html"):
        return False

    if content_type.startswith(("image/", "video/", "audio/")):
        return True

    return ext in MEDIA_EXTENSIONS


async def download_file(
    client: httpx.AsyncClient,
    url: str,
    config: ScraperConfig,
    storage: Storage,
) -> Path | None:
    """Stream download a media file, validate mime/magic bytes, and deduplicate by sha256."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None

    # Check media URL cache before downloading
    if storage.has_media_url_downloaded(url):
        return None

    temp_path = config.download_dir / f"temp_{uuid.uuid4().hex}"

    try:
        async with client.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=config.request_timeout,
        ) as resp:
            if resp.status_code != 200:
                logger.debug(f"Skipping {url}: HTTP {resp.status_code}")
                return None

            content_type = (
                resp.headers.get("content-type", "").split(";")[0].strip().lower()
            )
            if content_type.startswith(
                ("text/html", "application/json", "text/plain")
            ):
                storage.record_downloaded_media_url(url, "")
                return None

            content_length = resp.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) < config.min_size_bytes:
                        storage.record_downloaded_media_url(url, "")
                        return None
                except ValueError:
                    pass

            ext = extension_from_content_type(content_type) or extension_from_url(url)

            is_generic_or_file = (
                not ext
                or content_type
                in {"application/octet-stream", "binary/octet-stream", ""}
                or "/file/" in url
            )
            if not looks_like_media(content_type, ext) and not is_generic_or_file:
                storage.record_downloaded_media_url(url, "")
                return None

            sha256 = hashlib.sha256()
            bytes_written = 0
            first_chunk = b""

            with temp_path.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=32768):
                    if not first_chunk:
                        first_chunk = chunk
                    fh.write(chunk)
                    sha256.update(chunk)
                    bytes_written += len(chunk)

        if bytes_written < config.min_size_bytes:
            storage.record_downloaded_media_url(url, "")
            return None

        if (not ext or ext == ".bin") and first_chunk:
            detected = detect_media_type_from_bytes(first_chunk)
            if detected:
                ext = detected[1]
            elif not looks_like_media(content_type, ext):
                storage.record_downloaded_media_url(url, "")
                return None

        file_hash = sha256.hexdigest()

        # Record media URL as downloaded
        storage.record_downloaded_media_url(url, file_hash)

        if storage.has_hash_downloaded(file_hash):
            return None

        final_path = config.download_dir / f"{file_hash}{ext or '.bin'}"

        if final_path.exists():
            storage.record_downloaded_hash(file_hash, final_path)
            return final_path

        temp_path.replace(final_path)
        storage.record_downloaded_hash(file_hash, final_path)

        size_display = max(1, bytes_written // 1024)
        logger.info(f"Downloaded: {final_path.name} ({size_display} KB)")
        return final_path

    except Exception as exc:
        logger.debug(f"Download failed for {url}: {exc}")
        return None

    finally:
        temp_path.unlink(missing_ok=True)


async def download_many(
    client: httpx.AsyncClient,
    urls: Iterable[str],
    config: ScraperConfig,
    storage: Storage,
    download_fn: Callable[[httpx.AsyncClient, str], Awaitable[Path | None]] | None = None,
) -> None:
    """Concurrently download a batch of candidate media URLs."""
    unique_urls: list[str] = []
    seen: set[str] = set()

    for url in urls:
        if url and url not in seen:
            seen.add(url)
            unique_urls.append(url)

    if not unique_urls:
        return

    if (
        config.max_downloads_per_page > 0
        and len(unique_urls) > config.max_downloads_per_page
    ):
        logger.warning(
            f"Limiting downloads from page to {config.max_downloads_per_page} "
            f"of {len(unique_urls)} candidates."
        )
        unique_urls = unique_urls[: config.max_downloads_per_page]

    sem = asyncio.Semaphore(max(1, config.max_connections))
    dl_worker = download_fn or (lambda c, u: download_file(c, u, config, storage))

    async def _download(url: str):
        async with sem:
            await dl_worker(client, url)

    results = await asyncio.gather(
        *(_download(u) for u in unique_urls),
        return_exceptions=True,
    )

    for url, result in zip(unique_urls, results, strict=True):
        if isinstance(result, Exception):
            logger.debug(f"Download task failed for {url}: {result}")
