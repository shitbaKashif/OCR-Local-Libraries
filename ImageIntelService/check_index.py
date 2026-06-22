import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pyodbc

conn = pyodbc.connect(
    'DRIVER={SQL Server};Server=127.0.0.1,1433;Database=opd_attachments;'
    'UID=sa;PWD=Pass5432;TrustServerCertificate=yes;', timeout=10
)
cur = conn.cursor()
cur.execute("""
    SELECT i.att_id, a.att_title,
           i.sha256_hash,
           i.phash,
           LEN(i.ocr_text) AS ocr_chars,
           LEFT(REPLACE(i.ocr_text, CHAR(10), ' '), 80) AS ocr_preview
    FROM   opd_att_index i
    JOIN   opd_attachments a ON a.att_id = i.att_id
    ORDER  BY i.att_id
""")
print(f"{'id':<7} {'title':<8} {'sha256':<16} {'phash':>22} {'ocr_ch':>7}  ocr_preview")
print("-" * 110)
for r in cur.fetchall():
    print(f"{r[0]:<7} {r[1]:<8} {r[2][:14]}.. {r[3]:>22} {r[4]:>7}  {r[5]}")
conn.close()
