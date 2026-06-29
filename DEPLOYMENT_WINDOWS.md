# Windows Laptop Deployment

This guide is for local development and daily laptop use with Anaconda.

## 1. Open Anaconda PowerShell

Use Anaconda Prompt or PowerShell with Anaconda available. The known working interpreter is:

```powershell
C:\ProgramData\anaconda3\python.exe
```

From the project folder:

```powershell
Set-Location "C:\Path\To\Time Secretary"
```

## 2. Install Dependencies

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m pip install -r requirements.txt
```

## 3. Configure `.env`

```powershell
Copy-Item .env.example .env
```

Recommended laptop defaults:

```dotenv
APP_ENV=development
DEPLOYMENT_MODE=laptop
DEV_MODE=true
SIMULATE_SMS=true
SMS_PROVIDER=dev
START_SCHEDULER=false
DATABASE_URL=sqlite:///./data/time_secretary.db
REPORTS_DIR=./reports
BACKUPS_DIR=./backups
LLM_ENABLED=false
LLM_PROVIDER=none
```

Keep real phone numbers and Twilio credentials only in `.env`.

## 4. Run The App

```powershell
& 'C:\ProgramData\anaconda3\python.exe' run_dev.py
```

Open:

```text
http://127.0.0.1:8000/dashboard
```

Settings page:

```text
http://127.0.0.1:8000/dashboard/settings
```

## 5. Simulate SMS

```powershell
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "worked on Project Alpha report"
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "blue threshold maybe"
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "remind me tomorrow to add references"
```

## 6. Optional Twilio Testing

Set:

```dotenv
DEV_MODE=false
SIMULATE_SMS=false
SMS_PROVIDER=twilio
USER_PHONE_NUMBER=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
PUBLIC_BASE_URL=
REQUIRE_TWILIO_SIGNATURE_VALIDATION=true
```

Expose the local server with a tunnel and point Twilio Messaging webhook to:

```text
https://your-public-url.example/sms/inbound
```

## 7. Optional Local LLM

LLM support is off by default. The app works fully without it.

For low-confidence SMS parsing with Ollama:

```dotenv
LLM_ENABLED=true
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
LLM_BASE_URL=http://localhost:11434
LLM_USE_FOR_LOW_CONFIDENCE_ONLY=true
LLM_SAVE_RAW_RESPONSES=false
```

The LLM is only used for low-confidence natural SMS parsing unless `LLM_USE_FOR_LOW_CONFIDENCE_ONLY=false`.

For local LLM-assisted briefing narratives:

```powershell
ollama pull llama3.2:3b
```

```dotenv
LLM_REPORTS_ENABLED=true
LLM_REPORT_PROVIDER=ollama
LLM_REPORT_MODEL=llama3.2:3b
LLM_REPORT_CACHE_ENABLED=true
LLM_REPORT_VALIDATE_CLAIMS=true
INCLUDE_SENSITIVE_LOCAL_REPORTS=false
```

Safe checks:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\test_llm_report_model.py
& 'C:\ProgramData\anaconda3\python.exe' scripts\benchmark_llm_models.py
& 'C:\ProgramData\anaconda3\python.exe' scripts\evaluate_report_quality_fake_data.py
```

## 8. Work Hours And Secure Capture

SMS location commands such as `arrived work`, `left work`, `arrived home`, and `left home` feed the Work Hours Ledger. Review it at:

```text
http://127.0.0.1:8000/dashboard/work-hours
```

Sensitive notes should use `/secure-capture` over Tailscale rather than SMS/Twilio. Configure:

```dotenv
SECURE_CAPTURE_ENABLED=true
SECURE_CAPTURE_TOKEN=choose-a-long-random-secret
SECURE_CAPTURE_ALLOW_LLM=false
LLM_ALLOW_WORK_NOTES=false
LOG_SECURE_CAPTURE_BODY=false
TAILSCALE_ONLY_MODE=true
EXPORT_INCLUDE_SENSITIVE=false
```

Dashboard pages:

```text
http://127.0.0.1:8000/dashboard/work-intelligence
http://127.0.0.1:8000/dashboard/secure-captures
```

## 9. Backups And Exports

Create a backup archive:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\backup_database.py
```

Export CSV files:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\export_data.py
```

Backups include the SQLite database snapshot, reports, `.env.example`, README, and a manifest. They do not include `.env`.

## 10. Tests

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m pytest
```
