# Telegram Members Export

Local terminal tool for creating named Telegram sessions, switching the active session, and exporting group or supergroup members to CSV with local avatar downloads.

## Start

Run the app from the repository root:

```bash
python3 main.py
```

On first launch, the bootstrapper:

1. verifies Python `>= 3.10`
2. creates a local `.venv`
3. installs runtime dependencies from `requirements.txt`
4. restarts the app inside `.venv`

No manual `.env` editing is required for normal use.

If you start it in a non-interactive shell, it exits with a clear message instead of waiting for prompts.

## Interactive Menu

The terminal UI uses arrow-key prompts and shows:

- the current active session, if one exists
- a short last-export status, when available

Main actions:

- `Export members`
- `Create new session`
- `Switch active session`
- `Exit`

## Create a Session

`Create new session` asks for:

- session name
- `API_ID`
- masked `API_HASH`
- Telegram login phone, code, and password prompts through `SessionPrompts`

After login completes, the session metadata is saved locally and you can mark the new session as active.

## Switch Active Session

`Switch active session` shows all saved sessions in an arrow-key picker with enough context to tell them apart:

- session name
- account label or masked phone
- active marker

## Export Members

`Export members`:

1. requires an active session
2. asks for a pasted group or supergroup title, including Unicode and emoji
3. resolves matching dialogs with exact, normalized, then substring ranking
4. shows a disambiguation picker if multiple dialogs share the same title
5. exports members except the current account
6. downloads profile avatars locally
7. writes a CSV with paired value/status columns for optional fields

Output files are created under:

- `.runtime/exports/<run_id>/members.csv`
- `.runtime/exports/<run_id>/avatars/`

## Runtime Storage And Secrets

Local runtime state lives under `.runtime/` and is intentionally private:

- `.runtime/sessions/<name>.session`
- `.runtime/session_meta/<name>.json`
- `.runtime/active_session.json`
- `.runtime/exports/<run_id>/...`

Treat both `.session` files and stored `API_HASH` values as secrets. Keep the repository directory private and do not commit `.runtime/` or `.venv/`.

The code uses restrictive permissions where the platform supports them:

- directories: `700`
- files: `600`

## CSV Format

The export CSV uses:

- `;` as the separator
- `utf-8-sig` encoding

Optional and enriched fields expose both the value column and a status column with one of:

- `value`
- `empty`
- `unavailable`
- `error`

## Telegram Limitations

The tool cannot bypass Telegram privacy or API constraints. Expected external limitations include:

- hidden profile fields
- inaccessible linked channels
- incomplete participant visibility
- `FloodWait` and other throttling
- forum topics inside a forum supergroup are out of scope

The exporter retries retryable Telegram failures with bounded waits, but if Telegram keeps rejecting the request, the affected field or operation becomes `error`.

## Smoke Checks

Useful local checks:

```bash
python3 -m compileall main.py app tests
.venv/bin/python -m pytest
python3 main.py
```

The last command should be run from an interactive terminal. In a non-interactive environment it exits predictably.
