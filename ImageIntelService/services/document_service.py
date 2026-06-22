"""
document_service.py — normalise any supported file format to raster JPEG/PNG bytes
so the rest of the OCR pipeline sees a plain image regardless of what the client sent.

Supported conversions:
  PDF  → JPEG  (first page, 2× zoom for OCR quality; direct text extraction for digital PDFs)
  SVG  → PNG   (rasterised via svglib/reportlab)
  JPEG / JPG / PNG / WEBP / BMP / TIFF → returned unchanged

For digital (text-based) PDFs the embedded text is extracted and returned alongside
the rendered image so amount_extraction can run directly on clean text instead of
relying on OCR character recognition.
"""

import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# PDF magic bytes: %PDF
_PDF_MAGIC = b"%PDF"

# SVG: starts with '<svg' or '<?xml' (XML wrapper) — check first 512 bytes
def _is_svg(data: bytes) -> bool:
    head = data[:512].lstrip()
    return head.startswith(b"<svg") or head.startswith(b"<?xml") or b"<svg" in head[:256]


def _pdf_to_image(pdf_bytes: bytes) -> tuple[bytes, Optional[str]]:
    """
    Render the first page of a PDF to JPEG at 2× zoom (≈144 DPI).
    Also attempt direct text extraction for digital (non-scanned) PDFs.

    Returns (jpeg_bytes, extracted_text_or_None).
    extracted_text is set only when the PDF contains selectable text (≥100 chars),
    in which case it can substitute for OCR on the rendered image.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError("pymupdf is required for PDF support: pip install pymupdf")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Cannot open PDF: {e}") from e

    if doc.page_count == 0:
        raise ValueError("PDF has no pages")

    page = doc[0]

    # Direct text extraction (works for digitally generated PDFs)
    raw_text = page.get_text("text").strip()
    extracted_text: Optional[str] = raw_text if len(raw_text) >= 100 else None
    if extracted_text:
        logger.info("PDF text extraction: %d chars (skipping OCR fallback)", len(extracted_text))
    else:
        logger.info("PDF appears scanned/image-based (%d text chars) — will OCR rendered image", len(raw_text))

    # Always render to image for pHash computation
    mat = fitz.Matrix(2, 2)  # 2× = 144 DPI at default 72 DPI base
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    jpeg_bytes = pix.tobytes("jpeg")
    logger.info("PDF page rendered: %dx%d → %d bytes JPEG", pix.width, pix.height, len(jpeg_bytes))
    return jpeg_bytes, extracted_text


def _svg_to_image(svg_bytes: bytes) -> bytes:
    """
    Rasterise an SVG to PNG using svglib + reportlab.
    Falls back to a ValueError if svglib is not installed.
    """
    try:
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPM
    except ImportError:
        raise RuntimeError("svglib and reportlab are required for SVG support: pip install svglib reportlab")

    try:
        drawing = svg2rlg(io.BytesIO(svg_bytes))
        if drawing is None:
            raise ValueError("svglib could not parse SVG content")
        png_bytes = renderPM.drawToString(drawing, fmt="PNG")
        logger.info("SVG rasterised → %d bytes PNG", len(png_bytes))
        return png_bytes
    except Exception as e:
        raise ValueError(f"SVG rasterisation failed: {e}") from e


def normalize_to_image(raw_bytes: bytes) -> tuple[bytes, Optional[str]]:
    """
    Normalise any supported format to raster image bytes.

    Returns:
        (image_bytes, extracted_text)
        - image_bytes : JPEG/PNG suitable for PIL/Tesseract/EasyOCR
        - extracted_text : non-None only for text-based PDFs; None for all other formats
    """
    if raw_bytes[:4] == _PDF_MAGIC:
        return _pdf_to_image(raw_bytes)

    if _is_svg(raw_bytes):
        return _svg_to_image(raw_bytes), None

    # Already a raster format (JPEG, PNG, WEBP, BMP, TIFF)
    return raw_bytes, None
