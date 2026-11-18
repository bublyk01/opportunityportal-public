# OpportunityHub development instructions

## Architecture

- Use Python 3.13, Django 5.2 LTS, SQLite for local development, Django templates,
  HTML/CSS, and minimal JavaScript.
- Keep one server-rendered application; do not add React, an API, or microservices.
- Keep settings/routing in config/, application code in opportunities/, import
  services in opportunities/services/, and interface files in templates/ and static/.

## Development

- Read existing files before editing and preserve unrelated changes.
- Make focused changes with Django's built-in features.
- Create migrations for model changes and test meaningful behavior.
- Run manage.py check, manage.py test, and makemigrations --check --dry-run.
- Update README.md when setup or behavior changes.
- Ask before adding dependencies beyond the existing requirements files.
- Do not delete files, commit, push, or deploy without explicit permission.

## Security and privacy

- Only authorized staff may modify opportunities through Django authentication
  and model permissions. Public pages expose published opportunities only.
- Preserve CSRF middleware, template autoescaping, and ORM access.
- Accept HTTP/HTTPS source links only. Do not hardcode secrets or enable debug
  outside local development.
- Keep .env, databases, virtual environments, browser profiles, credentials,
  logs, backups, imported snapshots, and other runtime data out of Git.
- Importers must use explicitly approved sources, validate with full_clean()
  before saving, create drafts or review items, and avoid arbitrary URL fetching.
- Keep source examples and tests synthetic. Do not add real source identities,
  private text, personal details, or live credentials to public files.
- Review imported descriptions and links for privacy before publishing records.
- Do not connect external accounts or enable scheduled imports without authorization.
