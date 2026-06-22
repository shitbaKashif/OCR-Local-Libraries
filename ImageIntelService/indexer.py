"""
Backfill indexer — runs via Windows Task Scheduler.
Usage:
  python indexer.py --mode backfill      # all unindexed images
  python indexer.py --mode incremental   # images not yet in opd_att_index
"""
import argparse
import hashlib
import io
import logging
import os
import sys
import time
from datetime import datetime

import pyodbc
from dotenv import load_dotenv
from PIL import Image
import imagehash
import easyocr
import numpy as np

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('indexer.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

CONN_STR = os.getenv(
    'DB_CONNECTION',
    'DRIVER={SQL Server};'
    'Server=127.0.0.1,1433;'
    'Database=opd_attachments;'
    'UID=sa;PWD=Pass5432;'
    'TrustServerCertificate=yes;'
)

BATCH_SIZE = 20

_reader: easyocr.Reader | None = None


def get_reader() -> easyocr.Reader:
    global _reader
    if _reader is None:
        model_dir = os.getenv('EASYOCR_MODEL_DIR', './easyocr_models')
        logger.info("Loading EasyOCR model from %s ...", model_dir)
        _reader = easyocr.Reader(['en', 'ar'], gpu=False, model_storage_directory=model_dir, verbose=False)
        logger.info("EasyOCR model loaded.")
    return _reader


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_phash_signed(data: bytes) -> int | None:
    try:
        img = Image.open(io.BytesIO(data)).convert('RGB')
        ph = imagehash.phash(img)
        value = int(str(ph), 16)
        if value >= (1 << 63):
            value -= (1 << 64)
        return value
    except Exception as e:
        logger.warning("pHash failed: %s", e)
        return None


def extract_ocr_text(data: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(data)).convert('RGB')
        img_array = np.array(img)
        reader = get_reader()
        raw = reader.readtext(img_array, detail=1, paragraph=False)
        lines = [text for (_, text, conf) in raw if conf >= 0.3]
        return "\n".join(lines)
    except Exception as e:
        logger.warning("OCR failed: %s", e)
        return None


def run(mode: str) -> None:
    logger.info("Indexer starting — mode: %s", mode)
    conn = pyodbc.connect(CONN_STR, timeout=30)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT att_id, att_content, att_type
        FROM   opd_attachments
        WHERE  att_id NOT IN (SELECT att_id FROM opd_att_index)
        ORDER  BY att_id ASC
    """)
    rows = cursor.fetchall()
    total = len(rows)
    logger.info("Found %d unindexed rows.", total)

    indexed = 0
    errors = 0
    start = time.time()

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        for row in batch:
            att_id = row[0]
            att_content: bytes | None = row[1]
            try:
                if not att_content:
                    raise ValueError("att_content is NULL or empty")

                sha256 = compute_sha256(att_content)
                phash = compute_phash_signed(att_content)
                ocr_text = extract_ocr_text(att_content)

                cursor.execute("""
                    INSERT INTO opd_att_index (att_id, sha256_hash, phash, ocr_text)
                    VALUES (?, ?, ?, ?)
                """, att_id, sha256, phash, ocr_text)
                conn.commit()
                indexed += 1

                if indexed % 50 == 0:
                    elapsed = time.time() - start
                    logger.info("Progress: %d/%d indexed, %d errors, %.1fs elapsed", indexed, total, errors, elapsed)

            except Exception as e:
                errors += 1
                logger.error("att_id=%d failed: %s", att_id, e)
                # Store SHA-256 only, NULL phash/ocr_text, so we don't retry this row
                try:
                    if att_content:
                        sha256 = compute_sha256(att_content)
                        cursor.execute("""
                            INSERT INTO opd_att_index (att_id, sha256_hash, phash, ocr_text)
                            VALUES (?, ?, NULL, NULL)
                        """, att_id, sha256)
                        conn.commit()
                except Exception as inner:
                    logger.error("att_id=%d fallback insert also failed: %s", att_id, inner)

    elapsed = time.time() - start
    logger.info("Done. indexed=%d errors=%d total=%d elapsed=%.1fs", indexed, errors, total, elapsed)
    cursor.close()
    conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['backfill', 'incremental'], required=True)
    args = parser.parse_args()
    run(args.mode)
