# Mini PC Homebase Deployment

This guide is for an always-on Windows mini PC running Time Secretary as the homebase.

## 1. Install Base Requirements

Install Anaconda on the mini PC, then copy or clone this project to a stable folder such as:

```text
C:\Time Secretary
```

Open Anaconda PowerShell:

```powershell
Set-Location "C:\Time Secretary"
& 'C:\ProgramData\anaconda3\python.exe' -m pip install -r requirements.txt
```

## 2. Restore Data From Laptop

On the laptop, create a backup:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\backup_database.py
```

Move the generated `.zip` from `backups\` to the mini PC.

On the mini PC:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\migrate_to_mini_pc.py "C:\Path\To\time-secretary-backup-YYYYMMDD-HHMMSS.zip" --target-root "C:\Time Secretary"
```

The restore script copies the incoming database into the configured `DATABASE_URL` path and preserves any existing database as a `.pre_restore_YYYYMMDD-HHMMSS` file.

## 3. Configure `.env`

```powershell
Copy-Item .env.example .env
```

Recommended mini PC settings:

```dotenv
APP_ENV=production
DEPLOYMENT_MODE=mini_pc
DEV_MODE=false
SIMULATE_SMS=false
SMS_PROVIDER=twilio
START_SCHEDULER=true
DATABASE_URL=sqlite:///./data/time_secretary.db
REPORTS_DIR=./reports
BACKUPS_DIR=./backups
BACKUP_ENABLED=true
BACKUP_TIME=03:00
BACKUP_RETENTION_DAYS=60
LLM_ENABLED=false
LLM_PROVIDER=none
```

Add Twilio values only on the mini PC:

```dotenv
USER_PHONE_NUMBER=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
PUBLIC_BASE_URL=
REQUIRE_TWILIO_SIGNATURE_VALIDATION=true
```

## 4. Run A Local Smoke Test

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m uvicorn time_secretary.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/dashboard/settings
```

Stop the process with `Ctrl+C` after the smoke test.

## 5. Start Automatically With Task Scheduler

Run PowerShell as Administrator and adjust `$project` if needed:

```powershell
$project = 'C:\Time Secretary'
$python = 'C:\ProgramData\anaconda3\python.exe'
$args = '-m uvicorn time_secretary.main:app --host 127.0.0.1 --port 8000'
$action = New-ScheduledTaskAction -Execute $python -Argument $args -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'Time Secretary' -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest
```

Start it:

```powershell
Start-ScheduledTask -TaskName 'Time Secretary'
```

Check it:

```powershell
Get-ScheduledTask -TaskName 'Time Secretary'
```

## 6. SMS Webhook

Twilio needs a public HTTPS URL for:

```text
/sms/inbound
```

Use a tunnel or reverse proxy, then set `PUBLIC_BASE_URL` in `.env` to the public root URL. Keep the FastAPI bind address at `127.0.0.1` unless you intentionally expose the dashboard on the LAN.

## 7. Backups

With `START_SCHEDULER=true` and `BACKUP_ENABLED=true`, the app schedules a daily backup at `BACKUP_TIME`.

Manual backup:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\backup_database.py
```

Manual CSV export:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\export_data.py --output-dir "C:\Time Secretary\backups\exports"
```

## 8. Optional Ollama

Keep LLM disabled until the deterministic app is stable.

For low-confidence SMS parsing:

```dotenv
LLM_ENABLED=true
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
LLM_BASE_URL=http://localhost:11434
LLM_USE_FOR_LOW_CONFIDENCE_ONLY=true
LLM_SAVE_RAW_RESPONSES=false
```

Use `/dashboard/settings` to test availability. If Ollama is unavailable, invalid, or returns bad JSON, Time Secretary falls back to deterministic behavior and logs the failed LLM call.

For local LLM-assisted briefing narratives on an Intel N100 / 16 GB mini PC:

```powershell
ollama pull llama3.2:3b
```

```dotenv
LLM_REPORTS_ENABLED=true
LLM_REPORT_PROVIDER=ollama
LLM_REPORT_MODEL=llama3.2:3b
LLM_REPORT_TIMEOUT_SECONDS=90
LLM_REPORT_MAX_INPUT_CHARS=12000
LLM_REPORT_MAX_OUTPUT_TOKENS=1200
LLM_REPORT_TEMPERATURE=0.2
LLM_REPORT_CACHE_ENABLED=true
LLM_REPORT_VALIDATE_CLAIMS=true
INCLUDE_SENSITIVE_LOCAL_REPORTS=false
```

Run safe model checks:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\test_llm_report_model.py
& 'C:\ProgramData\anaconda3\python.exe' scripts\benchmark_llm_models.py
& 'C:\ProgramData\anaconda3\python.exe' scripts\evaluate_report_quality_fake_data.py
```

Sensitive secure captures are not included in local reports or LLM prompts unless `INCLUDE_SENSITIVE_LOCAL_REPORTS=true` and the individual briefing request asks for sensitive output.

## 9. Tailscale Secure Capture

Install Tailscale on the mini PC and iPhone, sign into the same tailnet, and expose Time Secretary privately with Tailscale Serve. Use Serve, not Funnel, for sensitive notes.

Configure `.env`:

```dotenv
SECURE_CAPTURE_ENABLED=true
SECURE_CAPTURE_TOKEN=choose-a-long-random-secret
SECURE_CAPTURE_ALLOW_LLM=false
LLM_ALLOW_WORK_NOTES=false
LOG_SECURE_CAPTURE_BODY=false
TAILSCALE_ONLY_MODE=true
EXPORT_INCLUDE_SENSITIVE=false
```

iPhone Shortcut:

1. Ask for Text.
2. Get Current Date.
3. Get Contents of URL.
4. POST JSON to `https://your-mini-pc.your-tailnet.ts.net/secure-capture`.
5. Include `capture_type`, `text`, `source`, `created_at`, and `secret`.
6. Show notification `Note captured.`

Use SMS for low-sensitivity timekeeping and location events. Use secure capture for sensitive notes and local-only captures.

## 10. Update Procedure

1. Stop the scheduled task.
2. Create a backup.
3. Copy updated project files.
4. Install dependencies.
5. Start the app once and check `/health`.
6. Start the scheduled task.

```powershell
Stop-ScheduledTask -TaskName 'Time Secretary'
& 'C:\ProgramData\anaconda3\python.exe' scripts\backup_database.py
& 'C:\ProgramData\anaconda3\python.exe' -m pip install -r requirements.txt
Start-ScheduledTask -TaskName 'Time Secretary'
```
