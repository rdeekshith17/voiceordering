# PR 2 — AI ON/OFF, weekly schedule, and safe fallback routing

Restaurants choose when the AI answers calls and what callers get when it
doesn't. Off by default: until a restaurant ticks **Use AI phone controls** on
the new **AI phone** page, every call is answered by the AI exactly as before.

## Restaurant admin experience (Portal → Management → AI phone)

- **Status card:** what happens to a call right now, why, and the next change
  ("AI is off: calls are forwarded to staff · Outside scheduled hours. Next
  change: AI on at Oct 09 · 6:00 AM").
- **Pause 1 hour / until midnight / until I resume**, **Turn AI off now**, **Resume AI**.
- **When should the AI answer?** Always on · On a schedule · Always off.
- **Weekly schedule:** any number of windows per day, in the restaurant's time
  zone; an end at or before the start runs past midnight. "Copy Monday to every day".
- **When the AI is off:** forward to staff · take a voicemail · play a closed message.
  When forwarding: **if staff don't answer or the line is busy** → voicemail or
  the closed message (chosen per restaurant).
- **Holidays & exceptions:** AI off (or on) for whole local days.
- **Live calls** shows how each call was handled (AI, forwarded + answered or
  not, voicemail with a play link, closed message).
- Kitchen staff can't open the page or call its APIs.

## Call flow

```
/twilio/voice → restaurant from the dialed number → routing_decision()
   AI on  → existing AI flow (unchanged)
   AI off → off action:
       forward   → <Dial timeout=20 action=/twilio/dial-status>
                     answered → hang up
                     no-answer / busy / failed → voicemail or closed message
       voicemail → <Record> → /twilio/voicemail saves the recording link
       message   → <Say>closed message</Say><Hangup/>
```

Decision order (highest first): platform emergency stop → restaurant "Turn AI
off now" → newest active pause/exception → mode (always on / off / schedule).

- New calls see changes immediately (no cache, no deploy); calls already in
  progress finish normally.
- Missing or placeholder transfer number (`+15550134200`): forwarding skips
  straight to the no-answer fallback instead of dialing a dead line.
- If the policy can't be read: forward to staff when a real number is set,
  otherwise let the AI answer, so a caller is never dropped.
- The AI's own "talk to a person" transfer also gets the ring timeout and
  fallback, but only for restaurants with the controls on (otherwise unchanged).

## Changes

| Area | Change |
|---|---|
| Logic | `voiceorder/routing.py`: pure evaluator (modes, windows, overnight, DST, overrides, emergency, next change) |
| Database | Migration `006_voice_routing` (`voice_routing`, `voice_routing_windows`, `voice_routing_overrides`); `007_call_meta` (`call_transcripts.meta` JSON) |
| Store | `routing_config`, `save_routing` (versioned, all-or-nothing), pauses/exceptions, emergency stops, `set_call_meta` |
| DB layer | `Database.transaction()` for multi-statement saves |
| Twilio | `/twilio/dial-status`, `/twilio/voicemail` (signature-checked); `transfer_call` gains timeout + action; `voicemail`, `closed_message` TwiML |
| Portal | `/portal/phone`; APIs `GET/PUT /portal/api/voice-routing`, `POST …/pause`, `…/resume`, `…/stop`, `POST/DELETE …/exceptions` (admin only) |
| Fix | Calls to unmapped numbers now read the default restaurant fresh, not the startup copy |
| Audit | Mode/schedule saves, flag changes, pauses, exceptions, cancellations, emergency stops |

Platform-wide emergency stop (until the Super Admin UI): `store.set_routing_emergency("*", True, actor="you")`.

## Rollback

Additive migrations; the previous release ignores the new tables and column.
Fastest off switch without a deploy: untick **Use AI phone controls**, or set
`DISABLED_FEATURES=voice_schedule_enabled` (every restaurant goes back to
"AI answers every call").

## Verification

- `pytest -q`: 318 passed, 1 failed (known env-dependent filler test), 6 skipped.
- Postgres (`TEST_DATABASE_URL`): 6 passed, incl. a save interrupted after
  deleting the old windows → old schedule intact (also checked on SQLite).
- `test_routing.py`: every mode, two windows/day, weekdays only, overnight and
  week-wrapping windows, both 2026 Chicago DST changes, overrides, emergencies.
- `test_voice_routing.py`: the real webhooks for every route and fallback,
  placeholder number, policy failure, mid-call change, AI transfer, portal
  APIs, stale-version save, tenant isolation, kitchen 403.
- Production Docker image on a copy of production data: migrations 001–007
  applied; in Chrome, controls off → AI; schedule 6–7 AM → forwarded to staff;
  always on → AI (returning-caller greeting intact); pause 1 h → forwarded;
  resume → AI; holiday added as a whole local day.
- **Not yet done:** real calls on the live number (plan gate) — needs a
  person with a phone after deploy.
