import re
import logging
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Keyword anchors ────────────────────────────────────────────────────────────
# Lines containing these keywords are prioritised for date extraction.
_INVOICE_KW = re.compile(
    r'\binv(?:oice)?\.?\s*date\b'
    r'|\bbill\s*date\b'
    r'|\breceipt\s*date\b'
    r'|\bdate\s*of\s*(?:issue|service|visit|bill)\b'
    r'|\bdate\s*:\s'           # "Date: 25/04/2021"
    r'|\bdated\b'
    r'|\bتاریخ\b'              # Urdu: date
    r'|\bمورخ\b',              # Urdu: dated
    re.IGNORECASE,
)

# Lines with these keywords contain dates that are NOT the receipt issue date.
_EXCLUDE_KW = re.compile(
    r'\bdue\b'                         # "Due Date", "Due Dale" (OCR noise)
    r'|\bexpir[yed]?\b'
    r'|\bexp\.?\s*(?:date|:)'
    r'|\bvalid\s*(?:till|upto|thru|through)\b'
    r'|\bbest\s*before\b'
    r'|\bdelivery\s*date\b'
    r'|\bnext\s*(?:visit|appointment)\b'
    r'|\bd\.?l\.?\s*no\b'              # D.L.No — licence number, not a date
    r'|\bgst\s*no\b',
    re.IGNORECASE,
)

# ── Month name alternation (longest first to avoid prefix shadowing) ───────────
_MONTHS = (
    r'(?:january|february|september|october|november|december'
    r'|august|march|april|june|july|jan|feb|mar|apr|may|jun'
    r'|jul|aug|sep|sept|oct|nov|dec)'
)

# ── Date candidate pattern ─────────────────────────────────────────────────────
# All groups are non-capturing so re.findall returns full-match strings, not tuples.
# (?!\d) lookahead at terminal positions prevents greedy under-matching:
#   without it, "0?[1-9]" matches "1" from "15", leaving "5" unconsumed,
#   yielding "2024-11-1" instead of "2024-11-15".  The lookahead forces
#   backtracking to the [12]\d alternative which greedily consumes "15".
_DATE_RE = re.compile(
    r'(?:'
    # ── DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY (+ optional HH:MM[:SS]) ──
    r'(?:0?[1-9]|[12]\d|3[01])[/\-\. ](?:0?[1-9]|1[0-2])[/\-\. ](?:\d{4}|\d{2})(?!\d)'
    r'(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?'
    r'|'
    # ── YYYY-MM-DD  (ISO 8601) ───────────────────────────────────────
    r'(?:19|20)\d{2}[/\-](?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])(?!\d)'
    r'|'
    # ── DD Mon YYYY  (e.g. "25 Apr 2021", "25-Apr-2021", "21 -Jun-2026") ──
    r'(?:0?[1-9]|[12]\d|3[01])[\s\-]+' + _MONTHS + r'[\s\-,]+(?:19|20)\d{2}(?!\d)'
    r'|'
    # ── Mon DD, YYYY  (e.g. "Apr 25, 2021") ─────────────────────────
    + _MONTHS + r'[\s\-]+(?:0?[1-9]|[12]\d|3[01])[\s,]+(?:19|20)\d{2}(?!\d)'
    r')',
    re.IGNORECASE,
)

# strptime format strings tried in order (most → least common on Pakistani receipts)
_FORMATS = [
    '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%d %m %Y',   # DD sep MM sep YYYY
    '%d/%m/%y', '%d-%m-%y', '%d.%m.%y',                 # DD sep MM sep YY
    '%Y-%m-%d', '%Y/%m/%d',                              # ISO / reversed
    '%d %b %Y', '%d %b, %Y', '%d-%b-%Y',                # DD Mon YYYY
    '%d %B %Y', '%d %B, %Y', '%d-%B-%Y',                # DD Month YYYY
    '%b %d, %Y', '%b %d %Y', '%B %d, %Y', '%B %d %Y',  # Mon DD, YYYY
]


def _parse(raw: str) -> Optional[date]:
    """Try every known strptime format on `raw`. Returns a date or None."""
    # Strip trailing time component ("25/04/2021 12:23:51" → "25/04/2021")
    clean = re.sub(r'\s+\d{1,2}:\d{2}(?::\d{2})?$', '', raw.strip().rstrip(',.'))
    # Normalise OCR separator noise: "21 -Jun-2026" → "21-Jun-2026"
    # (?<=\d)\s+- : space(s) before a dash, when preceded by a digit (day number)
    # -\s+(?=\w)  : dash followed by space(s) before a letter/digit (month name or year)
    clean = re.sub(r'(?<=\d)\s+-', '-', clean)
    clean = re.sub(r'-\s+(?=\w)', '-', clean)
    for fmt in _FORMATS:
        try:
            return datetime.strptime(clean, fmt).date()
        except ValueError:
            continue
    return None


def _plausible(d: date) -> bool:
    """Reject dates that cannot be a Pakistani OPD receipt date."""
    today = date.today()
    if d.year < 2000:
        return False
    if (today - d).days > 6 * 365:   # older than 6 years — impossible receipt
        return False
    if (d - today).days > 14:        # more than 2 weeks in future — not a receipt date
        return False
    return True


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_receipt_date(ocr_results: list[dict]) -> Optional[str]:
    """
    Extract the invoice/receipt date from OCR results.
    Returns ISO-8601 string (YYYY-MM-DD) or None if no plausible date found.

    Strategy:
      Pass 1 — keyword-anchored: lines with invoice date keywords are scanned
                first; excluded-keyword lines (due dates, expiry, D.L.No) are skipped.
      Pass 2 — fallback: any plausible date found in non-excluded lines.
      In both passes the *most recent* plausible date wins — the issue date is the
      latest date appearing on the receipt (older dates are part of transaction history).
    """
    keyword_dates: list[date] = []
    fallback_dates: list[date] = []

    for r in ocr_results:
        text = r.get('text', '')
        if not text:
            continue
        if _EXCLUDE_KW.search(text):
            continue

        candidates = [_parse(m) for m in _DATE_RE.findall(text)]
        valid = [d for d in candidates if d and _plausible(d)]

        if _INVOICE_KW.search(text):
            keyword_dates.extend(valid)
        else:
            fallback_dates.extend(valid)

    if keyword_dates:
        best = max(keyword_dates)
        logger.info("Date extraction (keyword): %s", best.isoformat())
        return best.isoformat()

    if fallback_dates:
        best = max(fallback_dates)
        logger.info("Date extraction (fallback): %s", best.isoformat())
        return best.isoformat()

    logger.info("Date extraction: no plausible date found")
    return None
