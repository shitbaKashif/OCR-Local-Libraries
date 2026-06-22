# OPD Verification API — Jazz PMCL Medical Claims Fraud Detection

Automated medical receipt verification system that detects duplicate claims and validates claimed amounts against OCR-extracted totals on scanned Pakistani OPD receipts.

---

## System Architecture

Two services cooperate to handle each request:

```flow
Client
  │
  │  POST /api/v1/verify  (port 5000, public)
  ▼
.NET 10 API (OpdVerificationApi)
  │  1. Auth check (static token from IIS env var)
  │  2. L1 duplicate check — exact SHA-256 in SQL Server
  │  3. Call Python sidecar (internal)
  │     └── OCR + amount extraction + hashing
  │  4. Amount verification (claimed vs extracted, ±1 PKR tolerance)
  │  5. L2 pHash near-duplicate check (Hamming ≤ 12 bits)
  │  6. L3 Jaccard text near-duplicate check (≥ 0.85)
  │  7. Persist new image to SQL Server
  │  8. Return { AmountVerified, ImageDup }
  │
  │  POST /process  (port 8001, internal-only)
  ▼
Python FastAPI Sidecar (ImageIntelService)
  ├── Tesseract OCR (3 variants, PSM 6)
  ├── EasyOCR (standard + histogram-equalized)
  ├── Amount extraction stages A→B→C→D
  │     A: /- suffix (slash notation)
  │     B: financial keyword + adjacent number
  │     C: spatial bottom 30% of receipt
  │     D: Local Groq Vision (only when A/B/C all fail)
  └── 5-minute SHA-256 keyed result cache (500 entries)

SQL Server 2017+
  ├── opd_attachments (image bytes, blob store — read-only by API)
  └── opd_att_index   (sha256, phash, ocr_text — written by API)
```

---

## Prerequisites

| Component | Version | Notes |
|-----------|---------|-------|
| Windows Server / Windows 11 | — | IIS required |
| IIS | 10+ | With ASP.NET Core Module v2 + HttpPlatformHandler |
| .NET SDK | 10.0+ | For building the .NET API |
| .NET Runtime | 10.0+ | For running the .NET API |
| Python | 3.11 | 3.11 recommended; 3.10 also works |
| Tesseract OCR | 5.x | Must be in system PATH |
| Microsoft ODBC Driver | 17 or 18 | For SQL Server connectivity |
| SQL Server | 2017+ | `Server=127.0.0.1,1433` (never `localhost`) |
| Groq API Key | — | Free tier sufficient for fallback use |

### Install Tesseract (Windows)

Download from `https://github.com/UB-Mannheim/tesseract/wiki`, install, and add to PATH:

```powershell
# Verify Tesseract is in PATH
tesseract --version
```

If Tesseract is not in PATH, set the environment variable before starting the sidecar:

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

---

## Directory Structure

```path
e:\OCR\
├── OpdVerificationApi\          # .NET 10 C# API (public, port 5000)
│   ├── Controllers\
│   │   └── VerifyController.cs
│   ├── Services\
│   ├── Data\
│   ├── Models\
│   ├── appsettings.json         # Connection string, sidecar URL, dup thresholds
│   └── web.config               # IIS AspNetCoreModuleV2 config
│
├── ImageIntelService\           # Python FastAPI sidecar (internal, port 8001)
│   ├── main.py
│   ├── routers\
│   ├── services\
│   │   ├── ocr_service.py
│   │   ├── amount_service.py
│   │   ├── hash_service.py
│   │   └── sanitizer.py
│   ├── requirements.txt
│   ├── setup_venv.bat           # First-time setup script
│   ├── indexer.py               # Backfill indexer — computes hashes for existing DB images
│   ├── .env                     # API keys and sidecar config (never commit)
│   └── web.config               # IIS HttpPlatformHandler config
│
├── Data\                        # Sample/seed receipt images (M1.jpg – M11.jpg)
├── insert_images.py             # One-time: inserts Data/ images into opd_attachments (idempotent)
├── run_once_create_index.sql    # Run ONCE on production DB before first deploy
├── setup_docker_db.sql          # Docker local testing — creates SQL_DB database
└── .gitignore
```

---

## Database Setup

### Production (SQL Server existing DB — `opd-media`)

The production SQL Server has the **`opd-media`** database (as per the Jazz DB schema) containing the `opd_attachments` table with existing claim images. Before the first deployment, update `appsettings.json` connection string and run the one-time migration to add the index table:

**1. Update `appsettings.json` for production:**

```json
"OpdMedia": "Server=127.0.0.1,1433;Database=opd-media;User Id=sa;Password=<prod_password>;TrustServerCertificate=True;MultipleActiveResultSets=True;Connect Timeout=30;Max Pool Size=100;"
```

**2. Run the migration (creates `opd_att_index` in `opd-media`):**

```powershell
sqlcmd -S 127.0.0.1,1433 -U sa -P <prod_password> -i run_once_create_index.sql
```

This creates `dbo.opd_att_index` with columns: `att_id`, `sha256_hash`, `phash`, `ocr_text`, `indexed_at`.

**3. Backfill existing images (see "Loading Existing Images" section below):**

```powershell
Set-Location e:\OCR\ImageIntelService
venv\Scripts\python.exe indexer.py --mode backfill
```

### Local Development (Docker SQL Server — `opd_attachments`)

For local Docker testing, the DB is named `opd_attachments` (not `opd-media`). The default `appsettings.json` already targets this name — no change needed.

```powershell
# Start SQL Server in Docker (SQL Server 2016 compatible)
docker run -e "ACCEPT_EULA=Y" -e "SA_PASSWORD=YourPass" `
  -p 1433:1433 --name SQL_DB -d `
  mcr.microsoft.com/mssql/server:2017-latest

# Wait ~30 seconds for SQL Server to start, then run full setup
e:\OCR\ImageIntelService\venv\Scripts\python.exe e:\OCR\insert_images.py
```

---

## Loading Existing Images into the Fraud Detection Index

The production `opd_attachments` database already contains historical claim images stored as raw bytes (`att_content VARBINARY(MAX)`). Before the fraud detection API can detect duplicates against these images, each one must be indexed — i.e., its SHA-256 hash, perceptual hash (pHash), and OCR text must be computed and written to `opd_att_index`.

**Two scripts handle this:**

- **`insert_images.py`** — Inserts images from `Data\` into `opd_attachments`. Idempotent: skips files already present by `att_title`.
- **`ImageIntelService\indexer.py`** — Reads every row in `opd_attachments` not yet in `opd_att_index`, computes sha256 + pHash + OCR text, and writes to `opd_att_index`.

### Step 1 — Insert seed images from Data\

The `Data\` folder contains 11 sample receipts (M1.jpg – M11.jpg). Run this once to seed them into the DB:

```powershell
e:\OCR\ImageIntelService\venv\Scripts\python.exe e:\OCR\insert_images.py
```

Output shows `OK` for new inserts and `SKIP` for images already present. Safe to re-run — no duplicates are created.

### Step 2 — Backfill hashes for all existing images

This step reads image bytes directly from `opd_attachments`, computes sha256 + pHash + EasyOCR text for every row NOT yet in `opd_att_index`, and writes the results. Run from `ImageIntelService\` so the `.env` and EasyOCR model path resolve correctly:

```powershell
Set-Location e:\OCR\ImageIntelService
venv\Scripts\python.exe indexer.py --mode backfill
```

Expected output:

```text
2026-06-16 10:21:26,959 INFO: Indexer starting — mode: backfill
2026-06-16 10:21:27,004 INFO: Found 11 unindexed rows.
2026-06-16 10:21:27,141 INFO: Loading EasyOCR model from ./easyocr_models ...
2026-06-16 10:24:16,159 INFO: Done. indexed=11 errors=0 total=11 elapsed=169.1s
```

**Timing:** ~15 seconds per image on CPU (EasyOCR model inference). For large production backlogs, run overnight or schedule via Windows Task Scheduler.

### How the indexer handles existing images (already in DB as bytes)

```text
opd_attachments table                    opd_att_index table
─────────────────────────────            ──────────────────────────────────────────
att_id │ att_content (bytes)             att_id │ sha256_hash │ phash  │ ocr_text
───────┼─────────────────────            ───────┼─────────────┼────────┼──────────
  1    │ <JPEG bytes>              →       1    │ 864af9fc... │ -3122..│ "OPD SLIP..."
  2    │ <JPEG bytes>              →       2    │ 2cba9ab1... │ -3062..│ "DENTAL..."
```

The indexer processes each unindexed image in sequence:

1. Queries `opd_attachments WHERE att_id NOT IN (SELECT att_id FROM opd_att_index)`
2. For each unindexed row, reads `att_content` bytes from DB
3. Computes SHA-256 (exact duplicate detection) using `hashlib.sha256()`
4. Computes pHash (near-duplicate detection) using `imagehash.phash()`, stored as signed BIGINT for SQL Server compatibility
5. Runs EasyOCR (English + Arabic) to extract text for L3 Jaccard similarity
6. Inserts a row into `opd_att_index` for each image
7. If OCR fails for a specific image, stores sha256 only with NULL phash/ocr_text — the image is not retried but still gets L1 duplicate protection

### Verify the index is complete

```powershell
Set-Location e:\OCR\ImageIntelService
venv\Scripts\python.exe check_index.py
```

Every row in `opd_attachments` should appear in the output with a sha256 hash. If any row is missing, run the indexer again — it picks up where it left off.

### Running the indexer on a schedule (Windows Task Scheduler)

For ongoing production use, schedule incremental runs to index newly arriving claims overnight:

```powershell
# Create a scheduled task that runs the indexer nightly at 02:00
$action  = New-ScheduledTaskAction -Execute "e:\OCR\ImageIntelService\venv\Scripts\python.exe" `
             -Argument "e:\OCR\ImageIntelService\indexer.py --mode backfill" `
             -WorkingDirectory "e:\OCR\ImageIntelService"
$trigger = New-ScheduledTaskTrigger -Daily -At "02:00AM"
Register-ScheduledTask -TaskName "OPD Indexer" -Action $action -Trigger $trigger -RunLevel Highest
```

---

## Configuration

### OpdVerificationApi — `appsettings.json`

```json
{
  "ConnectionStrings": {
    "OpdMedia": "Server=127.0.0.1,1433;Database=opd_attachments;User Id=sa;Password=YourPass;TrustServerCertificate=True;MultipleActiveResultSets=True;Connect Timeout=30;Max Pool Size=100;"
  },
  "PythonSidecar": {
    "BaseUrl": "http://127.0.0.1:8001",
    "TimeoutSeconds": 90
  },
  "DupDetection": {
    "PHashThreshold": 12,
    "JaccardThreshold": 0.85
  }
}
```

> **Note:** `Server=127.0.0.1,1433` — never use `localhost`. On this server, `localhost` resolves to IPv6 but SQL Server listens on IPv4 only; this causes a silent connection failure.

### OpdVerificationApi — IIS Environment Variable (MANDATORY)

The API token is **never stored in source files**. Set it via IIS Manager:

1. IIS Manager → Sites → OpdVerificationApi → Configuration Editor
2. Section: `system.webServer/aspNetCore`
3. `environmentVariables` → add: `Name = JAZZ_API_TOKEN`, `Value = <token>`

Or via `appcmd` (run as Administrator):

```powershell
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set config "OpdVerificationApi" `
  /section:system.webServer/aspNetCore `
  /+environmentVariables.[name='JAZZ_API_TOKEN',value='JazzWorld21']
```

### ImageIntelService — `.env`

Copy `.env.example` (or create `.env`) in `ImageIntelService\`:

```ini
GROQ_API_KEY=<your-groq-api-key>
GROQ_MODEL=llama-3.3-70b-versatile
GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
SIDECAR_PORT=8001
SIDECAR_HOST=127.0.0.1
EASYOCR_MODEL_DIR=./easyocr_models
LOG_LEVEL=INFO
```

Get a free Groq API key at `https://console.groq.com`.

---

## Python Sidecar Setup (First Time)

```powershell
cd e:\OCR\ImageIntelService

# Run the first-time setup (creates venv, installs PyTorch CPU, downloads EasyOCR models ~350 MB)
.\setup_venv.bat

# Then copy and fill in your .env
copy .env.example .env
# Edit .env and set GROQ_API_KEY
```

---

## Running the System

**Start order is mandatory: Python sidecar must be running before the .NET API handles requests.**

### Step 1 — First-time setup (Python sidecar, run once)

```powershell
cd e:\OCR\ImageIntelService
.\setup_venv.bat
```

This creates the Python virtual environment, installs all packages, and downloads EasyOCR models (~350 MB). Run only once per machine.

### Step 2 — Start the Python Sidecar

```powershell
cd e:\OCR\ImageIntelService
venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
```

> To pre-load EasyOCR at startup and eliminate the 20-second first-request delay,
> set `PRELOAD_EASYOCR=1` before the uvicorn command:
>
> ```powershell
> $env:PRELOAD_EASYOCR = "1"
> venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
> ```

Verify it is running:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
# Expected: { "status": "ok", "tesseract": "ok", "easyocr": "loaded" }
```

### Step 3 — Build the .NET API

```powershell
cd e:\OCR\OpdVerificationApi
dotnet publish -c Release -o publish\
```

### Step 4 — Start the .NET API

```powershell
cd e:\OCR\OpdVerificationApi
$env:JAZZ_API_TOKEN = "JazzWorld21"
dotnet run
```

Or run the published binary directly:

```powershell
$env:JAZZ_API_TOKEN = "JazzWorld21"
dotnet e:\OCR\OpdVerificationApi\publish\OpdVerificationApi.dll
```

Verify it is running:

```powershell
Invoke-RestMethod http://localhost:5000/health
# Expected: { "status": "ok" }
```

### Step 5 — Send a test request

```powershell
$bytes  = [System.IO.File]::ReadAllBytes("E:\OCR\Data\T1.pdf")
$b64    = [Convert]::ToBase64String($bytes)
$body   = @{ token = "JazzWorld21"; amount_pkr = 6297; image_base64 = $b64 } | ConvertTo-Json
Invoke-RestMethod -Uri http://localhost:5000/api/v1/verify -Method Post -ContentType "application/json" -Body $body
# Expected: { "AmountVerified": true/false, "ImageDup": true/false }
```

---

## IIS Deployment

### Prerequisites on IIS Server

```powershell
# Install .NET 10 Hosting Bundle (includes ASP.NET Core Module v2)
# Download from: https://dotnet.microsoft.com/download/dotnet/10.0
# Run: dotnet-hosting-10.x.x-win.exe

# Install HttpPlatformHandler for IIS (for Python sidecar)
# Download from: https://www.iis.net/downloads/microsoft/httpplatformhandler
# Run the MSI installer

# Verify modules are registered
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" list modules | Select-String "AspNetCore\|HttpPlatform"
```

### Deploy .NET API to IIS

```powershell
# 1. Publish
cd e:\OCR\OpdVerificationApi
dotnet publish -c Release -o C:\inetpub\OpdVerificationApi\

# 2. Create logs directory (required by web.config)
New-Item -ItemType Directory -Force C:\inetpub\OpdVerificationApi\logs\

# 3. Create IIS Application Pool (no managed code — .NET handles itself)
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" add apppool /name:OpdVerificationApiPool /managedRuntimeVersion:""

# 4. Create IIS Site
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" add site /name:OpdVerificationApi `
  /physicalPath:C:\inetpub\OpdVerificationApi\ `
  /bindings:"http/*:5000:"

# 5. Assign app pool
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set app "OpdVerificationApi/" `
  /applicationPool:OpdVerificationApiPool

# 6. Set JAZZ_API_TOKEN (required before starting)
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set config "OpdVerificationApi" `
  /section:system.webServer/aspNetCore `
  /+environmentVariables.[name='JAZZ_API_TOKEN',value='JazzWorld21']

# 7. Start site
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" start site "OpdVerificationApi"
```

### Deploy Python Sidecar to IIS

```powershell
# 1. Copy ImageIntelService to IIS root
Copy-Item -Recurse e:\OCR\ImageIntelService\ C:\inetpub\ImageIntelService\

# 2. Create logs directory
New-Item -ItemType Directory -Force C:\inetpub\ImageIntelService\logs\

# 3. Run first-time setup (only once)
cd C:\inetpub\ImageIntelService\
.\setup_venv.bat

# 4. Create .env with Groq key
# Edit C:\inetpub\ImageIntelService\.env and set GROQ_API_KEY

# 5. Create IIS Application Pool (no managed runtime)
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" add apppool /name:ImageIntelPool /managedRuntimeVersion:""

# 6. Create IIS Site (port 8001, localhost only — no public binding)
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" add site /name:ImageIntelService `
  /physicalPath:C:\inetpub\ImageIntelService\ `
  /bindings:"http/127.0.0.1:8001:"

# 7. Assign app pool
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set app "ImageIntelService/" `
  /applicationPool:ImageIntelPool

# 8. Start site
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" start site "ImageIntelService"
```

### Verify Deployment

```powershell
# Health checks
Invoke-RestMethod http://localhost:5000/health
Invoke-RestMethod http://127.0.0.1:8001/health
```

Expected responses:

- .NET API: `{ "status": "ok" }`
- Python sidecar: `{ "status": "ok", "tesseract": "ok", "easyocr": "loaded" | "lazy" }`

---

## API Reference

### Endpoint

```endp
POST http://<server>:5000/api/v1/verify
Content-Type: application/json
```

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `token` | string | Yes | Static API token (must match `JAZZ_API_TOKEN` env var) |
| `amount_pkr` | decimal | Yes | Claimed amount in PKR (0.01 – 500,000) |
| `image_base64` | string | Yes | Base64-encoded receipt image |

**Supported image formats:** JPEG, PNG, BMP, TIFF, WebP, PDF, SVG  
**Maximum image size:** 20 MB  
**Encoding:** standard Base64 (no data URI prefix — no `data:image/jpeg;base64,`)

### Response Body (200 OK)

| Field | Type | Description |
|-------|------|-------------|
| `AmountVerified` | bool | `true` if extracted amount matches claimed amount within ±1.00 PKR |
| `ImageDup`       | bool | `true` if this image was previously submitted (duplicate claim) |

**Result combinations and their meaning:**

- `AmountVerified: true, ImageDup: false` — Clean claim. Amount matches and image is new.
- `AmountVerified: false, ImageDup: false` — Amount mismatch. Employee entered wrong amount.
- `AmountVerified: true/false, ImageDup: true` — Duplicate claim. Same image already in DB.
- `AmountVerified: false, ImageDup: true` — Duplicate claim. Amount is not checked when the image is already a duplicate.

> **Important:** When `ImageDup` is `true`, the API returns `AmountVerified: false` immediately without running OCR. Amount verification is irrelevant for a duplicate claim — the image itself is the fraud signal. This is a fast-path optimization (no OCR, no Groq call).

### Error Responses

| Status | Body | Cause |
|--------|------|-------|
| 400 | `{"error": "amount_pkr must be between 0.01 and 500000"}` | Amount out of range |
| 400 | `{"error": "image_base64 is required"}` | Missing image |
| 400 | `{"error": "Invalid base64 encoding"}` | Malformed base64 string |
| 400 | `{"error": "Unsupported format. Accepted: JPEG, PNG, BMP, TIFF, WEBP, PDF, SVG"}` | Wrong image format |
| 400 | `{"error": "Image exceeds 20 MB limit"}` | File too large |
| 401 | `{"error": "Unauthorized"}` | Wrong or missing token |
| 503 | `{"error": "Database unavailable"}` | SQL Server connection failure |
| 503 | `{"error": "Image processing service unavailable"}` | Python sidecar not running |
| 500 | `{"error": "Internal server error"}` | Unhandled server error |

---

## Postman Collection

### Health Check

```check
Method: GET
URL:    http://localhost:5000/health
```

### Verify Receipt

```o
Method:  POST
URL:     http://localhost:5000/api/v1/verify
Headers: Content-Type: application/json

Body (raw JSON):
{
    "token": "JazzWorld21",
    "amount_pkr": 500.00,
    "image_base64": "{{base64_encoded_image}}"
}
```

**To generate base64 in Postman Pre-request Script:**

```javascript
// If you have the image file path accessible via environment:
// Use Postman's built-in file upload and convert manually, or use the PowerShell command below.
```

**Success Response (200):**

```json
{
    "AmountVerified": true,
    "ImageDup": false
}
```

**Duplicate Claim Response (200):**

```json
{
    "AmountVerified": false,
    "ImageDup": true
}
```

**Unauthorized (401):**

```json
{
    "error": "Unauthorized"
}
```

---

## PowerShell API Calls

Health Check:

```powershell
Invoke-RestMethod -Uri "http://localhost:5000/health" -Method Get
```

### Verify a Receipt Image

```powershell
# Step 1: Convert image to base64
$imagePath = "C:\path\to\receipt.jpg"
$imageBytes = [System.IO.File]::ReadAllBytes($imagePath)
$base64 = [Convert]::ToBase64String($imageBytes)

# Step 2: Build request body
$body = @{
    token        = "JazzWorld21"
    amount_pkr   = 500.00
    image_base64 = $base64
} | ConvertTo-Json

# Step 3: Call the API
$response = Invoke-RestMethod `
    -Uri "http://localhost:5000/api/v1/verify" `
    -Method Post `
    -ContentType "application/json" `
    -Body $body

# Step 4: Inspect result
Write-Host "Amount Verified: $($response.AmountVerified)"
Write-Host "Image Duplicate: $($response.ImageDup)"
```

### Batch Test Multiple Images

```powershell
$token     = "JazzWorld21"
$testCases = @(
    @{ path = "C:\receipts\test1.jpg"; amount = 500.00 },
    @{ path = "C:\receipts\test2.png"; amount = 1250.00 },
    @{ path = "C:\receipts\test3.jpg"; amount = 85.00   }
)

foreach ($tc in $testCases) {
    $bytes  = [System.IO.File]::ReadAllBytes($tc.path)
    $b64    = [Convert]::ToBase64String($bytes)
    $body   = @{ token = $token; amount_pkr = $tc.amount; image_base64 = $b64 } | ConvertTo-Json
    $result = Invoke-RestMethod -Uri "http://localhost:5000/api/v1/verify" `
                                -Method Post -ContentType "application/json" -Body $body
    Write-Host "$($tc.path): AmountVerified=$($result.AmountVerified), ImageDup=$($result.ImageDup)"
}
```

---

## Amount Extraction Pipeline

The Python sidecar extracts amounts through four sequential stages, stopping at the first successful extraction:

| Stage | Method | External Call | Description |
| ----- | ------ | ------------- | ----------- |
| A | Slash notation `/-` | No | Detects `500/-`, `Rs.1,250/-`, `PKR 85/-` — most reliable for Pakistani receipts |
| B | Keyword + adjacency | No | Finds financial keyword (Total, Grand, Net, Payable, فیس, etc.) then extracts the adjacent number |
| C | Spatial bottom 30% | No | Scans the lowest section of the receipt where totals appear |
| D | Local Groq Vision | **Local** | Last resort — sends image to the locally-running Groq instance (`GROQ_BASE_URL`); falls back to text-only local Groq if vision fails |

> **Local Groq:** Stage D calls a Groq instance running locally on the same server (`GROQ_BASE_URL` in `.env`). No receipt data leaves the on-premise environment. The `GROQ_BASE_URL` env var points the client to `http://127.0.0.1:8080` by default; set it to your local Groq server address.

**Privacy:** Medical receipt images are only sent to the local Groq Vision instance when all three local OCR stages (A, B, C) completely fail. Patient PHI (names, phone numbers, CNIC, addresses) is stripped from OCR text before any Groq text call.

**Supported amount ranges:** PKR 10 – PKR 500,000  
**Tolerance:** ±1.00 PKR between claimed and extracted amounts

---

## Duplicate Detection

Three-layer duplicate detection runs on every non-duplicate image:

| Layer | Method | Threshold | Speed |
|-------|--------|-----------|-------|
| L1 | Exact SHA-256 hash match | Exact | < 5ms (database index) |
| L2 | Perceptual hash (pHash) Hamming distance | ≤ 12 bits | < 10ms (in-memory) |
| L3 | Jaccard similarity on OCR text trigrams | ≥ 0.85 | < 50ms |

L3 only runs when L2 finds a candidate — preventing false positives from visually similar but textually different receipts.

---

## Troubleshooting

### Sidecar not starting

```powershell
# Check if port 8001 is in use
netstat -ano | Select-String ":8001"

# Check Python venv exists
Test-Path e:\OCR\ImageIntelService\venv\Scripts\python.exe

# Start manually and read errors
cd e:\OCR\ImageIntelService
venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
```

### Tesseract not found

```error
RuntimeError: tesseract is not installed or not in your PATH
```

Fix: Install Tesseract 5 from the link above and ensure `tesseract.exe` is in PATH. Or set:

```powershell
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

### SQL Server connection refused

```connection
Cannot open server '127.0.0.1' requested by the login
```

- Verify SQL Server is running: `Get-Service -Name "MSSQL*"`
- Always use `127.0.0.1,1433` — not `localhost` (IPv6 resolution issue)
- Check SA login is enabled in SQL Server Configuration Manager

### Groq API rate limit (Stage D exhausted)

When Groq Vision returns HTTP 429, the sidecar automatically falls back to the text-only Groq endpoint. If both are rate-limited, `grand_total` will be `null` and `AmountVerified` will be `false`.

Groq free tier limits reset daily. Paid tiers eliminate this issue for production.

### Amount extracted as null for a valid receipt

Indicates all four stages failed. Possible causes:

- Heavily degraded/blurry scan — consider requesting a clearer photo
- Handwritten-only receipt with no printed total
- Non-standard format not recognized by keyword list

Check sidecar logs for `amount_source: not_found`.

### First request is slow (20–30 seconds)

EasyOCR loads its neural network models on the first call. Set `PRELOAD_EASYOCR=1` in `.env` to load at startup instead of on first request.

### IIS returning 502.5 for .NET API

The .NET Hosting Bundle is not installed. Download from `https://dotnet.microsoft.com/download/dotnet/10.0` and install the **Hosting Bundle** (not just the Runtime).

### IIS returning 502 for Python sidecar

HttpPlatformHandler is not installed or the Python venv path in `web.config` is wrong. Verify:

```xml
<httpPlatform processPath="venv\Scripts\python.exe" ... />
```

The path is relative to the IIS site's physical path. If deployed to `C:\inetpub\ImageIntelService\`, the full path must be `C:\inetpub\ImageIntelService\venv\Scripts\python.exe`.

---

## Security Notes

- `JAZZ_API_TOKEN` is stored **only** in the IIS environment variable — never in `appsettings.json`, `web.config`, or any source file
- `GROQ_API_KEY` is stored **only** in `ImageIntelService\.env` — covered by `.gitignore`
- The Python sidecar binds to `127.0.0.1:8001` only — not accessible from the network
- PHI is stripped from OCR text before any Groq text-only API call
- Receipt images sent to Groq Vision only as last resort (stages A/B/C all failed)
- DB password in `appsettings.json` should be rotated and moved to IIS environment variable before public deployment

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Public API | .NET 10 (C#), ASP.NET Core, Dapper |
| OCR / AI Sidecar | Python 3.11, FastAPI, Uvicorn |
| OCR Engines | Tesseract 5, EasyOCR 1.7 |
| Vision Model | Llama 4 Scout (meta-llama/llama-4-scout-17b-16e-instruct) via Groq |
| Text Fallback | Llama 3.3 70B (llama-3.3-70b-versatile) via Groq |
| Database | SQL Server 2017+, via pyodbc (Python) and Dapper (C#) |
| Hashing | SHA-256 (exact dup), ImageHash pHash (near dup) |
| IIS Hosting | AspNetCoreModuleV2 (.NET), HttpPlatformHandler (Python) |
