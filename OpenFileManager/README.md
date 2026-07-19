# OpenFilesLogger starter

This starter logs Windows file-open/access audit events. It is designed for owned/administered computers with user consent.

You can now test it **without PostgreSQL** first.

## What it captures

- File path
- File extension and category: `office`, `pdf`, `cad`, or `other`
- Windows user/domain
- Process name and process ID
- Event time
- Windows Security log record ID
- Raw event JSON

It does not read file contents.

## Requirements

For local preview only:

- Windows machine where you have administrator rights
- Python 3.10+
- PowerShell

For PostgreSQL upload mode:

- PostgreSQL database
- `psycopg`, installed with `pip install -r requirements.txt`

## 1. Enable Windows file-system auditing

Open PowerShell as Administrator and run, replacing the paths with the folders/drives you are authorized to audit:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows_auditing.ps1 -Paths "D:\Projects","E:\CAD","C:\Users\Public\Documents"
```

Auditing every file on `C:\` can create very high event volume. Start with project/document folders first.

## 2. No-database test mode

These commands do **not** connect to PostgreSQL and do **not** upload anything.

### Preview recent file-open/access events

Run from an elevated terminal:

```bash
python file_open_logger.py preview
```

Or use the helper PowerShell script:

```powershell
powershell -ExecutionPolicy Bypass -File .\test_without_postgres.ps1
```

Open a PDF, Word/Excel file, or CAD file inside one of your audited folders, then run preview again.

Show more rows:

```bash
python file_open_logger.py preview --limit 100
```

Print machine-readable JSON lines:

```bash
python file_open_logger.py preview --json
```

`dry-run` is also available as an alias:

```bash
python file_open_logger.py dry-run
```

### Watch the console continuously without PostgreSQL

```bash
python file_open_logger.py watch-preview
```

### Save locally to a JSONL file without PostgreSQL

```bash
python file_open_logger.py log-local --output open_file_events.jsonl
```

Each line in `open_file_events.jsonl` is one JSON object. This is useful for checking exactly what would later be sent to PostgreSQL.

## 3. Configuration

PowerShell example:

```powershell
$env:FILE_LOGGER_MODE="documents"       # documents or all
$env:FILE_LOGGER_POLL_SECONDS="10"
$env:FILE_LOGGER_LOOKBACK_SECONDS="300"
$env:FILE_LOGGER_IGNORE_SYSTEM_PATHS="1"
$env:FILE_LOGGER_PREVIEW_LIMIT="50"
$env:FILE_LOGGER_LOCAL_OUTPUT="open_file_events.jsonl"
```

Use `documents` to log Office/PDF/CAD file extensions. Use `all` to keep all audited file paths.

## 4. PostgreSQL mode, after local testing

Create the database/user only when you are ready to upload metadata to PostgreSQL:

```sql
CREATE USER file_logger WITH PASSWORD 'change-me';
CREATE DATABASE filelogger OWNER file_logger;
```

Install PostgreSQL dependency:

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Set connection string:

```powershell
$env:FILE_LOGGER_PG_DSN="postgresql://file_logger:change-me@localhost:5432/filelogger"
```

Initialize and run:

```bash
python file_open_logger.py init-db
python file_open_logger.py run-once
python file_open_logger.py run
```

Check latest rows:

```sql
SELECT event_time, username, file_category, process_name, path
FROM file_open_events
ORDER BY event_time DESC
LIMIT 20;
```

## Notes

- Windows Event ID 4663 is generated only for objects whose audit rules/SACL match the access. Enabling audit policy alone is not enough.
- Local preview, local JSONL logging, and PostgreSQL mode all read the same Windows Security events.
- For network shares, run the auditing on the file server for best results.
- Office applications may create temporary/autosave files; this starter filters common `~$` and `.tmp` files.
