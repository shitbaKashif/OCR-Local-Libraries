"""Run once: downloads EasyOCR models to ./easyocr_models/"""
import os
import sys
os.makedirs('./easyocr_models', exist_ok=True)

print("Downloading EasyOCR en+ar models (approx 350 MB)...", flush=True)
import easyocr
reader = easyocr.Reader(
    ['en', 'ar'],
    model_storage_directory='./easyocr_models',
    gpu=False,
    verbose=False
)
print("Models ready.", flush=True)

files = os.listdir('./easyocr_models')
total = sum(os.path.getsize(f'./easyocr_models/{f}') for f in files)
print(f"Downloaded {len(files)} file(s), {total/1024/1024:.1f} MB total:")
for f in sorted(files):
    mb = os.path.getsize(f'./easyocr_models/{f}') / 1024 / 1024
    print(f"  {f:<40} {mb:6.1f} MB")
