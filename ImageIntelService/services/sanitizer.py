import re

FINANCIAL_KEYWORD_RE = re.compile(
    r'\b(total|grand|net|payable|amount|due|balance|bill|fee|charges'
    r'|tax|vat|gst|discount|subtotal|payment|receipt|invoice'
    r'|واجب|مجموعی|کل|بل|فیس|رقم|ادائیگی)\b',
    re.IGNORECASE
)

MONETARY_RE = re.compile(
    r'(?:rs\.?\s*|pkr\.?\s*|₨\s*)?\d{3,9}(?:[,،]\d{3})*(?:\.\d{1,2})?(?:\s*/-)?',
    re.IGNORECASE
)

PHI_PATTERNS = re.compile(
    r'(?:patient|name|dr\.?|doctor|mr\.?|mrs\.?|ms\.?|age\s*:|'
    r'address|phone|mobile|cell|cnic|ntn|contact|referred)',
    re.IGNORECASE
)


def sanitize_for_external(text: str) -> str:
    """
    Returns only lines that are clearly financial data.
    Excludes lines containing PHI patterns even if they have numbers.
    """
    kept = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if PHI_PATTERNS.search(stripped):
            continue
        if FINANCIAL_KEYWORD_RE.search(stripped):
            kept.append(stripped)
            continue
        if MONETARY_RE.search(stripped):
            alpha_ratio = sum(c.isalpha() for c in stripped) / max(len(stripped), 1)
            if alpha_ratio < 0.4:
                kept.append(stripped)
    return '\n'.join(kept)
