# Public source distribution

This folder is a standalone source snapshot for a new repository. It contains
application source, schema migrations, templates, styles, synthetic tests,
requirements, and generic documentation.

The distribution excludes Git history and remote configuration, local environment
values, databases, user accounts, imported records, browser profiles and sessions,
logs, checkpoints, backups, virtual environments, and editor state. Source
identities and example content have been replaced with synthetic values; no
operational source account is configured by the included example configuration.

Before local setup or Git initialization, run the source audit:

```powershell
python -B scripts/check_public_export.py
```

It checks a clean source distribution and rejects runtime files, `.env`, virtual
environments, databases, caches, and `.git`. Run it on a clean source copy before
publication; a working development folder will acquire private files and no
longer satisfy that distribution check.

Use only the reviewed source distribution when preparing a public repository. Start with new history;
copying an existing repository's Git directory can reintroduce deleted secrets,
private content, author metadata, and remote addresses.

After local use, this folder may gain private runtime data. Review all files and
staged changes before each publication. Never publish local databases, exported
records, logs, browser profiles, credentials, or private source snapshots. Review
opportunity text and source links separately before making records public in the
application. Ignore rules are an accidental-addition guard, not a privacy guarantee.

Nothing has been committed, pushed, deployed, or connected to an external account
as part of preparing this source distribution.
