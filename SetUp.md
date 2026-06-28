# OPD Verification API — Jazz PMCL Medical Claims Fraud Detection

Automated medical receipt verification system that detects duplicate claims and validates claimed amounts against OCR-extracted totals on scanned Pakistani OPD receipts.

---

## System Architecture

```text
Client  (mobile app / backend service)
  │
  │  POST http://SERVER:5000/api/v1/verify
  ▼
┌────────────────────────────────────────────────────────────────────┐
│  .NET 10 API  —  OpdVerificationApi                                │
│  Running 24/7 under IIS on the production server                   │
│                                                                    │
│  1. Auth check    — token in request body vs JAZZ_API_TOKEN (IIS)  │
│  2. L1 dup check  — exact SHA-256 match in SQL Server              │
│  3. Call sidecar  — http://127.0.0.1:8001/process (same machine)   │
│  4. Amount verify — claimed PKR vs OCR-extracted total (±1 PKR)    │
│  5. Date validate — receipt date within last 3 months              │
│  6. L2 dup check  — perceptual hash, Hamming ≤ 12 bits             │
│  7. L3 dup check  — OCR text Jaccard similarity ≥ 0.85             │
│  8. Persist       — write to opd_att_index in SQL Server           │
│  9. Return JSON   — { AmountVerified, ImageDup, DateValid, receipt_date }
└────────────────────────────────────────────────────────────────────┘
  │
  │  POST http://127.0.0.1:8001/process  (loopback only, not public)
  ▼
┌────────────────────────────────────────────────────────────────────┐
│  Python FastAPI Sidecar  —  ImageIntelService                      │
│  Running 24/7 via Windows Task Scheduler on the same server        │
│                                                                    │
│  - Tesseract OCR (3 PSM variants)                                  │
│  - EasyOCR (English + Arabic, standard + histogram-equalized)      │
│  - Amount extraction: slash notation → keyword match → spatial     │
│  - Date extraction: keyword-anchored pass, then fallback scan      │
│  - pHash computation for near-duplicate detection                  │
└────────────────────────────────────────────────────────────────────┘
  │
  ▼
SQL Server  —  opd-media database
  ├── opd_attachments   (existing image store — read-only by this API)
  └── opd_att_index     (fraud index — sha256, phash, ocr_text)
```

---

## Critical: Both Services Must Run on the Same Server

**The .NET API calls the sidecar at `http://127.0.0.1:8001`.**  
`127.0.0.1` is the loopback address — it only resolves to the machine that made the call.

| Setup | Result |
| --- | --- |
| .NET API on server + sidecar on same server | ✅ Works |
| .NET API on server + sidecar on dev laptop | ❌ Fails — every request returns `503 Image processing service unavailable` |
| .NET API on server + sidecar on a different server | ❌ Fails — loopback cannot cross machine boundaries |

If you need to split them across machines in future, the sidecar URL in `appsettings.json` must change to the sidecar machine's real IP, and the sidecar must bind to `0.0.0.0` instead of `127.0.0.1` — but that exposes it to the network and requires firewall protection. For now, keep both on the same server.

---

## Critical: Both Services Must Run 24/7

The system has **zero fallback**. If either service is down, all requests fail:

| What is down | What callers see |
| --- | --- |
| Python sidecar stopped | `503 Image processing service unavailable` on every request |
| .NET API stopped | Connection refused / timeout — no response at all |
| SQL Server stopped | `503 Database unavailable` on every request |

Both services must be configured to start automatically on server boot and restart on crash (covered in the deployment steps below).

---

## What to Change Before Deploying

### File 1 — `OpdVerificationApi\appsettings.Production.json`

This file is automatically applied by .NET when `ASPNETCORE_ENVIRONMENT=Production` is set in IIS (configured in Step 6). **Edit this file** for production — do not touch `appsettings.json`.

Set the production database connection string:

```json
{
  "ConnectionStrings": {
    "OpdMedia": "Server=127.0.0.1,1433;Database=opd-media;User Id=sa;Password=PROD_PASSWORD_HERE;TrustServerCertificate=True;MultipleActiveResultSets=True;Connect Timeout=30;Max Pool Size=100;"
  },
  "Logging": {
    "LogLevel": {
      "Default": "Warning",
      "Microsoft.AspNetCore": "Warning"
    }
  }
}
```

**What to change:**

| Field | Current (dev) | Change to |
| --- | --- | --- |
| `Database=` | `opd_attachments` | `opd-media` |
| `Password=` | `Pass5432` | actual production SA password |
| `Server=` | `127.0.0.1,1433` | keep as-is — never use `localhost` |

> **Why `127.0.0.1` not `localhost`:** On Windows Server, `localhost` resolves to IPv6 (`::1`) first.  
> SQL Server listens on IPv4 only. Using `localhost` causes a ~2-second connection delay or silent failure on every single request. Always use `127.0.0.1,1433`.

### File 2 — `ImageIntelService\.env` (create on the server)

This file does not exist in the repository — it must be created on the server manually. See Step 6 in the deployment section.

### Everything else — no changes needed

The API token is set in IIS, not in any file. The sidecar URL (`http://127.0.0.1:8001`) is already correct for same-server deployment.

---

## IIS Server: Install These Before Deploying

Everything below must be installed on the IIS server. Verify each one — a missing component will cause silent failures or cryptic errors.

### 1 — .NET 10 Hosting Bundle (MANDATORY for .NET API)

Download the **Hosting Bundle** (not just the Runtime) from `https://dotnet.microsoft.com/download/dotnet/10.0`.  
Run `dotnet-hosting-10.x.x-win.exe` as Administrator. Then run `iisreset`.

```powershell
dotnet --version    # Expected: 10.x.x or higher
```

If this command fails, the Hosting Bundle is not installed. Without it, IIS returns **502.5** on every request.

### 2 — Python 3.11 (MANDATORY for sidecar)

Download from `https://www.python.org/downloads/release/python-3119/`.  
During install: tick **"Add Python to PATH"** and **"Install for all users"**.

```powershell
python --version    # Expected: Python 3.11.x
```

### 3 — Tesseract OCR 5 (MANDATORY for sidecar)

Download from `https://github.com/UB-Mannheim/tesseract/wiki`.  
Install as Administrator. Tick **"Add to PATH"** during install.

```powershell
tesseract --version    # Expected: tesseract 5.x.x
```

If Tesseract installs but is not in PATH, you can tell the sidecar where it is via the `.env` file — see Step 4.

### 4 — Microsoft ODBC Driver 17 or 18 (required for backfill indexer)

The real-time sidecar (`/process` endpoint) does **not** use the database — only the backfill `indexer.py` script does. You still need this driver if you will run the backfill after deployment.

Download from Microsoft's website: search "Download ODBC Driver for SQL Server".

```powershell
Get-OdbcDriver | Where-Object { $_.Name -like "*SQL Server*" } | Select-Object Name
# Expected: ODBC Driver 17 for SQL Server  OR  ODBC Driver 18 for SQL Server
```

### 5 — Confirm IIS AspNetCoreModuleV2 is registered

Installed automatically with the .NET Hosting Bundle. Verify:

```powershell
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" list modules | Select-String "AspNetCore"
# Expected output contains: AspNetCoreModuleV2
```

### Server hardware minimum requirements

| Resource | Minimum | Why |
| --- | --- | --- |
| RAM | 4 GB | EasyOCR loads ~1.5–2 GB of neural network models into RAM and keeps them resident |
| CPU | 4 cores | Tesseract + EasyOCR are CPU-intensive; each OCR request takes 3–20 seconds |
| Disk | 2 GB free in deploy path | Python venv ~800 MB, EasyOCR models ~350 MB, logs |

---

## Production Deployment: Step-by-Step

Follow these steps **in order**. The SQL migration must happen before the first API request.

---

### STEP 1 — Run the database migration (one time only, must be first)

This creates the `opd_att_index` table that the API writes to. **Without this table, the API crashes on first use.**

Run on the production SQL Server (or from any machine that can reach it):

```powershell
sqlcmd -S 127.0.0.1,1433 -U sa -P PROD_PASSWORD_HERE -i run_once_create_index.sql
```

Expected output:
```
opd_att_index table created.
IX_att_index_sha256 index created.
Migration complete.
```

It is safe to re-run — the script checks before creating. If the table exists, it prints "already exists — skipped."

---

### STEP 2 — Update `appsettings.Production.json` on your dev machine

Edit `e:\OCR\OpdVerificationApi\appsettings.Production.json` with the production DB name and password as described in the "What to Change" section above. Save the file — it will be included in the publish output.

---

### STEP 3 — Build the publish package (on dev machine)

```powershell
cd e:\OCR\OpdVerificationApi
dotnet publish -c Release -o publish\
```

This creates `e:\OCR\OpdVerificationApi\publish\` — a self-contained folder with the binary, `appsettings.json`, `appsettings.Production.json`, and `web.config`.

---

### STEP 4 — Copy files to the IIS server

| What to copy | From (dev machine) | To (IIS server) |
| --- | --- | --- |
| .NET API | `e:\OCR\OpdVerificationApi\publish\` | `C:\inetpub\OpdVerificationApi\` |
| Python sidecar | `e:\OCR\ImageIntelService\` | `C:\inetpub\ImageIntelService\` |
| DB migration | `e:\OCR\run_once_create_index.sql` | `C:\inetpub\run_once_create_index.sql` |

> **EasyOCR models are included** in `ImageIntelService\easyocr_models\` (`craft_mlt_25k.pth` + `arabic.pth`).  
> **No internet download is required on the server.** The sidecar will use these files directly.

---

### STEP 5 — Install Python packages on the server (one time only)

Open PowerShell as **Administrator** on the IIS server:

```powershell
cd C:\inetpub\ImageIntelService
.\setup_venv.bat
```

This creates the virtual environment and installs all packages. The EasyOCR model download step in the script will complete instantly because the model files are already present in `easyocr_models\`.

If `setup_venv.bat` fails at the PyTorch step (no internet or proxy issue), install manually:

```powershell
venv\Scripts\pip install torch torchvision --extra-index-url https://download.pytorch.org/whl/cpu
venv\Scripts\pip install -r requirements.txt
```

---

### STEP 6 — Create the sidecar `.env` file on the server

Create `C:\inetpub\ImageIntelService\.env`:

```ini
SIDECAR_PORT=8001
SIDECAR_HOST=127.0.0.1
EASYOCR_MODEL_DIR=./easyocr_models
LOG_LEVEL=INFO
PRELOAD_EASYOCR=1
```

> **`PRELOAD_EASYOCR=1` is strongly recommended for production.**  
> Without it, EasyOCR loads on the first request, making that request take 20–30 seconds.  
> With it, the sidecar loads models at startup — first request responds normally.

If Tesseract is installed but **not** in the system PATH, add:

```ini
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

---

### STEP 7 — Deploy the .NET API to IIS

Run all commands as **Administrator**:

```powershell
# Create logs directory (web.config writes stdout here)
New-Item -ItemType Directory -Force "C:\inetpub\OpdVerificationApi\logs"

# Create an Application Pool with no managed runtime
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" add apppool `
    /name:OpdVerificationApiPool /managedRuntimeVersion:""

# Disable idle timeout — CRITICAL for 24/7 operation
# Default is 20 minutes: after 20 min of no requests, IIS kills the process
# The next request then has a cold-start delay. Set to 0 to disable.
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set apppool OpdVerificationApiPool `
    /processModel.idleTimeout:"00:00:00"

# Create the IIS site on port 5000
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" add site `
    /name:OpdVerificationApi `
    /physicalPath:"C:\inetpub\OpdVerificationApi" `
    /bindings:"http/*:5000:"

# Assign the app pool
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set app "OpdVerificationApi/" `
    /applicationPool:OpdVerificationApiPool

# Set ASPNETCORE_ENVIRONMENT=Production so appsettings.Production.json is loaded
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set config "OpdVerificationApi" `
    /section:system.webServer/aspNetCore `
    /+environmentVariables.[name='ASPNETCORE_ENVIRONMENT',value='Production']

# Start the site
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" start site "OpdVerificationApi"
```

---

### STEP 8 — Set the API token in IIS

The token is **never stored in source files**. Set it as an IIS environment variable:

```powershell
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set config "OpdVerificationApi" `
    /section:system.webServer/aspNetCore `
    /+environmentVariables.[name='JAZZ_API_TOKEN',value='YOUR_TOKEN_HERE']
```

Verify it was set:

```powershell
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" list config "OpdVerificationApi" `
    /section:system.webServer/aspNetCore
# Look for: name="JAZZ_API_TOKEN"
```

Restart the site to apply:

```powershell
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" stop site "OpdVerificationApi"
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" start site "OpdVerificationApi"
```

**To update the token in future:**

```powershell
# Remove old
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set config "OpdVerificationApi" `
    /section:system.webServer/aspNetCore `
    /-environmentVariables.[name='JAZZ_API_TOKEN']
# Add new
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" set config "OpdVerificationApi" `
    /section:system.webServer/aspNetCore `
    /+environmentVariables.[name='JAZZ_API_TOKEN',value='NEW_TOKEN_HERE']
# Restart
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" stop site "OpdVerificationApi"
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" start site "OpdVerificationApi"
```

---

### STEP 9 — Register the Python sidecar with Windows Task Scheduler

The sidecar must run permanently. Task Scheduler keeps it alive across server reboots and auto-restarts it if it crashes.

Run as **Administrator**:

```powershell
$action = New-ScheduledTaskAction `
    -Execute   "C:\inetpub\ImageIntelService\venv\Scripts\python.exe" `
    -Argument  "-m uvicorn main:app --host 127.0.0.1 --port 8001 --workers 1" `
    -WorkingDirectory "C:\inetpub\ImageIntelService"

$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)    # no timeout — runs forever

Register-ScheduledTask `
    -TaskName    "OPD Python Sidecar" `
    -Description "ImageIntelService — OCR sidecar for OPD Verification API" `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -RunLevel    Highest `
    -User        "SYSTEM"

# Start immediately without rebooting
Start-ScheduledTask -TaskName "OPD Python Sidecar"
```

> **`--workers 1` is mandatory.** EasyOCR is not safe to run in multiple worker processes from the same Python environment. Running `--workers 2` or higher will cause model loading failures or memory corruption. If you need higher throughput in future, run multiple sidecar instances on different ports with a load balancer in front.

---

### STEP 10 — Open port 5000 in Windows Firewall

Port 5000 must be reachable from client machines. By default, Windows Firewall blocks it.

```powershell
New-NetFirewallRule `
    -DisplayName "OPD Verification API (port 5000)" `
    -Direction   Inbound `
    -Protocol    TCP `
    -LocalPort   5000 `
    -Action      Allow
```

Port 8001 (sidecar) must **not** be opened — it is internal-only and should remain blocked.

---

### STEP 11 — Verify the full system

Run in order. Each must pass before proceeding to the next.

```powershell
# 1. Sidecar health (run from the server itself)
Invoke-RestMethod http://127.0.0.1:8001/health
# Expected: { "status": "ok" }

# 2. .NET API health (from the server)
Invoke-RestMethod http://127.0.0.1:5000/health
# Expected: { "status": "ok" }

# 3. Auth rejection test (wrong token must return 401)
try {
    $bad = @{ token = "wrong"; amount_pkr = 100; image_base64 = "dGVzdA==" } | ConvertTo-Json
    Invoke-RestMethod -Uri http://127.0.0.1:5000/api/v1/verify -Method Post `
        -ContentType "application/json" -Body $bad
} catch {
    Write-Host "HTTP $($_.Exception.Response.StatusCode.value__)"
    # Expected: HTTP 401
}

# 4. End-to-end test with a real receipt image
$bytes = [System.IO.File]::ReadAllBytes("C:\path\to\any_receipt.jpg")
$b64   = [Convert]::ToBase64String($bytes)
$body  = @{ token = "YOUR_TOKEN_HERE"; amount_pkr = 500; image_base64 = $b64 } | ConvertTo-Json
$r     = Invoke-RestMethod -Uri http://127.0.0.1:5000/api/v1/verify `
             -Method Post -ContentType "application/json" -Body $body

Write-Host "AmountVerified : $($r.AmountVerified)"
Write-Host "ImageDup       : $($r.ImageDup)"
Write-Host "DateValid      : $($r.DateValid)"
$rd = if ($r.receipt_date) { $r.receipt_date } else { "(not found)" }
Write-Host "receipt_date   : $rd"
```

---

## One-Time: Backfill Existing Images into the Fraud Index

The `opd-media` database already has historical claim images in `opd_attachments`. The API cannot detect duplicates against images that are not yet indexed. Run the backfill **once** after first deployment.

The sidecar must be running first.

```powershell
cd C:\inetpub\ImageIntelService
venv\Scripts\python.exe indexer.py --mode backfill
```

Expected output:
```
INFO: Indexer starting — mode: backfill
INFO: Found 1842 unindexed rows.
INFO: Loading EasyOCR model from ./easyocr_models ...
INFO: Done. indexed=1842 errors=0 total=1842 elapsed=7640.1s
```

**Timing:** ~15 seconds per image on CPU. For large historical backlogs, schedule it overnight:

```powershell
$action  = New-ScheduledTaskAction `
    -Execute "C:\inetpub\ImageIntelService\venv\Scripts\python.exe" `
    -Argument "indexer.py --mode backfill" `
    -WorkingDirectory "C:\inetpub\ImageIntelService"
$trigger = New-ScheduledTaskTrigger -Daily -At "02:00AM"
Register-ScheduledTask -TaskName "OPD Indexer Nightly" `
    -Action $action -Trigger $trigger -RunLevel Highest
```

The indexer only processes rows not yet in `opd_att_index` — safe to run repeatedly.

---

## Real-World Operations: Issues You Will Face

### Server reboots (Windows Updates, power events)

**IIS** starts automatically with Windows — the .NET API resumes within seconds of boot.  
**The Python sidecar** starts via the Task Scheduler `AtStartup` trigger — it resumes automatically.  
**First request after a reboot** will be slow (20–30 sec) while EasyOCR loads, even with `PRELOAD_EASYOCR=1`, because the sidecar itself takes time to start. This is expected and not an error.

### Sidecar crashes or stops responding

Task Scheduler is configured to restart it 5 times with 2-minute intervals. If it crashes more than 5 times, it stays down — this indicates a recurring error (OOM, bad image crash, etc.) that needs investigation.

To manually check sidecar status:

```powershell
Get-ScheduledTask -TaskName "OPD Python Sidecar" | Select-Object TaskName, State
# State should be "Running". "Ready" means it is stopped.

netstat -ano | Select-String ":8001"
# Should show a LISTENING entry on 127.0.0.1:8001
```

To restart it manually:

```powershell
Stop-ScheduledTask  -TaskName "OPD Python Sidecar"
Start-ScheduledTask -TaskName "OPD Python Sidecar"
```

### EasyOCR out-of-memory

EasyOCR keeps ~1.5–2 GB of neural network models loaded in RAM permanently. If the server runs low on memory (other processes, SQL Server buffer pool, etc.), the sidecar process may be killed by Windows.

Signs: sidecar stops responding, Task Manager shows Python process disappearing.  
Fix: Ensure at least 4 GB RAM is available for the sidecar process. If the server is memory-constrained, consider increasing RAM or reducing SQL Server's max server memory setting.

### IIS App Pool shuts down after idle period

IIS default: after 20 minutes of no requests, the App Pool process is killed. The next request triggers a cold start.  
This was already addressed in Step 7 by setting `idleTimeout` to `00:00:00` (disabled). If you see unexpected cold starts, verify the setting:

```powershell
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" list apppool OpdVerificationApiPool /processModel.idleTimeout
# Expected: 00:00:00
```

### Slow first request (20–30 seconds)

This happens when the sidecar starts without `PRELOAD_EASYOCR=1`. The model loads on the first call.  
With `PRELOAD_EASYOCR=1` in `.env`, the model loads at startup — first request is normal speed.

If you added `PRELOAD_EASYOCR=1` to `.env` after deploying, restart the Task Scheduler task:

```powershell
Stop-ScheduledTask  -TaskName "OPD Python Sidecar"
Start-ScheduledTask -TaskName "OPD Python Sidecar"
```

### High concurrent load / slow responses under traffic

The sidecar runs **single-worker** (`--workers 1`). Each OCR request takes 3–20 seconds depending on image complexity. Requests queue up when multiple clients submit simultaneously.

For higher throughput: register a second sidecar instance on port 8002, update `appsettings.json` to load-balance between 8001 and 8002, and register a second Task Scheduler task. Contact the development team before doing this — the `.env` and model paths must be duplicated.

### Logs filling up disk space

IIS stdout logs are written to `C:\inetpub\OpdVerificationApi\logs\`. These can grow large over time.  
Configure IIS log rotation via IIS Manager → Logging → set "Maximum file size" and enable "Daily" rotation.

Sidecar logs go to console (captured by Task Scheduler — no log file by default). To enable file logging, set `LOG_LEVEL=WARNING` in `.env` to reduce noise.

### Token rotation

If `JAZZ_API_TOKEN` must change, follow the update procedure in Step 8 above. The API returns `401` immediately for any request with the old token once the new one is applied and the site is restarted.

### `opd_att_index` table growth

Every new (non-duplicate) receipt submission adds one row to `opd_att_index`. The table will grow indefinitely. The `ocr_text` column (`NVARCHAR(MAX)`) can hold several KB per row.

For a high-volume deployment, monitor table size periodically:

```sql
SELECT
    COUNT(*)                                        AS total_rows,
    SUM(DATALENGTH(ocr_text)) / 1024 / 1024        AS ocr_text_mb,
    SUM(DATALENGTH(sha256_hash) + 8 + 100) / 1024  AS index_kb
FROM opd_att_index;
```

There is no automatic purge — rows must be retained for fraud detection to work against historical submissions.

---

## API Reference

### Main API — `POST /api/v1/verify`

This is the only endpoint clients call. The sidecar (`/process`) is internal and never called directly by clients.

**Endpoint:**
```
POST http://SERVER_IP:5000/api/v1/verify
Content-Type: application/json
```

> Use the server's actual IP or hostname when calling from other machines.  
> Use `http://127.0.0.1:5000` only when calling from the server itself.

---

#### Request Body

```json
{
    "token":        "JazzWorld21",
    "amount_pkr":   1500.00,
    "image_base64": "BASE64_ENCODED_IMAGE_HERE"
}
```

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `token` | string | Yes | Must match `JAZZ_API_TOKEN` set in IIS |
| `amount_pkr` | decimal | Yes | 0.01 – 500,000 PKR |
| `image_base64` | string | Yes | Standard Base64 — **no** `data:image/...;base64,` prefix |

**Supported image formats:** JPEG, PNG, BMP, TIFF, WebP, PDF, SVG  
**Maximum image size:** 20 MB (before base64 encoding; base64 adds ~33% — keep source under 15 MB)

---

#### Response Body (200 OK)

```json
{
    "AmountVerified": true,
    "ImageDup":       false,
    "DateValid":      true,
    "receipt_date":   "2026-06-21"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `AmountVerified` | bool | `true` if OCR-extracted total matches `amount_pkr` within ±1.00 PKR |
| `ImageDup` | bool | `true` if this exact image was previously submitted (recycled receipt) |
| `DateValid` | bool | `true` if receipt date is within 3 months of today |
| `receipt_date` | string / null | ISO-8601 date found on the receipt, or `null` if not found |

**A claim is only clean when `AmountVerified=true`, `ImageDup=false`, and `DateValid=true`.**

---

#### All Response Scenarios

#### Scenario 1 — Clean claim (approve)

```json
{
    "AmountVerified": true,
    "ImageDup":       false,
    "DateValid":      true,
    "receipt_date":   "2026-06-21"
}
```

#### Scenario 2 — Amount mismatch (claimed 3000 but receipt shows 1500)

```json
{
    "AmountVerified": false,
    "ImageDup":       false,
    "DateValid":      true,
    "receipt_date":   "2026-06-21"
}
```

#### Scenario 3 — Expired receipt (date older than 3 months)

```json
{
    "AmountVerified": true,
    "ImageDup":       false,
    "DateValid":      false,
    "receipt_date":   "2025-11-15"
}
```

#### Scenario 4 — No date found on receipt

```json
{
    "AmountVerified": true,
    "ImageDup":       false,
    "DateValid":      false,
    "receipt_date":   null
}
```

#### Scenario 5 — Duplicate claim (same receipt submitted before)

```json
{
    "AmountVerified": false,
    "ImageDup":       true,
    "DateValid":      false,
    "receipt_date":   null
}
```

> When `ImageDup=true`, no OCR is run — the API returns in under 30 ms.  
> Amount and date are not re-checked. The submission is fraudulent by definition.

#### Scenario 6 — Wrong token

```json
{ "error": "Unauthorized" }
```

HTTP status: 401

---

#### All Error Responses

| HTTP | Body | Cause |
| --- | --- | --- |
| 400 | `{"error": "amount_pkr must be between 0.01 and 500000"}` | Amount out of range |
| 400 | `{"error": "image_base64 is required"}` | Missing image field |
| 400 | `{"error": "Invalid base64 encoding"}` | Malformed base64 string |
| 400 | `{"error": "Unsupported format. Accepted: JPEG, PNG, BMP, TIFF, WEBP, PDF, SVG"}` | Wrong file format |
| 400 | `{"error": "Image exceeds 20 MB limit"}` | Image too large |
| 401 | `{"error": "Unauthorized"}` | Token missing or wrong |
| 503 | `{"error": "Database unavailable"}` | SQL Server not reachable |
| 503 | `{"error": "Image processing service unavailable"}` | Python sidecar not running |
| 500 | `{"error": "Internal server error"}` | Unhandled exception |

---

### Health Check — `GET /health`

```text
GET http://SERVER_IP:5000/health
```

Response:
```json
{ "status": "ok" }
```

Returns 200 when the .NET API is running and connected to the database.

---

### Internal Sidecar Health — `GET /health` on port 8001

Only accessible from the server itself. Used for monitoring and troubleshooting.

```text
GET http://127.0.0.1:8001/health
```

Response:
```json
{ "status": "ok" }
```

If the sidecar is still starting up (EasyOCR loading), the `/health` endpoint will not respond at all until Uvicorn is ready — a connection error means it is not yet up. The first receipt request will be slow (20–30 sec) if `PRELOAD_EASYOCR=1` is not set.

---

## Postman Setup

### Environment variables (set in Postman under Environments)

| Variable | Value |
| --- | --- |
| `base_url` | `http://SERVER_IP:5000` |
| `token` | your `JAZZ_API_TOKEN` value |

### Collection: Health Check

```text
Method:  GET
URL:     {{base_url}}/health
Headers: (none)
Body:    (none)
```

Expected response:
```json
{ "status": "ok" }
```

### Collection: Verify Receipt

```text
Method:  POST
URL:     {{base_url}}/api/v1/verify
Headers: Content-Type: application/json
Body (raw JSON):
{
    "token":        "{{token}}",
    "amount_pkr":   1500.00,
    "image_base64": "{{image_base64}}"
}
```

**To load an image into `image_base64` — Postman Pre-request Script:**

```javascript
// In Postman: Scripts tab → Pre-request → paste this
// Then select your image file using pm.variables.set from a Collection Runner CSV
// OR convert manually using PowerShell (see below) and paste the base64 string
```

**To generate base64 from a file — PowerShell (run on your machine):**

```powershell
$bytes = [System.IO.File]::ReadAllBytes("C:\path\to\receipt.jpg")
$b64   = [Convert]::ToBase64String($bytes)
$b64 | Set-Clipboard    # copies to clipboard — paste into Postman body
```

---

## PowerShell API Calls

### Health check

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/health" -Method Get
```

### Verify a single receipt

```powershell
$bytes = [System.IO.File]::ReadAllBytes("C:\path\to\receipt.jpg")
$b64   = [Convert]::ToBase64String($bytes)
$body  = @{
    token        = "YOUR_TOKEN_HERE"
    amount_pkr   = 1500.00
    image_base64 = $b64
} | ConvertTo-Json

try {
    $r = Invoke-RestMethod `
        -Uri "http://127.0.0.1:5000/api/v1/verify" `
        -Method Post -ContentType "application/json" -Body $body

    Write-Host "AmountVerified : $($r.AmountVerified)"
    Write-Host "ImageDup       : $($r.ImageDup)"
    Write-Host "DateValid      : $($r.DateValid)"
    $rd = if ($r.receipt_date) { $r.receipt_date } else { "(not found)" }
    Write-Host "receipt_date   : $rd"
} catch {
    $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    Write-Host "Error $($_.Exception.Response.StatusCode.value__): $($reader.ReadToEnd())"
}
```

### Batch test multiple images

```powershell
$token = "YOUR_TOKEN_HERE"
$tests = @(
    @{ path = "C:\receipts\r1.jpg"; amount = 1500.00 },
    @{ path = "C:\receipts\r2.png"; amount = 3050.00 },
    @{ path = "C:\receipts\r3.jpg"; amount = 850.00  }
)

foreach ($t in $tests) {
    $b64  = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($t.path))
    $body = @{ token = $token; amount_pkr = $t.amount; image_base64 = $b64 } | ConvertTo-Json
    try {
        $r  = Invoke-RestMethod -Uri "http://127.0.0.1:5000/api/v1/verify" `
                  -Method Post -ContentType "application/json" -Body $body
        $rd = if ($r.receipt_date) { $r.receipt_date } else { "null" }
        $name = [System.IO.Path]::GetFileName($t.path)
        Write-Host "$name  AmountVerified=$($r.AmountVerified)  ImageDup=$($r.ImageDup)  DateValid=$($r.DateValid)  date=$rd"
    } catch {
        Write-Host "$([System.IO.Path]::GetFileName($t.path))  ERROR: $($_.Exception.Response.StatusCode.value__)"
    }
}
```

---

## Troubleshooting

### 503 — "Image processing service unavailable" on every request

Python sidecar is not running. Check:

```powershell
Get-ScheduledTask -TaskName "OPD Python Sidecar" | Select-Object TaskName, State
netstat -ano | Select-String ":8001"
Invoke-RestMethod http://127.0.0.1:8001/health
```

Start it manually to see the error:

```powershell
cd C:\inetpub\ImageIntelService
venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001 --workers 1
```

### 503 — "Database unavailable" on every request

SQL Server is unreachable:

```powershell
Get-Service -Name "MSSQL*"
sqlcmd -S 127.0.0.1,1433 -U sa -P PROD_PASSWORD -Q "SELECT 1"
```

Confirm the connection string in `appsettings.Production.json` is correct. Never use `localhost` — always `127.0.0.1,1433`.

### 401 on every request

`JAZZ_API_TOKEN` is not set in IIS or the value does not match what the client is sending:

```powershell
& "$env:SystemRoot\system32\inetsrv\appcmd.exe" list config "OpdVerificationApi" `
    /section:system.webServer/aspNetCore
```

### 502.5 from IIS

.NET 10 Hosting Bundle is not installed, or it was installed before IIS (IIS must be installed first, then the Hosting Bundle, then `iisreset`). Reinstall the Hosting Bundle, then run `iisreset`.

### `opd_att_index` does not exist — crash on first request

The SQL migration script was not run. Run it now:

```powershell
sqlcmd -S 127.0.0.1,1433 -U sa -P PROD_PASSWORD -i C:\inetpub\run_once_create_index.sql
```

### Tesseract not found

```text
RuntimeError: tesseract is not installed or it's not in your PATH
```

Add this to `C:\inetpub\ImageIntelService\.env` and restart the sidecar task:

```ini
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

### Re-testing an image that already shows `ImageDup=true`

Once indexed, an image always returns `ImageDup=true`. To re-test amount/date extraction, delete its row:

```powershell
$bytes = [System.IO.File]::ReadAllBytes("C:\path\to\image.jpg")
$sha   = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
$hash  = ($sha | ForEach-Object { $_.ToString("x2") }) -join ""

$conn = New-Object System.Data.SqlClient.SqlConnection(
    "Server=127.0.0.1,1433;Database=opd-media;User Id=sa;Password=PROD_PASSWORD;TrustServerCertificate=True;")
$conn.Open()
$cmd = $conn.CreateCommand()
$cmd.CommandText = "DELETE FROM opd_att_index WHERE sha256_hash = @h"
$cmd.Parameters.AddWithValue("@h", $hash) | Out-Null
Write-Host "Deleted $($cmd.ExecuteNonQuery()) row(s). Image can now be re-submitted."
$conn.Close()
```

---

## Technology Stack

| Layer | Technology |
| --- | --- |
| Public API | .NET 10 (C#), ASP.NET Core, Dapper |
| OCR / AI Sidecar | Python 3.11, FastAPI, Uvicorn |
| OCR Engines | Tesseract 5, EasyOCR 1.7 |
| Database | SQL Server 2017+, pyodbc (Python), Dapper (C#) |
| Hashing | SHA-256 (exact dup), ImageHash pHash (near dup) |
| Hosting | IIS 10 + AspNetCoreModuleV2 (.NET API) · Windows Task Scheduler (Python sidecar) |
