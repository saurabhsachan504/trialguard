# TrialGuard — Hindi guide

Chrome extension ke liye Python backend: **signup**, **5 free trial** (device se
bandhe hue), aur uske baad **$5/month subscription**.

FastAPI · SQLAlchemy · JWT · SQLite (dev) → Postgres (prod) · Stripe/Razorpay/Mock

---

## Sabse pehle: MAC address wali baat

Aapne kaha tha ki trial ki ginti MAC address se ho. **Chrome extension MAC
address nahi padh sakta.** Browser ka sandbox network-hardware ka koi API deta
hi nahi — `chrome.system.network` sirf ChromeOS kiosk mode mein hai. Extension
ke JS se ye kisi bhi trick se nahi hota.

Isliye maine wahi *maksad* poora kiya hai, browser jo sabse strong pehchaan de
sakta hai uske saath, aur asli MAC ka optional raasta bhi de diya hai:

| Layer | Kya hai | Reset karna kitna mushkil |
|---|---|---|
| `installation_id` | Random UUID, `chrome.storage.local` mein | Aasan (storage clear) |
| **Device hash** | installation_id + hardware ka HMAC — precise, per-install key | Aasan (storage clear) |
| **Machine hash** | *Sirf hardware* ka HMAC (platform, screen, cores, RAM, GPU) | Mushkil (hardware badalna padega) |
| **Asli MAC** *(optional)* | `native-helper/` wala helper padhta hai | Bahut mushkil |
| **Server caps** | Device se max 3 account, machine se 9; dono ledger | Client se chhua hi nahi ja sakta |

Raw fingerprint kabhi store nahi hota — server `DEVICE_HASH_SECRET` se HMAC
karke sirf digest rakhta hai. Database leak ho bhi jaye to us se koi device
pehchana ya replay nahi kar sakta.

Agar aap native helper install karwa dete ho, to `device.js` khud asli MAC utha
lega aur backend usi par key kar dega — aur kuch badalna nahi padega.

---

## Asli rule jo cheating rokta hai

Sirf account par 5 trial ginoge to koi bhi dusra email banakar phir 5 le lega.
Isliye **teen ledger** hain, aur teeno mein jagah honi chahiye:

```
allowed  =  active_subscription
         YA (user.trials_used     < 5      # ye account
         AUR device_ledger.used   < 5      # ye install, koi bhi account
         AUR machine_ledger.used  < 15)    # ye hardware, koi bhi install
```

**Device ledger** account se azaad hai — ek browser install par total 5 free run,
chahe 50 email bana lo.

**Machine ledger** isliye zaroori hai kyunki device hash mein
`installation_id` hai, jo client bhejta hai — matlab
`chrome.storage.local` clear karte hi naya device ban jata hai. Machine hash
sirf hardware se banta hai, jo storage clear karne se nahi badalta, isliye poore
computer par cap lagta hai (`MACHINE_TRIAL_LIMIT`, default 15). Ye jaan-boojh kar
5 se dheela rakha hai kyunki iski entropy kam hai — ek jaise hardware wale do
alag log ek hi row par aa sakte hain. Agar teen se kam hardware trait mile to ye
check chhod diya jata hai, aur asli MAC mil jaye to bhi (kyunki wo behtar key hai).

Signup par bhi cap: `MAX_ACCOUNTS_PER_DEVICE` (default 3) per device, aur uska
3 guna per machine.

**Saaf baat:** native helper ke bina, agar koi hardware hi badal de (dusra
laptop, VM, ya spoof kiye hue values) to use naya allowance mil jayega. Ye
browser sandbox ki seema hai, code ki kami nahi — agar ye aapke business ke liye
maayne rakhta hai to `native-helper/` wala MAC raasta hi jawab hai.

Faisla hamesha **server** karta hai. Extension ka code user padh aur badal sakta
hai, isliye wo sirf *pooch* sakta hai, *decide* nahi kar sakta.

---

## Chalane ka tarika

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # local dev ke liye jaisa hai waisa chal jayega
uvicorn app.main:app --reload
```

API docs: <http://localhost:8000/docs>

Tests: `pytest` (34 tests, na internet chahiye na koi API key)

Docker + Postgres: `docker compose up --build`

---

## Main endpoints

**Auth** — `/auth/signup`, `/auth/login`, `/auth/refresh`, `/auth/logout`,
`/auth/me`, `/auth/devices`, `/auth/password/forgot|reset|change`,
`/auth/verify-email`

**Trial** — do endpoint sabse important hain:

- `POST /entitlement/check` — sirf poochta hai "abhi chala sakte hain?".
  Trial **kharch nahi** karta. Popup khulte hi ye call karo.
- `POST /usage/consume` — **ek trial kharch karta hai**. Khatam hone par `402`
  deta hai. `Idempotency-Key` header bhejo taaki retry par do baar na kate.

`402` ka jawab aisa aata hai:

```json
{
  "detail": {
    "message": "You have used all 5 free trials. Subscribe for $5/month to continue.",
    "entitlement": { "reason": "trial_exhausted", "upgrade_url": "…" }
  }
}
```

**Billing** — `/billing/plans`, `/billing/checkout`, `/billing/subscription`,
`/billing/portal`, `/billing/cancel`, aur webhooks
`/webhooks/stripe` · `/webhooks/razorpay`

**Admin** (header `X-Admin-Key`) — `/admin/stats`, `/admin/grant-trials`,
`/admin/block-device`, `/admin/reset-device-trials` (jaise koi laptop bech de to
uska counter saaf karne ke liye)

---

## Extension mein jodna

`extension-client/` ki files apne extension mein copy karo,
`manifest.snippet.json` ko apne manifest mein merge karo, aur `api.js` mein
`API_BASE` apne server ka URL kar do.

Phir apne feature ko wrap kar do:

```js
import { runGated } from './gate.js';

const outcome = await runGated('run_feature', () => doTheActualWork(payload));
if (!outcome.ok && outcome.reason === 'subscription_required') {
  // popup upgrade button dikha dega, badge laal ho jayega
}
```

`gate.js` kaam se **pehle** `/usage/consume` call karta hai aur server ki haan
par hi aage badhta hai. Network fail hone par wo **band** rehta hai (fail
closed) — jaan-boojh kar, kyunki jo banda aapka API block kar sakta hai use
unlimited free run nahi milna chahiye.

| File | Kaam |
|---|---|
| `device.js` | Fingerprint banata hai; helper laga ho to asli MAC uthata hai |
| `api.js` | HTTP client, token storage, auto-refresh |
| `gate.js` | `runGated()` wrapper + badge par bache hue trial |
| `popup.html` / `popup.js` | Signup / login / trial meter / upgrade button |
| `background.example.js` | MV3 service worker mein kaise jode |

---

## Payment: bina account ke test

Default `PAYMENT_PROVIDER=mock` hai — `MOCK_BILLING_SECRET` set karne par ek
nakli checkout page milta hai jahan token paste karke "Pay now" dabate hi
subscription active ho jata hai.

Ye routes tabhi chalte hain jab teeno sach hon: `PAYMENT_PROVIDER=mock`,
`ENV != prod`, aur `MOCK_BILLING_SECRET` set ho. `/billing/mock/confirm` ko valid
token chahiye aur wo sirf calling user ka subscription banata hai — kisi aur ka
`user_id` dekar cheating nahi ho sakti. **Live server par `MOCK_BILLING_SECRET`
khaali rakho.**

**Asli provider par jaana:**

- **Razorpay** — India se bill kar rahe ho to yahi sahi hai. Stripe Indian
  business ko Indian customers se collect karne nahi deta. Dashboard mein
  monthly plan banao, `RAZORPAY_KEY_ID` / `KEY_SECRET` / `PLAN_ID` /
  `WEBHOOK_SECRET` set karo, webhook `/api/v1/webhooks/razorpay` par lagao.
- **Stripe** — global customers ke liye. `STRIPE_SECRET_KEY` aur
  `STRIPE_WEBHOOK_SECRET` set karo, webhook `/api/v1/webhooks/stripe` par lagao.

Dono ka code pehle se likha hai — sirf env variable badalna hai.

---

## Live jaane se pehle checklist

- [ ] `SECRET_KEY` aur `DEVICE_HASH_SECRET` mein lambi random value daalo.
      (`ENV=prod` par dev wali default values ke saath app start hi nahi hogi.)
      `DEVICE_HASH_SECRET` baad mein badla to saare device ledger reset ho
      jayenge — ek baar set karke sambhal ke rakho.
- [ ] `DATABASE_URL` Postgres par karo, `alembic upgrade head` chalao.
- [ ] `PAYMENT_PROVIDER` asli provider par karo aur webhook set karo.
- [ ] `MOCK_BILLING_SECRET` khaali kar do.
- [ ] `TRUST_PROXY_HEADERS=true` sirf tab jab aapka apna proxy `X-Forwarded-For`
      set karta ho — warna rate limit bypass ho sakta hai.
- [ ] `ALLOWED_ORIGINS=chrome-extension://<aapki-extension-id>` — sirf apni id.
- [ ] `EMAIL_BACKEND=smtp` taaki verification/reset mail sach mein jaayein.
- [ ] HTTPS par serve karo.
- [ ] `ADMIN_API_KEY` set karo, warna admin routes band rahenge.

---

## Security jo pehle se lagi hai

- Password bcrypt se hash (SHA-256 pre-hash, taaki lambe password ki poori
  entropy bache).
- Access token 15 minute; refresh token rotate hota hai, aur purana token dobara
  use hua to **saare session revoke** ho jaate hain (token chori ka pata chalta hai).
- Login/signup kabhi nahi batate ki email registered hai ya nahi.
- Login, signup, password reset par rate limit.
- Webhook signature verify hota hai aur event id save hota hai, isliye provider
  ka retry safe hai.
- `/usage/consume` user row aur device ledger ko lock karta hai, isliye do
  parallel request aakhri trial dono kharch nahi kar sakti.
