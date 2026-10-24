from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import Callable

import httpx
from bs4 import BeautifulSoup, Tag

from scapper.constants import (
    MEDIA_EXTENSIONS,
    TELEGRAM_DOMAINS,
    TELEGRAM_MENTION_RE,
    TELEGRAM_RESERVED,
    TELEGRAM_URL_RE,
    TELEGRAPH_URL_RE,
)
from scapper.downloader import extension_from_url, fetch_url
from scapper.urls import (
    normalize_telegram_url,
    normalize_telegraph_url,
    slugify_telegraph_candidates,
)

logger = logging.getLogger("scraper")


async def scrape_telegram_web_preview(
    client: httpx.AsyncClient,
    url: str,
    fetch_fn: Callable | None = None,
) -> tuple[list[str], set[str], set[str], str | None]:
    """
    Parse a Telegram public web preview page (https://t.me/s/channel) for media,
    outbound telegraph/telegram links, and pagination.
    Returns (media_urls, telegraph_urls, discovered_telegram, before_url).
    """
    media_urls: list[str] = []
    telegraph_urls: set[str] = set()
    discovered_telegram: set[str] = set()
    before_url: str | None = None
    do_fetch = fetch_fn or fetch_url

    m_curr = TELEGRAM_URL_RE.match(url)
    current_channel = m_curr.group(1).lower() if m_curr else None

    try:
        resp = await do_fetch(client, url)

        if resp is None or resp.status_code != 200:
            logger.warning(f"Failed to fetch Telegram preview: {url}")
            return media_urls, telegraph_urls, discovered_telegram, before_url

        resp_url_str = str(getattr(resp, "url", url))
        parsed_resp = urllib.parse.urlparse(resp_url_str)
        resp_netloc = parsed_resp.netloc.lower().split(":")[0].removeprefix("www.")
        if resp_netloc not in TELEGRAM_DOMAINS or not parsed_resp.path.startswith(
            "/s/"
        ):
            logger.warning(
                f"Telegram preview for '{url}' redirected to '{resp_url_str}' "
                "(channel does not exist or preview unavailable). Skipping."
            )
            return media_urls, telegraph_urls, discovered_telegram, before_url

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        messages = soup.select(".tgme_widget_message")

        # 1. Photos and video thumbnails in message wraps and albums
        for wrap in soup.find_all(
            class_=re.compile(
                r"tgme_widget_message_photo_wrap|tgme_widget_message_video_thumb|tgme_widget_message_roundvideo_thumb"
            )
        ):
            style = wrap.get("style", "")
            if isinstance(style, str) and style:
                m = re.search(r"url\((['\"]?)(?P<url>[^'\")]+)\1\)", style)
                if m:
                    media_urls.append(urllib.parse.urljoin(url, m.group("url")))

        # 2. Photos in link previews
        for preview in soup.select(
            ".link_preview_image, .link_preview_right_image, .tgme_widget_message_link_preview"
        ):
            style = preview.get("style", "")
            if isinstance(style, str) and style:
                m = re.search(r"url\((['\"]?)(?P<url>[^'\")]+)\1\)", style)
                if m:
                    media_urls.append(urllib.parse.urljoin(url, m.group("url")))

        # 3. Videos, round videos, and video sources
        for tag in soup.find_all(["video", "source"]):
            src = tag.get("src")
            if isinstance(src, str) and src:
                media_urls.append(urllib.parse.urljoin(url, src))
            poster = tag.get("poster")
            if isinstance(poster, str) and poster and not src:
                media_urls.append(urllib.parse.urljoin(url, poster))

        # 4. Audios and voice clips
        for tag in soup.find_all(["audio"]):
            src = tag.get("src")
            if isinstance(src, str) and src:
                media_urls.append(urllib.parse.urljoin(url, src))

        # 5. Documents / downloadable attachments
        for doc_wrap in soup.select("a.tgme_widget_message_document_wrap"):
            doc_href = doc_wrap.get("href")
            if isinstance(doc_href, str) and doc_href:
                media_urls.append(urllib.parse.urljoin(url, doc_href))

        # 6. Direct img tags (skipping avatar/profile icons)
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not isinstance(src, str) or not src:
                continue

            parent_cls = img.parent.get("class") if img.parent else None
            parent_classes = (
                " ".join(parent_cls)
                if isinstance(parent_cls, list)
                else (parent_cls or "")
            )
            img_cls = img.get("class")
            classes = (
                " ".join(img_cls) if isinstance(img_cls, list) else (img_cls or "")
            )
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

        # 7. Telegraph & Telegram links strictly inside message contents (excluding pagination, headers, self-links)
        for a in soup.select(
            ".tgme_widget_message_text a, .tgme_widget_message_inline_button, "
            ".tgme_widget_message_forward_info a, .tgme_widget_message_reply a"
        ):
            href_val = a.get("href")
            if not isinstance(href_val, str) or not href_val:
                continue

            href = urllib.parse.urljoin(url, href_val)
            ext = extension_from_url(href)
            if ext in MEDIA_EXTENSIONS or "/file/" in href:
                media_urls.append(href)
                continue

            tg = normalize_telegraph_url(href)
            if tg:
                telegraph_urls.add(tg)
                continue

            tel = normalize_telegram_url(href)
            if tel:
                m_tel = TELEGRAM_URL_RE.match(tel)
                cand_channel = m_tel.group(1).lower() if m_tel else None
                if (
                    cand_channel
                    and cand_channel != current_channel
                    and cand_channel not in TELEGRAM_RESERVED
                ):
                    discovered_telegram.add(f"https://t.me/s/{cand_channel}")

        # 8. Text mentions (@username) and Telegraph links in message bodies
        for msg_text_elem in soup.select(".tgme_widget_message_text"):
            msg_text = msg_text_elem.get_text()
            for mention in TELEGRAM_MENTION_RE.findall(msg_text):
                m_lower = mention.lower()
                if m_lower != current_channel and m_lower not in TELEGRAM_RESERVED:
                    m_norm = normalize_telegram_url(f"https://t.me/{mention}")
                    if m_norm:
                        discovered_telegram.add(m_norm)

            for ph_match in TELEGRAPH_URL_RE.finditer(msg_text):
                ph_norm = normalize_telegraph_url(ph_match.group(0))
                if ph_norm:
                    telegraph_urls.add(ph_norm)

        # 9. Cross-pollinate Telegraph candidate slugs from this Telegram channel username (only once per channel root)
        if m_curr and "?before=" not in url:
            ch_name = m_curr.group(1)
            for ph_cand in slugify_telegraph_candidates(ch_name):
                ph_norm = normalize_telegraph_url(
                    f"https://telegra.ph/{ph_cand}"
                )
                if ph_norm:
                    telegraph_urls.add(ph_norm)

        # 10. Pagination for older messages
        next_link = soup.find("a", class_=re.compile(r"(?:tgme|tme)_messages_more"))
        if next_link is None:
            next_link = soup.find("a", href=re.compile(r"[?&]before="))

        if isinstance(next_link, Tag):
            href = next_link.get("href")
            if isinstance(href, str) and href:
                before_url = urllib.parse.urljoin(url, href)
        elif messages and current_channel:
            msg_ids = []
            for msg in messages:
                post_attr = msg.get("data-post", "")
                if isinstance(post_attr, str) and "/" in post_attr:
                    ch, mid = post_attr.split("/", 1)
                    if ch.lower() == current_channel and mid.isdigit():
                        msg_ids.append(int(mid))
            if msg_ids and min(msg_ids) > 1:
                before_url = (
                    f"https://t.me/s/{current_channel}?before={min(msg_ids)}"
                )

        # Ensure pagination URL is never treated as a newly discovered external channel
        if before_url:
            norm_before = normalize_telegram_url(before_url)
            if norm_before:
                discovered_telegram.discard(norm_before)
                m_b = TELEGRAM_URL_RE.match(norm_before)
                if m_b:
                    discovered_telegram.discard(
                        f"https://t.me/s/{m_b.group(1).lower()}"
                    )

    except Exception as exc:
        logger.error(f"Error scraping Telegram preview {url}: {exc}")

    return media_urls, telegraph_urls, discovered_telegram, before_url
