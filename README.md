# OpportunityHub

A portal for internships, jobs, and events, built with Python 3.13, Django 5.2,
SQLite, Django templates, HTML/CSS, and minimal JavaScript.

Public pages show published opportunities with search, filters, pagination, and
detail pages. Authorized staff manage drafts, imported items, and publication
through Django Admin. This source distribution contains no accounts or live data.

## Local setup

Before local setup or Git initialization, audit the clean source distribution:

```powershell
python -B scripts/check_public_export.py
```

The audit expects source files only, with no `.env`, virtual environment, database,
Python cache, or Git directory. Run it on a clean distribution again before
publishing. Local development creates private files and therefore changes that
boundary.

Run setup commands from this folder in PowerShell. Create `.env` only if it does
not already exist; its generated secret stays local.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -c "import secrets; from pathlib import Path; p = Path('.env'); p.write_text(p.read_text().replace('replace-with-a-generated-secret', secrets.token_urlsafe(64)), encoding='utf-8')"
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

Open `http://127.0.0.1:8000/` for the public list and
`http://127.0.0.1:8000/admin/` for staff sign-in. On macOS/Linux, create the
virtual environment with `python3 -m venv .venv` and use `.venv/bin/python`.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required generated secret; startup rejects missing or placeholder values. |
| `DJANGO_DEBUG` | Defaults to `False`; set `True` only during local development. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hostnames; defaults to `127.0.0.1,localhost`. |
| `SLACK_CHANNEL_URLS` | Explicit approved channel allowlist; empty in the example configuration. |
| `GMAIL_ACCOUNT_EMAIL` | Exact authorized mailbox address; empty disables Gmail importing. |

Existing environment variables take precedence over `.env`. Source code and
tests use synthetic importer identities and reserved `.invalid` domains.

## Staff workflow

New manually entered opportunities start as drafts. Staff need the relevant
Django model permissions to add, edit, or publish records; staff status alone
does not grant those permissions. Public list and detail views exclude drafts
and archived records, including when viewed by staff.

Admin actions can publish selected drafts or all saved drafts. The explicit
**allow missing details** actions also permit a missing organization or
opportunity type after review, while retaining title and other validation.
Public pages mark these records as incomplete. Imported records normally become
drafts or review items and require a separate staff publication decision.

Before publishing imported content, inspect descriptions and source links for
personal information, private conversations, confidential material, and content
you do not have permission to redistribute. Imported snapshots, browser sessions,
and command output can contain private data even when a resulting opportunity
is suitable for publication.

## Optional importers

Browser-based Notion, Slack, and Gmail import code is included for adaptation.
Install its existing optional dependency only when needed:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-importers.txt
```

The distributed Slack and Notion sources are synthetic. Browser collection
refuses placeholder `.invalid` sources. To enable an importer, deliberately
configure an approved source and review its source identity validation and
allowlist in `opportunities/services/`; setting a channel URL alone does not
replace the synthetic Slack workspace. Configure Gmail only for a mailbox you
are authorized to read. Importers use separate local browser profiles.

Management commands provide preview modes by default and use `--import` to save
validated draft opportunities or review items. Review each command's `--help`
before use. No browser session, scheduled task, source account, or live
integration is included or activated by this distribution.

## Project layout

```text
config/                    Django settings and routing
opportunities/             Models, admin, forms, public views, and tests
opportunities/services/    Validation, publication, and optional import logic
opportunities/migrations/  Database schema migrations
templates/                 Public pages and admin templates
static/                    Styles and static source files
scripts/                   Optional local scheduling helpers
```

Keep this as one server-rendered Django application. Source links accept only
HTTP/HTTPS. Preserve authentication, permissions, CSRF protection, template
escaping, and ORM access. Import services call `full_clean()` before saving.

## Verification

After local setup:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m pip check
```

Tests use a separate test database and synthetic fixtures. Model changes require
reviewed migrations. No production deployment configuration is supplied.

## Publishing the source

See [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md) for the source distribution boundary.
Create a new repository from this folder when ready; do not attach the original
repository's history or remotes. Keep `.env`, databases, profiles, logs, backups,
and private imported content out of the repository. `.gitignore` helps prevent
accidental additions but does not sanitize files that have already been tracked.
