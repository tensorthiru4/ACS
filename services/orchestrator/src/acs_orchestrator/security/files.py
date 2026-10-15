"""Content validation for purchase-order uploads (PDF / voice notes / e-mail text).

The declared Content-Type and filename are attacker-controlled, so the type is
derived from magic bytes and must match an allow-list. PDFs carrying active
content are rejected before they reach Gemini or Cloud Storage.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass


class RejectedFileError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedFile:
    content_type: str
    sha256: str
    size: int
    filename: str


_PDF_ACTIVE_CONTENT = (b"/JavaScript", b"/Launch", b"/EmbeddedFiles", b"/RichMedia", b"/XFA")


def sniff_content_type(data: bytes) -> str | None:
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if data[4:8] == b"ftyp" and data[8:12] in (b"M4A ", b"mp42", b"isom", b"dash"):
        return "audio/mp4"
    return None


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str | None, default: str = "upload") -> str:
    """Strip paths, control characters and odd unicode; keep it short."""
    name = unicodedata.normalize("NFKC", name or "").replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME.sub("_", name).strip("._")
    return (name or default)[:120]


def validate_po_file(
    data: bytes, *, filename: str | None, max_bytes: int, allow_text: bool = False
) -> ValidatedFile:
    if not data:
        raise RejectedFileError("empty file")
    if len(data) > max_bytes:
        raise RejectedFileError(f"file exceeds {max_bytes} bytes")
    content_type = sniff_content_type(data)
    if content_type is None and allow_text:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RejectedFileError("text body is not valid UTF-8") from exc
        if any(unicodedata.category(c) == "Cc" and c not in "\r\n\t" for c in text):
            raise RejectedFileError("text body contains control characters")
        content_type = "text/plain"
    if content_type is None:
        raise RejectedFileError(
            "unsupported file type (allowed: PDF, WAV, MP3, OGG, FLAC, WEBM, M4A)"
        )
    if content_type == "application/pdf":
        if any(marker in data for marker in _PDF_ACTIVE_CONTENT):
            raise RejectedFileError("PDF contains active content")
        if b"/Encrypt" in data:
            raise RejectedFileError("encrypted PDFs are not accepted")
    return ValidatedFile(
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        filename=safe_filename(filename),
    )
