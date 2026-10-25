import asyncio
import tempfile
import unittest
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

from scapper.config import ScraperConfig
from scapper.constants import (
    BROWSER_PROFILES,
    TELEGRAM_MENTION_RE,
    TELEGRAM_RESERVED,
    TELEGRAM_URL_RE,
    TELEGRAPH_DOMAINS,
    TELEGRAPH_RESERVED,
    TELEGRAPH_URL_RE,
)
from scapper.discovery import unwrap_ddg
from scapper.downloader import (
    _parse_retry_after,
    download_file,
    download_many,
    probe_url_exists,
)
from scapper.engine import MediaScraper
from scapper.storage import Storage
from scapper.telegram_api import download_telethon_media
from scapper.telegram_web import scrape_telegram_web_preview
from scapper.telegraph import scrape_telegraph_page
from scapper.urls import (
    canonicalize_seed,
    ensure_scheme,
    extract_base_slug,
    extract_chat_username,
    normalize_telegram_url,
    normalize_telegraph_url,
    slugify_telegram_candidates,
    slugify_telegraph_candidates,
)
from scapper.utils import detect_media_type_from_bytes


class TestReservedAndRegex(unittest.TestCase):
    def test_telegraph_reserved_not_matched_as_article(self):
        # Media upload paths must not be matched as Telegraph articles
        file_url = "https://telegra.ph/file/a1b2c3d4e5.jpg"
        m = TELEGRAPH_URL_RE.search(file_url)
        self.assertIsNone(m)

        upload_url = "https://telegra.ph/upload"
        m2 = TELEGRAPH_URL_RE.search(upload_url)
        self.assertIsNone(m2)

        # Real article URL should match
        article_url = "https://telegra.ph/My-Story-Title-01-02"
        m3 = TELEGRAPH_URL_RE.search(article_url)
        self.assertIsNotNone(m3)
        assert m3 is not None
        self.assertEqual(m3.group(1), "My-Story-Title-01-02")

    def test_telegraph_url_re_finditer_discovery(self):
        text = "Check out https://telegra.ph/sample-article-10-24 for updates."
        matches = list(TELEGRAPH_URL_RE.finditer(text))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].group(0), "https://telegra.ph/sample-article-10-24")
        self.assertEqual(matches[0].group(1), "sample-article-10-24")

        normalized = normalize_telegraph_url(matches[0].group(0))
        self.assertEqual(normalized, "https://telegra.ph/sample-article-10-24")

    def test_telegram_mention_re(self):
        self.assertEqual(TELEGRAM_MENTION_RE.findall("Check [@my_channel] now"), ["my_channel"])
        self.assertEqual(TELEGRAM_MENTION_RE.findall('Quote: "@my_channel"'), ["my_channel"])
        self.assertEqual(TELEGRAM_MENTION_RE.findall("(@my_channel)"), ["my_channel"])
        self.assertEqual(TELEGRAM_MENTION_RE.findall("contact@my_channel.com"), [])

    def test_telegram_reserved_words(self):
        self.assertIn("contact", TELEGRAM_RESERVED)
        self.assertIn("about", TELEGRAM_RESERVED)
        self.assertIn("blog", TELEGRAM_RESERVED)
        self.assertIn("file", TELEGRAPH_RESERVED)
        self.assertIn("upload", TELEGRAPH_RESERVED)


class TestSlugifiersAndNormalizers(unittest.TestCase):
    def test_slugify_telegram_candidates_url(self):
        url = "https://t.me/s/cool_channel?before=100"
        candidates = slugify_telegram_candidates(url)
        self.assertIn("cool_channel", candidates)
        self.assertNotIn("100", candidates)

    def test_slugify_telegram_candidates_message_permalink(self):
        url = "https://t.me/s/cool_channel/456"
        candidates = slugify_telegram_candidates(url)
        self.assertIn("cool_channel", candidates)
        self.assertNotIn("456", candidates)

        url_no_s = "https://t.me/cool_channel/789"
        candidates_no_s = slugify_telegram_candidates(url_no_s)
        self.assertIn("cool_channel", candidates_no_s)

    def test_extract_chat_username(self):
        # Reserved words must be rejected
        self.assertIsNone(extract_chat_username("contact"))
        self.assertIsNone(extract_chat_username("https://t.me/about"))

        # Valid channels and custom domains must succeed
        self.assertEqual(extract_chat_username("durov"), "durov")
        self.assertEqual(extract_chat_username("https://t.me/s/durov/123"), "durov")
        self.assertEqual(extract_chat_username("https://telegram.dog/durov"), "durov")

    def test_unwrap_ddg(self):
        rel_href = "/l/?uddg=https%3A%2F%2Ftelegra.ph%2Fmy-post&rut=123"
        unwrapped = unwrap_ddg(rel_href)
        self.assertEqual(unwrapped, "https://telegra.ph/my-post")

    def test_detect_media_type_from_bytes(self):
        self.assertEqual(detect_media_type_from_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF"), ("image/jpeg", ".jpg"))
        self.assertEqual(detect_media_type_from_bytes(b"\x89PNG\r\n\x1a\n\x00\x00"), ("image/png", ".png"))
        self.assertEqual(detect_media_type_from_bytes(b"RIFF\x20\x00\x00\x00WEBPVP8 "), ("image/webp", ".webp"))
        self.assertEqual(detect_media_type_from_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00"), ("video/mp4", ".mp4"))
        self.assertEqual(detect_media_type_from_bytes(b"\x00\x00\x00\x1cftypavif\x00\x00"), ("image/avif", ".avif"))
        self.assertIsNone(detect_media_type_from_bytes(b"<html>hello</html>"))

    def test_slugify_telegram_candidates_reserved(self):
        self.assertEqual(slugify_telegram_candidates("contact"), [])
        self.assertEqual(slugify_telegram_candidates("https://t.me/s/about"), [])

    def test_slugify_telegraph_candidates_url(self):
        url = "https://telegra.ph/some-great-title-10-24?ref=1"
        candidates = slugify_telegraph_candidates(url)
        self.assertIn("some-great-title-10-24", candidates)

    def test_slugify_telegraph_candidates_reserved(self):
        self.assertEqual(slugify_telegraph_candidates("file"), [])
        self.assertEqual(slugify_telegraph_candidates("https://telegra.ph/upload"), [])

    def test_normalize_telegraph_url(self):
        self.assertIsNone(normalize_telegraph_url("https://telegra.ph/file/pic.png"))
        self.assertIsNone(normalize_telegraph_url("https://telegra.ph/upload"))
        self.assertEqual(
            normalize_telegraph_url("https://telegra.ph/valid-slug-01-01"),
            "https://telegra.ph/valid-slug-01-01",
        )
        self.assertEqual(
            normalize_telegraph_url("https://www.telegra.ph/valid-slug-01-01"),
            "https://telegra.ph/valid-slug-01-01",
        )


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.seeds_file = Path(self.temp_dir.name) / "seeds.txt"
        self.storage = Storage(self.db_path, self.seeds_file)

    def tearDown(self):
        self.storage.close()
        self.temp_dir.cleanup()

    def test_reusable_connection_and_close(self):
        c1 = self.storage._get_conn()
        c2 = self.storage._get_conn()
        self.assertIs(c1, c2)
        self.storage.close()
        self.assertIsNone(self.storage._conn)

    def test_downloaded_media_urls_cache(self):
        url = "https://telegra.ph/file/abc12345.jpg"
        self.assertFalse(self.storage.has_media_url_downloaded(url))
        self.storage.record_downloaded_media_url(url, "somehash123")
        self.assertTrue(self.storage.has_media_url_downloaded(url))

    def test_discovered_seeds_reserved_filtering(self):
        self.storage.add_discovered_seed("contact")
        self.storage.add_discovered_seed("upload")
        self.storage.add_discovered_seed("durov")
        seeds = self.storage.get_discovered_seeds()
        self.assertNotIn("contact", seeds)
        self.assertNotIn("upload", seeds)
        self.assertIn("durov", seeds)

    def test_clean_discovered_seeds(self):
        self.seeds_file.write_text("contact\nupload\ndurov\nDUROV\ndurov\n", encoding="utf-8")
        count = self.storage.clean_discovered_seeds()
        self.assertEqual(count, 1)
        cleaned = self.seeds_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(cleaned, ["durov"])

    def test_clean_discovered_seeds_case_insensitive(self):
        self.seeds_file.write_text("TechNews\ntechnews\nTECHNEWS\n", encoding="utf-8")
        count = self.storage.clean_discovered_seeds()
        self.assertEqual(count, 1)
        cleaned = self.seeds_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(cleaned, ["technews"])

    def test_storage_init_creates_subdirectories(self):
        sub_dir = Path(self.temp_dir.name) / "sub1" / "sub2"
        db = sub_dir / "test.db"
        seeds = sub_dir / "seeds.txt"
        s = Storage(db, seeds)
        self.assertTrue(db.exists())
        self.assertTrue(sub_dir.exists())
        s.close()

    def test_min_pending_depth_with_max_depth(self):
        self.storage.enqueue("https://t.me/s/ch1", "telegram", 0)
        self.storage.enqueue("https://t.me/s/ch2", "telegram", 3)
        self.storage.enqueue("https://t.me/s/ch3", "telegram", 5)

        self.assertEqual(self.storage.get_min_pending_depth(max_depth=2), 0)
        self.storage.remove_from_queue("https://t.me/s/ch1")
        self.assertIsNone(self.storage.get_min_pending_depth(max_depth=2))
        self.assertEqual(self.storage.get_min_pending_depth(max_depth=4), 3)


class TestHtmlParsing(unittest.IsolatedAsyncioTestCase):
    async def test_telegraph_parsing_authors_and_scripts(self):
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Article Title</title></head>
        <body>
            <header>
                <h1>Header Article Title</h1>
                <address>
                    <a rel="author" href="https://t.me/author_channel">Author Channel</a>
                    <time datetime="2026-10-24">October 24, 2026</time>
                </address>
            </header>
            <article>
                <script>var x = 1;</script>
                <style>.foo { color: red; }</style>
                <p>Hello world body text.</p>
                <figure>
                    <img src="/file/image1.jpg">
                </figure>
                <a href="/file/video1.mp4">Direct Video Link</a>
                <a href="https://t.me/s/linked_channel">Linked Channel</a>
            </article>
        </body>
        </html>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html

        media_urls, tg_urls, _ph_urls = await scrape_telegraph_page(
            MagicMock(), "https://telegra.ph/test-article-10-24", fetch_fn=AsyncMock(return_value=mock_resp)
        )

        self.assertIn("https://telegra.ph/file/image1.jpg", media_urls)
        self.assertIn("https://telegra.ph/file/video1.mp4", media_urls)
        self.assertIn("https://t.me/s/author_channel", tg_urls)
        self.assertIn("https://t.me/s/linked_channel", tg_urls)

    async def test_telegram_web_preview_no_self_reference_loops(self):
        html = """
        <div class="tgme_page_wrap">
            <div class="tgme_header_title">My Channel</div>
            <div class="tgme_widget_message_wrap">
                <div class="tgme_widget_message" data-post="mychannel/101">
                    <a class="tgme_widget_message_date" href="https://t.me/s/mychannel/101">10:00</a>
                    <div class="tgme_widget_message_text">
                        Check out <a href="https://t.me/s/otherchannel">Other Channel</a>
                        and <a href="https://telegra.ph/article-title-10-24">Telegraph Post</a>
                    </div>
                    <div class="tgme_widget_message_photo_wrap" style="background-image:url('https://cdn.telegram.org/file1.jpg')"></div>
                </div>
            </div>
            <div class="tgme_page_actions">
                <a class="tme_messages_more" href="/s/mychannel?before=101">Load more</a>
            </div>
        </div>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://t.me/s/mychannel"
        mock_resp.text = html

        media_urls, ph_urls, tg_urls, before_url = await scrape_telegram_web_preview(
            MagicMock(), "https://t.me/s/mychannel", fetch_fn=AsyncMock(return_value=mock_resp)
        )

        self.assertIn("https://cdn.telegram.org/file1.jpg", media_urls)
        self.assertEqual(before_url, "https://t.me/s/mychannel?before=101")
        self.assertIn("https://t.me/s/otherchannel", tg_urls)
        self.assertNotIn("https://t.me/s/mychannel", tg_urls)
        self.assertNotIn("https://t.me/s/mychannel?before=101", tg_urls)
        self.assertIn("https://telegra.ph/article-title-10-24", ph_urls)

    async def test_telegram_web_preview_case_insensitive_pagination_discard(self):
        html = """
        <div class="tgme_page_wrap">
            <div class="tgme_widget_message_wrap">
                <div class="tgme_widget_message" data-post="MyChannel/200">
                    <div class="tgme_widget_message_text">
                        Visit <a href="https://t.me/s/MyChannel">Self Link</a>
                    </div>
                </div>
            </div>
            <div class="tgme_page_actions">
                <a class="tme_messages_more" href="/s/MyChannel?before=200">Load more</a>
            </div>
        </div>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://t.me/s/MyChannel"
        mock_resp.text = html

        _, _, tg_urls, before_url = await scrape_telegram_web_preview(
            MagicMock(), "https://t.me/s/MyChannel", fetch_fn=AsyncMock(return_value=mock_resp)
        )
        self.assertEqual(before_url, "https://t.me/s/MyChannel?before=200")
        self.assertNotIn("https://t.me/s/mychannel", tg_urls)
        self.assertNotIn("https://t.me/s/MyChannel", tg_urls)
        self.assertNotIn("https://t.me/s/mychannel?before=200", tg_urls)

    async def test_telegram_web_preview_redirect_skipped(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://telegram.org/"
        mock_resp.text = "<html><body>Telegram Messenger</body></html>"

        media_urls, ph_urls, tg_urls, before = await scrape_telegram_web_preview(
            MagicMock(), "https://t.me/s/nonexistent_channel", fetch_fn=AsyncMock(return_value=mock_resp)
        )
        self.assertEqual(media_urls, [])
        self.assertEqual(ph_urls, set())
        self.assertEqual(tg_urls, set())
        self.assertIsNone(before)

    async def test_telegram_web_preview_extracts_video_thumbs(self):
        html = """
        <div class="tgme_page_wrap">
            <div class="tgme_widget_message_wrap">
                <div class="tgme_widget_message" data-post="mychannel/10">
                    <div class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn.telesco.pe/file/thumb1.jpg')"></div>
                </div>
            </div>
        </div>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://t.me/s/mychannel"
        mock_resp.text = html

        media_urls, _, _, _ = await scrape_telegram_web_preview(
            MagicMock(), "https://t.me/s/mychannel", fetch_fn=AsyncMock(return_value=mock_resp)
        )
        self.assertIn("https://cdn.telesco.pe/file/thumb1.jpg", media_urls)

    async def test_telethon_flood_wait_retry_success(self):
        from telethon.errors import FloodWaitError

        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            cfg = ScraperConfig(download_dir=temp_dir / "downloads")
            cfg.download_dir.mkdir(parents=True, exist_ok=True)
            storage = Storage(temp_dir / "test.db", temp_dir / "seeds.txt")

            client = MagicMock()
            download_target = temp_dir / "sample_video.mp4"
            download_target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"A" * 200000)

            calls = 0

            async def mock_download_media(msg, file):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise FloodWaitError(request=None, capture=0)
                return str(download_target)

            client.download_media = AsyncMock(side_effect=mock_download_media)
            msg = MagicMock()
            msg.document = MagicMock()
            msg.document.size = 200000
            msg.document.mime_type = "video/mp4"

            try:
                result = await download_telethon_media(client, msg, cfg, storage)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(calls, 2)
                self.assertTrue(result.exists())
                self.assertTrue(storage.has_hash_downloaded(result.stem))
            finally:
                storage.close()


class TestBugFixesAndImprovements(unittest.IsolatedAsyncioTestCase):
    def test_extract_base_slug_preserves_numbers(self):
        self.assertEqual(extract_base_slug("top-10"), "top-10")
        self.assertEqual(extract_base_slug("part-01"), "part-01")
        self.assertEqual(extract_base_slug("https://telegra.ph/article-title-10-24"), "article-title")

    def test_probed_base_slugs_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "test.db"
            seeds_file = Path(td) / "seeds.txt"
            storage = Storage(db_file, seeds_file)
            self.assertFalse(storage.has_base_slug_probed("My-Slug"))
            storage.mark_base_slug_probed("My-Slug")
            self.assertTrue(storage.has_base_slug_probed("my-slug"))
            self.assertTrue(storage.has_base_slug_probed("MY-SLUG"))
            storage.close()

    def test_uncrawl_seed(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "test.db"
            seeds_file = Path(td) / "seeds.txt"
            storage = Storage(db_file, seeds_file)
            channel_url = "https://t.me/s/mychannel"
            storage.mark_url_crawled(channel_url)
            self.assertTrue(storage.has_url_crawled(channel_url))

            storage.uncrawl_seed("mychannel")
            self.assertFalse(storage.has_url_crawled(channel_url))
            storage.close()

    def test_config_load_bad_types(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_file = Path(td) / "bad_config.json"
            cfg_file.write_text(
                '{"min_size_kb": "invalid", "max_connections": "bad", "web_delay_range": ["x", "y"]}',
                encoding="utf-8",
            )
            cfg = ScraperConfig.load(cfg_file)
            self.assertEqual(cfg.min_size_kb, 20)
            self.assertEqual(cfg.max_connections, 5)
            self.assertEqual(cfg.web_delay_min, 1.0)
            self.assertEqual(cfg.web_delay_max, 3.0)

    def test_telegram_three_character_usernames(self):
        self.assertEqual(normalize_telegram_url("https://t.me/s/ton"), "https://t.me/s/ton")
        self.assertEqual(normalize_telegram_url("https://t.me/dog"), "https://t.me/s/dog")
        self.assertEqual(slugify_telegram_candidates("ton"), ["ton"])
        self.assertEqual(
            TELEGRAM_MENTION_RE.findall("Join @dog and @btc now!"),
            ["dog", "btc"],
        )

    def test_detect_media_type_mkv_and_m4a(self):
        mkv_bytes = (
            b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04"
            b"\x42\xf3\x81\x08\x42\x82\x88matroska"
        )
        self.assertEqual(detect_media_type_from_bytes(mkv_bytes), ("video/x-matroska", ".mkv"))

        webm_bytes = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm"
        self.assertEqual(detect_media_type_from_bytes(webm_bytes), ("video/webm", ".webm"))

        m4a_bytes = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00"
        self.assertEqual(detect_media_type_from_bytes(m4a_bytes), ("audio/mp4", ".m4a"))

    def test_normalize_telegram_url_query_validation(self):
        self.assertEqual(
            normalize_telegram_url("https://t.me/s/channel?before=150"),
            "https://t.me/s/channel?before=150",
        )
        self.assertEqual(
            normalize_telegram_url("https://t.me/s/channel?before=null"),
            "https://t.me/s/channel",
        )
        self.assertEqual(
            normalize_telegram_url("https://t.me/s/channel?before=0"),
            "https://t.me/s/channel",
        )
        self.assertEqual(
            normalize_telegram_url("https://t.me/s/channel?before=-5"),
            "https://t.me/s/channel",
        )

    async def test_download_file_html_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = ScraperConfig(
                db_path=Path(td) / "test.db",
                discovered_seeds_file=Path(td) / "seeds.txt",
                download_dir=Path(td) / "downloads",
            )
            cfg.download_dir.mkdir(parents=True, exist_ok=True)
            storage = Storage(cfg.db_path, cfg.discovered_seeds_file)
            try:
                mock_client = MagicMock()
                mock_resp = AsyncMock()
                mock_resp.status_code = 200
                mock_resp.headers = {"content-type": "text/html; charset=utf-8"}

                def fake_stream(*args, **kwargs):
                    class FakeStreamContext:
                        async def __aenter__(self):
                            return mock_resp

                        async def __aexit__(self, *exc):
                            pass

                    return FakeStreamContext()

                mock_client.stream = fake_stream

                url = "https://telegra.ph/file/not_a_photo.jpg"
                res = await download_file(mock_client, url, cfg, storage)
                self.assertIsNone(res)
                self.assertTrue(storage.has_media_url_downloaded(url))
            finally:
                storage.close()

    async def test_telethon_flood_wait_excessive_skipped(self):
        from telethon.errors import FloodWaitError

        with tempfile.TemporaryDirectory() as td:
            cfg = ScraperConfig(
                db_path=Path(td) / "test.db",
                discovered_seeds_file=Path(td) / "seeds.txt",
                download_dir=Path(td) / "downloads",
            )
            cfg.download_dir.mkdir(parents=True, exist_ok=True)
            storage = Storage(cfg.db_path, cfg.discovered_seeds_file)
            try:
                client = MagicMock()
                client.download_media = AsyncMock(
                    side_effect=FloodWaitError(request=None, capture=50000)
                )
                msg = MagicMock()
                msg.document = None
                msg.photo = MagicMock()

                res = await download_telethon_media(client, msg, cfg, storage)
                self.assertIsNone(res)
            finally:
                storage.close()

    def test_uncrawl_seed_with_full_urls(self):
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "test.db"
            seeds_file = Path(td) / "seeds.txt"
            storage = Storage(db_file, seeds_file)
            channel_url = "https://t.me/s/mychannel"
            storage.mark_url_crawled(channel_url)
            self.assertTrue(storage.has_url_crawled(channel_url))

            # Passing a non-s URL should uncrawl https://t.me/s/mychannel
            storage.uncrawl_seed("https://t.me/mychannel")
            self.assertFalse(storage.has_url_crawled(channel_url))

            # Test with Telegra.ph URL
            telegraph_url = "https://telegra.ph/sample-article-10-24"
            storage.mark_url_crawled(telegraph_url)
            self.assertTrue(storage.has_url_crawled(telegraph_url))
            storage.uncrawl_seed("https://www.telegra.ph/sample-article-10-24")
            self.assertFalse(storage.has_url_crawled(telegraph_url))
            storage.close()

    def test_storage_clear_crawled_urls(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            storage.mark_url_crawled("https://t.me/s/ch1")
            storage.mark_url_crawled("https://t.me/s/ch2")
            self.assertTrue(storage.has_url_crawled("https://t.me/s/ch1"))
            storage.clear_crawled_urls()
            self.assertFalse(storage.has_url_crawled("https://t.me/s/ch1"))
            self.assertFalse(storage.has_url_crawled("https://t.me/s/ch2"))
            storage.close()

    def test_private_telegram_urls_warning(self):
        for u in [
            "https://t.me/+invitecode",
            "https://t.me/s/+invitecode",
            "https://t.me/joinchat/xyz",
            "https://t.me/s/joinchat/xyz",
            "https://t.me/c/12345/10",
            "https://t.me/s/c/12345/10",
        ]:
            self.assertIsNone(normalize_telegram_url(u))

    def test_percent_encoded_telegraph_urls(self):
        url = "https://telegra.ph/%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82-10-24"
        norm = normalize_telegraph_url(url)
        self.assertEqual(norm, url)
        matches = list(TELEGRAPH_URL_RE.finditer(f"Article at {url}."))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].group(0), url)

    def test_parse_retry_after(self):
        self.assertEqual(_parse_retry_after("1.5"), 1.5)
        self.assertEqual(_parse_retry_after("0"), 0.5)
        self.assertEqual(_parse_retry_after("86400"), 30.0)
        self.assertEqual(_parse_retry_after("invalid"), 2.0)
        self.assertEqual(_parse_retry_after(None, default=5.0), 5.0)

    def test_extract_chat_username_with_fragment(self):
        self.assertEqual(extract_chat_username("durov#section1"), "durov")
        self.assertEqual(extract_chat_username("https://t.me/s/durov?before=10#sec"), "durov")

    def test_detect_media_type_case_insensitive_ftyp(self):
        m4a_bytes = b"\x00\x00\x00\x20ftypm4a \x00\x00\x00\x00"
        self.assertEqual(detect_media_type_from_bytes(m4a_bytes), ("audio/mp4", ".m4a"))
        heic_bytes = b"\x00\x00\x00\x20ftypmsf1\x00\x00\x00\x00"
        self.assertEqual(detect_media_type_from_bytes(heic_bytes), ("image/heic", ".heic"))

    async def test_deduplication_of_media_urls_in_page_scraping(self):
        html = """
        <article>
            <a href="/file/duplicate.jpg"><img src="/file/duplicate.jpg"></a>
            <img src="/file/unique.jpg">
        </article>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        media, _, _ = await scrape_telegraph_page(
            MagicMock(), "https://telegra.ph/article-10-24", fetch_fn=AsyncMock(return_value=mock_resp)
        )
        self.assertEqual(len(media), 2)
        self.assertEqual(media, ["https://telegra.ph/file/duplicate.jpg", "https://telegra.ph/file/unique.jpg"])

    async def test_download_file_retry_on_429(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = ScraperConfig(
                db_path=Path(td) / "test.db",
                discovered_seeds_file=Path(td) / "seeds.txt",
                download_dir=Path(td) / "downloads",
            )
            cfg.download_dir.mkdir(parents=True, exist_ok=True)
            storage = Storage(cfg.db_path, cfg.discovered_seeds_file)
            try:
                mock_client = MagicMock()
                call_count = 0

                def fake_stream(*args, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    class FakeResp:
                        if call_count == 1:
                            status_code = 429
                            headers = {"retry-after": "0.1"}
                        else:
                            status_code = 200
                            headers = {"content-type": "image/jpeg", "content-length": "25000"}

                        async def aiter_bytes(self, chunk_size=32768):
                            yield b"\xff\xd8\xff\xe0" + b"X" * 25000

                    class FakeStreamContext:
                        async def __aenter__(self):
                            return FakeResp()

                        async def __aexit__(self, *exc):
                            pass

                    return FakeStreamContext()

                mock_client.stream = fake_stream
                url = "https://telegra.ph/file/test_image.jpg"
                result = await download_file(mock_client, url, cfg, storage)
                self.assertIsNotNone(result)
                self.assertEqual(call_count, 2)
                self.assertTrue(storage.has_media_url_downloaded(url))
            finally:
                storage.close()

    async def test_transient_error_raises_in_scraping(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("503", request=MagicMock(), response=mock_resp)

        with self.assertRaises(httpx.HTTPStatusError):
            await scrape_telegraph_page(
                MagicMock(), "https://telegra.ph/article-10-24", fetch_fn=AsyncMock(return_value=mock_resp)
            )

        with self.assertRaises(httpx.HTTPStatusError):
            await scrape_telegram_web_preview(
                MagicMock(), "https://t.me/s/mychannel", fetch_fn=AsyncMock(return_value=mock_resp)
            )

    def test_extract_chat_username_rejects_private_links(self):
        for invalid in [
            "https://t.me/+invitecode",
            "https://t.me/s/+invitecode",
            "+invitecode",
            "https://t.me/joinchat/xyz123",
            "https://t.me/s/joinchat/xyz123",
            "https://t.me/c/12345/678",
            "https://t.me/s/c/12345/678",
            "https://example.com/not_telegram",
        ]:
            self.assertIsNone(extract_chat_username(invalid))

        self.assertEqual(extract_chat_username("durov"), "durov")
        self.assertEqual(extract_chat_username("@durov"), "durov")
        self.assertEqual(extract_chat_username("https://t.me/s/durov"), "durov")

    def test_canonicalize_seed(self):
        self.assertEqual(canonicalize_seed("durov"), "durov")
        self.assertEqual(canonicalize_seed("@durov"), "durov")
        self.assertEqual(canonicalize_seed("https://t.me/s/durov"), "durov")
        self.assertEqual(canonicalize_seed("https://t.me/durov"), "durov")
        self.assertEqual(
            canonicalize_seed("https://telegra.ph/Sample-Post-10-24"),
            "Sample-Post",
        )
        self.assertEqual(
            canonicalize_seed("https://graph.org/Sample-Post-10-24"),
            "Sample-Post",
        )

        # Rejected seeds
        self.assertIsNone(canonicalize_seed("https://t.me/+invitecode"))
        self.assertIsNone(canonicalize_seed("+invitecode"))
        self.assertIsNone(canonicalize_seed("https://telegra.ph/file/a1b2c3d4.jpg"))
        self.assertIsNone(canonicalize_seed("https://telegra.ph/upload"))
        self.assertIsNone(canonicalize_seed("https://example.com/something"))
        self.assertIsNone(canonicalize_seed("contact"))
        self.assertIsNone(canonicalize_seed("about"))
        self.assertIsNone(canonicalize_seed("file"))

    def test_discovered_seeds_rejects_private_and_file_urls(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            seeds_file = Path(td) / "seeds.txt"
            storage = Storage(db_path, seeds_file)
            try:
                storage.add_discovered_seed("https://t.me/+invitecode")
                storage.add_discovered_seed("https://telegra.ph/file/a1b2c3d4.jpg")
                storage.add_discovered_seed("https://example.com/foo")
                storage.add_discovered_seed("contact")
                storage.add_discovered_seed("durov")
                seeds = storage.get_discovered_seeds()
                self.assertEqual(seeds, ["durov"])

                # Test cleanup with polluted seeds file
                seeds_file.write_text(
                    "durov\n+invite\nhttps://telegra.ph/file/test.jpg\ncontest\ncontact\n",
                    encoding="utf-8",
                )
                storage.clean_discovered_seeds()
                cleaned = seeds_file.read_text(encoding="utf-8").splitlines()
                self.assertEqual(cleaned, ["durov", "contest"])
            finally:
                storage.close()

    def test_uncrawl_seed_resets_api_channel_progress(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            try:
                storage.update_channel_progress("mychannel", 12345)
                self.assertEqual(storage.get_channel_last_message_id("mychannel"), 12345)

                storage.uncrawl_seed("https://t.me/s/mychannel")
                self.assertEqual(storage.get_channel_last_message_id("mychannel"), 0)

                storage.update_channel_progress("otherchannel", 999)
                self.assertEqual(storage.get_channel_last_message_id("otherchannel"), 999)
                storage.clear_crawled_urls()
                self.assertEqual(storage.get_channel_last_message_id("otherchannel"), 0)
            finally:
                storage.close()

    def test_enqueue_depth_relaxation(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            try:
                url = "https://t.me/s/test_relax"
                self.assertTrue(storage.enqueue(url, "telegram", 3))
                # Higher depth should not update and should return False
                self.assertFalse(storage.enqueue(url, "telegram", 5))
                # Lower depth should relax depth and return True
                self.assertTrue(storage.enqueue(url, "telegram", 1))

                batch = storage.get_pending_batch(1)
                self.assertEqual(len(batch), 1)
                self.assertEqual(batch[0][0], url)

                batch_at_3 = storage.get_pending_batch(3)
                self.assertEqual(len(batch_at_3), 0)
            finally:
                storage.close()

    def test_parse_retry_after_http_date(self):
        import email.utils
        from datetime import datetime, timedelta

        future = datetime.now(UTC) + timedelta(seconds=12)
        http_date_str = email.utils.format_datetime(future)
        parsed = _parse_retry_after(http_date_str)
        self.assertGreaterEqual(parsed, 10.0)
        self.assertLessEqual(parsed, 15.0)

        past = datetime.now(UTC) - timedelta(seconds=60)
        past_str = email.utils.format_datetime(past)
        self.assertEqual(_parse_retry_after(past_str), 0.5)

    def test_graph_org_support(self):
        url = "https://graph.org/sample-article-10-24"
        norm = normalize_telegraph_url(url)
        self.assertEqual(norm, "https://telegra.ph/sample-article-10-24")

        self.assertIn("graph.org", TELEGRAPH_DOMAINS)
        m = TELEGRAPH_URL_RE.search(url)
        self.assertIsNotNone(m)
        assert m is not None
        self.assertEqual(m.group(1), "sample-article-10-24")

    async def test_incomplete_download_triggers_retry(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = ScraperConfig(
                db_path=Path(td) / "test.db",
                discovered_seeds_file=Path(td) / "seeds.txt",
                download_dir=Path(td) / "downloads",
            )
            cfg.download_dir.mkdir(parents=True, exist_ok=True)
            storage = Storage(cfg.db_path, cfg.discovered_seeds_file)
            try:
                mock_client = MagicMock()
                call_count = 0

                def fake_stream(*args, **kwargs):
                    nonlocal call_count
                    call_count += 1

                    class FakeResp:
                        status_code = 200

                        def __init__(self):
                            self.headers = {"content-type": "image/jpeg", "content-length": "50000"}

                        async def aiter_bytes(self, chunk_size=32768):
                            if call_count == 1:
                                # Truncated stream on first attempt (only 25000 bytes)
                                yield b"\xff\xd8\xff\xe0" + b"X" * 24996
                            else:
                                # Complete stream on second attempt
                                yield b"\xff\xd8\xff\xe0" + b"X" * 49996

                    class FakeStreamContext:
                        async def __aenter__(self):
                            return FakeResp()

                        async def __aexit__(self, *exc):
                            pass

                    return FakeStreamContext()

                mock_client.stream = fake_stream
                url = "https://telegra.ph/file/test_incomplete.jpg"
                result = await download_file(mock_client, url, cfg, storage, retries=2)
                self.assertIsNotNone(result)
                self.assertEqual(call_count, 2)
                self.assertTrue(storage.has_media_url_downloaded(url))
            finally:
                storage.close()

    async def test_probe_url_exists_redirect_validation(self):
        mock_client = MagicMock()

        # Mock redirect to root homepage
        resp_root = MagicMock()
        resp_root.status_code = 200
        resp_root.url = httpx.URL("https://telegra.ph/")
        mock_client.head = AsyncMock(return_value=resp_root)

        exists = await probe_url_exists(mock_client, "https://telegra.ph/nonexistent-10-24")
        self.assertFalse(exists)

        # Mock redirect to telegram.org from public preview
        resp_tg_org = MagicMock()
        resp_tg_org.status_code = 200
        resp_tg_org.url = httpx.URL("https://telegram.org/")
        mock_client.head = AsyncMock(return_value=resp_tg_org)

        exists_tg = await probe_url_exists(mock_client, "https://t.me/s/invalid_chan")
        self.assertFalse(exists_tg)

    def test_graph_org_in_engine_add_seed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = ScraperConfig(
                db_path=Path(td) / "test.db",
                discovered_seeds_file=Path(td) / "seeds.txt",
                download_dir=Path(td) / "downloads",
            )
            scraper = MediaScraper(cfg)
            scraper.add_seed("https://graph.org/my-story-10-24", depth=0)
            pending = scraper.storage.get_pending_batch(0)
            scraper.storage.close()
            # Must normalize to telegra.ph and be recognized as telegraph type
            self.assertTrue(any(url == "https://telegra.ph/my-story-10-24" and t == "telegraph" for url, t in pending))

    def test_config_load_explicit_nulls(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_file = Path(td) / "null_config.json"
            cfg_file.write_text(
                '{"db_path": null, "download_dir": null, "discovered_seeds_file": null, "discovery": {"keywords": null, "search_engines": null}}',
                encoding="utf-8",
            )
            cfg = ScraperConfig.load(cfg_file)
            self.assertEqual(cfg.db_path, Path("scraper_cache.db"))
            self.assertEqual(cfg.download_dir, Path("./downloads"))
            self.assertEqual(cfg.discovered_seeds_file, Path("discovered_seeds.txt"))
            self.assertEqual(cfg.keywords, ("wallpaper",))
            self.assertEqual(cfg.search_engines, ("duckduckgo",))

    def test_unencoded_unicode_telegraph_urls(self):
        url = "https://telegra.ph/Привет-10-24"
        norm = normalize_telegraph_url(url)
        self.assertEqual(norm, "https://telegra.ph/%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82-10-24")

        canonical = canonicalize_seed(url)
        self.assertEqual(canonical, "%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82")

    async def test_download_many_cancelled_error_propagation(self):
        async def mock_worker(c, u):
            raise asyncio.CancelledError()

        cfg = ScraperConfig()
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await download_many(
                        MagicMock(),
                        ["https://telegra.ph/file/pic.jpg"],
                        cfg,
                        storage,
                        download_fn=mock_worker,
                    )
            finally:
                storage.close()

    def test_uncrawl_seed_and_clear_resets_probed_slugs(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            try:
                storage.mark_base_slug_probed("fresh-slug")
                self.assertTrue(storage.has_base_slug_probed("fresh-slug"))

                storage.uncrawl_seed("https://telegra.ph/fresh-slug-10-24")
                self.assertFalse(storage.has_base_slug_probed("fresh-slug"))

                storage.mark_base_slug_probed("fresh-slug-2")
                self.assertTrue(storage.has_base_slug_probed("fresh-slug-2"))
                storage.clear_crawled_urls()
                self.assertFalse(storage.has_base_slug_probed("fresh-slug-2"))
            finally:
                storage.close()

    def test_matroska_mkv_detection_larger_buffer(self):
        # Header with EBML metadata before "matroska" at byte 80
        mkv_bytes = (
            b"\x1a\x45\xdf\xa3"
            + b"\x00" * 76
            + b"matroska"
            + b"\x00" * 100
        )
        self.assertEqual(detect_media_type_from_bytes(mkv_bytes), ("video/x-matroska", ".mkv"))

    async def test_download_file_creates_download_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as td:
            sub_download = Path(td) / "deep" / "nested" / "downloads"
            cfg = ScraperConfig(
                db_path=Path(td) / "test.db",
                discovered_seeds_file=Path(td) / "seeds.txt",
                download_dir=sub_download,
            )
            storage = Storage(cfg.db_path, cfg.discovered_seeds_file)
            try:
                mock_client = MagicMock()

                def fake_stream(*args, **kwargs):
                    class FakeResp:
                        status_code = 200

                        def __init__(self):
                            self.headers = {"content-type": "image/jpeg", "content-length": "25000"}

                        async def aiter_bytes(self, chunk_size=32768):
                            yield b"\xff\xd8\xff\xe0" + b"X" * 25000

                    class FakeStreamContext:
                        async def __aenter__(self):
                            return FakeResp()

                        async def __aexit__(self, *exc):
                            pass

                    return FakeStreamContext()

                mock_client.stream = fake_stream
                self.assertFalse(sub_download.exists())
                res = await download_file(mock_client, "https://telegra.ph/file/test.jpg", cfg, storage)
                self.assertIsNotNone(res)
                self.assertTrue(sub_download.exists())
            finally:
                storage.close()

    def test_browser_profiles_coherence(self):
        for profile in BROWSER_PROFILES:
            ua = profile["User-Agent"]
            if "Firefox" in ua or "Safari" in ua and "Chrome" not in ua:
                self.assertNotIn("Sec-Ch-Ua", profile)
            if "Windows" in ua and "Chrome" in ua:
                self.assertEqual(profile.get("Sec-Ch-Ua-Platform"), '"Windows"')

    async def test_telegram_web_preview_pagination_stops_at_channel_start(self):
        # Simulate Telegram channel start page having only ?after=1 pagination button
        html = """
        <div class="tgme_page">
            <a class="tme_messages_more js-messages_more" href="/s/durov?after=1">Load newer</a>
            <div class="tgme_widget_message" data-post="durov/1">
                <div class="tgme_widget_message_text">First post ever</div>
            </div>
        </div>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = httpx.URL("https://t.me/s/durov?before=5")
        mock_resp.text = html

        _, _, _, before_url = await scrape_telegram_web_preview(
            MagicMock(),
            "https://t.me/s/durov?before=5",
            fetch_fn=AsyncMock(return_value=mock_resp),
        )
        self.assertIsNone(before_url)

    def test_urls_case_insensitivity(self):
        self.assertEqual(canonicalize_seed("HTTPS://T.ME/DUROV"), "durov")
        self.assertEqual(
            canonicalize_seed("HTTPS://TELEGRA.PH/SAMPLE-10-24"), "SAMPLE"
        )
        self.assertEqual(
            extract_base_slug("HTTPS://TELEGRA.PH/sample-10-24"), "sample"
        )

    def test_subdomain_channel_url_normalization(self):
        self.assertEqual(
            normalize_telegram_url("https://durov.t.me"),
            "https://t.me/s/durov",
        )
        self.assertEqual(
            normalize_telegram_url("https://durov.t.me/42"),
            "https://t.me/s/durov/42",
        )
        self.assertEqual(
            normalize_telegram_url("https://mychannel.telegram.me"),
            "https://t.me/s/mychannel",
        )
        self.assertEqual(
            normalize_telegram_url("https://mychannel.tx.me"),
            "https://t.me/s/mychannel",
        )

    def test_force_crawl_clears_paginated_history_and_queue(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            try:
                base = "https://t.me/s/mychannel"
                page2 = "https://t.me/s/mychannel?before=100"
                page3 = "https://t.me/s/mychannel?before=80"
                storage.mark_url_crawled(base)
                storage.mark_url_crawled(page2)
                storage.mark_url_crawled(page3)
                storage.enqueue("https://t.me/s/mychannel?before=60", "telegram", 0)

                self.assertTrue(storage.has_url_crawled(base))
                self.assertTrue(storage.has_url_crawled(page2))
                self.assertTrue(storage.has_url_crawled(page3))

                storage.uncrawl_seed("mychannel")

                self.assertFalse(storage.has_url_crawled(base))
                self.assertFalse(storage.has_url_crawled(page2))
                self.assertFalse(storage.has_url_crawled(page3))
                batch = storage.get_pending_batch(0)
                self.assertEqual(len(batch), 0)
            finally:
                storage.close()

    def test_detect_media_type_aac_opus(self):
        # ADTS AAC: 0xFFF1 (layer bits 00)
        aac_bytes = b"\xff\xf1\x50\x80" + b"\x00" * 30
        self.assertEqual(detect_media_type_from_bytes(aac_bytes), ("audio/aac", ".aac"))

        # MP3: 0xFFFB (layer bits 01)
        mp3_bytes = b"\xff\xfb\x90\x64" + b"\x00" * 30
        self.assertEqual(detect_media_type_from_bytes(mp3_bytes), ("audio/mpeg", ".mp3"))

        # Ogg Opus
        opus_bytes = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x13OpusHead" + b"\x00" * 30
        self.assertEqual(detect_media_type_from_bytes(opus_bytes), ("audio/opus", ".opus"))

    def test_unwrap_search_url_yandex_and_ddg(self):
        from scapper.discovery import unwrap_search_url

        # DuckDuckGo uddg
        ddg = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Ftelegra.ph%2Farticle-10-24"
        self.assertEqual(unwrap_search_url(ddg), "https://telegra.ph/article-10-24")

        # Yandex redirect
        yandex = "https://yandex.com/clck/jsredir?from=search&url=https%3A%2F%2Ft.me%2Fs%2Fdurov"
        self.assertEqual(unwrap_search_url(yandex), "https://t.me/s/durov")

        # Standard direct link
        direct = "https://t.me/s/durov"
        self.assertEqual(unwrap_search_url(direct), direct)

    async def test_telegram_web_preview_in_message_before_links_not_hijacking_pagination(self):
        html = """
        <div class="tgme_page">
            <div class="tgme_widget_message" data-post="mychannel/100">
                <div class="tgme_widget_message_text">
                    Check this out: <a href="https://t.me/s/otherchannel?before=999">Other</a>
                </div>
            </div>
            <a class="tme_messages_more js-messages_more" href="/s/mychannel?before=90">Load older</a>
        </div>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = httpx.URL("https://t.me/s/mychannel")
        mock_resp.text = html

        _, _, _, before_url = await scrape_telegram_web_preview(
            MagicMock(),
            "https://t.me/s/mychannel",
            fetch_fn=AsyncMock(return_value=mock_resp),
        )
        self.assertEqual(before_url, "https://t.me/s/mychannel?before=90")

    async def test_telegram_web_preview_discovers_raw_telegram_urls_in_message_text(self):
        html = """
        <div class="tgme_page">
            <div class="tgme_widget_message" data-post="mychannel/100">
                <div class="tgme_widget_message_text">
                    Visit https://t.me/unlinked_chan and https://t.me/another_unlinked!
                </div>
            </div>
        </div>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = httpx.URL("https://t.me/s/mychannel")
        mock_resp.text = html

        _, _, discovered, _ = await scrape_telegram_web_preview(
            MagicMock(),
            "https://t.me/s/mychannel",
            fetch_fn=AsyncMock(return_value=mock_resp),
        )
        self.assertIn("https://t.me/s/unlinked_chan", discovered)
        self.assertIn("https://t.me/s/another_unlinked", discovered)

    def test_cli_seed_deduplication_preserves_distinct_dated_telegraph_articles(self):
        seeds = [
            "https://telegra.ph/My-Story-10-24",
            "https://telegra.ph/My-Story-10-25",
            "https://www.telegra.ph/My-Story-10-24",
            "@durov",
            "https://t.me/durov",
            "https://t.me/s/durov",
            "My-Story",
            "wallpaper",
        ]
        unique_seeds: list[str] = []
        seen: set[str] = set()
        for seed in seeds:
            s_clean = seed.strip()
            if not s_clean or s_clean.startswith("#"):
                continue

            norm_ph = normalize_telegraph_url(s_clean)
            if norm_ph:
                key = f"telegraph:{norm_ph.lower()}"
                if key not in seen:
                    seen.add(key)
                    unique_seeds.append(norm_ph)
                continue

            norm_tg = normalize_telegram_url(s_clean)
            if not norm_tg and s_clean.startswith("@"):
                norm_tg = normalize_telegram_url(f"https://t.me/{s_clean.lstrip('@')}")

            if norm_tg:
                m_tg = TELEGRAM_URL_RE.match(norm_tg)
                ch_key = m_tg.group(1).lower() if m_tg else norm_tg.lower()
                key = f"telegram:{ch_key}"
            else:
                canonical = canonicalize_seed(s_clean) or s_clean.lower()
                key = f"seed:{canonical.lower()}"

            if key not in seen:
                seen.add(key)
                unique_seeds.append(s_clean)

        self.assertIn("https://telegra.ph/My-Story-10-24", unique_seeds)
        self.assertIn("https://telegra.ph/My-Story-10-25", unique_seeds)
        self.assertEqual(len([s for s in unique_seeds if "durov" in s.lower()]), 1)
        self.assertEqual(len([s for s in unique_seeds if "My-Story-10-24" in s]), 1)

    def test_uncrawl_seed_like_pattern_escapes_underscores(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            try:
                storage.mark_url_crawled("https://t.me/s/my_channel")
                storage.mark_url_crawled("https://t.me/s/myxchannel")
                storage.mark_url_crawled("https://t.me/s/my1channel")

                storage.uncrawl_seed("my_channel")

                self.assertFalse(storage.has_url_crawled("https://t.me/s/my_channel"))
                self.assertTrue(storage.has_url_crawled("https://t.me/s/myxchannel"))
                self.assertTrue(storage.has_url_crawled("https://t.me/s/my1channel"))
            finally:
                storage.close()

    async def test_non_telegraph_file_urls_rejected_as_media(self):
        html = """
        <article>
            <a href="https://github.com/test/repo/file/doc.txt">Docs</a>
            <a href="https://telegra.ph/file/real_media.jpg">Media</a>
        </article>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = httpx.URL("https://telegra.ph/my-article-10-24")
        mock_resp.text = html
        media, _, _ = await scrape_telegraph_page(
            MagicMock(), "https://telegra.ph/my-article-10-24", fetch_fn=AsyncMock(return_value=mock_resp)
        )
        self.assertIn("https://telegra.ph/file/real_media.jpg", media)
        self.assertNotIn("https://github.com/test/repo/file/doc.txt", media)

    async def test_downloader_magic_byte_extension_correction_and_html_rejection(self):
        webp_bytes = b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"X" * 25000
        html_bytes = b"<!DOCTYPE html><html><body>Error 404</body></html>" + b" " * 25000

        with tempfile.TemporaryDirectory() as td:
            cfg = ScraperConfig(
                db_path=Path(td) / "test.db",
                discovered_seeds_file=Path(td) / "seeds.txt",
                download_dir=Path(td) / "downloads",
            )
            storage = Storage(cfg.db_path, cfg.discovered_seeds_file)
            try:
                # 1. URL ending in .jpg but body is WEBP -> should save as .webp
                mock_client1 = MagicMock()
                class FakeResp1:
                    status_code = 200

                    def __init__(self):
                        self.headers = {"content-type": "application/octet-stream", "content-length": str(len(webp_bytes))}

                    async def aiter_bytes(self, chunk_size=32768):
                        yield webp_bytes

                class FakeContext1:
                    async def __aenter__(self):
                        return FakeResp1()
                    async def __aexit__(self, *exc):
                        pass

                mock_client1.stream = lambda *a, **k: FakeContext1()
                res1 = await download_file(mock_client1, "https://example.com/image.jpg", cfg, storage)
                self.assertIsNotNone(res1)
                assert res1 is not None
                self.assertEqual(res1.suffix, ".webp")

                # 2. URL ending in .jpg but body is HTML -> should be rejected
                mock_client2 = MagicMock()
                class FakeResp2:
                    status_code = 200

                    def __init__(self):
                        self.headers = {"content-type": "application/octet-stream", "content-length": str(len(html_bytes))}

                    async def aiter_bytes(self, chunk_size=32768):
                        yield html_bytes

                class FakeContext2:
                    async def __aenter__(self):
                        return FakeResp2()
                    async def __aexit__(self, *exc):
                        pass

                mock_client2.stream = lambda *a, **k: FakeContext2()
                res2 = await download_file(mock_client2, "https://example.com/fake_image.jpg", cfg, storage)
                self.assertIsNone(res2)
            finally:
                storage.close()

    def test_uncrawl_seed_prefix_safety(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            try:
                storage.mark_url_crawled("https://t.me/s/cat")
                storage.mark_url_crawled("https://t.me/s/caterpillar")
                storage.mark_url_crawled("https://t.me/s/cat?before=100")
                storage.mark_url_crawled("https://telegra.ph/news-10-24")
                storage.mark_url_crawled("https://telegra.ph/newsletter-10-24")

                storage.uncrawl_seed("cat")
                self.assertFalse(storage.has_url_crawled("https://t.me/s/cat"))
                self.assertFalse(storage.has_url_crawled("https://t.me/s/cat?before=100"))
                self.assertTrue(storage.has_url_crawled("https://t.me/s/caterpillar"))

                storage.uncrawl_seed("https://telegra.ph/news-10-24")
                self.assertFalse(storage.has_url_crawled("https://telegra.ph/news-10-24"))
                self.assertTrue(storage.has_url_crawled("https://telegra.ph/newsletter-10-24"))
            finally:
                storage.close()

    def test_ensure_scheme_protocol_relative(self):
        self.assertEqual(ensure_scheme("//t.me/durov"), "https://t.me/durov")
        self.assertEqual(ensure_scheme("//telegra.ph/my-slug"), "https://telegra.ph/my-slug")
        self.assertEqual(normalize_telegram_url("//t.me/durov"), "https://t.me/s/durov")
        self.assertEqual(normalize_telegraph_url("//telegra.ph/my-slug-10-24"), "https://telegra.ph/my-slug-10-24")

    def test_slugify_telegraph_candidates_extracts_base_slug_variants(self):
        candidates = slugify_telegraph_candidates("https://telegra.ph/my-great-story-10-24")
        self.assertIn("my-great-story-10-24", candidates)
        self.assertIn("my-great-story", candidates)
        self.assertIn("mygreatstory", candidates)

    def test_parse_retry_after_naive_datetime(self):
        # Even if HTTP-date parses as offset-naive, it should not raise TypeError
        naive_str = "Sun, 06 Nov 2030 08:49:37"
        wait = _parse_retry_after(naive_str, default=5.0)
        self.assertGreater(wait, 0.5)

    async def test_downloader_permanent_4xx_cached_as_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            cfg = ScraperConfig(download_dir=Path(td) / "downloads")
            try:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 404
                mock_resp.headers = {}

                class FakeContext:
                    async def __aenter__(self):
                        return mock_resp
                    async def __aexit__(self, *exc):
                        pass

                mock_client.stream = lambda *a, **k: FakeContext()
                url = "https://example.com/dead_image.jpg"
                res = await download_file(mock_client, url, cfg, storage)
                self.assertIsNone(res)
                self.assertTrue(storage.has_media_url_downloaded(url))
            finally:
                storage.close()

    async def test_downloader_utf8_bom_html_rejection(self):
        bom_html = b"\xef\xbb\xbf<!DOCTYPE html><html><body>Error</body></html>" + b" " * 25000
        with tempfile.TemporaryDirectory() as td:
            storage = Storage(Path(td) / "test.db", Path(td) / "seeds.txt")
            cfg = ScraperConfig(download_dir=Path(td) / "downloads")
            try:
                mock_client = MagicMock()
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.headers = {"content-type": "application/octet-stream", "content-length": str(len(bom_html))}

                class FakeContext:
                    async def __aenter__(self):
                        return mock_resp
                    async def __aexit__(self, *exc):
                        pass

                async def fake_aiter(*args, **kwargs):
                    yield bom_html

                mock_resp.aiter_bytes = fake_aiter
                mock_client.stream = lambda *a, **k: FakeContext()

                url = "https://example.com/fake_bom.jpg"
                res = await download_file(mock_client, url, cfg, storage)
                self.assertIsNone(res)
                self.assertTrue(storage.has_media_url_downloaded(url))
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
