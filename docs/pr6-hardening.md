# PR 6 — Hardening and rollout

No new features for restaurants; this makes what exists safer and gives operators
the documents to run it. (The multilingual feature, PR 5, was skipped; its switch
is removed.)

## Security fixes

| Finding | Fix |
|---|---|
| Legacy `/owner` pages took the shared password in the URL (`?key=…`): it leaked into browser history, server logs and shared links | Pages now open for a signed-in **restaurant Admin**; scripts can still send `X-Owner-Secret` as a header; the URL form is refused |
| `/owner` orders table inserted the customer name (from caller speech) and menu names into the page unescaped | Every value escaped; buttons use data attributes instead of code built from names |
| Session cookies not marked HTTPS-only | `Secure` on HTTPS (Render's `X-Forwarded-Proto`), still works on local http |
| No browser security headers | Every response: `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, and HSTS on HTTPS |
| Restaurant portal logins had no lockout | 5 failures in 15 min per email → locked 15 min; the lockout is audited |
| Customer phone numbers in logs | A log filter masks any phone number to its last 4 digits (call IDs, order numbers stay readable) |
| The public text demo (`/chat`) could place **real POS orders** for anyone on the internet and spend AI credits | `CHAT_DEMO_ENABLED` (set `0` for Render in `render.yaml`); per-visitor rate limit when on |
| Anyone can self-sign up | `PORTAL_SIGNUP_ENABLED=0` turns it off now that `/admin` creates restaurants (left on by default; your call) |

## Reliability fixes

| Finding | Fix |
|---|---|
| **POS down at submit: caller was told "I've saved your order", but nothing saved it** — the order was lost and staff never knew | The order is kept on the **Orders** page as **pos failed** with a **critical alert** to call the customer back (phone + items); the caller is told it isn't confirmed and staff will call. Once per cart, never duplicated. Phone, web chat and tool API |
| Database error while a caller is on hold for the kitchen → Twilio "application error" | Keeps holding and retries; after the hold cap, apologizes and continues (never an approval) |
| Database error while restoring a call after a restart → crash | The caller hears "I lost track of your call" instead of an error |

## Load test (`scripts/loadtest_calls.py`)

Real app + Postgres on this Mac, real Twilio webhooks, fake AI at 1.5 s per reply:

| Simultaneous calls (3 turns each) | Failed | Greeting p95 | Turn polls p95 | Slowest webhook |
|---|---|---|---|---|
| 10 | 0 | 32 ms | 20 ms | 32 ms |
| 40 | 0 | 128 ms | 51 ms | 130 ms |
| 100 | 0 | 313 ms | 69 ms | 325 ms |

Twilio gives up at ~15 s, so the margin is large. Render's free plan has a fraction of
a CPU and Neon adds network round-trips, so expect several times these numbers in
production — re-run against a staging database before a big launch.

## Failure testing (`tests/unit/test_failure_injection.py`)

AI provider error, voice service outage (real fallback path), database write
failure mid-call, database outage while on hold, session restore during an
outage, replayed Twilio webhooks (no duplicate order), POS outage at submit.
Every case: the caller hears a sensible sentence, never a 500.

## Operator documents

- `docs/runbook.md` — where things are, env vars, deploy, rollback, emergencies,
  routine tasks (admins, staff, key rotation), smoke-test call.
- `docs/onboarding-checklist.md` — bringing a new restaurant live, step by step.

## Verification

- `pytest -q`: 356 passed, 1 failed (known env-dependent filler test), 7 skipped.
- Postgres: 7 passed.
