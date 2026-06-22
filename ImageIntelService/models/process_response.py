from pydantic import BaseModel
from typing import Optional


class ProcessResponse(BaseModel):
    sha256_hash: str
    phash: int
    grand_total: Optional[float]
    ocr_text: str
    ocr_success: bool
    amount_source: str
