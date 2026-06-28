@echo off
echo === ImageIntelService — First-time setup ===

REM 1. Create venv
python -m venv venv
if errorlevel 1 ( echo ERROR: python not found. Install Python 3.11 first. & exit /b 1 )

REM 2. Install PyTorch CPU build (must be before requirements.txt)
echo Installing PyTorch CPU build...
venv\Scripts\pip install torch torchvision --extra-index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 ( echo ERROR: torch install failed. & exit /b 1 )

REM 3. Install remaining dependencies
echo Installing requirements...
venv\Scripts\pip install -r requirements.txt
if errorlevel 1 ( echo ERROR: requirements install failed. & exit /b 1 )

REM 4. Pre-download EasyOCR models (~350 MB, run once)
echo Downloading EasyOCR models...
venv\Scripts\python -c "import os; os.makedirs('./easyocr_models', exist_ok=True); import easyocr; easyocr.Reader(['en','ar'], model_storage_directory='./easyocr_models'); print('Models ready.')"
if errorlevel 1 ( echo ERROR: EasyOCR model download failed. & exit /b 1 )

echo.
echo === Setup complete. Configure .env before starting. ===
