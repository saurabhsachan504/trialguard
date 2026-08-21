# TubeNotes — YouTube Summarizer web app

Wahi FastAPI service ab ek web app bhi serve karti hai: YouTube ka link daalo,
video ki **apni bhasha** me summary aa jayegi, aur PDF download kar sakte ho.

Design aapki di hui FastDL wali image jaisa hai — gradient hero, upar pills,
beech me link box (Paste + Summarize), aur neeche icon ke saath
**Get Summary** aur **Download the PDF**.

---

## Chalane ka tarika

```bash
pip install -r requirements-dev.txt     # 2 nayi library aayi hain
uvicorn app.main:app --reload
```

Ab <http://localhost:8000/> kholo. Bas — API aur website dono ek hi jagah par
hain, isliye CORS ka koi jhanjhat nahi.

`.env` me sirf ek cheez check kar lena:

```bash
OLLAMA_URL=https://ollama.trueworks.in     # wahi jo extension use karti hai
OLLAMA_MODEL=gemma2:9b
OLLAMA_INDIC_MODEL=sarvam-m-q4
```

---

## Kaam kaise karta hai

1. **Link parse** — `youtube.com/watch`, `youtu.be`, `/shorts`, `/embed`,
   `/live` aur seedha 11-character video id, sab chalte hain.
2. **Thumbnail turant** — oEmbed se title aur thumbnail aata hai. Ye **free** hai,
   koi trial nahi katta, taaki user ko turant dikhe ki sahi video pakda gaya.
3. **Transcript** — pehle `youtube-transcript-api`, fail hone par `yt-dlp`.
   Caption track chunte waqt wahi logic hai jo aapki extension me tha: auto (ASR)
   track hi asli boli hui bhasha batata hai, isliye usi bhasha ka manual track
   pehle chuna jata hai. (Warna Punjabi video par creator ke Hindi subtitles
   uthkar galat bhasha me summary ban jati.)
4. **Bhasha** — caption ka apna code sabse pehle dekha jata hai, kyunki Hindi aur
   Marathi dono Devanagari me likhe jate hain aur sirf script dekhkar farq nahi
   pata chalta. Uske baad script se detect hota hai.
5. **Model routing** — Marathi/Gujarati/Tamil/Telugu/Kannada/Malayalam/Bengali/
   Punjabi/Urdu → `OLLAMA_INDIC_MODEL`. Baaki sab → `OLLAMA_MODEL`. Ye isliye ki
   general model in bhashaon me English me bhagne lagta hai ya toota-phoota
   likhta hai.
6. **Streaming** — summary token-by-token aati hai (NDJSON), isliye 2-3 second me
   text dikhna shuru ho jata hai.

---

## Bhasha (language) — kaise kaam karta hai

**Default: video ki apni bhasha.** Hero mein "Summary language" dropdown hai,
jo shuru mein **"Same as the video"** par rehta hai.

Teen layer hain, taaki galat bhasha mein summary aa hi na sake:

**1. Prompt ke labels pehle se translate hote hain.** Ye wo bug tha jo aapko
mila. Pehle template mein English labels the (`## Overview`, `**Key Point:**`)
aur model se kaha jata tha "inhe translate kar lena". Model template ko jaisa ka
taisa copy karta hai — aur pehli heading English aate hi poora jawab English
mein chala jata hai. Ab Hindi ke liye prompt mein seedha `## अवलोकन`,
`**मुख्य बिंदु:**` jaata hai. Hindi, Marathi, Gujarati, Bengali, Tamil, Telugu,
Kannada, Malayalam, Punjabi aur Urdu ke labels bane hue hain.

**2. Language rule prompt ke shuru aur ant, dono jagah.** Model system prompt ke
pehle aur aakhri hisse ko sabse zyada weight deta hai.

**3. Script check + auto-translate.** Jawab aane ke baad server dekhta hai ki
text sach mein Devanagari (ya Tamil, Telugu…) mein hai ya nahi. Agar model ne
phir bhi English likh diya, to server khud Google se translate karke sahi bhasha
mein bhejta hai aur UI mein likha aata hai *"The model answered in the wrong
language — translated to Hindi."* Matlab galat bhasha nikal hi nahi sakti.

### Translate to other language

Do jagah hai, dono aapke extension jaisa:

- **Summary banne se pehle** — hero ka "Summary language" dropdown. Yahan koi
  bhasha chun li to summary usi mein banegi (46 bhashayein).
- **Summary banne ke baad** — result ke toolbar mein **🌐 Translate to…**
  dropdown. Jo screen par hai use turant kisi bhi bhasha mein badal deta hai.

### PDF ki language

**Download PDF** ya **Full notes → PDF** dabate hi ek language picker khulta hai
(search box ke saath, 46 bhashayein). Jo chuna, PDF usi me banega.

- Sabse upar ek suggested option rehta hai — summary pehle se screen par ho to
  *"Keep current — हिन्दी"*, warna *"Same as the video"*.
- Screen par jo summary hai use dobara banane ki zaroorat nahi: agar aapne dusri
  bhasha chuni to bas translate hota hai, phir PDF khulta hai.
- **Full notes → PDF** poore video ko chunk-by-chunk padhkar detailed notes
  banata hai, usi chuni hui bhasha me.

Dono PDF ka **format, colour, cover — sab bilkul same** hai (ek hi print
template se bante hain).

**PDF apne aap follow karta hai.** PDF usi text se banta hai jo screen par dikh
raha hai — to Tamil mein translate kiya to PDF bhi Tamil mein aayega. (Pehle ye
galat tha: translate ke baad PDF dobara generate karta tha aur translation
phenk deta tha. Fix kar diya — ab toolbar ka "Download PDF" hamesha wahi print
karta hai jo saamne hai, aur detailed notes chahiye to alag button hai:
**"Full notes → PDF"**.)

### Full notes — kuch bhi nahi chhutta

"Full Notes" on-screen summary se bilkul alag cheez hai:

| | Summary | Full Notes (PDF) |
|---|---|---|
| Transcript | 12,000 character tak sample hota hai | **poora**, ek bhi line chhodi nahi jati |
| Length | ~400 shabd | koi limit nahi — 2 ghante ka lecture 40-50 page bhi ban sakta hai |
| Tarika | ek baar mein | transcript ko chhote tukdon mein baantkar har tukde ko poora likha jata hai |

Teen cheezein isko pakka karti hain:

**Chhote chunk.** 3,500 character ke tukde (pehle 6,000 the) aur 400 character ka
overlap. Chhota tukda matlab model ke paas compress karne ki wajah kam, aur
overlap se koi point do tukdon ke beech mein nahi girta.

**Koi cap nahi.** Pehle 30 chunk ki hadd thi (~3 ghanta) aur usse aage ka video
**chupchaap kat jata tha**. Ab `NOTES_MAX_CHUNKS=0` hai — matlab koi limit nahi.
Agar aap khud limit lagao to UI par saaf likha aata hai ki kitne hisse chhoote.

**Fail hua tukda batata hai.** Pehle koi chunk fail hota to chupchaap gayab ho
jata tha. Ab 3 baar retry hota hai, aur phir bhi na bane to warning aati hai ki
kaunsa part missing hai.

Prompt bhi badla hai — ab saaf likha hai: *"Ye notes video dekhne ki jagah lete
hain. Har claim, example, kahani, naam, tareekh, number, definition, step,
comparison, sawaal aur jawab likho. Koi length limit nahi — agar is hisse mein
15 point hain to 15 hi likho."*

**Speed:** lambe video mein kai minute lagenge (progress bar part-by-part
dikhta hai). `NOTES_CONCURRENCY=2` default hai; agar aapke Ollama par
`OLLAMA_NUM_PARALLEL` 1 se zyada hai to ise 3-4 karke tez kar sakte ho.

### Model routing

| Bhasha | Model | Kyun |
|---|---|---|
| Hindi, English, Spanish, French, German, Arabic, Russian, Japanese… | `OLLAMA_MODEL` | Ye model in bhashaon mein achha likhta hai |
| Marathi, Gujarati, Tamil, Telugu, Kannada, Malayalam, Bengali, Punjabi, Urdu, Odia, Assamese, Nepali | `OLLAMA_INDIC_MODEL` | General model in par English mein bhaag jata hai |
| Baaki sab (Swahili, Sinhala…) | English mein likhkar Google se translate | Kamzor bhasha mein zabardasti likhwane se toota-phoota text aata hai |

---

## Trial ka hisaab

**1 video = 1 credit.** Extension jaisa hi. Idempotency key `video:<videoId>` hai,
matlab:

- Summary, Key Points aur Full Notes — teeno ek hi video ke, ek hi credit me
- Wahi video dobara → **kuch nahi katta**, hamesha ke liye
- Free account = 5 alag video, phir $5/month
- Extension aur web app ka **account same** hai, credits bhi milkar ginte hain

Galat URL daalne par credit nahi katta — check pehle hota hai.

---

## Endpoints

| Method | Path | Charge | Kaam |
|---|---|---|---|
| POST | `/api/v1/video/info` | ❌ free | Title + thumbnail |
| POST | `/api/v1/summarize` | ✅ 1/video | Streaming summary (`mode`: `summary` \| `key_points`) |
| POST | `/api/v1/notes` | ✅ same key | Poore video ke chunked detailed notes (PDF ke liye) |
| POST | `/api/v1/translate` | ❌ free | Bane hue summary/notes ko dusri bhasha mein badalna |

`/summarize` aur `/notes` dono `target_lang` leti hain (`"hi"`, `"ta"`, ya
`null`/`"auto"` = video ki apni bhasha).

Response NDJSON hai — har line ek JSON:

```json
{"type":"meta","video":{...},"language":"hi","language_name":"Hindi","entitlement":{...}}
{"type":"delta","text":"## अवलोकन"}
{"type":"done","text":"<poora markdown>"}
{"type":"error","message":"..."}
```

---

## PDF kaise banta hai

PDF taiyar hote hi browser ka print dialog seedha khul jata hai — wahan
**Destination: Save as PDF** chunna hai.

**Popup nahi khulta.** `window.open()` browser tabhi allow karta hai jab user ka
click abhi-abhi hua ho. Full notes banne me kai minute lagte hain, to tab tak
browser ke liye wo click purana ho chuka hota hai aur popup block ho jata tha
(*"Your browser blocked the popup"*). Ab document ek **chhupe hue iframe** me
render hota hai aur wahin se print hota hai — isme popup permission ki zaroorat
hi nahi, chahe generate hone me 10 minute lagen.

Neeche ek chhota sa link bhi rehta hai — *"Print dialog not showing? Open the
print view"* — agar kabhi dialog na aaye. Us par click karna khud ek user click
hai, isliye wo hamesha chalega.

Ye jaan-boojh kar hai: server par PDF banane wale engine (WeasyPrint/reportlab)
me Devanagari/Tamil fonts alag se package karne padte hain, warna PDF me
khaali dabbe (tofu) aate hain. Browser me wo fonts pehle se hain, isliye output
perfect aata hai. Test karke dekh liya — Hindi PDF bilkul saaf banta hai.

---

## Testing

```bash
pytest        # 82 tests, ~5 second
```

Tests me password hashing ka cost jaan-boojh kar kam rakha hai
(`BCRYPT_ROUNDS=4` sirf test ke liye) — warna har signup ka hash hi poora time
kha jata tha. Production me 12 hi rehta hai, aur purane users ke password bhi
chalte rehte hain kyunki cost hash ke andar hi likha hota hai.

Naye test URL parsing, bhasha detection, model routing, chunking, streaming,
trial ki ginti aur error handling cover karte hain. Transcript aur Ollama stub
kiye hue hain, isliye tests bina internet ke chalte hain.

Browser me bhi poora flow chalakar dekha gaya: signup → summarize → Hindi
markdown render → wahi video dobara (credit nahi kata) → 5 ke baad upgrade
screen → PDF print view.

---

## Dhyan dene layak

**YouTube datacenter IP ko throttle karta hai.** Aapki extension me transcript
user ke browser se aata tha, isliye ye problem nahi thi. Web app me server se
aata hai. Agar `/summarize` par baar-baar "no usable subtitles" aane lage aur
video me captions mojood hon, to `.env` me proxy laga do:

```bash
YOUTUBE_PROXY=http://user:pass@proxy-host:port
```

Dono raaste (transcript-api aur yt-dlp) is proxy ko khud utha lete hain.

**Kuch video kabhi kaam nahi karenge** — jinme caption track hai hi nahi (bahut
saare music/vlog uploads), ya creator ne subtitles band kar rakhe hain, ya video
private/age-restricted hai. Aise me UI saaf batata hai, credit nahi katta.

**Lambe video.** On-screen summary ke liye transcript ko poore video me se
4 bade hisso me sample kiya jata hai (shuru, beech, aakhir sab cover), taaki
model ka time bandha rahe. "Full Notes" poora transcript chunk-by-chunk padhta
hai — slow hai par kuch chhutta nahi.
