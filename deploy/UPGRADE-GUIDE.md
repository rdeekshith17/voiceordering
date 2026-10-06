# VoiceOrderAI — Account Upgrade Guide (do-it-yourself)

These three steps require the account owner (you) — Hulk can't click through
identity verification, billing, or carrier registration on your behalf. Do them
in this order. Anything marked **(verify in console)** means the exact label may
differ slightly from what's written here; the path is right, the wording may vary.

---

## (a) Twilio: trial → paid upgrade

**Why:** On trial, inbound callers hear a "trial account" disclaimer, and you can
only call/text verified numbers. Real customers can't use the service until this
is done. Your number **+1 562 268 0097 stays the same** — no webhook changes needed.

**Cost:** Pay-as-you-go; the upgrade flow asks you to add funds (historically a
$20 minimum top-up — **verify in console**). ~$0.01–0.02/min voice, ~$1.15/mo per
local number.

**Click path** (per Twilio's official upgrade guide):

1. Log in at https://console.twilio.com
2. Click the **Upgrade** link/banner at the top of the console.
3. Click **Continue**; enter your legal first/last name and phone number.
4. Select the country of your ID and **upload a government ID** (clear photo).
5. Select your identity type (individual vs business) and enter the address.
6. Tax information page: enter a tax number, or select "No, I cannot provide a
   tax number" for a private individual. **Continue**.
7. **Fund your account:** choose an amount, add a credit/debit card, and decide
   on **Auto Recharge** (recommended ON — otherwise the account can hit $0 and
   suspend mid-service). **Continue**.
8. Done — the trial disclaimer disappears on the next call.

**After upgrading**, come back and do (c) below before relying on SMS receipts.

---

## (b) Square: enable card processing for location `LP080BBB3A045`

**Why:** This account currently "has not been enabled to take payments", so
VoiceOrderAI creates pay-at-pickup orders. Completing onboarding unlocks the
Square-hosted payment links the app already generates.

**Click path:**

1. Log in at https://squareup.com/dashboard
2. Open the **Setup Guide** (Square's onboarding checklist — look for it on the
   dashboard home or under **Settings**). This is where Square puts the
   **identity verification** steps.
3. Complete **identity verification**: business details, owner identity, and a
   linked bank account for transfers. Square requires this by regulation before
   any card processing. It only needs to be done once per account.
4. **(Verify in console)** Under **Settings → Business → Online**, confirm
   **Online Payments** is enabled for the account.
5. Confirm the location: **Settings → Locations** → find `LP080BBB3A045` and
   check it shows as able to accept payments.
6. Optional cleanup: cancel the old test orders in **Orders** (steak burrito
   ~$11.37, chicken burritos from earlier testing).

**How you'll know it worked:** the next VoiceOrderAI order will include a
working `square.link` payment URL in the SMS receipt instead of falling back to
pay-at-pickup, and the order will be payable in the Square dashboard.

---

## (c) A2P 10DLC registration (US SMS receipts)

**Why:** US carriers block/filter business SMS from unregistered 10-digit
numbers. Without this, order-confirmation texts show as sent in Twilio but never
arrive (Twilio error **30034**). Do this **after** the Twilio upgrade (a).

**Cost:** Roughly $4 one-time brand fee + ~$15 campaign vetting + ~$1.50–2/mo per
campaign **(verify in console — fees change)**. Approval takes 1–7 business days.

**Click path** (Console → **Messaging → Regulatory Compliance → A2P 10DLC**;
some consoles label it **Onboarding** — **verify in console**):

1. **Create your profile** if prompted (Twilio "Starter Profile").
2. **Register a Brand** — who is sending:
   - If the restaurant is a **sole proprietorship** (no EIN): choose the
     **Sole Proprietor** brand type — cheapest and fastest, one phone number
     per campaign.
   - Otherwise: **Standard Brand** with legal business name, EIN, address,
     website, and business vertical.
   - Status goes PENDING → APPROVED (usually 24–48h).
3. **Create a Campaign** — what you're sending:
   - Use case: **Low Volume Mixed** (order confirmations + receipts fit here;
     alternatives: Customer Care, Notifications).
   - You'll be asked for **sample messages** (e.g. "Your order from Hyderabad
     House is confirmed. Pickup in ~20 min. Reply STOP to opt out.") and how
     customers **opt in / opt out / get help**. Write these plainly and
     truthfully.
4. **Link the campaign to a Messaging Service** and **attach +1 562 268 0097**
   to it (Sole Proprietor campaigns allow exactly one number).
5. Wait for **VERIFIED**. You'll get an email when brand + campaign are approved.

**One caution:** once your number belongs to a Messaging Service, Twilio uses
the *service's* webhook settings for SMS. VoiceOrderAI sends SMS via the REST
API (outbound only) and takes voice webhooks from the **phone number's** Voice
configuration, which is unaffected — but if you ever add inbound-SMS handling,
set that webhook on the Messaging Service, not the number.

**How you'll know it worked:** the Twilio console shows the number as
"registered", and SMS receipts to non-verified US phones start arriving.

---

## Order of operations (recommended)

1. Deploy to stable hosting first (`DEPLOY.md`) — webhooks are easiest to set
   once, on the final URL.
2. Twilio upgrade (a) — removes the trial disclaimer immediately.
3. Square onboarding (b) — unlocks card payments (can run in parallel with a).
4. A2P 10DLC (c) — start early; the 1–7 day carrier wait is the long pole.
5. Add `TWILIO_AUTH_TOKEN` to the server env and restart — webhook signature
   validation switches on automatically.
