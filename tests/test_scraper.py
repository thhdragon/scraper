import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from scapper.config import ScraperConfig
from scapper.constants import (
    TELEGRAM_MENTION_RE,
    TELEGRAM_RESERVED,
    TELEGRAPH_RESERVED,
    TELEGRAPH_URL_RE,
)
from scapper.discovery import unwrap_ddg
from scapper.downloader import download_file
from scapper.storage import Storage
from scapper.telegram_api import download_telethon_media
from scapper.telegram_web import scrape_telegram_web_preview
from scapper.telegraph import scrape_telegraph_page
from scapper.urls import (
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


if __name__ == "__main__":
    unittest.main()
