# TrialGuard — registration, device-bound free trials and $5/mo subscriptions

Python backend for a Chrome extension. Users sign up, get **5 free runs bound to
their device**, and must subscribe for **$5/month** after that.

FastAPI · SQLAlchemy · JWT · SQLite (dev) → Postgres (prod) · Stripe/Razorpay/Mock

> Hindi version: [`README.hi.md`](README.hi.md)
> Web app guide: [`WEBAPP.hi.md`](WEBAPP.hi.md)

It also ships a **web app** at `/` — paste a YouTube link, get a summary in the
video's own language, download PDF notes. Same accounts and same credits as the
extension. See `WEBAPP.hi.md`.

---

## Read this first: the MAC address

You asked for the trial limit to be checked against the device's MAC address.
**A Chrome extension cannot read a MAC address.** The browser sandbox exposes no
network-hardware API — `chrome.system.network` is ChromeOS-kiosk only, and
there is no workaround from extension JS. Any tutorial claiming otherwise is
wrong or is describing a native app.

So this backend implements the *intent* of that requirement with the strongest
identifier the platform actually allows, plus an optional path to a real MAC:

| Layer | What it is | Reset difficulty |
|---|---|---|
| `installation_id` | Random UUID in `chrome.storage.local` | Easy (clear storage) |
| **Device hash** | HMAC of installation_id + hardware traits — the precise, per-install key | Easy (clear storage) |
| **Machine hash** | HMAC of *hardware only* (platform, screen, cores, RAM, GPU) — no client-chosen field | Hard (needs different hardware) |
| **Real MAC** *(optional)* | Read by the native helper in `native-helper/` | Very hard |
| **Server caps** | Max 3 accounts per device, 9 per machine; both ledgers | Cannot be touched from the client |

The raw fingerprint is never stored. The server HMACs it with
`DEVICE_HASH_SECRET` and stores only the digest, so a database leak cannot be
replayed or reversed into device identities.

If you install the optional native helper, `device.js` picks up the real MAC
automatically and the backend keys off that instead — no other change needed.

---

## The rule that actually stops trial abuse

Counting 5 trials per *account* is trivially defeated: sign up with a second
email. So there are **three ledgers**, and access requires **all** to have room:

```
allowed  =  active_subscription
         OR (user.trials_used     < 5     # this account
         AND device_ledger.used   < 5     # this install, any account
         AND machine_ledger.used  < 15)   # this hardware, any install
```

The **device ledger** is account-independent: one browser install gets 5 free
runs total no matter how many emails you throw at it.

The **machine ledger** exists because the device hash includes the
client-supplied `installation_id` — so clearing `chrome.storage.local` mints a
fresh device. The machine hash is derived from hardware traits only, which a
storage wipe cannot change, so it caps the whole computer at
`MACHINE_TRIAL_LIMIT` (default 15) runs. It is deliberately looser than 5
because it is lower entropy: two different people on identical hardware could
land on the same row. It is skipped entirely when fewer than three hardware
traits are reported, and when a real MAC is available (that key is better).

Signup is capped too: `MAX_ACCOUNTS_PER_DEVICE` (default 3) per device hash and
3× that per machine hash.

**Honest limit:** without the native helper, a determined user who changes
hardware traits (different machine, VM, spoofed values) gets a fresh allowance.
That is inherent to the browser sandbox, not a gap in this code — the MAC-based
path in `native-helper/` is the answer if that matters to your business.

Everything is decided server-side. The extension is code the user can read and
edit, so it may *ask* but never *decide*.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # works as-is for local dev
uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs> for interactive API docs.

Run the tests:

```bash
pytest            # 34 tests, no network or API keys needed
```

With Docker + Postgres:

```bash
cp .env.example .env
docker compose up --build     # runs migrations, then serves on :8000
```

---

## API

Base path `/api/v1`. All authenticated calls use `Authorization: Bearer <access_token>`.

### Auth

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/signup` | Create account + bind first device → tokens + entitlement |
| POST | `/auth/login` | Sign in → tokens + entitlement |
| POST | `/auth/refresh` | Rotate refresh token |
| POST | `/auth/logout` | Revoke this session (or all) |
| GET | `/auth/me` | Current user |
| GET/DELETE | `/auth/devices[/{id}]` | List / remove devices |
| POST | `/auth/verify-email`, `/auth/resend-verification` | Email verification |
| POST | `/auth/password/forgot`, `/password/reset`, `/password/change` | Passwords |

### Entitlement & usage — the two the extension calls constantly

| Method | Path | Purpose |
|---|---|---|
| POST | `/entitlement/check` | **Read-only.** "Can this user run right now?" Never spends a trial. |
| POST | `/usage/consume` | **Spends one trial.** Returns `402` when exhausted. |
| GET | `/usage/history` | Recent runs |

`POST /usage/consume`

```jsonc
// request
{
  "device": { "installation_id": "…", "platform": "Win32", "screen": "1920x1080x24", "gpu": "…" },
  "action": "run"
}
// header: Idempotency-Key: <uuid>   ← so a retried request is not charged twice

// 200
{
  "allowed": true, "consumed": true, "duplicate": false, "granted_by": "trial",
  "entitlement": { "trials_remaining": 3, "device_trials_remaining": 3, "plan": "free_trial", … }
}

// 402 — out of trials
{
  "detail": {
    "message": "You have used all 5 free trials. Subscribe for $5/month to continue.",
    "entitlement": { "reason": "trial_exhausted", "upgrade_url": "…", … }
  }
}
```

`reason` values: `trial_available`, `subscription_active`, `trial_exhausted`,
`device_trial_exhausted`, `machine_trial_exhausted`,
`email_verification_required`, `account_disabled`, `device_blocked`.

### Billing

| Method | Path | Purpose |
|---|---|---|
| GET | `/billing/plans` | The $5/month plan |
| POST | `/billing/checkout` | Hosted checkout URL |
| GET | `/billing/subscription` | Current subscription |
| POST | `/billing/portal` | Provider portal (change card / cancel) |
| POST | `/billing/cancel` | Cancel at period end |
| POST | `/webhooks/stripe`, `/webhooks/razorpay` | Signature-verified, idempotent |

### Summarization (web app)

| Method | Path | Charges | Purpose |
|---|---|---|---|
| POST | `/video/info` | no | Title + thumbnail, shown before anything is generated |
| POST | `/summarize` | 1 per video | Streaming summary or key points, NDJSON |
| POST | `/notes` | same key | Whole-video chunked notes, used to build the PDF |

One trial = one video: the idempotency key is `video:<youtubeId>`, so summary,
key points and PDF notes for a video share one charge and re-runs are free.

### Admin (header `X-Admin-Key`)

`/admin/stats`, `/admin/users`, `/admin/grant-trials`, `/admin/block-device`,
`/admin/reset-device-trials` — the last one is for legitimate cases like a
resold laptop.

---

## Wiring up the extension

Copy `extension-client/*.js` and `popup.html` into your extension, merge
`manifest.snippet.json` into your manifest, and set `API_BASE` in `api.js`.

Then wrap your existing feature:

```js
import { runGated } from './gate.js';

const outcome = await runGated('run_feature', () => doTheActualWork(payload));
if (!outcome.ok && outcome.reason === 'subscription_required') {
  // popup already shows the upgrade button; badge turns red
}
```

`gate.js` calls `/usage/consume` **before** the work and only proceeds if the
server said yes. It fails closed on network errors — that is deliberate, since
an attacker who can block your API should not thereby get unlimited runs.

| File | Role |
|---|---|
| `device.js` | Builds the fingerprint; picks up the native MAC if the helper is installed |
| `api.js` | HTTP client, token storage, single-flight refresh, `ApiError.needsSubscription` |
| `gate.js` | `runGated()` wrapper + toolbar badge showing trials left |
| `popup.html` / `popup.js` | Signup / login / trial meter / upgrade button |
| `background.example.js` | How to wire it into an MV3 service worker |

---

## Testing the money flow without a payment account

`PAYMENT_PROVIDER=mock` plus a `MOCK_BILLING_SECRET` gives you a local fake
checkout page:

```bash
curl -X POST localhost:8000/api/v1/billing/checkout -H "Authorization: Bearer $TOKEN" -d '{}'
# open the returned URL, paste the access token, click "Pay now"
```

The mock routes are disabled unless **all** of: `PAYMENT_PROVIDER=mock`,
`ENV != prod`, and `MOCK_BILLING_SECRET` is set. `/billing/mock/confirm`
requires a valid access token and only ever affects the caller — it cannot be
pointed at someone else's `user_id`. The mock webhook verifies an HMAC over the
raw body exactly like a real provider. **Leave `MOCK_BILLING_SECRET` empty in
any internet-reachable deployment.**

### Switching to a real provider

**Stripe** — create a $5/month recurring price (or leave `STRIPE_PRICE_ID`
empty and it is built inline), set `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET`, point a webhook at `/api/v1/webhooks/stripe` for
`checkout.session.completed`, `customer.subscription.*`, `invoice.paid`,
`invoice.payment_failed`.

**Razorpay** — likely the right choice if you bill from an Indian entity, since
Stripe does not support Indian businesses collecting from Indian customers.
Create a monthly plan, set `RAZORPAY_KEY_ID` / `KEY_SECRET` / `PLAN_ID` /
`WEBHOOK_SECRET`, point a webhook at `/api/v1/webhooks/razorpay`.

Both are already implemented — it is an env-var change, not a code change.

---

## Security notes

- Passwords: bcrypt, SHA-256 pre-hashed so >72-byte passwords keep full entropy.
- Access tokens 15 min; refresh tokens rotate, and **replaying a rotated token
  revokes the entire family** (stolen-token detection).
- Login and signup responses never reveal whether an email exists.
- Rate limits on login, signup, password reset and verification resend.
  `X-Forwarded-For` is ignored unless `TRUST_PROXY_HEADERS=true`, so clients
  cannot spoof their IP past the limiter; turn it on only behind your own proxy.
- Webhooks verify provider signatures and record event ids, so retries are safe.
- `/usage/consume` locks the user row and device ledger (`SELECT … FOR UPDATE`
  on Postgres), so two parallel requests cannot both spend the last trial.
- CORS: set `ALLOWED_ORIGINS=chrome-extension://<your-id>` in production.

### Before going live

- [ ] Set strong `SECRET_KEY` and `DEVICE_HASH_SECRET` (the app refuses to boot
      in `ENV=prod` with the dev defaults). Changing `DEVICE_HASH_SECRET` later
      resets every device ledger — set it once.
- [ ] `DATABASE_URL` → Postgres; run `alembic upgrade head`.
- [ ] `PAYMENT_PROVIDER` → `stripe` or `razorpay`; configure the webhook.
- [ ] `MOCK_BILLING_SECRET` → empty, so the fake checkout cannot be reached.
- [ ] `TRUST_PROXY_HEADERS=true` only if a proxy you control sets `X-Forwarded-For`.
- [ ] `ALLOWED_ORIGINS` → your extension id only.
- [ ] `EMAIL_BACKEND=smtp` so verification and reset mails actually send.
- [ ] Serve over HTTPS (any reverse proxy; the app already sends `--proxy-headers`).
- [ ] Set `ADMIN_API_KEY` to something long, or the admin routes stay disabled.

---

## Layout

```
app/
  config.py          settings (env-driven)
  database.py        engine/session; SQLite→Postgres by URL
  models.py          User, Device, DeviceTrialLedger, UsageEvent, Subscription, tokens
  schemas.py         Pydantic request/response models
  security.py        bcrypt, JWT, device HMAC
  deps.py            auth dependencies
  routers/           auth, usage, billing, webhooks, admin
  services/
    youtube.py       URL parsing, transcript (captions -> yt-dlp), metadata
    summarizer.py    language routing + Ollama streaming, prompts
    devices.py       fingerprint → hash, device registration, per-device caps
    entitlements.py  THE trial/subscription decision engine
    billing.py       applies webhook events to subscription state
    payments/        base + stripe + razorpay + mock
extension-client/    JS to drop into your extension
native-helper/       optional native messaging host that reads the real MAC
app/static/          the web app (single HTML + JS, served at "/")
tests/               69 tests
alembic/             migrations
```
