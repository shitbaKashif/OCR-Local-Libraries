import asyncio
import base64
import logging
from fastapi import APIRouter, HTTPException
from models.process_request import ProcessRequest
from models.process_response import ProcessResponse
from services.document_service import normalize_to_image
from services.ocr_service import extract_text, get_full_text
from services.hash_service import compute_sha256, compute_phash
from services.amount_service import extract_grand_total
from services.date_service import extract_receipt_date

router = APIRouter()
logger = logging.getLogger(__name__)

_phash_cache: dict[str, int] = {}


def _safe_normalize(raw_bytes: bytes) -> tuple[bytes, str | None]:
    try:
        return normalize_to_image(raw_bytes)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.warning("Format normalization failed: %s", e)
        raise HTTPException(status_code=422, detail="Image could not be decoded")


def _safe_extract_text(image_bytes: bytes) -> tuple[list[dict], int]:
    try:
        return extract_text(image_bytes)
    except Exception as e:
        logger.warning("OCR failed (corrupt or unsupported image): %s", e)
        raise HTTPException(status_code=422, detail="Image could not be decoded for OCR")


def _safe_phash(image_bytes: bytes) -> int:
    try:
        return compute_phash(image_bytes)
    except Exception:
        return 0


def _pdf_text_to_ocr_results(text: str, img_height: int) -> tuple[list[dict], int]:
    """
    Convert PDF-extracted text into the same list[dict] format that OCR produces.
    Assigns synthetic y_center values so spatial extraction stages work normally.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    n = len(lines)
    if n == 0:
        return [], img_height
    results = []
    for i, line in enumerate(lines):
        y = (i + 1) * img_height / (n + 1)
        results.append({
            "text": line,
            "confidence": 1.0,
            "y_center": float(y),
            "x_center": float(500),
            "source": "pdf_text",
        })
    return results, img_height


@router.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest) -> ProcessResponse:
    try:
        raw_bytes = base64.b64decode(req.image_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 encoding")

    if len(raw_bytes) == 0:
        raise HTTPException(status_code=400, detail="Image data is empty")

    # Step 1: Normalise format (PDF/SVG → JPEG/PNG; rasters pass through)
    # Run in thread because PDF rendering is CPU-bound
    image_bytes, pdf_text = await asyncio.to_thread(_safe_normalize, raw_bytes)

    # Step 2: SHA-256 first (0.1ms), then check pHash cache before computing it.
    #         OCR runs in parallel with pHash when a cache miss occurs.
    sha256 = await asyncio.to_thread(compute_sha256, raw_bytes)

    cached_ph = _phash_cache.get(sha256)
    ocr_thread = asyncio.to_thread(_safe_extract_text, image_bytes)

    if pdf_text:
        # Text-based PDF: use extracted text directly (perfect quality, no OCR errors)
        # Still render so we get the correct img_height for spatial stages
        try:
            if cached_ph is not None:
                ph = cached_ph
                (_, img_height) = await ocr_thread
            else:
                ph, (_, img_height) = await asyncio.gather(
                    asyncio.to_thread(_safe_phash, image_bytes), ocr_thread)
                _phash_cache[sha256] = ph
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Unexpected error during PDF processing: %s", e)
            raise HTTPException(status_code=500, detail="Image processing failed")
        ocr_results, _ = _pdf_text_to_ocr_results(pdf_text, img_height)
        logger.info("PDF text path: %d OCR lines from direct extraction", len(ocr_results))
    else:
        # Raster image (JPEG/PNG/WEBP/BMP/TIFF) or scanned PDF rendered to image
        try:
            if cached_ph is not None:
                ph = cached_ph
                ocr_results, img_height = await ocr_thread
            else:
                ph, (ocr_results, img_height) = await asyncio.gather(
                    asyncio.to_thread(_safe_phash, image_bytes), ocr_thread)
                _phash_cache[sha256] = ph
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Unexpected error during image processing: %s", e)
            raise HTTPException(status_code=500, detail="Image processing failed")

    full_text = get_full_text(ocr_results)
    extraction   = extract_grand_total(ocr_results, img_height, full_text)
    receipt_date = extract_receipt_date(ocr_results)

    return ProcessResponse(
        sha256_hash=sha256,
        phash=ph,
        grand_total=extraction.amount,
        ocr_text=full_text,
        ocr_success=extraction.amount is not None,
        amount_source=extraction.source,
        receipt_date=receipt_date,
    )
