import hashlib
import io
import imagehash
from PIL import Image


def compute_sha256(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def compute_phash(image_bytes: bytes) -> int:
    """
    Returns pHash as a signed int64 (compatible with MSSQL BIGINT).
    imagehash.phash returns values up to 2^64-1; MSSQL BIGINT is signed 64-bit.
    Values >= 2^63 are wrapped to negative by two's complement.
    .NET reads BIGINT as long and casts to ulong for XOR/Hamming — bit pattern is preserved.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    ph = imagehash.phash(img)
    value = int(str(ph), 16)
    if value >= (1 << 63):
        value -= (1 << 64)
    return value
