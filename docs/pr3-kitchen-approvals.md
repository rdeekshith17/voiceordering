# PR 3 — Kitchen approvals (human in the loop) + calls that survive restarts

When a caller asks for something the AI can't promise on its own, the AI puts
them on hold, the kitchen decides on the **Kitchen** page, and the AI relays the
real answer. Off by default per restaurant (`hitl_enabled`); until an admin turns
it on, calls behave exactly as before.

## What triggers a kitchen check

| Request | Rule |
|---|---|
| Custom change that isn't a listed modifier ("extra crispy") | AI asks the kitchen |
| Allergy / dietary safety ("allergic", "celiac", "no peanuts", "gluten-free") | **Always** reviewed — enforced in code: the order can't be submitted until the kitchen has approved an allergy request |
| Availability doubt | AI asks the kitchen |
| Order total above the admin's threshold | Must be approved before submit (0 = off) |
| Listed modifier ("no onions" when that option exists) | Normal ordering, no check |

## Call flow

```
caller asks → AI calls request_kitchen_approval → "Let me check with the kitchen, please hold"
  → /twilio/hold polls every ~3 s ("Still checking…" every ~12 s)
     kitchen decides  → AI hears a [KITCHEN UPDATE] → relays it, confirms with the caller
     deadline passes → timed out (never an approval) → AI offers transfer / drop / continue
     caller hangs up  → request cancelled, nothing submitted
```

- `submit_order` is refused while a request is pending (code guard, not prompt).
- An approval whose item is removed from the cart is cancelled.
- Staff notes reach the AI quoted as information, never as instructions.
- Allergy approvals are relayed with "can't guarantee against cross-contact";
  the AI never says food is allergen-free.

## Kitchen page (`/portal/kitchen`)

- Kitchen logins land here; admins see it too, plus **Approval settings**.
- Live queue (refreshes every 2 s): category, item, request, time left, whether
  the caller is still on the line. Approve / Reject / Need more info + note.
- Allergy requests are highlighted and require a note.
- Two people deciding at once: one decision wins, the other sees "already decided".
- Optional sound alert (remembered per device). No customer phone numbers or names.
- Admin settings: on/off, hold time (20–180 s, default 60), large-order threshold,
  what the AI offers on timeout.

## Calls survive restarts

The call's cart and AI conversation are saved after every turn (`call_sessions`).
If Render restarts or redeploys mid-call — including while the caller is on hold —
the next Twilio webhook rebuilds the session from the database and the call
continues. The saved session is deleted when the call ends.

## Changes

| Area | Change |
|---|---|
| Rules | `voiceorder/approvals.py`: categories, allergy detection, submit guard, kitchen-update text, policy |
| Database | `008_kitchen_approvals` (`approval_requests`, append-only `approval_events`), `009_call_sessions` |
| Store | create (deduplicated) / decide (atomic, version + deadline checked) / expire / cancel / relay; session save/load/delete |
| Agent | `AgentSession.add_tool`, `guards`, `state`; core tools unchanged |
| Twilio | `/twilio/hold`; gather/turn/hold restore sessions; status callback cancels requests and drops the session |
| Portal | `/portal/kitchen`; `GET /portal/api/approvals`, `POST …/{id}/decision`, `PUT …/settings` (admin) |
| Roles | Kitchen users now land on the Kitchen page (they also keep Orders) |

## Rollback

Additive migrations. Untick **Ask the kitchen during calls**, or set
`DISABLED_FEATURES=hitl_enabled`, to stop all kitchen checks immediately.

## Verification

- `pytest -q`: 332 passed, 1 failed (known env-dependent filler test), 7 skipped.
- Postgres: 7 passed, incl. six staff "tablets" deciding one request at the
  same instant on separate connections → exactly one decision (3 runs).
- `test_kitchen_approvals.py`: rules, store, portal (tenant isolation, no
  customer details, allergy note, admin-only settings, expiry), and full calls
  through the real webhooks: approve, submit blocked while pending, timeout,
  allergy guard, hang-up cancel, **restart while on hold resumes**, feature off.
- Production Docker image on a copy of production data with the **real AI**:
  admin turned approvals on; caller asked for an extra-crispy tortilla → "Let me
  check that with the kitchen. Please hold"; request appeared on the Kitchen page
  (58 s left); approved in Chrome → ~2 s later the AI said "The kitchen said yes,
  they can make it extra crispy. Should I go ahead and add that…?"
- Allergy card in Chrome: approve without a note refused with a visible message;
  with a note, approved.
- **Not yet done:** a real phone call on the live number.
