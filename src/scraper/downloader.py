from __future__ import annotations

import asyncio
import email.utils
import hashlib
import logging
import random
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from scraper.constants import (
    BROWSER_PROFILES,
    MEDIA_EXTENSIONS,
    MIME_EXTENSIONS,
    TELEGRAM_DOMAINS,
    TELEGRAPH_DOMAINS,
)
from scraper.urls import is_telegraph_media_url
from scraper.utils import detect_media_type_from_bytes

if TYPE_CHECKING:
    from scraper.config import ScraperConfig
    from scraper.storage import Storage

logger = logging.getLogger("scraper")


def make_client(config: ScraperConfig) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient configured with connection limits and headers."""
    limits = httpx.Limits(
        max_connections=config.max_connections,
        max_keepalive_connections=config.max_connections,
    )
    timeout = httpx.Timeout(config.request_timeout, connect=10.0)
    profile = random.choice(BROWSER_PROFILES)

    headers = {
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        **profile,
    }

    return httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    )


def _parse_retry_after(
    retry_after: str | None,
    default: float = 2.0,
    max_wait: float = 30.0,
) -> float:
    """Parse Retry-After header safely, handling delta-seconds or HTTP-date."""
    if not retry_after:
        return default
    try:
        val = float(retry_after)
        return min(max_wait, max(0.5, val))
    except (ValueError, TypeError):
        pass

    try:
        dt = email.utils.parsedate_to_datetime(retry_after)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            diff = (dt - now).total_seconds()
            return min(max_wait, max(0.5, diff))
    except Exception:
        pass

    return default


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
                wait_time = _parse_retry_after(
                    retry_after,
                    default=min(30.0, 2**attempt + random.random()),
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
            wait_time = _parse_retry_after(
                retry_after,
                default=1.5 + random.uniform(0.5, 2.0),
            )
            await asyncio.sleep(wait_time)
            resp = await client.head(
                url,
                timeout=timeout,
                follow_redirects=True,
            )

        # Some endpoints dislike HEAD; fall back to GET stream without downloading full body.
        if resp.status_code == 405:
            async with client.stream(
                "GET",
                url,
                timeout=timeout,
                follow_redirects=True,
            ) as get_resp:
                return _validate_probe_response(url, get_resp)

        return _validate_probe_response(url, resp)

    except Exception:
        return False


def _validate_probe_response(url: str, resp: httpx.Response) -> bool:
    """Validate status code and destination of probe response."""
    if resp.status_code != 200:
        return False

    # Validate that redirect did not land on root path or wrong destination
    resp_url_str = str(getattr(resp, "url", url))
    parsed_orig = urllib.parse.urlparse(url)
    parsed_final = urllib.parse.urlparse(resp_url_str)

    if parsed_orig.path.strip("/") and not parsed_final.path.strip("/"):
        return False

    orig_netloc = parsed_orig.netloc.lower().split(":")[0].removeprefix("www.")
    final_netloc = parsed_final.netloc.lower().split(":")[0].removeprefix("www.")
    if orig_netloc in TELEGRAM_DOMAINS and (
        final_netloc not in TELEGRAM_DOMAINS
        or not parsed_final.path.startswith("/s/")
    ):
        return False
    return not (
        orig_netloc in TELEGRAPH_DOMAINS
        and final_netloc not in TELEGRAPH_DOMAINS
    )


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
    retries: int = 2,
) -> Path | None:
    """Stream download a media file, validate mime/magic bytes, and deduplicate by sha256."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None

    # Check media URL cache before downloading
    if storage.has_media_url_downloaded(url):
        return None

    config.download_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, retries + 1):
        temp_path = config.download_dir / f"temp_{uuid.uuid4().hex}"
        try:
            async with client.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=config.request_timeout,
            ) as resp:
                if resp.status_code == 429:
                    if attempt < retries:
                        retry_after = resp.headers.get("retry-after")
                        wait_time = _parse_retry_after(retry_after, default=2.0)
                        await asyncio.sleep(wait_time)
                        continue
                    logger.debug(f"Skipping {url}: HTTP 429 rate limit exceeded")
                    return None

                if resp.status_code in {500, 502, 503, 504}:
                    if attempt < retries:
                        await asyncio.sleep(min(15.0, 2**attempt + random.random()))
                        continue
                    logger.debug(f"Skipping {url}: HTTP {resp.status_code}")
                    return None

                if resp.status_code in {400, 401, 403, 404, 410, 415}:
                    storage.record_downloaded_media_url(url, "")
                    logger.debug(f"Skipping {url}: HTTP {resp.status_code} (marked invalid)")
                    return None

                if resp.status_code != 200:
                    logger.debug(f"Skipping {url}: HTTP {resp.status_code}")
                    return None

                content_type = (
                    resp.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if content_type.startswith(
                    (
                        "text/html",
                        "application/json",
                        "text/plain",
                        "application/xml",
                        "text/xml",
                        "text/css",
                        "application/javascript",
                        "text/javascript",
                    )
                ):
                    storage.record_downloaded_media_url(url, "")
                    return None

                content_length = resp.headers.get("content-length")
                expected_length: int | None = None
                if content_length:
                    try:
                        expected_length = int(content_length)
                        if expected_length < config.min_size_bytes:
                            storage.record_downloaded_media_url(url, "")
                            return None
                    except ValueError:
                        pass

                ext = extension_from_content_type(content_type) or extension_from_url(url)

                is_generic_or_file = (
                    not ext
                    or content_type
                    in {"application/octet-stream", "binary/octet-stream", ""}
                    or is_telegraph_media_url(url)
                )
                if not looks_like_media(content_type, ext) and not is_generic_or_file:
                    storage.record_downloaded_media_url(url, "")
                    return None

                sha256 = hashlib.sha256()
                bytes_written = 0
                header_bytes = b""

                with temp_path.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(chunk_size=32768):
                        if len(header_bytes) < 4096:
                            header_bytes += chunk[: 4096 - len(header_bytes)]
                        fh.write(chunk)
                        sha256.update(chunk)
                        bytes_written += len(chunk)

            if expected_length is not None and bytes_written < expected_length:
                raise httpx.TransportError(
                    f"Incomplete download for {url}: received {bytes_written}/{expected_length} bytes"
                )

            if bytes_written < config.min_size_bytes:
                storage.record_downloaded_media_url(url, "")
                return None

            if header_bytes:
                header_strip = header_bytes.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
                if header_strip.startswith(
                    (b"<!doctype", b"<html", b"<?xml", b"{\"", b"{'")
                ):
                    storage.record_downloaded_media_url(url, "")
                    return None

                detected = detect_media_type_from_bytes(header_bytes)
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

        except httpx.TransportError as exc:
            if attempt < retries:
                await asyncio.sleep(min(15.0, 2**attempt + random.random()))
                continue
            logger.debug(f"Download failed for {url}: {exc}")
            return None
        except Exception as exc:
            logger.debug(f"Download failed for {url}: {exc}")
            return None
        finally:
            temp_path.unlink(missing_ok=True)

    return None


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
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.debug(f"Download task failed for {url}: {result}")
