# TubeNotes backend ko DGX par live karna → `https://tubenotes.trueworks.in`

Aapka sawal: *"kya mujhe 127.0.0.1:8000 public karna padega?"*

**Haan — par "public karna" ka matlab wo URL kholna nahi hai.** `127.0.0.1`
kisi bhi computer par "yahi machine" hota hai. Aapke friend ke Chrome ne
`http://127.0.0.1:8000` khola to usne **apne hi laptop** ke andar dekha, jahan
kuch chal hi nahi raha. Isliye wahi backend **DGX par** chalana hai, aur DGX ko
ek asli naam dena hai — `tubenotes.trueworks.in`.

---

## ⚠️ Pehle ye padho — abhi jo live hai wo `dev` mode mein chal raha hai

Maine aapka live server check kiya:

```
GET https://tubenotes.trueworks.in/healthz
{"status":"ok","env":"dev","version":"1.0.0"}
                 ^^^^^
```

`env` mein `prod` hona chahiye tha, `dev` hai. Iska matlab server ne `.env`
padha hi nahi (ya usme `ENV=dev` hai), aur tab **saari default values lag jati
hain**. Do cheezein isse pakki hoti hain:

**1. `/docs` sabke liye khula hai.** Maine kholkar dekha —
`https://tubenotes.trueworks.in/docs` par poora Swagger UI chal raha hai. Code
mein wo `ENV != "prod"` par hi khulta hai, isliye ye confirm karta hai ki server
prod mode mein nahi hai.

**2. `SECRET_KEY` shayad default hi hai** — `dev-only-insecure-secret-change-me`.
Ye JWT sign karti hai. Agar default hai, to jo bhi ye string jaanta hai wo
**kisi bhi user ka token khud bana sakta hai** — bina password ke login, aur
unlimited free videos. Maine ye aapke live server par test nahi kiya (wo aapka
production hai), par ek command se aap khud dekh sakte ho:

```bash
cd ~/apps/tubenotes && grep -E "^(ENV|SECRET_KEY|DATABASE_URL)=" .env
```

Agar `.env` hai hi nahi, ya `SECRET_KEY` mein `change-me` dikhe, to niche wale
Step 3 se poora setup kar lo. `SECRET_KEY` badalne par sab logged-out ho jayenge
(naye tokens banenge) — **par `DEVICE_HASH_SECRET` mat badalna** agar us par
kisi ka trial chal chuka hai.

Ek acchi khabar bhi: `/api/v1/billing/mock/confirm` route **nahi** dikha, yaani
`MOCK_BILLING_SECRET` khali hai. Wahi sahi hai — warna koi bhi khud ko free
subscription de leta.

---

Uske baad:

- aapka laptop band ho, VS Code band ho — koi farak nahi, server DGX par chalta rahega
- friend ho ya koi bhi customer — sabke liye ek hi URL
- sab accounts, 5 free video ka hisaab aur subscription **ek hi database** mein

---

## Ek nazar mein poora rasta

```
  Customer ka browser
        │  https://tubenotes.trueworks.in
        ▼
  DGX ka Nginx (aaPanel)          ← SSL yahin khatam hota hai
        │  http://127.0.0.1:8000
        ▼
  Docker: api container           ← FastAPI, restart: unless-stopped
        │  db:5432 (private network)
        ▼
  Docker: postgres container      ← data yahan, volume mein
```

Sirf **443** internet ke liye khula hai. Na 8000 khula hai, na Postgres —
dono sirf DGX ke andar se dikhte hain.

---

## Step 1 — DNS (5 minute, phir 5-30 minute wait)

Jahan `trueworks.in` ka DNS hai wahan ek record jodo:

| Type | Name | Value |
|---|---|---|
| A | `tubenotes` | DGX ka **public IP** |

Public IP DGX par se: `curl -s ifconfig.me`

Check karo (apne laptop se):

```bash
nslookup tubenotes.trueworks.in
```

Jab tak isme DGX ka IP na dikhe, aage mat badho — SSL isi par atkega.

---

## Step 2 — Code DGX par le jao

```bash
ssh <aap>@<dgx-ip>
mkdir -p ~/apps && cd ~/apps
# zip upload karke:
unzip trialguard-backend.zip -d tubenotes && cd tubenotes
```

---

## Step 3 — `.env` banao

```bash
cp .env.prod.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # 3 baar chalao
nano .env
```

Ye 4 zaroor bharo:

| Key | Kya daalein |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` ka output |
| `SECRET_KEY` | pehla random string |
| `DEVICE_HASH_SECRET` | **doosra, alag** random string |
| `ADMIN_API_KEY` | teesra random string |

> `DEVICE_HASH_SECRET` baad mein badla to **har device ka trial ledger reset**
> ho jayega — yaani sabko dobara 5 free video mil jayenge. Ek baar set karo aur
> kahin safe likh lo.

`MOCK_BILLING_SECRET` **khali hi rehne do**. Wo test-only endpoint hai jisse koi
bhi apne aap ko free subscription de sakta hai. `deploy.sh` isko check karta hai
aur bhara hua mila to deploy rok deta hai.

---

## Step 4 — Chala do

```bash
bash deploy/deploy.sh
```

Ye khud hi: secrets check → image build → Postgres uthao → **migrations
chalao** → API start → health check.

Sahi chala to:

```
==> health check  OK
{"status":"ok","env":"prod","version":"1.0.0"}
```

Ab dekho ki DGX par hi jawab aa raha hai:

```bash
curl http://127.0.0.1:8000/healthz
```

Bahar se abhi nahi aayega — Nginx baaki hai.

---

## Step 5 — aaPanel mein site + SSL

1. **Website → Add site**
   - Domain: `tubenotes.trueworks.in`
   - PHP version: **Static / Pure static** (PHP ki zaroorat nahi)
2. Us site par **SSL → Let's Encrypt → Apply**
3. **Force HTTPS** ON
4. Us site ka **Config file** kholo, aur `deploy/nginx-tubenotes.conf`
   wala `location /` block paste kar do
   *(agar aaPanel ne pehle se koi `location /` banaya hai to use replace karo)*
5. **Save** → Nginx apne aap reload ho jata hai

Ab bahar se:

```bash
curl https://tubenotes.trueworks.in/healthz
```

### Us config mein do line kyun hain — maine naapkar dekha

**`gzip off;`** — ye sabse zaroori hai. Summary aur full notes stream hokar
aate hain, ek-ek line banate hi. gzip ON ho to Nginx poora response jama karke
compress karta hai, isliye pehla byte tabhi milta hai jab sab ban chuka ho:

```
gzip on   ->  pehla byte 5.01s par   (poora response bhi 5.01s)
gzip off  ->  pehla byte 0.00s par   (poora response  5.01s)
```

Full notes 20-30 minute lete hain — gzip ON ke saath user ko utni der khali
screen dikhegi. **aaPanel naye site mein gzip khud ON karta hai**, isliye ye
line honi hi chahiye.

**`proxy_read_timeout 3600s;`** — Ollama ek chunk par 60s se zyada le sakta hai
aur us dauran kuch nahi bhejta. Nginx ka default 60s hai:

```
read_timeout 5s   + upstream 8s chup  ->  connection toot gaya (5.0s par)
read_timeout 3600s + wahi upstream    ->  200 OK (8.0s par)
```

`proxy_buffering off;` bhi rakha hai, par imaandari se: **mere test mein isse
koi farak nahi pada** — asli mujrim gzip tha. Nuksan koi nahi, isliye safety ke
liye rehne diya.

---

## Step 6 — Extension ko naya URL do

Extension v2.1.0 mein ye **pehle se** ho chuka hai:

```js
const DEFAULT_API_BASE = "https://tubenotes.trueworks.in/api/v1";
```

Dono manifest mein `https://tubenotes.trueworks.in/*` bhi jud gaya hai.
Aapko sirf naya build load karna hai. Aapke apne dev testing ke liye Options
page se `trialGuardApiUrl` local URL par set kar sakte ho — wo live default ko
override kar deta hai.

---

## "Mera laptop band ho jaye tab bhi chale" — ye kaise pakka hota hai

`docker-compose.prod.yml` mein dono service par `restart: unless-stopped` hai.
Iska matlab:

- container crash ho → Docker use turant wapas uthata hai
- **DGX reboot ho → boot par apne aap chalu ho jate hain**
- aap SSH se logout ho jao → koi farak nahi, ye aapke shell ke bachche nahi hain

Ek baar khud check kar lena:

```bash
sudo reboot
# 2-3 minute baad
curl https://tubenotes.trueworks.in/healthz
```

Sirf ek shart: DGX par Docker service boot par enabled ho —

```bash
sudo systemctl enable docker
```

---

## Rozmarra ke commands

```bash
cd ~/apps/tubenotes
C="docker compose -f docker-compose.prod.yml"

$C ps                    # kya chal raha hai
$C logs -f api           # live log
$C logs --tail=100 api   # aakhri 100 line
$C restart api           # sirf API restart
$C down                  # band (data volume mein bacha rehta hai)
bash deploy/deploy.sh    # code update ke baad - build + migrate + restart
```

**Backup** (ye zaroor set karna — user accounts isme hain):

```bash
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U trialguard trialguard | gzip > ~/backup-$(date +%F).sql.gz
```

Crontab mein roz raat 2 baje:

```
0 2 * * * cd ~/apps/tubenotes && docker compose -f docker-compose.prod.yml exec -T db pg_dump -U trialguard trialguard | gzip > ~/backups/tubenotes-$(date +\%F).sql.gz
```

Wapas laane ke liye:

```bash
gunzip -c backup.sql.gz | docker compose -f docker-compose.prod.yml exec -T db psql -U trialguard trialguard
```

---

## Maine yahan kya-kya chala kar dekha

Docker daemon is sandbox mein nahi tha, isliye **container ke bina wahi cheezein
theek usi config par** chalayin: asli PostgreSQL 16, asli Nginx (wahi
`location` block), aur prod `.env` (`ENV=prod`, `DEBUG=false`,
`TRUST_PROXY_HEADERS=true`).

```
alembic upgrade head (Postgres par)   -> 10 tables bani
                                         users, devices, device_trial_ledger,
                                         subscriptions, usage_events, ...
signup via Nginx                      -> 5 of 5 free videos
5 alag video                          -> 4 3 2 1 0
wahi video dobara                     -> duplicate, credit nahi kata
6th video                             -> blocked, subscription_required
ek hi IP se 7 signup                  -> 201 201 201 201 201 429 429
7 alag IP se 7 signup                 -> saare 201        (rate limit sahi IP par lag rahi hai)
API restart, phir login               -> data bacha, credits 4 ke 4
docker compose config                 -> valid
```

Rate-limit wala test isliye ahem hai: `TRUST_PROXY_HEADERS=true` na hota to
Nginx ke peeche har request ek hi IP se aati dikhti aur **poori duniya ek hi
bucket** mein gir jati — ek banda 5 signup karke sabko block kar deta.

---

## PDF/summary ki speed — kya badla

**Purani `.env` sab kuch overwrite karti hai.** Code ke naye default tabhi lagenge
jab `.env` me wo line na ho. DGX par ye chaar line update/add karo:

```bash
NOTES_CONCURRENCY=4              # pehle 2 tha
NOTES_CHUNK_CHARS=6000           # pehle 3500 tha
OLLAMA_SKIP_THINKING=true        # naya
TRANSCRIPT_CACHE_TTL_SECONDS=1800  # naya
```

Phir `bash deploy/deploy.sh`.

**1. Chunks 4 ek saath (pehle 2).** Aapke Ollama par `OLLAMA_NUM_PARALLEL=4` hai,
to aadhi capacity khali ja rahi thi. Ye ussey aage nahi ja sakta — Ollama utni hi
requests ek saath chalata hai, baaki queue me lagti hain.

**2. Chunk 3500 → 6000 chars.** Ek ghante ki video ke liye 16 ki jagah **9**
chunks. `num_ctx` pehle se 8192 hai, to 6000-char chunk (~1500-2000 token) prompt
aur poore jawab ke saath aaram se samata hai.

**3. Har call par naya HTTPS connection banta tha.** `httpx.AsyncClient` har
request par naya banaya ja raha tha — yaani har chunk par naya TCP + TLS
handshake, `https://ollama.trueworks.in` tak. Ab poore process ke liye ek hi
client hai jo connections zinda rakhta hai.

**4. `think:false`.** Sarvam pehle ek chhupa hua `<think>` block likhta hai aur
`strip_think()` use phenk deta hai — us par kharch hue tokens poori tarah bekaar
jate hain. Ab Ollama se wo skip karne ko kaha jata hai. Purane build ise HTTP 400
karte hain; wo ek baar detect hokar yaad rakh liya jata hai, phir kabhi nahi
bheja jata. Aapka Q4 build accept karta hai ya nahi ye chalte hi pata chal
jayega — dono surat me kuch tootta nahi.

**5. Transcript ab ek hi baar aata hai.** Pehle `/summarize` YouTube se transcript
laata tha, phir `/notes` **wahi transcript dobara** laata tha — dono baar server
ke IP se, jo YouTube khushi se rate-limit karta hai. Ab 30 minute ka cache hai.

**6. Translate** ab 4 request ek saath bhejta hai (pehle ek-ek), aur lambi line
ko bhi todta hai. Doosri wali ek asli bug thi: `_chunk` sirf `\n` par todta tha,
to story-format notes ka 5000-char paragraph poora ek URL me jata, Google mana kar
deta, aur PDF **original language me** aa jata tha.

### Kitna farak — naapa hua

Asli `full_notes()` chalakar, ek 60-minute ki Hindi video (49,450 chars) par,
Ollama ko 40 token/second maankar:

```
pehle (concurrency 2, chunk 3500)   16 chunks    1.00x
ab    (concurrency 4, chunk 6000)    9 chunks    1.7x tez
ab + think:false bhi chala to        9 chunks    3.4x tez
```

Absolute minute aapke model ki asli speed par depend karte hain — bharosa **ratio**
par karo, minute par nahi. Aur ye model maanta hai ki output input ke anupat me
hi rehta hai; asal me chhote chunks har baar apna heading/intro dobara likhte
hain, isliye 6000 wala fayda isse thoda zyada hi hoga.

**7. Summary ka rendering.** Ye web app ki apni bug thi: har token aane par
`body.innerHTML = md2html(poora_text)` chalta tha — yaani poora Markdown dobara
parse aur poora DOM dobara banta tha, har delta par. Asli Chromium me naapa:

```
12k ka summary, 3009 delta:
  har delta par paint   3009 paints   19.5 s CPU
  har 4th par           753 paints     4.9 s
  sirf ant me             1 paint      0.016 s
```

Ye CPU usi thread par jata hai jo network se tokens padh raha hai, isliye ye
summary ko sach me dheema karta tha. Ab ek animation frame me ek hi paint hota
hai. Naya painter, asli browser me:

```
  800 delta  ->   7 paints   (114x kam)
 1600 delta  ->  11 paints   (145x kam)
 3200 delta  ->  40 paints   ( 80x kam)
 beech me text dikhta rahta hai : haan
 ant me poora text              : haan
```

**8. Adhoora PDF ab chup nahi rehta.** Pehle koi section fail hota to warning
sirf screen par aati thi — PDF me uska koi nishaan nahi. Tab band karte hi wo
warning gayab, aur PDF poora dikhta tha jabki usme ched tha. Ab wo warning
**notes ke andar** bhi jati hai, to PDF me saaf likha aata hai kaun sa part
chhoot gaya.

**9. PDF me ek hi heading/paragraph baar-baar aana.** Ye model ka loop tha.
Chhote model ko jab kaha jaye "exhaustive notes likho, kuch mat chhodo" aur
chunk me content hi thoda ho, to wo `num_predict` ka poora budget wahi ek
heading aur wahi ek line dohra kar bhar deta hai — isi se ek chhoti clip ka
13-page PDF ban gaya tha. Do taraf se roka:

- **`collapse_repeats()`** — jo block pehle likha ja chuka hai wo hata diya jata
  hai. Pehla hamesha rehta hai, aur 25 character se chhoti line ko haath nahi
  lagaya jata (taki "- haan" jaisi normal chhoti line na ude). Ye har chunk par
  bhi lagta hai aur poore notes par bhi, kyunki chunks overlap karte hain to do
  chunk ek hi baat likh sakte hain.
- **`num_predict` ab chunk ke size se bandha hai** — input ke token ka 2.5 guna.
  Normal 6000-char chunk ko **poora 4096 budget milta hai** (yaani asli video ke
  notes kabhi kate nahi), cap sirf tab lagta hai jab chunk sach me chhota ho —
  wahi ek jagah hai jahan model ke paas kehne ko kuch bacha hi nahi hota.

Naapa hua (13 baar dohrata hua nakli model):

```
model ne bheja   : 13 baar wahi heading
notes me bacha   : 1 baar
asli content     : zinda hai
chhote chunk ka budget : 800 token (pehle hamesha 4096)
normal chunk ka budget : 4096 (poora, koi katauti nahi)
```

Ek aur cheez isi ne pakdi: mera pehla `collapse_repeats` dangling heading hatate
waqt **har** section ko kha raha tha. Purane test suite ne turant pakad liya
(`test_notes_read_the_entire_transcript_not_a_sample` fail hua) — ab wo sirf
akeli heading line hatata hai, "## X" ke niche text ho to use nahi chhoota.

### Naye tests

```
112 passed        (pehle 90 the, 22 naye)
```

Naye tests: think:false bheja ja raha hai / plain model par nahi bheja ja raha /
HTTP 400 par fallback aur wo yaad rehta hai / client reuse ho raha hai / chunks
sach me overlap kar rahe hain / concurrency ke bawajood order sahi hai /
transcript dobara fetch nahi hota / cache band bhi ho sakta hai / cache expire
hota hai / lamba paragraph tootta hai / markdown ki lines saath rehti hain /
translate parallel + order.

Har naye test ke liye **purana bug wapas daalkar** dekha — 6 test turant FAIL
hue. Yaani ye khali PASS nahi chhaap rahe.

---

## Sabse bada speed jump — sabhi users ke beech share hone wala cache

Ek hi video ko das log summarize karte hain aur jawab har baar wahi hota hai. Ab
pehla banda banwata hai, baaki sabko **DB se turant** milta hai — na YouTube,
na Ollama.

Asli server + Postgres par naapa (dusra user, wahi video):

```
jawab aane me laga  : 515 ms      (pehle: 10-30 minute)
cached flag         : true
transcript source   : cache
notes ke chars      : 16,811      (byte-to-byte wahi)
credit kata         : 1
```

### Aapke chaaron faisle laga diye

**1. Cache hit par bhi credit katta hai.** User ke liye value wahi hai. Aur wahi
video dobara khud ne kholi to phir bhi ek hi credit — `video:<id>` wali purani
idempotency waise hi kaam kar rahi hai.

**2. Stampede lock.** Ek hi fresh video par das log ek saath click karein to
sirf ek generate karega; baaki lock par ruk kar, uske khatam hote hi bani hui
row padh lenge. Ye lock **process ke andar** hai — do uvicorn worker ho to dono
alag-alag generate kar sakte hain. Wo thoda GPU zaya karta hai par tootta kuch
nahi, kyunki duplicate row ko `put()` normal maanta hai. Cross-worker lock ke
liye Redis ya Postgres advisory lock chahiye — utni machinery is bachat ke liye
zyada hai.

**3. Purani rows hatana.** 180 din se jise kisi ne nahi khola:

```bash
curl -fsS -X POST -H "X-Admin-Key: $ADMIN_API_KEY" \
  https://tubenotes.trueworks.in/api/v1/admin/cache/purge
```

Crontab me har Sunday raat 4 baje:

```
0 4 * * 0 curl -fsS -X POST -H "X-Admin-Key: KEY" https://tubenotes.trueworks.in/api/v1/admin/cache/purge
```

Kitna bhara hai ye dekhne ke liye:

```bash
curl -H "X-Admin-Key: $ADMIN_API_KEY" https://tubenotes.trueworks.in/api/v1/admin/cache/stats
# {"rows":1,"total_chars":16811,"approx_mb":0.0,"total_hits":1,"prompt_version":1}
```

**4. Unlisted/private video kabhi cache nahi hoti.** Save karne se pehle ek baar
YouTube ka watch page dekha jata hai. Unlisted/private mila → cache nahi. Aur
agar **pata hi na chale** (YouTube ne block kiya, consent wall) → tab bhi cache
nahi. Ye jaanbujhkar hai: shak ho to share mat karo.

### Cache key me kya-kya hai

```
video_id + mode + language + model + prompt_version
```

`prompt_version` (`app/services/output_cache.py`) **sabse zaroori hai**. Jab bhi
`summarizer.py` ka koi prompt badlo, ise **1 badha dena** — warna aap prompt
sudharoge aur cached users ko hamesha purana output milta rahega. Purani rows
apne aap ignore ho jayengi aur cron unhe hata dega.

### Ek chalaki jo zaroori thi

"Same as the video" wali request me output ki language transcript se tay hoti
hai — aur transcript laana hi to sabse slow kaam hai. Isliye har row me video ki
apni language bhi likhi jati hai (`detected_lang`), aur agli baar wo DB se hi
padh li jati hai. Iske bina auto wali request par cache hit hone ke bawajood
YouTube ka chakkar lagta rehta.

### Naye tests

```
134 passed        (pehle 112 the, 22 naye)
```

Isme: dusre user ko cache milta hai / hit par bhi credit katta hai / ek hi banda
dobara par ek hi credit / alag language alag row / summary aur notes alag /
prompt_version badalne par purana nahi milta / unlisted cache nahi hoti / check
fail ho to bhi cache nahi hoti / cache band ki ja sakti hai / bahut chhota jawab
cache nahi hota / auto language bina transcript ke / purge sirf thandi rows
hatata hai / purge bina admin key ke nahi / stats / lock.

Har ek ke liye **wahi bug wapas daalkar** dekha — paanchon baar sahi test FAIL
hue.

---

## Aage kya baaki hai

1. **Asli payment.** Abhi `PAYMENT_PROVIDER=mock` hai — koi paisa nahi chalta.
   India se bill karna ho to Razorpay: dashboard se 4 key `.env` mein, aur
   webhook URL `https://tubenotes.trueworks.in/api/v1/billing/webhook`.
   Bologe to laga dunga.
2. **Web app ka domain.** Backend `WEB_APP_ENABLED=true` ke saath UI bhi serve
   karta hai, yaani `https://tubenotes.trueworks.in/` par TubeNotes khul
   jayega. Chaho to `tubenotes.trueworks.in` bhi isi par point kar do.
3. **Ollama abhi seedha browser se call hota hai.** Koi technical banda
   extension ka code padhkar TrialGuard hata sakta hai aur aapka Ollama free
   mein use kar sakta hai. Pakka rokna ho to Ollama ko backend ke peeche daalna
   padega — alag kaam hai.
