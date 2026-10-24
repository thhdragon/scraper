from __future__ import annotations

import logging
import sys
import typing


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def detect_media_type_from_bytes(data: bytes) -> tuple[str, str] | None:
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and len(data) >= 12:
        if data[8:12] == b"WEBP":
            return "image/webp", ".webp"
        if data[8:12] == b"WAVE":
            return "audio/wav", ".wav"
        if data[8:12] == b"AVI ":
            return "video/x-msvideo", ".avi"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        major_brand = data[8:12]
        if major_brand in {b"avif", b"avis"}:
            return "image/avif", ".avif"
        if major_brand in {b"heic", b"heix", b"hevc", b"mif1"}:
            return "image/heic", ".heic"
        if major_brand in {b"M4A ", b"M4B "}:
            return "audio/mp4", ".m4a"
        if major_brand in {b"qt  ", b"moov"}:
            return "video/quicktime", ".mov"
        return "video/mp4", ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        if b"matroska" in data[:64]:
            return "video/x-matroska", ".mkv"
        return "video/webm", ".webm"
    if data.startswith(b"OggS"):
        return "audio/ogg", ".ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac", ".flac"
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
    ):
        return "audio/mpeg", ".mp3"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    return None


def _safe_int(val: typing.Any, default: int, min_val: int | None = None) -> int:
    try:
        res = int(val)
        if min_val is not None:
            res = max(min_val, res)
        return res
    except (TypeError, ValueError):
        return default


def _safe_float(val: typing.Any, default: float, min_val: float | None = None) -> float:
    try:
        res = float(val)
        if min_val is not None:
            res = max(min_val, res)
        return res
    except (TypeError, ValueError):
        return default
