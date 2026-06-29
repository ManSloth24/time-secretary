# Time Secretary

Time Secretary is a local-first SMS time tracker and lightweight secretary. It sends check-ins, classifies natural replies into Work/Home/Unknown time entries, captures todos/reminders/project notes, and generates daily, weekly, and monthly Markdown reports.

Data is stored locally in SQLite by default. SMS transport can use Twilio, but the app does not send time logs, todos, notes, reports, or project data to any LLM or analytics provider. SMS message contents still pass through the SMS provider you configure.

The app also accepts texts at any time. If a message is not clearly a time log, it becomes a todo, reminder, project note, idea, follow-up, question, or Secretary Inbox item with a review date. Captured thoughts are surfaced in the EOD report, weekly review, dashboard, and SMS review flow.

## Setup

1. Open Anaconda PowerShell and install dependencies:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m pip install -r requirements.txt
```

2. Configure environment:

```powershell
Copy-Item .env.example .env
```

Edit `.env` with local settings. Leave private values blank until you are ready to enable real SMS. Do not commit `.env`.

## Run In DEV_MODE Without Twilio

Set this in `.env`:

```dotenv
DEV_MODE=true
START_SCHEDULER=false
```

Start the app:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' run_dev.py
```

Open `http://127.0.0.1:8000/dashboard`.

Operational settings are available at `http://127.0.0.1:8000/dashboard/settings`.

Simulate an inbound text:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "worked on Project Alpha report"
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "remind me tomorrow to add references"
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "todo high finish project beta writeup by Friday"
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "agenda today"
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "circle back on follow-up options next week"
& 'C:\ProgramData\anaconda3\python.exe' simulate_sms.py "review inbox"
```

Seed demo data:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' seed_demo_data.py
```

## Run With Twilio

Set these values in `.env`:

```dotenv
DEV_MODE=false
USER_PHONE_NUMBER=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
PUBLIC_BASE_URL=
REQUIRE_TWILIO_SIGNATURE_VALIDATION=true
```

Use placeholders in source control only. Put real phone numbers and credentials only in `.env` or your local environment.

Expose the local FastAPI server with a tunnel such as ngrok:

```powershell
ngrok http 8000
```

Set `PUBLIC_BASE_URL` to the HTTPS tunnel URL. In Twilio, configure the Messaging webhook to:

```text
https://your-public-url.example/sms/inbound
```

The webhook route validates Twilio signatures unless `DEV_MODE=true` or `REQUIRE_TWILIO_SIGNATURE_VALIDATION=false`.

## Scheduler

Set `START_SCHEDULER=true` to send check-in prompts and due reminders from the running FastAPI process. Leave it off while manually testing with `simulate_sms.py`.

Prompts are sent only during `ACTIVE_START_TIME` to `ACTIVE_END_TIME`. Set `QUIET_MODE=true` to suppress scheduled check-ins. The scheduler checks idempotently for the current interval so restarts do not double-send the same prompt.

Useful SMS commands:

```text
pause 1h
pause until 2pm
resume
status
skip
report today
report week
report month
```

## Corrections And Projects

Correct entries from the dashboard or by SMS:

```text
fix last category = Work
fix last category = Home
fix last project = Project Alpha
project add Project Alpha
alias Project Alpha = alpha, alpha material, project report
```

Corrections create local classification rules so future matching improves.

## Secretary Commands

```text
capture look into better review thresholding
note Project Alpha: need references on follow-up item
idea try a cleaner dashboard review queue
circle back on follow-up options next week
follow up with operations about the blocked queue item
review inbox
review captured
make todo 3
remind me about 3 tomorrow
dismiss 3
assign 3 to project Project Alpha
what did I capture today?
what do I need to circle back on?
what notes need action?
show unassigned
show stale items
show snoozed
todo buy batteries
todo high finish project beta writeup by Friday
done 12
done send the stakeholder report
reminders
snooze 30m
cancel
list todos
list work todos
list project Project Alpha
notes Project Alpha
agenda today
agenda tomorrow
what did I say about Project Alpha?
project status Personal Project
help secretary
```

Natural messages like `remind me tomorrow to check the project update`, `note for Project Alpha project: follow-up question needs references`, and `worked on Project Alpha report, remind me tomorrow to add references` are parsed deterministically and stored locally.

During `review inbox`, short replies act on the current inbox item:

```text
todo
remind tomorrow
snooze week
assign Project Alpha
dismiss
next
done
```

## Reports

Reports are Markdown files saved under `reports/` when `SAVE_REPORTS_TO_DISK=true`.

Generate them from the dashboard, SMS commands, or API:

```powershell
Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8000/reports/generate -Body @{ report_type = "daily" }
```

Daily, weekly, monthly, and yearly/YTD reports include time totals, Work/Home split, project allocation, missed check-ins, todos, reminders, project notes, decisions, action items, work-hours ledger details, work intelligence, and suggested next priorities.

Daily reports include `Things To Circle Back On`. The dashboard also has `/dashboard/circle-back` for inbox items, notes needing action, todos without due dates, snoozed reminders, stale items, and unassigned captures.

## Secure Local Briefings

Briefings let you request local reports, project notes, meeting prep, topic summaries, open actions, risks, questions, and run context from SMS, the dashboard, or secure iPhone Shortcut capture.

SMS examples:

```text
brief me on Project Alpha
send me notes on Project Alpha
meeting prep Project Alpha
prep me for project beta
what do I need to know about Run A-001
open items for Project Alpha
what changed on Project Alpha this week
risks for Project Alpha
questions for Project Alpha meeting
```

SMS replies are link-only by default. The full briefing is stored locally in SQLite and Markdown under `reports/briefings/`. The SMS response does not include private details:

```text
Briefing ready: http://device.tailnet.ts.net:8002/dashboard/briefings/8f3a91c0
```

Dashboard/API:

- `/dashboard/briefings`
- `/dashboard/briefings/{opaque_id}`
- `POST /briefings/generate`

Generate by API:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8002/briefings/generate -ContentType 'application/json' -Body '{"topic":"Project Alpha","briefing_type":"meeting_prep","include_sensitive":false,"source":"dashboard"}'
```

Briefing settings:

```dotenv
BRIEFINGS_ENABLED=true
BRIEFING_DEFAULT_WINDOW_DAYS=30
BRIEFING_SMS_LINK_ONLY=true
BRIEFING_USE_OPAQUE_IDS=true
BRIEFING_INCLUDE_SENSITIVE_DEFAULT=false
BRIEFING_REPORTS_DIR=./reports/briefings
BRIEFING_PUBLIC_BASE_URL=
BRIEFING_TAILSCALE_BASE_URL=
```

## Local LLM Report Engine

Briefings are deterministic by default. When enabled, the local report engine builds a deterministic fact pack first, then optionally asks a local Ollama model to write a short narrative. If the model is unavailable, slow, invalid, or fails validation, Time Secretary keeps the deterministic briefing.

Recommended small-mini-PC model:

```powershell
ollama pull llama3.2:3b
```

Enable the report engine in `.env`:

```dotenv
LLM_REPORTS_ENABLED=true
LLM_REPORT_PROVIDER=ollama
LLM_REPORT_MODEL=llama3.2:3b
LLM_REPORT_TIMEOUT_SECONDS=90
LLM_REPORT_MAX_INPUT_CHARS=12000
LLM_REPORT_MAX_OUTPUT_TOKENS=1200
LLM_REPORT_TEMPERATURE=0.2
LLM_REPORT_USE_STRUCTURED_OUTPUT=true
LLM_REPORT_CACHE_ENABLED=true
LLM_REPORT_VALIDATE_CLAIMS=true
```

Sensitive notes stay blocked from local LLM report prompts unless all gates are on:

```dotenv
LLM_ENABLED=true
LLM_ALLOW_WORK_NOTES=true
SECURE_CAPTURE_ALLOW_LLM=true
INCLUDE_SENSITIVE_LOCAL_REPORTS=true
```

Even then, the individual briefing request must use `include_sensitive=true`. With defaults, sensitive secure-capture text is withheld from fact packs and reports.

Each generated briefing saves local artifacts under `reports/briefings/.../`:

- `deterministic_fact_pack.json`
- `deterministic_briefing.md`
- `llm_narrative.md` when accepted
- `final_briefing.md`

The dashboard settings page shows report-model config, Ollama availability, recent failures, average generation time, and buttons for a test prompt and benchmark helper.

Useful scripts:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\test_llm_report_model.py
& 'C:\ProgramData\anaconda3\python.exe' scripts\benchmark_llm_models.py
& 'C:\ProgramData\anaconda3\python.exe' scripts\evaluate_report_quality_fake_data.py
```

## Public Compliance Pages

The `/docs` folder contains public Privacy Policy and Terms & Conditions pages for SMS registration/compliance:

- [Privacy Policy](docs/privacy-policy.md)
- [Terms & Conditions](docs/terms-and-conditions.md)
- [Compliance index](docs/index.md)

These pages are safe to publish publicly. They do not contain secrets, credentials, private app data, generated reports, secure capture contents, or private briefing content.

If using GitHub Pages, publish the `/docs` folder and use the resulting Privacy Policy and Terms & Conditions URLs for Twilio registration.

## Optional Local LLM

LLM parsing is disabled by default. The deterministic app works fully without an LLM.

For local Ollama-assisted low-confidence SMS parsing:

```dotenv
LLM_ENABLED=true
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
LLM_BASE_URL=http://localhost:11434
LLM_USE_FOR_LOW_CONFIDENCE_ONLY=true
LLM_SAVE_RAW_RESPONSES=false
```

LLM calls are logged in the local `llm_calls` table. Invalid JSON, unavailable providers, or low-confidence responses fall back to deterministic behavior.

This SMS parser setting is separate from `LLM_REPORTS_ENABLED`, which controls local LLM-assisted briefing/report narratives.

## Two-Channel Capture Strategy

Use SMS/Twilio for low-sensitivity timekeeping:

```text
arrived work
left work
arrived home
left home
lunch
worked on Project Alpha report
remind me tomorrow to check project update
```

Use Secure Capture over Tailscale/iPhone Shortcuts for sensitive notes and local-only captures. Secure captures stay local, do not go through Twilio, and do not use cloud LLMs.

## SMS Location And Work Hours

Location SMS commands:

```text
arrived work
left work
arrived home
left home
at work
at home
where am I
location status
add place Work
list places
```

Work-hours commands:

```text
work hours today
work hours week
work hours month
work hours year
work summary
fix arrived work 7:35am
fix left work 5:20pm
report year
report ytd
```

The Work Hours Ledger tracks worksite duration separately from logged Work activity. Lunch at work stays inside worksite duration but is separated from logged work activity.

Dashboard pages:

- `/dashboard/work-hours`
- `/dashboard/work-intelligence`
- `/dashboard/briefings`
- `/dashboard/secure-captures`

## Secure Capture With Tailscale And iPhone Shortcuts

Set a shared secret in `.env`:

```dotenv
SECURE_CAPTURE_ENABLED=true
SECURE_CAPTURE_TOKEN=choose-a-long-random-secret
SECURE_CAPTURE_ALLOW_LLM=false
LLM_ALLOW_WORK_NOTES=false
LOG_SECURE_CAPTURE_BODY=false
TAILSCALE_ONLY_MODE=true
```

Shortcut setup:

1. Add **Ask for Text**.
2. Add **Get Current Date**.
3. Add **Get Contents of URL**.
4. Method: `POST`.
5. URL: `https://your-tailscale-name.your-tailnet.ts.net/secure-capture`.
6. Request body: JSON.
7. Show notification: `Note captured.`

JSON body:

```json
{
  "capture_type": "work_note",
  "text": "Shortcut input text",
  "source": "iphone_shortcut",
  "created_at": "Current Date",
  "secret": "choose-a-long-random-secret"
}
```

Briefing request body:

```json
{
  "capture_type": "briefing_request",
  "text": "Generate a meeting prep report on Run A-001 and Project Alpha follow-up concerns",
  "source": "iphone_shortcut",
  "created_at": "Current Date",
  "secret": "choose-a-long-random-secret"
}
```

Other capture types: `run_change`, `observation`, `todo`, `reminder`, `project_update`, `decision`, `time_entry`, `run_metric`, `process_result`, `briefing_request`, `report_request`, `meeting_prep_request`.

Security policy:

- Sensitive notes should use `/secure-capture`, not SMS.
- Secure capture does not go through Twilio.
- Secure capture data remains local in SQLite and local backups.
- Secure capture is not sent to cloud LLMs.
- Local LLM parsing for notes remains off unless both `SECURE_CAPTURE_ALLOW_LLM=true` and `LLM_ALLOW_WORK_NOTES=true`.
- Sensitive local report content remains blocked unless `INCLUDE_SENSITIVE_LOCAL_REPORTS=true` and the individual request asks for sensitive output.
- Sensitive CSV export text is redacted unless `EXPORT_INCLUDE_SENSITIVE=true`.
- Briefing/report content is never sent over SMS; SMS gets an opaque local link only.
- Briefings do not use cloud LLMs. Deterministic local generation works with `LLM_ENABLED=false` and `LLM_REPORTS_ENABLED=false`.

Example curl test:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8002/secure-capture -ContentType 'application/json' -Body '{"capture_type":"work_note","text":"private note","source":"iphone_shortcut","secret":"choose-a-long-random-secret"}'
```

## Backup, Export, And Migration

Create a backup:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\backup_database.py
```

Export CSV files:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\export_data.py
```

Restore a backup on the mini PC:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' scripts\migrate_to_mini_pc.py "C:\Path\To\time-secretary-backup-YYYYMMDD-HHMMSS.zip" --target-root "C:\Time Secretary"
```

Deployment docs:

- `DEPLOYMENT_WINDOWS.md`
- `DEPLOYMENT_MINIPC.md`
- `MIGRATION.md`

## Tests

Run:

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -m pytest
```
