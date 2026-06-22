import logging
import re
import os
import json
from dataclasses import dataclass
from typing import Optional
from datetime import date

# ── Pakistani receipt amount patterns ─────────────────────────────────────────
#
# SLASH_NOTATION  — 500/-  Rs.500/-  PKR 1,250.00/-  (most common in PK)
# [\s.]{0,3} before /- tolerates OCR noise: "85 . /-" → 85
SLASH_NOTATION = re.compile(
    r'(?:rs\.?\s*|pkr\.?\s*|₨\s*)?'
    r'(\d{3,9}(?:\.\d{1,2})?'
    r'|\d{1,3}(?:[,،]\d{3})+(?:\.\d{1,2})?'
    r'|\d{1,2}(?:\.\d{1,2})?)'
    r'[\s.]{0,3}/-',
    re.IGNORECASE,
)

# AMOUNT_RE — general PKR amount with optional currency prefix
AMOUNT_RE = re.compile(
    r'(?:rs\.?\s*|pkr\.?\s*|₨\s*)'
    r'(\d{1,9}(?:[,،]\d{3})*(?:\.\d{1,2})?)',
    re.IGNORECASE,
)

# BARE_AMOUNT_RE — plain digits used only with keyword context (stage_b / stage_c)
BARE_AMOUNT_RE = re.compile(
    r'\b(\d{1,3}(?:[,،]\d{3})+(?:\.\d{1,2})?'   # comma-separated thousands
    r'|\d{3,9}(?:\.\d{1,2})?)',                   # 3-9 digit integer or decimal
)

# Tesseract lines to skip in Stage B same-line matching.
# These contain phone numbers, GST IDs, or date fields that look like amounts.
_TESSERACT_NOISY_LINE = re.compile(
    r'\bgst\s*no\b|\bmobile\b|\bph(?:one|\.?\s*no)\b'
    r'|\bdue\s+da[lt]\w*\b|\binv\w*\s+date\b|\bd\.?l\.?\s*no\b',
    re.IGNORECASE,
)

# Financial keywords — used to anchor bare-number extraction
# Word boundaries prevent "net" matching inside "annex", "fee" inside "feel" etc.
TOTAL_KEYWORDS = re.compile(
    r'\btotal\b|\bgrand\b|\bnet\b|\bpayable\b|\bamount\b|\bdue\b'
    r'|\bbalance\b|\bbill\b|\bfee\b|\bcharges\b|\bpayment\b|\breceipt\b'
    r'|\binvoice\b|\bsubtotal\b|\bsub-total\b'
    r'|واجب|مجموعی|کل\s*رقم|بل|فیس|ادائیگی|رقم',
    re.IGNORECASE,
)

_groq_client = None
logger = logging.getLogger(__name__)

_GROQ_VISION_MODEL = os.getenv('GROQ_VISION_MODEL', 'meta-llama/llama-4-scout-17b-16e-instruct')


@dataclass
class ExtractionResult:
    amount: Optional[float]
    source: str  # slash_notation | keyword_match | spatial_bottom | groq_vision | groq_fallback | not_found


# ── Amount validation ──────────────────────────────────────────────────────────

def is_valid_pkr_amount(value: float) -> bool:
    """Reject values that are clearly not PKR medical charges."""
    if value < 10 or value > 500_000:
        return False
    # Reject integer values that look like phone numbers (>= 10 digits)
    if value == float(int(value)) and int(value) >= 10_000_000_000:
        return False
    # Reject integer values that match the current or adjacent calendar years —
    # these appear as bare numbers in receipt dates/headers, not as amounts.
    # Only a 3-year window (prev, current, next) so Rs.2000 / Rs.2010 are NOT rejected.
    iv = int(value)
    current_year = date.today().year
    if current_year - 1 <= iv <= current_year + 1 and value == float(iv):
        return False
    return True


_ARABIC_INDIC = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')


def parse_amount(raw: str) -> Optional[float]:
    normalized = (
        str(raw)
        .translate(_ARABIC_INDIC)
        .replace('£', '8')
        .replace(',', '')
        .replace('،', '')
        .strip()
    )
    try:
        return float(normalized)
    except (ValueError, TypeError):
        return None


def _best_amount(matches: list[str]) -> Optional[float]:
    """Return the rightmost valid PKR amount from regex match strings."""
    for raw in reversed(matches):
        val = parse_amount(raw)
        if val and is_valid_pkr_amount(val):
            return val
    return None


def _extract_from_text(text: str) -> Optional[float]:
    """Try slash notation first, then currency-prefixed amounts, then bare amounts."""
    val = _best_amount(SLASH_NOTATION.findall(text))
    if val:
        return val
    val = _best_amount(AMOUNT_RE.findall(text))
    if val:
        return val
    return _best_amount(BARE_AMOUNT_RE.findall(text))


# ── Stage A — slash notation (/- suffix) ──────────────────────────────────────

def stage_a_slash_notation(ocr_results: list[dict], img_height: int) -> Optional[ExtractionResult]:
    """
    Detect amounts with /- suffix anywhere on the receipt.
    Uses ALL OCR results (Tesseract + EasyOCR) — slash notation is distinctive
    enough to be reliable even from lower-confidence sources.
    Returns the bottom-most match (last total wins on multi-section receipts).
    """
    candidates = []
    for r in ocr_results:
        for raw in SLASH_NOTATION.findall(r["text"]):
            val = parse_amount(raw)
            if val and is_valid_pkr_amount(val):
                candidates.append((val, r["y_center"]))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1], reverse=True)
    return ExtractionResult(candidates[0][0], 'slash_notation')


# ── Stage B — financial keyword + adjacent amount ─────────────────────────────

def stage_b_keyword_match(ocr_results: list[dict]) -> Optional[ExtractionResult]:
    """
    Scan for a financial keyword then extract the nearest amount.

    Pass 1 — EasyOCR: full spatial proximity (y_center is pixel-accurate).
    Pass 2 — Tesseract: same-line and next-line only; spatial proximity skipped
              because Tesseract y_centers are synthetic proportional estimates,
              not actual pixel positions.
    """
    reliable = [r for r in ocr_results if r.get("source") != "tesseract"]
    for i, r in enumerate(reliable):
        if not TOTAL_KEYWORDS.search(r["text"]):
            continue
        probe_texts = [r["text"]]
        if i + 1 < len(reliable):
            probe_texts.append(reliable[i + 1]["text"])
        kw_y = r["y_center"]
        for other in reliable:
            if other is not r and abs(other["y_center"] - kw_y) <= 30:
                probe_texts.append(other["text"])
        for text in probe_texts:
            val = _extract_from_text(text)
            if val:
                return ExtractionResult(val, 'keyword_match')

    # Tesseract pass — fires when EasyOCR was skipped (Tesseract got ≥50 chars).
    # Same-line: only 30 chars after keyword end (amount must be adjacent; phone/GST
    #            numbers buried in long lines are ignored).
    # Same-line: skip lines containing phone/GST/date metadata (noisy false-positives).
    # Next-line: full extraction (single-value lines after a keyword header are clean).
    tesseract = [r for r in ocr_results if r.get("source") == "tesseract"]
    for i, r in enumerate(tesseract):
        kw_match = TOTAL_KEYWORDS.search(r["text"])
        if not kw_match:
            continue
        if not _TESSERACT_NOISY_LINE.search(r["text"]):
            suffix = r["text"][kw_match.end():][:30]
            val = _extract_from_text(suffix)
            if val:
                return ExtractionResult(val, 'keyword_match')
        if i + 1 < len(tesseract):
            val = _extract_from_text(tesseract[i + 1]["text"])
            if val:
                return ExtractionResult(val, 'keyword_match')

    return None


# ── Stage C — spatial bottom 30% ─────────────────────────────────────────────

def _has_keyword_neighbour(idx: int, items: list[dict], radius: int = 2) -> bool:
    lo = max(0, idx - radius)
    hi = min(len(items), idx + radius + 1)
    return any(TOTAL_KEYWORDS.search(items[j]["text"]) for j in range(lo, hi))


def stage_c_spatial_bottom(ocr_results: list[dict], img_height: int) -> Optional[ExtractionResult]:
    """
    Walk the bottom 30% of EasyOCR results bottom-to-top.
    Accepts:
      • /- suffix — no keyword needed (unambiguous)
      • Rs./PKR prefix on same line — no keyword needed
      • Bare number — only if a financial keyword is within ±2 lines
    Tesseract excluded: proportional y_centers are not reliable for spatial checks.
    """
    if img_height == 0:
        return None

    reliable = [r for r in ocr_results if r.get("source") != "tesseract"]
    threshold_y = img_height * 0.70
    bottom = [r for r in reliable if r["y_center"] >= threshold_y]
    full_idx = {id(r): i for i, r in enumerate(reliable)}

    for r in reversed(bottom):
        for raw in SLASH_NOTATION.findall(r["text"]):
            val = parse_amount(raw)
            if val and is_valid_pkr_amount(val):
                return ExtractionResult(val, 'spatial_bottom')

        val = _best_amount(AMOUNT_RE.findall(r["text"]))
        if val:
            return ExtractionResult(val, 'spatial_bottom')

        val = _best_amount(BARE_AMOUNT_RE.findall(r["text"]))
        if val:
            fi = full_idx.get(id(r), -1)
            if fi >= 0 and _has_keyword_neighbour(fi, reliable):
                return ExtractionResult(val, 'spatial_bottom')

    return None


# ── Stage D — Groq Vision ─────────────────────────────────────────────────────

VISION_PROMPT = (
    "This is a Pakistani medical receipt or bill image.\n"
    "Find the FINAL PAYABLE AMOUNT — the total the patient must pay.\n"
    'Return ONLY this JSON — nothing else:\n{"grand_total": <plain number or null>}\n\n'
    "Rules: plain number (500 not Rs.500/-), no commas (1500 not 1,500), "
    "multiple amounts -> FINAL/BOTTOM-MOST total."
)


def get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import AsyncGroq
        base_url = os.getenv('GROQ_BASE_URL')
        _groq_client = AsyncGroq(base_url=base_url) if base_url else AsyncGroq()
    return _groq_client


def _prepare_vision_image(image_bytes: bytes) -> Optional[bytes]:
    """Resize for vision: upscale tiny images to 800px, downscale large to 1024px."""
    import io as _io
    try:
        from PIL import Image
        img = Image.open(_io.BytesIO(image_bytes)).convert('RGB')
        w, h = img.size
        longest = max(w, h)
        if longest < 800:
            scale = 800 / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        elif longest > 1024:
            scale = 1024 / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format='JPEG', quality=95)
        return buf.getvalue()
    except Exception as e:
        logger.warning("Vision image prep failed: %s", e)
        return None


def _parse_vision_json(content: str) -> Optional[float]:
    """Extract grand_total float from vision model response (handles embedded JSON)."""
    raw = None
    try:
        data = json.loads(content)
        raw = data.get("grand_total")
    except (json.JSONDecodeError, AttributeError):
        m = re.search(r'\{[^{}]*"grand_total"\s*:\s*([^{}]*?)\}', content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                raw = data.get("grand_total")
            except Exception:
                pass
    if raw is None:
        return None
    val = parse_amount(str(raw))
    return val if val and is_valid_pkr_amount(val) else None


async def stage_d_groq_vision(image_bytes: bytes) -> Optional[ExtractionResult]:
    """
    Send receipt image to Groq Vision for direct amount extraction.
    Handles tiny images, low-contrast handwriting, and any case where
    Tesseract/EasyOCR cannot recover usable text.
    Privacy: image sent to Groq's API only when local OCR stages fail.
    """
    import base64 as _b64

    prepared = _prepare_vision_image(image_bytes)
    if prepared is None:
        return None

    b64_data = _b64.b64encode(prepared).decode()

    try:
        client = get_groq_client()
        r = await client.chat.completions.create(
            model=_GROQ_VISION_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "You are a JSON-only API. Output ONLY a JSON object — no explanations.",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"},
                        },
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                },
            ],
            max_tokens=256,
            temperature=0,
        )
        content = (r.choices[0].message.content or '').strip()
        logger.debug("Groq Vision raw: %s", content)

        val = _parse_vision_json(content)
        if val is not None:
            logger.info("Groq Vision: extracted %.2f", val)
            return ExtractionResult(val, 'groq_vision')
        logger.debug("Groq Vision: no valid amount in response")

    except Exception as e:
        msg = str(e)
        if '429' in msg or 'rate_limit' in msg.lower():
            logger.warning("Groq Vision rate-limited — falling back to text: %s", msg[:120])
        else:
            logger.warning("Groq Vision API failed: %s", e)

    return None


# ── Stage D — text-only Groq fallback (no image sent) ────────────────────────

GROQ_SYSTEM = """You are a Pakistani medical receipt amount extractor. Today: {today}.

RECEIPT TYPES: OPD slips, pharmacy bills, doctor consultation fees, lab tests, hospital admissions.

COMMON AMOUNT FORMATS on Pakistani receipts:
- "500/-"  "Rs.500/-"  "PKR 500/-"  (slash notation — very common)
- "Rs 500"  "Rs.500"  "PKR 500"  (currency prefix, no /-)
- "Total: 500"  "Grand Total: 1,250"  (labelled, colon separator)
- "Grand Total   500.00"  (labelled, space separator)
- "500" alone at the bottom of the receipt (doctor writes only the fee)
- Amounts may use commas: "1,500" means one thousand five hundred

YOUR TASK: identify the single FINAL PAYABLE AMOUNT — what the patient paid.

RULES (follow strictly):
1. Return ONLY valid JSON: {{"grand_total": <number or null>}}
2. Output a plain number — no Rs, no /-, no commas (e.g. 1250 not "1,250/- PKR")
3. Multiple amounts present → return the LAST / BOTTOM-MOST / LARGEST total
4. /- suffix → almost always the final total; prefer it over unlabelled numbers
5. Only ONE amount in the text → return it regardless of whether it's labelled
6. Cannot identify with confidence → return null
7. NEVER invent or guess an amount not present in the text"""


async def stage_d_groq_fallback(full_ocr_text: str) -> Optional[ExtractionResult]:
    from services.sanitizer import sanitize_for_external
    sanitized = sanitize_for_external(full_ocr_text)
    if not sanitized.strip():
        return None
    try:
        client = get_groq_client()
        response = await client.chat.completions.create(
            model=os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile'),
            messages=[
                {"role": "system", "content": GROQ_SYSTEM.format(today=date.today())},
                {"role": "user",   "content": f"Receipt OCR text:\n{sanitized}"},
            ],
            response_format={"type": "json_object"},
            max_tokens=80,
            temperature=0,
        )
        result = json.loads(response.choices[0].message.content)
        raw = result.get("grand_total")
        if raw is None:
            return None
        val = parse_amount(str(raw))
        if val and is_valid_pkr_amount(val):
            return ExtractionResult(val, 'groq_fallback')
    except Exception as e:
        logger.debug("Groq fallback failed: %s", e)
    return None


# ── Orchestrator ───────────────────────────────────────────────────────────────

async def extract_grand_total(
    ocr_results: list[dict],
    img_height: int,
    full_text: str,
    image_bytes: bytes | None = None,
) -> ExtractionResult:
    """Run all stages in priority order. First non-None result wins.

    Stages A/B/C: regex + spatial extraction from OCR text (no external calls).
    Stage D: Groq Vision when OCR stages fail and image_bytes available;
             falls back to text-only Groq when vision fails or is rate-limited.
    """
    result = (
        stage_a_slash_notation(ocr_results, img_height)
        or stage_b_keyword_match(ocr_results)
        or stage_c_spatial_bottom(ocr_results, img_height)
    )

    if result is None:
        if image_bytes:
            result = await stage_d_groq_vision(image_bytes)
        if result is None:
            result = await stage_d_groq_fallback(full_text)

    return result or ExtractionResult(None, 'not_found')
