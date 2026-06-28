import logging
import re
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
# (?<!\.) prevents matching digits that immediately follow a decimal point —
# OCR can garble "30,500.00" as "90.50000", which would otherwise match "50000"
# as a standalone amount (the "." creates a spurious word boundary before "5").
BARE_AMOUNT_RE = re.compile(
    r'(?<!\.)\b(\d{1,3}(?:[,،]\d{3})+(?:\.\d{1,2})?'   # comma-separated thousands
    r'|\d{3,9}(?:\.\d{1,2})?)',                           # 3-9 digit integer or decimal
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
# Intentionally excluded:
#   \binvoice\b — "INVOICE/BILL" headers / "Invoice No:" → false positives (e.g. extracts
#                 invoice year "2023" as an amount). "Invoice Total/Amount" is caught by
#                 \btotal\b / \bamount\b.
#   \bbill\b    — same reason: "INVOICE/BILL" document header, not a total field.
#   \breceipt\b — "PAYMENT RECEIPT" header is a label, not a total field.
TOTAL_KEYWORDS = re.compile(
    r'\btotal\b|\bgrand\b|\bnet\b|\bpayable\b|\bamount\b|\bdue\b'
    r'|\bbalance\b|\bfee\b|\bcharges\b|\bpayment\b'
    r'|\bsubtotal\b|\bsub-total\b'
    r'|واجب|مجموعی|کل\s*رقم|بل|فیس|ادائیگی|رقم',
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    amount: Optional[float]
    source: str  # slash_notation | keyword_match | spatial_bottom | not_found


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
    Uses ALL OCR results (Tesseract + EasyOCR) — slash notation is distinctive.
    Returns the LARGEST value: the grand total is always the largest /- amount,
    and this is robust against multi-pass OCR where y_center values across
    different image variants are not comparable.
    """
    best: Optional[float] = None
    for r in ocr_results:
        for raw in SLASH_NOTATION.findall(r["text"]):
            val = parse_amount(raw)
            if val and is_valid_pkr_amount(val):
                if best is None or val > best:
                    best = val
    return ExtractionResult(best, 'slash_notation') if best is not None else None


# ── Stage B — financial keyword + adjacent amount ─────────────────────────────

def stage_b_keyword_match(ocr_results: list[dict]) -> Optional[ExtractionResult]:
    """
    Scan for a financial keyword and collect ALL nearby amounts, then return the LARGEST.

    "Largest wins" is correct because the grand total is always larger than any
    subtotal, line item, or tax figure on the same receipt.  It also handles
    composite images with multiple receipts (two-receipts-side-by-side, etc.)
    where simple top-to-bottom first-match logic would pick a subtotal.

    Pass 1 — EasyOCR: full spatial proximity (y_center is pixel-accurate).
    Pass 2 — Tesseract: same-line (full suffix, no char limit) and next-line;
              spatial proximity skipped because Tesseract y_centers are synthetic.
    """
    candidates: list[float] = []

    # EasyOCR pass
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
                candidates.append(val)
                break  # one candidate per keyword line

    # Tesseract pass — fires when EasyOCR found nothing (Tesseract got ≥50 chars).
    # Same-line: full suffix after keyword (no char limit — rightmost amount via
    #            _best_amount handles "Subtotal: 9,000 | Grand Total: 30,500" correctly).
    # Same-line: skip lines with phone/GST/date metadata (noisy false-positives).
    # Next-line: full extraction (single-value lines after a keyword header are clean).
    tesseract = [r for r in ocr_results if r.get("source") == "tesseract"]
    for i, r in enumerate(tesseract):
        kw_match = TOTAL_KEYWORDS.search(r["text"])
        if not kw_match:
            continue
        if not _TESSERACT_NOISY_LINE.search(r["text"]):
            suffix = r["text"][kw_match.end():]
            val = _extract_from_text(suffix)
            if val:
                candidates.append(val)
        if i + 1 < len(tesseract):
            val = _extract_from_text(tesseract[i + 1]["text"])
            if val:
                candidates.append(val)

    if not candidates:
        return None
    return ExtractionResult(max(candidates), 'keyword_match')


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


# ── Orchestrator ───────────────────────────────────────────────────────────────

def extract_grand_total(
    ocr_results: list[dict],
    img_height: int,
    full_text: str,
) -> ExtractionResult:
    """Run extraction stages A → B → C in priority order. First non-None result wins."""
    return (
        stage_a_slash_notation(ocr_results, img_height)
        or stage_b_keyword_match(ocr_results)
        or stage_c_spatial_bottom(ocr_results, img_height)
        or ExtractionResult(None, 'not_found')
    )
