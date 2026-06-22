import os
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify Tesseract is reachable at startup (fast, no model download)
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = os.getenv(
            'TESSERACT_CMD', r'C:\Program Files\Tesseract-OCR\tesseract.exe'
        )
        version = pytesseract.get_tesseract_version()
        logger.info("Tesseract ready: %s", version)
    except Exception as e:
        logger.warning("Tesseract not available: %s — EasyOCR will be used as primary", e)

    # EasyOCR is loaded lazily on first use (heavy model, ~1 GB).
    # Set PRELOAD_EASYOCR=1 to warm it up at startup (useful on single-worker deploys).
    if os.getenv('PRELOAD_EASYOCR', '0') == '1':
        logger.info("Pre-loading EasyOCR model (PRELOAD_EASYOCR=1)...")
        from services.ocr_service import get_reader
        get_reader()
        logger.info("EasyOCR model ready.")
    else:
        logger.info("EasyOCR loads on first use (set PRELOAD_EASYOCR=1 to pre-load).")

    yield
    logger.info("ImageIntelService shutting down.")


app = FastAPI(
    title="ImageIntelService",
    description="OPD Receipt OCR, hashing, and amount extraction — internal sidecar",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

from routers.process import router as process_router
app.include_router(process_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
