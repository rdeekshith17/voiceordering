# Bringing a new restaurant live — checklist

Work through it top to bottom; each step says who does it and how to check it.

## 1. Account (Super Admin)
- [ ] `/admin/restaurants` → **Add a restaurant**: name, the Twilio number customers
      will call, time zone, the owner's email and a temporary password.
- [ ] Send the owner their login (`/portal/login`) and ask them to keep the password safe.

## 2. Phone number (Super Admin / Twilio console)
- [ ] Buy or assign the Twilio number. In Twilio → the number → Voice:
  - "A call comes in" → Webhook `https://voiceorderai.onrender.com/twilio/voice` (POST)
  - "Call status changes" → `https://voiceorderai.onrender.com/twilio/status` (POST)
- [ ] The number in Twilio matches the one on the restaurant's page in `/admin`.

## 3. Point of sale (restaurant Admin)
- [ ] Portal → **POS setup** → choose Square / Toast / Clover → paste the credentials.
- [ ] **Test connection** shows the restaurant's own location name.
- [ ] Check the menu: the AI only offers items from the synced menu.

## 4. Restaurant details (restaurant Admin)
- [ ] **System settings**: restaurant name, **time zone**, pickup time, tax rate.
- [ ] **Transfer number**: a real staff phone (not the placeholder). `/admin` shows
      "not set" until this is done.
- [ ] AI voice: preview and pick one.

## 5. Optional features (restaurant Admin or Super Admin)
- [ ] **AI phone** page: schedule / always on, what callers get when the AI is off,
      holidays. (Off = the AI answers every call.)
- [ ] **Kitchen approvals**: add a Kitchen login (`/admin` → Staff logins), put the
      **Kitchen** page on a tablet, then turn approvals on (Kitchen page → Approval settings).

## 6. Test before going live (both)
- [ ] Call the number: order one item end to end; the order shows in the portal
      **Orders** and in the POS; the caller gets a pickup time in the restaurant's time zone.
- [ ] Call again from the same phone: greeted by name, asked to confirm.
- [ ] Ask for a person: the call transfers to the staff number.
- [ ] If AI phone controls are on: **Turn AI off now** → call → reaches staff or voicemail → **Resume AI**.
- [ ] If kitchen approvals are on: ask for something off-menu → approve on the Kitchen page → the AI relays it.
- [ ] `/admin` → the restaurant shows no open alerts.

## 7. Live
- [ ] Owner confirms the menu, hours and transfer number one last time.
- [ ] Watch `/admin` → Live operations during the first day's busy periods.
