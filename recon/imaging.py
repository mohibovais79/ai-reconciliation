"""Image preprocessing: validate, EXIF-orient, strip metadata, downscale, re-encode JPEG."""

from __future__ import annotations

import hashlib
import io
import shutil
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from recon.config import Settings

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PDF_EXT = {".pdf"}


class ImageRejected(ValueError):
    """Upload refused with a user-facing reason."""


@dataclass
class PageImage:
    """One register page ready to send to a provider."""

    name: str
    sha256: str          # of the original uploaded bytes (cache key part)
    jpeg: bytes          # processed, metadata-free
    width: int
    height: int


def sha256(data: bytes) -> str:
    """Hex SHA-256 of bytes."""
    return hashlib.sha256(data).hexdigest()


def _process(img: Image.Image, settings: Settings) -> tuple[bytes, int, int]:
    """Orient, convert, downscale and re-encode without any metadata."""
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    side = settings.app.image_max_side
    if max(img.size) > side:
        img.thumbnail((side, side), Image.Resampling.LANCZOS)
    clean = Image.new("RGB", img.size)
    clean.paste(img)
    buf = io.BytesIO()
    clean.save(buf, format="JPEG", quality=settings.app.jpeg_quality, optimize=True)
    return buf.getvalue(), clean.width, clean.height


def _pdf_pages(data: bytes, name: str, settings: Settings) -> list[PageImage]:
    """Render PDF pages (needs optional pymupdf)."""
    try:
        import fitz  # type: ignore
    except ImportError:
        raise ImageRejected(f"{name}: PDF support needs `pip install pymupdf`.") from None
    out = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            jpeg, w, h = _process(img, settings)
            out.append(PageImage(f"{name}#p{i + 1}", sha256(data + bytes([i])), jpeg, w, h))
    return out


def load_pages(name: str, data: bytes, settings: Settings) -> list[PageImage]:
    """Validate one upload and return its processed page(s)."""
    ext = Path(name).suffix.lower()
    if len(data) > settings.app.max_file_mb * 1024 * 1024:
        raise ImageRejected(f"{name}: larger than {settings.app.max_file_mb} MB.")
    if ext in PDF_EXT:
        return _pdf_pages(data, name, settings)
    if ext not in IMAGE_EXT:
        raise ImageRejected(f"{name}: unsupported type (use jpg, png or webp).")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError):
        raise ImageRejected(f"{name}: not a readable image.") from None
    jpeg, w, h = _process(img, settings)
    return [PageImage(name, sha256(data), jpeg, w, h)]


def load_all(files: list[tuple[str, bytes]], settings: Settings) -> list[PageImage]:
    """Load every upload, enforcing the per-run page limit."""
    pages: list[PageImage] = []
    for name, data in files:
        pages.extend(load_pages(name, data, settings))
    if len(pages) > settings.app.max_pages:
        raise ImageRejected(f"{len(pages)} pages uploaded; the limit is {settings.app.max_pages}.")
    return pages


def save_session_image(session_dir: Path, page: PageImage) -> Path:
    """Keep the processed page for the review UI."""
    session_dir.mkdir(parents=True, exist_ok=True)
    p = session_dir / f"{page.sha256[:16]}.jpg"
    p.write_bytes(page.jpeg)
    return p


def delete_session_dir(session_dir: Path) -> None:
    """Remove all images stored for a session."""
    shutil.rmtree(session_dir, ignore_errors=True)
