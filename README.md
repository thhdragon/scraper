# Scapper

A high-performance media scraper for **Telegram** and **Telegra.ph** built in Python with resume capability, SHA-256 deduplication, intelligent crawler depth management, and hybrid dual-mode operation (public web previews or Telethon API).

---

## Features

- **Dual-Mode Operation**:
  - **Public Web Preview Crawler**: Scrapes public channels via `t.me/s/<channel>` without requiring any Telegram account or API credentials.
  - **Telegram Client API (Telethon)**: High-speed native API mode for direct full-resolution media downloads with automatic `FloodWaitError` backoff and persistent progress tracking.
- **Telegra.ph Extraction & Probing**:
  - Automatically parses linked Telegra.ph articles, extracting inline images, direct video links, and author channels.
  - Suffix and calendar date probing to discover related and multi-part posts (`slug-01-01` through `slug-12-31`).
- **Smart Queue & Depth Tracking**:
  - Breadth-first crawl queue tracked in SQLite.
  - Depth-bounded crawling prevents uncontrolled traversal.
  - Pagination preserves crawl depth instead of inflating depth count per message batch.
  - Channel self-reference loops (permalinks and post timestamps) are filtered out automatically.
- **Content Deduplication & Bandwidth Optimization**:
  - Downloaded media URL cache prevents re-downloading files across pages.
  - Content-based SHA-256 deduplication prevents storing duplicate images/videos across different channels.
  - Offloaded file hashing to worker threads to keep the async event loop responsive.
- **Resilient Storage**:
  - SQLite backend running in Write-Ahead Logging (WAL) mode with connection pooling and busy timeouts.
  - Discovered seeds automatically canonicalized and filtered against reserved routes (`contact`, `about`, `blog`, `file`, `upload`, etc.).

---

## Requirements & Installation

- **Python**: 3.13 or higher
- **Package Manager**: [uv](https://docs.astral.sh/uv/)

Clone the repository and install dependencies:

```bash
git clone <repo-url>
cd scapper
uv sync
```

---

## Quick Start

### 1. Configure Seeds

Copy the example seed file:

```bash
cp seeds.txt.example seeds.txt
```

Add usernames, links, or keywords to `seeds.txt`:

```text
# Telegram channel usernames or URLs
durov
https://t.me/s/example_channel

# Telegra.ph URLs or article slugs
https://telegra.ph/Sample-Article-10-24
Sample-Article

# Discovery keywords (if discovery is enabled)
wallpapers
```

You can also pass seeds directly via the command line:

```bash
uv run scapper durov wallpapers https://telegra.ph/Sample-Article-10-24
```

### 2. Run Scapper

Run the scraper in public web preview mode:

```bash
uv run scapper --seeds seeds.txt
```

---

## Command Line Interface

```text
usage: scapper [-h] [--seeds SEEDS] [--config CONFIG] [--db DB]
               [--download-dir DOWNLOAD_DIR] [--no-api] [--no-discovered]
               [--include-discovered] [--min-size MIN_SIZE]
               [--max-depth MAX_DEPTH] [--discover] [--keywords KEYWORDS]
               [--reset-queue] [--probe-dates] [--verbose]
               [extra_seeds ...]
```

### CLI Options

| Flag | Description |
| :--- | :--- |
| `extra_seeds` | One or more direct channel names, URLs, or search terms to crawl. |
| `--seeds PATH` | Path to the seed file (defaults to `seeds.txt`). |
| `--config PATH` | Path to JSON config file (defaults to `config.json`). |
| `--db PATH` | Override SQLite database file path. |
| `--download-dir PATH` | Override media download directory. |
| `--no-api` | Force public web scraping mode; bypasses Telethon API even if credentials are configured. |
| `--include-discovered`| Force loading previously discovered seeds from `discovered_seeds.txt`. |
| `--no-discovered` | Do not load `discovered_seeds.txt` on startup. |
| `--min-size KB` | Minimum file size in kilobytes (default: 20 KB). |
| `--max-depth DEPTH` | Maximum crawl depth (`0` = seed only, `-1` = infinite). |
| `--probe-dates` | Enable calendar date probing for Telegra.ph articles. |
| `--discover` | Run search engine discovery before crawling. |
| `--keywords WORDS` | Comma-separated discovery keywords (e.g. `wallpaper,nature`). |
| `--force-crawl` | Re-crawl provided seed URLs even if already marked crawled in database. |
| `--reset-crawled` | Clear crawled URLs history in database without clearing downloaded hashes. |
| `--reset-queue` | Clear the pending URL queue in the database before starting. |
| `--timeout SECONDS` | Override HTTP request timeout in seconds. |
| `--max-connections NUM` | Override maximum concurrent HTTP connections. |
| `--verbose` | Enable debug logging. |

---

## Configuration Reference (`config.json`)

```json
{
  "download_dir": "./downloads",
  "db_path": "scraper_cache.db",
  "min_size_kb": 20,

  "max_web_depth": -1,
  "max_downloads_per_page": 0,
  "max_suffix_probes": 0,

  "include_discovered": false,
  "discovered_seeds_file": "discovered_seeds.txt",

  "web_delay_range": [0, 0.1],
  "max_connections": 32,
  "request_timeout": 20.0,

  "probe_telegraph_dates": true,
  "probe_date_concurrency": 32,

  "discovery": {
    "enabled": false,
    "keywords": ["wallpaper"],
    "search_engines": ["duckduckgo"]
  },

  "telegram_api": {
    "api_id": null,
    "api_hash": null,
    "phone": null,
    "session_name": "telegram_media"
  }
}
```

### Key Descriptions

- **`download_dir`**: Destination folder where downloaded images and videos are saved.
- **`db_path`**: SQLite database tracking queue state, crawled pages, and downloaded hashes.
- **`min_size_kb`**: Minimum file size in kilobytes. Setting to `20` preserves web preview photos while ignoring tracker pixels and tiny thumbnails.
- **`max_web_depth`**: Maximum depth to follow discovered links (`0` crawls only initial seeds, `-1` crawls indefinitely).
- **`include_discovered`**: When `true`, automatically reloads channels discovered in past runs from `discovered_seeds.txt`.
- **`probe_telegraph_dates`**: Automatically probes dates (01-01 through 12-31) for base slugs to discover multi-part articles.
- **`telegram_api`**: Optional Telegram API credentials (`api_id`, `api_hash`) obtained from [my.telegram.org](https://my.telegram.org). When left null, Scapper operates seamlessly in public web preview mode.

---

## Development & Testing

All verification and development workflows use `uv`:

### Run Unit Tests
```bash
uv run python -m unittest discover -s tests
```

### Run Linter
```bash
uv run ruff check .
```

### Run Type Checker
```bash
uv run pyrefly check
```
