"""Wrapper: runs indexer with the correct venv and connection string."""
import os, sys

os.environ.setdefault('DB_CONNECTION',
    'DRIVER={SQL Server};'
    'Server=127.0.0.1,1433;'
    'Database=opd_attachments;'
    'UID=sa;PWD=Pass5432;'
    'TrustServerCertificate=yes;'
)
os.environ.setdefault('EASYOCR_MODEL_DIR', './easyocr_models')

sys.argv = ['indexer.py', '--mode', 'backfill']

import indexer
indexer.run('backfill')
