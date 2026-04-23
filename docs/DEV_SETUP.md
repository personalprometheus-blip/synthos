# Local Dev Setup — Synthos Retail Portal

This is for offline design iteration on the retail portal. Not for production —
pi5 runs gunicorn against the same code; this is Flask's built-in dev server
running on your Mac so you can touch templates without hitting production.

Created 2026-04-23 as part of the portal refactor groundwork.

---

## One-time setup

Already done as of 2026-04-23, but documenting for future-me / rebuilds.

### Prerequisites
- macOS with Homebrew
- `uv` package manager (we use `uv` not pip/Homebrew Python directly, because
  Homebrew's Python 3.13 has a broken `pyexpat` symbol on current macOS)

### Install uv
```bash
brew install uv
```

### Create venv + install deps
```bash
cd /Users/patrickmcguire/synthos
uv venv --python 3.13 .venv-portal-dev
uv pip install --python .venv-portal-dev/bin/python flask python-dotenv cryptography
```

`uv` downloads its own clean CPython 3.13 distribution, avoiding the Homebrew
`pyexpat` issue that bites the default `python3.13` on this machine.

### Gitignored
`.venv-*` is in `.gitignore` — the venv lives locally only.

---

## Daily usage

```bash
cd /Users/patrickmcguire/synthos
./scripts/dev_portal.sh
```

Then browse `http://localhost:5555/`.

### Alternate port
```bash
PORTAL_PORT=5556 ./scripts/dev_portal.sh
```

### Ctrl-C to stop.

If the server crashes and leaves the port in use:
```bash
lsof -iTCP:5555 -sTCP:LISTEN   # find the pid
kill <pid>
```

---

## What works without a pi5 DB snapshot

Pages that don't require a real customer session:
- `/login`
- `/check-email`
- `/verify-email/<token>` (will show error template with invalid token)
- `/construction` (when construction mode enabled)
- `/landing` (public landing page)

Pages that require auth **redirect to login** — you can hit them but won't
see their content without a logged-in session.

---

## To get real data (optional, for pages that need it)

Copy a fresh owner signals.db from pi5:

```bash
ssh pi4b "ssh SentinelRetail 'cat /home/pi516gb/synthos/synthos_build/data/customers/30eff008-c27a-4c71-a788-05f883e4e3a0/signals.db'" > /tmp/owner_signals.db
mkdir -p /Users/patrickmcguire/synthos/synthos_build/data/customers/30eff008-c27a-4c71-a788-05f883e4e3a0
mv /tmp/owner_signals.db /Users/patrickmcguire/synthos/synthos_build/data/customers/30eff008-c27a-4c71-a788-05f883e4e3a0/signals.db
```

(Similarly for other customer DBs if you need them.)

Also copy auth.db for login to work:
```bash
ssh pi4b "ssh SentinelRetail 'cat /home/pi516gb/synthos/synthos_build/data/auth.db'" > /tmp/auth.db
mv /tmp/auth.db /Users/patrickmcguire/synthos/synthos_build/data/auth.db
```

---

## Creating a test login locally

When you don't have pi5 access (travel), you can make a local test account:

1. Start the portal locally: `./scripts/dev_portal.sh`
2. Set `OWNER_EMAIL` / `OWNER_PASSWORD` env vars BEFORE starting so the portal
   seeds a dev owner account on first boot
3. OR use `auth.py` CLI to create a customer:
   ```bash
   cd synthos_build/src
   /Users/patrickmcguire/synthos/.venv-portal-dev/bin/python -c "
   import auth
   auth.ensure_admin_account(email='you@example.com', password='devpass123')
   "
   ```

---

## Workflow for design iteration

1. Start local portal: `./scripts/dev_portal.sh`
2. Edit templates in `synthos_build/src/templates/*.html` in your editor
3. Save → refresh browser → changes visible immediately (Flask reloads templates
   on each request in dev mode)
4. For Python code changes (route handlers, logic), stop + restart the server
5. When happy, commit + push to the branch
6. Deploy: merge branch to main, pull on pi5, restart `synthos-portal.service`

---

## Pattern for extracting a new inline HTML string

See `patch/2026-04-22-portal-template-extraction` for examples:

1. Identify the `_X_HTML = """..."""` constant in `retail_portal.py`
2. Create `synthos_build/src/templates/<page>.html` with the HTML body
3. Replace `render_template_string(_X_HTML, ...)` with `render_template('<page>.html', ...)`
4. Delete the inline constant (leave a comment pointing to the template)
5. Verify: `py_compile retail_portal.py`
6. Render-test: restart dev server, hit the page URL
7. Commit

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'X'`**
Install it: `uv pip install --python .venv-portal-dev/bin/python <package>`

**`Address already in use`**
Previous dev server didn't shut down: `kill $(lsof -tiTCP:5555)` or use another port.

**pyexpat / dlopen errors**
You're using Homebrew's Python 3.13 instead of uv's. Make sure you launch via
the venv: `./.venv-portal-dev/bin/python` not `/opt/homebrew/bin/python3.13`.

**Template not found**
Flask looks for templates in `synthos_build/src/templates/` (relative to the
retail_portal.py module location). Make sure the file is there.
