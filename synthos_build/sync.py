"""
sync_v1.0.py — Synthos Sync Script
Synthos · v1.0

Two modes:

OWNER MODE (Patrick running Synthos):
  python3 sync.py --watch
  Starts a local server at http://localhost:8765
  At end of each Claude session, Patrick posts a link in chat.
  Owner clicks link → all updated files download automatically.
  Then run: python3 sync.py

DEVELOPER MODE (Patrick building Synthos):
  python3 sync.py --register file1.py file2.py --version-tag 1.1 --notes "bug fixes"
  Registers which files changed so the watch server knows what to serve.

SYNC (after files are in downloads/):
  python3 sync.py              # process downloads/ and push to GitHub
  python3 sync.py --dry-run    # preview only
  python3 sync.py --no-push    # update locally, skip GitHub

BROWSER SETUP (point downloads here):
  python3 sync.py --setup
"""

import os
import sys
import ast
import json
import shutil
import hashlib
import argparse
import subprocess
import logging
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

SYNTHOS_VERSION = "1.0"

PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(PROJECT_DIR, 'downloads')
LOG_DIR       = os.path.join(PROJECT_DIR, 'logs')
WATCH_PORT    = 8765
MANIFEST_PATH = os.path.join(PROJECT_DIR, '.sync_session.json')
OUTPUTS_DIR   = os.path.expanduser('~/Downloads')  # fallback search location

SYNCABLE_FILES = {
    'database.py', 'agent1_trader.py', 'agent2_research.py',
    'agent3_sentiment.py', 'cleanup.py', 'heartbeat.py',
    'health_check.py', 'shutdown.py', 'patch.py', 'watchdog.py',
    'install.py', 'sync.py', 'boot_sequence.py', 'portal.py',
    'synthos_monitor.py', 'digest_agent.py', 'generate_unlock_key.py', 'daily_digest.py', 'uninstall.py', 'qpush.sh', 'qpull.sh', 'migrate_to_synthos.sh', 'first_run.sh',
    'README.md', 'VERSION_MANIFEST.txt', '.gitignore',
    'deadman_switch.md', 'api_security.md', 'pi_maintenance.md',
    'beta_agreement.md', 'legal_documents.md',
}

PROTECTED = {
    '.env', 'credentials.json', 'signals.db',
    '.kill_switch', '.pending_approvals.json', '.install_progress.json',
}

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s sync: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('sync')


# ── HELPERS ───────────────────────────────────────────────────────────────

def file_hash(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def strip_version_suffix(filename):
    import re
    base, ext = os.path.splitext(filename)
    stripped  = re.sub(r'_v\d+\.\d+(\.\d+)?$', '', base)
    return stripped + ext


def validate_file(path):
    _, ext = os.path.splitext(path)
    if ext == '.py':
        try:
            with open(path) as f:
                ast.parse(f.read())
            return True, None
        except SyntaxError as e:
            return False, f"Syntax error line {e.lineno}: {e.msg}"
    return True, None


def get_version_from_file(path):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith('SYNTHOS_VERSION'):
                    val = line.split('=')[1].split('#')[0].strip()
                    return val.strip('"').strip("'")
    except Exception:
        pass
    return None


def get_current_version():
    for fname in ['database.py', 'patch.py']:
        v = get_version_from_file(os.path.join(PROJECT_DIR, fname))
        if v:
            return v
    return SYNTHOS_VERSION


def push_to_github(files_changed, version, commit_message=None):
    if not commit_message:
        commit_message = f"Synthos v{version} — sync {datetime.now().strftime('%Y-%m-%d')}"
    try:
        subprocess.run(['git', 'add'] + files_changed,
                       cwd=PROJECT_DIR, check=True, capture_output=True)
        status = subprocess.run(['git', 'status', '--porcelain'],
                                cwd=PROJECT_DIR, capture_output=True, text=True)
        if not status.stdout.strip():
            log.info("Nothing to push — already up to date")
            return True
        subprocess.run(['git', 'commit', '-m', commit_message],
                       cwd=PROJECT_DIR, check=True, capture_output=True)
        result = subprocess.run(['git', 'push', 'origin', 'main'],
                                cwd=PROJECT_DIR, capture_output=True, text=True)
        if result.returncode == 0:
            log.info(f"Pushed to GitHub: {commit_message}")
            return True
        log.error(f"Push failed: {result.stderr[:200]}")
        return False
    except Exception as e:
        log.error(f"Push error: {e}")
        return False


# ── WATCH SERVER ──────────────────────────────────────────────────────────

def build_session_page(session):
    files   = session.get('files', [])
    version = session.get('version', '?')
    notes   = session.get('notes', '')
    ts      = session.get('timestamp', '')
    rows    = ''.join(
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #eee">{f}</td>'
        f'<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right">'
        f'<a href="/download/{f}" style="color:#111;font-weight:600">↓</a></td></tr>'
        for f in files
    )
    dl_calls = '\n'.join(
        f'setTimeout(()=>{{var a=document.createElement("a");a.href="/download/{f}";'
        f'a.download="{f}";document.body.appendChild(a);a.click();document.body.removeChild(a);}},{i*500});'
        for i, f in enumerate(files)
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Synthos Sync v{version}</title>
<style>
body{{font-family:'Inter',system-ui,sans-serif;background:#f5f5f5;margin:0;padding:32px}}
.card{{background:#fff;border:1px solid #ddd;padding:24px;max-width:520px;margin:0 auto}}
h2{{margin:0 0 4px;font-size:18px}}
.sub{{color:#888;font-size:11px;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse;margin-bottom:16px}}
th{{text-align:left;font-size:10px;color:#888;padding:6px 12px;border-bottom:2px solid #111;text-transform:uppercase;letter-spacing:.08em}}
.btn{{display:block;background:#111;color:#fff;padding:12px;text-align:center;font-weight:600;font-size:13px;cursor:pointer;border:none;width:100%;letter-spacing:.06em;margin-bottom:8px}}
.btn:hover{{background:#333}}
.note{{font-size:10px;color:#888;line-height:1.7;margin-top:12px}}
code{{background:#f0f0f0;padding:2px 6px;font-size:11px}}
</style></head>
<body><div class="card">
<h2>Synthos — Session Update</h2>
<div class="sub">v{version} · {ts}{' · ' + notes if notes else ''}</div>
<table><tr><th>File</th><th></th></tr>{rows}</table>
<button class="btn" onclick="downloadAll()">⬇ Download All {len(files)} Files</button>
<div class="note">
Files save to your browser downloads folder.<br>
Make sure <code>sync.py --watch</code> is still running, then:<br>
Run: <code>python3 sync.py</code>
</div></div>
<script>
function downloadAll(){{
{dl_calls}
document.querySelector('.btn').textContent='Downloading...';
setTimeout(()=>document.querySelector('.btn').textContent='✓ Done — run: python3 sync.py',{len(files)*500+500});
}}
</script>
</body></html>"""


def find_file(filename):
    """Search for a file in known locations."""
    search_dirs = [
        DOWNLOADS_DIR,
        PROJECT_DIR,
        os.path.expanduser('~/Downloads'),
    ]
    for d in search_dirs:
        p = os.path.join(d, filename)
        if os.path.exists(p):
            return p
    return None


class SyncHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def send_html(self, html, code=200):
        body = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/ping':
            self.send_html('ok')
            return

        if path in ('/', '/session'):
            if not os.path.exists(MANIFEST_PATH):
                self.send_html('<html><body style="font-family:sans-serif;padding:32px">'
                               '<h2>No session registered yet</h2>'
                               '<p>Patrick needs to run: <code>python3 sync.py --register ...</code></p>'
                               '</body></html>')
                return
            with open(MANIFEST_PATH) as f:
                session = json.load(f)
            self.send_html(build_session_page(session))
            return

        if path.startswith('/download/'):
            filename = path[len('/download/'):]
            fpath    = find_file(filename)
            if not fpath:
                self.send_html(f'<h3>File not found: {filename}</h3>', 404)
                return
            with open(fpath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', len(data))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_html('<h3>Not found</h3>', 404)


def start_watch_server():
    """Start local sync server. Owner keeps this running during sessions."""
    try:
        server = HTTPServer(('127.0.0.1', WATCH_PORT), SyncHandler)
    except OSError:
        log.error(f"Port {WATCH_PORT} already in use — is sync.py already running?")
        sys.exit(1)

    print()
    print("=" * 55)
    print("  SYNTHOS SYNC SERVER RUNNING")
    print("=" * 55)
    print(f"  Session page: http://localhost:{WATCH_PORT}/session")
    print()
    print("  Keep this running during your Claude session.")
    print("  When Patrick posts the session link, open it")
    print("  in your browser to download all updated files.")
    print("  Then run: python3 sync.py")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 55)
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Sync server stopped.")


def register_session(files, version, notes=""):
    """Developer (Patrick) registers which files changed this session."""
    # Verify files exist
    found   = []
    missing = []
    for f in files:
        plain = strip_version_suffix(f)
        if find_file(f) or find_file(plain):
            found.append(plain)
        else:
            missing.append(f)

    if missing:
        log.warning(f"Files not found (will still register): {', '.join(missing)}")

    manifest = {
        'version':   version,
        'notes':     notes,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'files':     found or files,
    }
    with open(MANIFEST_PATH, 'w') as f:
        json.dump(manifest, f, indent=2)

    print()
    print("=" * 55)
    print(f"  SESSION REGISTERED — v{version}")
    print("=" * 55)
    print(f"  Files: {', '.join(manifest['files'])}")
    print(f"  Notes: {notes or 'none'}")
    print()
    print("  POST THIS LINK IN CHAT FOR OWNER TO CLICK:")
    print()
    print(f"  http://localhost:{WATCH_PORT}/session")
    print()
    print("  Owner must have sync.py --watch running first.")
    print("=" * 55)
    print()


# ── MAIN SYNC ─────────────────────────────────────────────────────────────

def run_sync(dry_run=False, no_push=False, commit_message=None):
    log.info(f"Synthos Sync v{SYNTHOS_VERSION} — scanning downloads/")
    if dry_run:
        log.info("[DRY RUN] No changes will be applied")

    try:
        download_files = [p for p in Path(DOWNLOADS_DIR).iterdir()
                          if not p.name.startswith('.')]
    except Exception as e:
        log.error(f"Could not read downloads/: {e}")
        return False

    if not download_files:
        log.info(f"downloads/ is empty — nothing to sync")
        log.info(f"  Location: {DOWNLOADS_DIR}")
        return True

    log.info(f"Found {len(download_files)} file(s)")

    processed     = []
    skipped       = []
    errors        = []
    files_changed = []

    # Two-pass processing — sync.py always goes last
    # Pass 1: all files except sync.py
    # Pass 2: sync.py itself (so it doesn't replace itself mid-run)
    self_update = None
    sync_updated = False

    def process_file(src_path):
        original = src_path.name
        plain    = strip_version_suffix(original)

        if plain not in SYNCABLE_FILES:
            log.warning(f"  Unknown: {original} — skipping")
            skipped.append(original)
            return

        if plain in PROTECTED:
            log.error(f"  BLOCKED: {plain} is protected")
            skipped.append(original)
            return

        dest  = os.path.join(PROJECT_DIR, plain)
        log.info(f"  {original} → {plain}")

        valid, error = validate_file(str(src_path))
        if not valid:
            log.error(f"  INVALID: {error}")
            errors.append(original)
            return

        if os.path.exists(dest) and file_hash(str(src_path)) == file_hash(dest):
            log.info(f"  No change — identical")
            skipped.append(original)
            if not dry_run:
                os.remove(str(src_path))
            return

        if dry_run:
            log.info(f"  [DRY RUN] Would replace {plain}")
            processed.append(plain)
            return

        shutil.copy2(str(src_path), dest)
        log.info(f"  ✓ Replaced {plain}")
        processed.append(plain)
        files_changed.append(plain)
        os.remove(str(src_path))

    for src_path in sorted(download_files):
        plain = strip_version_suffix(src_path.name)
        if plain == 'sync.py':
            self_update = src_path  # defer to pass 2
            continue
        process_file(src_path)

    # Pass 2 — replace sync.py last
    if self_update:
        log.info("  Processing sync.py last (self-update)")
        process_file(self_update)
        sync_updated = True

    print()
    log.info(f"Updated: {len(processed)} · Skipped: {len(skipped)} · Errors: {len(errors)}")
    if sync_updated and not dry_run:
        log.info("⚡ sync.py was updated — changes take effect on next run")

    if errors:
        log.error(f"Fix errors before pushing: {', '.join(errors)}")
        return False

    if not processed or dry_run:
        return True

    if no_push:
        log.info("Skipping push (--no-push). Run: python3 patch.py --push")
        return True

    version = get_current_version()
    success = push_to_github(files_changed, version, commit_message)
    if success:
        log.info("✓ GitHub updated — Pi can pull: python3 patch.py --check-remote")
    return success


# ── SETUP INFO ────────────────────────────────────────────────────────────

def show_setup():
    print(f"""
{'='*58}
SYNTHOS SYNC — SETUP
{'='*58}

STEP 1 — Start the watch server (keep running during sessions):
  python3 sync.py --watch

STEP 2 — Set browser download folder to:
  {DOWNLOADS_DIR}

  Chrome:  Settings → Downloads → Location → Change
  Safari:  Preferences → General → File download location
  Firefox: Settings → Downloads → Save files to

STEP 3 — When Patrick posts a session link in chat:
  Click it → browser opens → click Download All
  Then run: python3 sync.py

{'='*58}
""")


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Synthos Sync — one-command update from Claude to GitHub'
    )
    parser.add_argument('--watch',       action='store_true',
                        help='Start local sync server (Owner keeps running during sessions)')
    parser.add_argument('--register',    nargs='+', metavar='FILE',
                        help='Register session files (Developer/Patrick)')
    parser.add_argument('--version-tag', metavar='V',
                        help='Version for --register (e.g. 1.1)')
    parser.add_argument('--notes',       metavar='NOTES',
                        help='Session notes for --register')
    parser.add_argument('--dry-run',     action='store_true',
                        help='Preview without applying')
    parser.add_argument('--no-push',     action='store_true',
                        help='Update locally, skip GitHub push')
    parser.add_argument('--message',     metavar='MSG',
                        help='Custom commit message')
    parser.add_argument('--setup',       action='store_true',
                        help='Show setup instructions')
    args = parser.parse_args()

    if args.setup:
        show_setup()
    elif args.watch:
        start_watch_server()
    elif args.register:
        register_session(
            args.register,
            version=args.version_tag or get_current_version(),
            notes=args.notes or '',
        )
    else:
        success = run_sync(
            dry_run=args.dry_run,
            no_push=args.no_push,
            commit_message=args.message,
        )
        sys.exit(0 if success else 1)
