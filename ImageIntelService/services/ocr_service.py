import hashlib
import logging
import threading
import time
import numpy as np
import easyocr
from PIL import Image, ImageOps, ImageEnhance
import io as _io
import os

logger = logging.getLogger(__name__)

_reader: easyocr.Reader | None = None
_reader_lock = threading.Lock()

# Arabic-Indic digit normalization + common OCR character confusions
_NORM_TABLE = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')

def _norm_text(text: str) -> str:
    return text.translate(_NORM_TABLE).replace('£', '8').replace('\x00', '')

# Resize bounds (longest side in pixels)
MAX_OCR_DIM = 2000
MIN_OCR_DIM = 1200

# If OCR yields fewer chars than this, try the next engine/variant
_MIN_CHARS = 50

# ── In-memory result cache ─────────────────────────────────────────────────────
# Key: SHA-256 hex of raw image bytes → (ocr_results, img_height)
# Prevents re-OCR when the same image is submitted multiple times.
# _cache_lock guards both dicts — required for thread safety when multiple
# concurrent requests hit asyncio.to_thread simultaneously.
_cache: dict[str, tuple[list[dict], int]] = {}
_cache_ts: dict[str, float] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 300.0   # 5 minutes
_CACHE_MAX = 500     # max entries before LRU eviction


def _cache_get(sha256: str) -> tuple[list[dict], int] | None:
    with _cache_lock:
        ts = _cache_ts.get(sha256)
        if ts and time.monotonic() - ts < _CACHE_TTL:
            logger.info("OCR cache hit sha256=%.8s", sha256)
            return _cache[sha256]
    return None


def _cache_set(sha256: str, result: tuple[list[dict], int]) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            oldest = min(_cache_ts, key=lambda k: _cache_ts[k])
            _cache.pop(oldest, None)
            _cache_ts.pop(oldest, None)
        _cache[sha256] = result
        _cache_ts[sha256] = time.monotonic()


# ── EasyOCR singleton ──────────────────────────────────────────────────────────

def get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                _reader = easyocr.Reader(
                    ['en', 'ar'],
                    gpu=False,
                    model_storage_directory=os.getenv('EASYOCR_MODEL_DIR', './easyocr_models'),
                    verbose=False,
                )
    return _reader


# ── Image preprocessing ────────────────────────────────────────────────────────

def _resize_for_ocr(img: Image.Image) -> Image.Image:
    """Scale image to [MIN_OCR_DIM, MAX_OCR_DIM] on longest side.
    Applies histogram equalization for very low-contrast images (std < 35).
    """
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_OCR_DIM:
        scale = MAX_OCR_DIM / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    elif longest < MIN_OCR_DIM:
        scale = MIN_OCR_DIM / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    gray = img.convert('L')
    std = float(np.std(np.array(gray)))
    if std < 35:
        logger.info("Low-contrast image (std=%.1f) — equalization applied", std)
        img = ImageOps.equalize(gray).convert('RGB')

    return img


# ── Tesseract OCR (primary — fast on CPU) ─────────────────────────────────────

def _tesseract_lines(img: Image.Image) -> list[str]:
    """Run Tesseract on three preprocessing variants; return deduplicated lines."""
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = os.getenv(
            'TESSERACT_CMD', r'C:\Program Files\Tesseract-OCR\tesseract.exe'
        )

        gray = img.convert('L')
        variants = [
            img,
            ImageOps.equalize(gray).convert('RGB'),
            ImageOps.autocontrast(gray, cutoff=2).convert('RGB'),
        ]

        seen: set[str] = set()
        lines: list[str] = []
        for v in variants:
            try:
                raw = pytesseract.image_to_string(v, config='--psm 6').strip()
                for line in raw.splitlines():
                    normed = _norm_text(line.strip())
                    if normed and normed not in seen:
                        seen.add(normed)
                        lines.append(normed)
            except Exception:
                pass
        return lines
    except ImportError:
        return []


def _tesseract_results(img: Image.Image, img_height: int) -> list[dict]:
    lines = _tesseract_lines(img)
    if not lines:
        return []
    n = len(lines)
    results = []
    for i, text in enumerate(lines):
        y = (i + 1) * img_height / (n + 1)
        results.append({
            "text": text,
            "confidence": 0.6,
            "y_center": float(y),
            "x_center": float(img.size[0] / 2),
            "source": "tesseract",
        })
    total_chars = sum(len(r["text"]) for r in results)
    logger.info("Tesseract: %d lines, %d chars", n, total_chars)
    return results


# ── EasyOCR (supplemental — handles Arabic/Urdu and varied fonts) ─────────────

def _easyocr_results(img: Image.Image, img_height: int, equalized: bool = False) -> list[dict]:
    src = img
    if equalized:
        gray = img.convert('L')
        src = ImageOps.equalize(gray).convert('RGB')

    arr = np.array(src)
    try:
        raw = get_reader().readtext(arr, detail=1, paragraph=False)
    except Exception as e:
        logger.warning("EasyOCR failed: %s", e)
        return []

    results = []
    for bbox, text, conf in raw:
        if conf < 0.3:
            continue
        y_c = (bbox[0][1] + bbox[2][1]) / 2.0
        x_c = (bbox[0][0] + bbox[2][0]) / 2.0
        results.append({
            "text": _norm_text(text),
            "confidence": float(conf),
            "y_center": float(y_c),
            "x_center": float(x_c),
            "source": "easyocr",
        })
    results.sort(key=lambda r: r["y_center"])
    chars = sum(len(r["text"]) for r in results)
    label = "equalized" if equalized else "standard"
    logger.info("EasyOCR %s: %d items, %d chars", label, len(results), chars)
    return results


# ── Merge helper ───────────────────────────────────────────────────────────────

def _merge(primary: list[dict], supplemental: list[dict]) -> list[dict]:
    """Add supplemental items whose text is not already in primary."""
    if not supplemental:
        return primary
    if not primary:
        return supplemental
    seen = {r["text"].lower() for r in primary}
    merged = list(primary)
    for r in supplemental:
        if r["text"].lower() not in seen:
            merged.append(r)
            seen.add(r["text"].lower())
    merged.sort(key=lambda r: r["y_center"])
    return merged


def _chars(results: list[dict]) -> int:
    return sum(len(r["text"]) for r in results)


# ── Main entry point ───────────────────────────────────────────────────────────

def extract_text(image_bytes: bytes) -> tuple[list[dict], int]:
    """
    Returns (ocr_results, image_height).

    Pipeline (Tesseract-first for CPU-only servers):
      Pass 1 — Tesseract 3 variants, PSM 6 (~3-9 s)
               If ≥ _MIN_CHARS → done
      Pass 2 — EasyOCR standard (~5-15 s)
               If ≥ _MIN_CHARS → merge with Tesseract and return
      Pass 3 — EasyOCR equalized (~5-15 s additional)
               Merge all, return best

    Result is cached by SHA-256 for _CACHE_TTL seconds so repeated submissions
    of the same image are answered instantly without re-running OCR.
    """
    sha256 = hashlib.sha256(image_bytes).hexdigest()

    cached = _cache_get(sha256)
    if cached is not None:
        return cached

    img = Image.open(_io.BytesIO(image_bytes)).convert('RGB')
    img = _resize_for_ocr(img)
    img_height = img.size[1]

    # ── Pass 1: Tesseract (fast primary) ──────────────────────────────────────
    t_results = _tesseract_results(img, img_height)
    if _chars(t_results) >= _MIN_CHARS:
        result = (t_results, img_height)
        _cache_set(sha256, result)
        return result

    # ── Pass 2: EasyOCR standard ──────────────────────────────────────────────
    logger.info("Tesseract: %d chars — trying EasyOCR", _chars(t_results))
    e_results = _easyocr_results(img, img_height, equalized=False)

    if _chars(e_results) >= _MIN_CHARS:
        merged = _merge(e_results, t_results)  # EasyOCR primary when it wins
        result = (merged, img_height)
        _cache_set(sha256, result)
        return result

    # ── Pass 3: EasyOCR equalized ──────────────────────────────────────────────
    logger.info("EasyOCR standard: %d chars — trying equalized", _chars(e_results))
    e_eq = _easyocr_results(img, img_height, equalized=True)
    if _chars(e_eq) > _chars(e_results):
        e_results = e_eq

    # Combine everything; EasyOCR takes precedence over Tesseract when it has more
    if _chars(e_results) >= _chars(t_results):
        merged = _merge(e_results, t_results)
    else:
        merged = _merge(t_results, e_results)

    result = (merged, img_height)
    _cache_set(sha256, result)
    return result


def get_full_text(ocr_results: list[dict]) -> str:
    return "\n".join(r["text"] for r in ocr_results)
