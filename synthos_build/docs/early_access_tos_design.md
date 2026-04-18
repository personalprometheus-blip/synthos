# Early-Access TOS Modal + Non-Restrictive Setup Overlay — Design

Partner to `docs/tos_early_access.md` (the TOS copy itself).
**Status: built and dormant behind `EARLY_ACCESS_TOS_ENABLED = False`.**
No behaviour change until the flag is flipped.

## Requirements

From the product conversation:

1. TOS pops up as a **modal on top of account creation**, blocking
   until the user clicks "I Agree".
2. Once accepted, **no other TOS prompt** appears in the app. This
   TOS supersedes the legacy `/terms` redirect flow.
3. After the user accepts, they land on the existing **setup page**
   (the Setup Guide tab). That page must be **non-restrictive** —
   the account is already functional; the page is just a reminder.
4. The setup page shows **each login** until the user ticks **"Don't
   show again"** + **"OK"**. An "OK" alone closes it for the session
   only.
5. **Test fixtures** (the non-human paper accounts I seed via API —
   `test_01`, `test_02`) **bypass both** the TOS modal and the setup
   overlay. **Real humans** — beta testers and early adopters —
   always get the full flow; they are the audience the TOS exists
   for.

## Dormant-by-default

Every new line of code is guarded by the feature flag
`EARLY_ACCESS_TOS_ENABLED` (module-level, `False` by default in
`retail_portal.py`). When `False`:

- The three new API routes (`/api/ea/status`, `/api/ea/accept-tos`,
  `/api/ea/hide-setup`) either return an inert shape or 404.
- `window.EARLY_ACCESS_TOS_ENABLED` is rendered as `false` to the
  client, so the `eaBoot()` bootstrap short-circuits.
- The TOS modal and setup overlay DOM elements remain in the page
  but hidden with `display:none` and never activated.
- The legacy `SETUP_COMPLETE !== '1'` auto-redirect to the Setup
  Guide tab still runs.

When `True`:

- `eaBoot()` calls `/api/ea/status`. If the user is a fixture, it
  exits. Otherwise it shows the TOS modal until accepted, then the
  setup overlay if not hidden.
- The Setup Guide tab no longer auto-opens. The overlay is a small
  bottom-right card that links to the full guide tab; it does not
  steal focus or block any UI.

## State model

All per-customer state lives in the existing `customer_settings`
table (no schema change). Keys:

| Key                        | Values          | Meaning                                                            |
|----------------------------|-----------------|--------------------------------------------------------------------|
| `ACCOUNT_TYPE`             | `user`/`fixture` | `fixture` bypasses TOS + setup overlay. Default (missing) = `user`. |
| `EA_TOS_ACCEPTED_VERSION`  | `"1.0"`          | When equal to `EARLY_ACCESS_TOS_VERSION`, modal doesn't show.       |
| `EA_TOS_ACCEPTED_AT`       | ISO 8601 UTC     | Audit timestamp — written once at accept.                           |
| `EA_SETUP_GUIDE_HIDDEN`    | `"0"`/`"1"`      | When `"1"`, overlay is suppressed permanently.                      |

The constants (`EA_TOS_ACCEPTED_KEY`, etc.) are exported at the top
of the feature block in `retail_portal.py` so test scripts and tools
can reference them without hard-coding strings.

## Supersession mechanism

The legacy TOS system lives at `/terms` and gates the portal via the
`login_required` decorator, which redirects to `/terms` whenever
`session['tos_version'] != TOS_CURRENT_VERSION`. To satisfy
requirement #2, `/api/ea/accept-tos` mirrors acceptance into the
session key that the legacy gate reads:

```python
session['tos_version'] = TOS_CURRENT_VERSION
```

Once the user accepts the early-access modal, the legacy gate
considers them compliant. They never see `/terms`. If we later need
to re-prompt — e.g. a material TOS change — we bump
`EARLY_ACCESS_TOS_VERSION`, and `eaBoot()` will show the modal again.

## Fixture identification

Fixtures are identified by an explicit settings key, not by UUID
pattern or name matching:

```
customer_settings.ACCOUNT_TYPE = 'fixture'
```

**This key is written only by the bootstrap script** (`tools/...`,
TBD) that I run to create a paper-only test account. It is not
reachable from any portal UI, and `_ea_is_fixture()` treats a missing
key as `user` — so a real customer can't accidentally be flagged as
a fixture.

**Action item for when the flag is flipped on:** update whichever
script seeds `test_01` / `test_02` to write `ACCOUNT_TYPE='fixture'`.
For now, if I flip the flag without that, the fixtures would briefly
see the modal too — a 30-second problem.

## Routes added

```
GET  /api/ea/status       → { enabled, fixture, tos_needs_accept,
                              setup_hidden, tos_version }
POST /api/ea/accept-tos   → { ok, version }  (writes acceptance keys
                              and mirrors into session)
POST /api/ea/hide-setup   → { ok }           (writes EA_SETUP_GUIDE_HIDDEN=1)
```

All three use `@authenticated_only` (not `@login_required`), because
the modal is the *supersession* mechanism for the legacy TOS — it
must be reachable before `session['tos_version']` matches.

## Client-side touchpoints

Inside `PORTAL_HTML`:

- **Early-access section header** — clearly labelled HTML block for
  the TOS modal (`#ea-tos-overlay`) and the setup overlay
  (`#ea-setup-overlay`), placed as siblings to the tab pages so they
  live outside any particular tab's layout.
- **Server-injected flag** — `window.EARLY_ACCESS_TOS_ENABLED`
  rendered from `{{ ea_enabled }}` near the top of the existing
  `<script>` block.
- **Boot replacement** — the existing three-line JS that runs
  `showTab('guide')` when `SETUP_COMPLETE !== '1'` is wrapped in a
  flag check. When the flag is on, `eaBoot()` runs instead.

## Fail-closed behaviours

- `eaBoot()` — if `/api/ea/status` errors, we show nothing. Silent.
  That's safer than popping a partial UI and confusing the user.
- `eaAcceptTos()` — if the save fails, the button re-enables and
  we toast an error. The modal stays up; nothing is persisted.
- `eaDismissSetupOverlay()` — if `/api/ea/hide-setup` fails, the
  overlay still closes for the session; the user just re-sees it
  next login. Non-fatal.

## Things deliberately NOT built

- **`/terms` page removal.** The legacy page stays in place. It's
  unreachable after acceptance but remains a safety net in case we
  ever need to disable this feature.
- **Admin tooling to flip the flag per-tenant.** One flag for the
  whole fleet is enough for stress-test roll-out. If we need per-
  tenant later, it becomes a setting key, not a constant.
- **Telemetry on how many users scroll before accepting.** Easy to
  add once the flag is on if we want to see it.

## When flipping the flag

Checklist:

1. Fill the TOS placeholders in `docs/tos_early_access.md`:
   `[EFFECTIVE_DATE]`, `[CONTACT_EMAIL]`, `[GOVERNING_STATE]`,
   `[VENUE_COUNTY, GOVERNING_STATE]`. These are rendered verbatim
   into the modal body at page load — no code change needed.
2. Tag the fixture customers:
   ```python
   from src.retail_database import get_customer_db
   for cid in ('80419c9e-b8c9-4885-8c65-42a77a0a6879',
               'e327ce1b-21d0-4bcf-a5ca-a69db987cddf'):
       get_customer_db(cid).set_setting('ACCOUNT_TYPE', 'fixture')
   ```
3. Set `EARLY_ACCESS_TOS_ENABLED = True` in `retail_portal.py`.
4. Restart the portal service on pi5.
5. Log in as a real-user account and verify: modal → accept → the
   setup overlay appears → OK dismisses for session → re-login
   re-shows overlay → tick "Don't show again" + OK → re-login no
   overlay.
6. Log in as `test_01` and verify **neither** shows.
