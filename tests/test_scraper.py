import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from scapper.scraper import (
    TELEGRAM_MENTION_RE,
    TELEGRAM_RESERVED,
    TELEGRAPH_RESERVED,
    TELEGRAPH_URL_RE,
    MediaScraper,
    ScraperConfig,
    Storage,
    detect_media_type_from_bytes,
)


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
        self.assertEqual(m3.group(1), "My-Story-Title-01-02")

    def test_telegraph_url_re_finditer_discovery(self):
        # Critical regression check:
        # re.finditer provides group(0) as full URL for normalization,
        # while re.findall returns only group(1) (the slug).
        text = "Check out https://telegra.ph/sample-article-10-24 for updates."
        matches = list(TELEGRAPH_URL_RE.finditer(text))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].group(0), "https://telegra.ph/sample-article-10-24")
        self.assertEqual(matches[0].group(1), "sample-article-10-24")

        normalized = MediaScraper.normalize_telegraph_url(matches[0].group(0))
        self.assertEqual(normalized, "https://telegra.ph/sample-article-10-24")

    def test_telegram_mention_re(self):
        # Test mentions inside brackets, quotes, and standard text
        self.assertEqual(TELEGRAM_MENTION_RE.findall("Check [@my_channel] now"), ["my_channel"])
        self.assertEqual(TELEGRAM_MENTION_RE.findall('Quote: "@my_channel"'), ["my_channel"])
        self.assertEqual(TELEGRAM_MENTION_RE.findall("(@my_channel)"), ["my_channel"])
        # Email addresses must NOT match
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
        candidates = MediaScraper.slugify_telegram_candidates(url)
        self.assertIn("cool_channel", candidates)
        self.assertNotIn("100", candidates)

    def test_slugify_telegram_candidates_message_permalink(self):
        # Permalinks with message ID must extract the channel username, not message ID
        url = "https://t.me/s/cool_channel/456"
        candidates = MediaScraper.slugify_telegram_candidates(url)
        self.assertIn("cool_channel", candidates)
        self.assertNotIn("456", candidates)

        url_no_s = "https://t.me/cool_channel/789"
        candidates_no_s = MediaScraper.slugify_telegram_candidates(url_no_s)
        self.assertIn("cool_channel", candidates_no_s)

    def test_extract_chat_username(self):
        scraper = MediaScraper(ScraperConfig())
        # Reserved words must be rejected
        self.assertIsNone(scraper._extract_chat_username("contact"))
        self.assertIsNone(scraper._extract_chat_username("https://t.me/about"))

        # Valid channels and custom domains must succeed
        self.assertEqual(scraper._extract_chat_username("durov"), "durov")
        self.assertEqual(scraper._extract_chat_username("https://t.me/s/durov/123"), "durov")
        self.assertEqual(scraper._extract_chat_username("https://telegram.dog/durov"), "durov")

    def test_unwrap_ddg(self):
        # Root-relative DDG links must be unwrapped
        rel_href = "/l/?uddg=https%3A%2F%2Ftelegra.ph%2Fmy-post&rut=123"
        unwrapped = MediaScraper._unwrap_ddg(rel_href)
        self.assertEqual(unwrapped, "https://telegra.ph/my-post")

    def test_detect_media_type_from_bytes(self):
        self.assertEqual(detect_media_type_from_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF"), ("image/jpeg", ".jpg"))
        self.assertEqual(detect_media_type_from_bytes(b"\x89PNG\r\n\x1a\n\x00\x00"), ("image/png", ".png"))
        self.assertEqual(detect_media_type_from_bytes(b"RIFF\x20\x00\x00\x00WEBPVP8 "), ("image/webp", ".webp"))
        self.assertEqual(detect_media_type_from_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00"), ("video/mp4", ".mp4"))
        self.assertEqual(detect_media_type_from_bytes(b"\x00\x00\x00\x1cftypavif\x00\x00"), ("image/avif", ".avif"))
        self.assertIsNone(detect_media_type_from_bytes(b"<html>hello</html>"))

    def test_slugify_telegram_candidates_reserved(self):
        self.assertEqual(MediaScraper.slugify_telegram_candidates("contact"), [])
        self.assertEqual(MediaScraper.slugify_telegram_candidates("https://t.me/s/about"), [])

    def test_slugify_telegraph_candidates_url(self):
        url = "https://telegra.ph/some-great-title-10-24?ref=1"
        candidates = MediaScraper.slugify_telegraph_candidates(url)
        self.assertIn("some-great-title-10-24", candidates)

    def test_slugify_telegraph_candidates_reserved(self):
        self.assertEqual(MediaScraper.slugify_telegraph_candidates("file"), [])
        self.assertEqual(MediaScraper.slugify_telegraph_candidates("https://telegra.ph/upload"), [])

    def test_normalize_telegraph_url(self):
        self.assertIsNone(MediaScraper.normalize_telegraph_url("https://telegra.ph/file/pic.png"))
        self.assertIsNone(MediaScraper.normalize_telegraph_url("https://telegra.ph/upload"))
        self.assertEqual(
            MediaScraper.normalize_telegraph_url("https://telegra.ph/valid-slug-01-01"),
            "https://telegra.ph/valid-slug-01-01",
        )
        self.assertEqual(
            MediaScraper.normalize_telegraph_url("https://www.telegra.ph/valid-slug-01-01"),
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
        self.assertIsNotNone(self.storage._conn)
        self.storage.record_downloaded_hash("hash123", Path("/path/to/file.jpg"))
        self.assertTrue(self.storage.has_hash_downloaded("hash123"))
        self.assertFalse(self.storage.has_hash_downloaded("hash999"))

    def test_downloaded_media_urls_cache(self):
        url = "https://telegra.ph/file/test_image.jpg"
        self.assertFalse(self.storage.has_media_url_downloaded(url))
        self.storage.record_downloaded_media_url(url, "hash123")
        self.assertTrue(self.storage.has_media_url_downloaded(url))

    def test_discovered_seeds_reserved_filtering(self):
        # Adding reserved name should be rejected
        self.storage.add_discovered_seed("contact")
        self.storage.add_discovered_seed("file")
        seeds = self.storage.get_discovered_seeds()
        self.assertNotIn("contact", seeds)
        self.assertNotIn("file", seeds)

        # Valid seed should be accepted
        self.storage.add_discovered_seed("legit_channel")
        seeds = self.storage.get_discovered_seeds()
        self.assertIn("legit_channel", seeds)

    def test_clean_discovered_seeds(self):
        with open(self.seeds_file, "w") as f:
            f.write("contact\nlegit_channel\nfile\nupload\nanother_channel\n")

        self.storage.clean_discovered_seeds()

        with open(self.seeds_file) as f:
            lines = [line.strip() for line in f if line.strip()]

        self.assertNotIn("contact", lines)
        self.assertNotIn("file", lines)
        self.assertNotIn("upload", lines)
        self.assertIn("legit_channel", lines)
        self.assertIn("another_channel", lines)

    def test_clean_discovered_seeds_case_insensitive(self):
        with open(self.seeds_file, "w") as f:
            f.write("MyChannel\nmychannel\nMYCHANNEL\n")

        self.storage.clean_discovered_seeds()

        with open(self.seeds_file) as f:
            lines = [line.strip() for line in f if line.strip()]

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].lower(), "mychannel")

    def test_storage_init_creates_subdirectories(self):
        nested_dir = Path(self.temp_dir.name) / "nested" / "sub"
        db_file = nested_dir / "cache.db"
        seeds_file = nested_dir / "discovered.txt"
        storage = Storage(db_file, seeds_file)
        self.assertTrue(nested_dir.exists())
        self.assertTrue(db_file.exists())
        storage.close()

    def test_min_pending_depth_with_max_depth(self):
        self.storage.enqueue("https://t.me/s/ch1", "telegram", depth=1)
        self.storage.enqueue("https://t.me/s/ch2", "telegram", depth=5)

        # When max_depth is 3, min pending depth within limit should be 1
        min_depth = self.storage.get_min_pending_depth(max_depth=3)
        self.assertEqual(min_depth, 1)

        # Crawl depth 1: remove from queue and mark crawled
        self.storage.remove_from_queue("https://t.me/s/ch1")
        self.storage.mark_url_crawled("https://t.me/s/ch1")

        # Now only depth 5 is pending, but max_depth is 3, so should return None
        min_depth_exceeded = self.storage.get_min_pending_depth(max_depth=3)
        self.assertIsNone(min_depth_exceeded)

        # If max_depth is 10, should return 5
        self.assertEqual(self.storage.get_min_pending_depth(max_depth=10), 5)


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
        cfg = ScraperConfig()
        scraper = MediaScraper(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        scraper._fetch = AsyncMock(return_value=mock_resp)

        media_urls, tg_urls, _ph_urls = await scraper.scrape_telegraph_page(
            MagicMock(), "https://telegra.ph/test-article-10-24"
        )

        # Check media extracted including <img> and <a> media
        self.assertIn("https://telegra.ph/file/image1.jpg", media_urls)
        self.assertIn("https://telegra.ph/file/video1.mp4", media_urls)

        # Check telegram links extracted
        self.assertIn("https://t.me/s/author_channel", tg_urls)
        self.assertIn("https://t.me/s/linked_channel", tg_urls)

    async def test_telegram_web_preview_no_self_reference_loops(self):
        # A preview page for channel "mychannel" contains message permalinks /s/mychannel/123
        # These should NOT be enqueued as new channels!
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
        cfg = ScraperConfig()
        scraper = MediaScraper(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        scraper._fetch = AsyncMock(return_value=mock_resp)

        media_urls, ph_urls, tg_urls, before_url = await scraper.scrape_telegram_web_preview(
            MagicMock(), "https://t.me/s/mychannel"
        )

        # Check media extracted
        self.assertIn("https://cdn.telegram.org/file1.jpg", media_urls)

        # Check pagination before_url extracted
        self.assertEqual(before_url, "https://t.me/s/mychannel?before=101")

        # Crucial bug fix check:
        # tg_urls should contain 'otherchannel', but MUST NOT contain 'mychannel' or the before link!
        self.assertIn("https://t.me/s/otherchannel", tg_urls)
        self.assertNotIn("https://t.me/s/mychannel", tg_urls)
        self.assertNotIn("https://t.me/s/mychannel?before=101", tg_urls)

        # Check telegraph URL extracted
        self.assertIn("https://telegra.ph/article-title-10-24", ph_urls)


if __name__ == "__main__":
    unittest.main()
