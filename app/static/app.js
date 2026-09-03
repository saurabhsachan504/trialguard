/* TubeNotes — front-end for the YouTube summarizer web app.
   Talks to the same FastAPI service that serves this page, so there is no CORS
   and no separate API host to configure. */
(function () {
  "use strict";

  const API = "/api/v1";
  const $ = (id) => document.getElementById(id);
  const TOK = "tn_tokens";
  const DEV = "tn_device";

  let mode = "summary";
  let lastNotes = null;   // { videoId, title, url, markdown, lang }
  let busy = false;

  // Offered output languages. "auto" keeps the video's own language, which is
  // the default because that is what most people want.
  const LANGS = [
    ["hi","हिन्दी — Hindi"],["en","English"],["mr","मराठी — Marathi"],
    ["gu","ગુજરાતી — Gujarati"],["bn","বাংলা — Bengali"],["pa","ਪੰਜਾਬੀ — Punjabi"],
    ["ta","தமிழ் — Tamil"],["te","తెలుగు — Telugu"],["kn","ಕನ್ನಡ — Kannada"],
    ["ml","മലയാളം — Malayalam"],["or","ଓଡ଼ିଆ — Odia"],["as","অসমীয়া — Assamese"],
    ["ur","اردو — Urdu"],["ne","नेपाली — Nepali"],["sa","संस्कृतम् — Sanskrit"],
    ["es","Español"],["fr","Français"],["de","Deutsch"],["pt","Português"],
    ["it","Italiano"],["nl","Nederlands"],["ru","Русский"],["uk","Українська"],
    ["ar","العربية"],["fa","فارسی"],["tr","Türkçe"],["he","עברית"],
    ["zh","中文"],["ja","日本語"],["ko","한국어"],["th","ไทย"],["vi","Tiếng Việt"],
    ["id","Bahasa Indonesia"],["ms","Bahasa Melayu"],["pl","Polski"],["ro","Română"],
    ["el","Ελληνικά"],["sv","Svenska"],["cs","Čeština"],["hu","Magyar"],
    ["fi","Suomi"],["da","Dansk"],["no","Norsk"],["sw","Kiswahili"],["si","සිංහල"],
  ];

  function fillLangSelect(el, { includeAuto }) {
    el.innerHTML = "";
    if (includeAuto) {
      const o = document.createElement("option");
      o.value = "auto"; o.textContent = "Same as the video";
      el.appendChild(o);
    } else {
      const o = document.createElement("option");
      o.value = ""; o.textContent = "🌐 Translate to…";
      el.appendChild(o);
    }
    for (const [code, name] of LANGS) {
      const o = document.createElement("option");
      o.value = code; o.textContent = name;
      el.appendChild(o);
    }
  }

  const outLang = () => {
    const v = $("outLang").value;
    return v && v !== "auto" ? v : null;
  };

  // =====================================================================
  // Device fingerprint — the server hashes this to enforce the free-trial
  // limit per machine. A browser cannot read a MAC address, so this is the
  // strongest identifier available; the server also keeps a hardware-only
  // ledger so clearing site data does not hand out fresh credits.
  // =====================================================================
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
  }

  function gpu() {
    try {
      const gl = document.createElement("canvas").getContext("webgl");
      const ext = gl && gl.getExtension("WEBGL_debug_renderer_info");
      return ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)).slice(0, 200) : null;
    } catch (_) { return null; }
  }

  function device() {
    let rec = null;
    try { rec = JSON.parse(localStorage.getItem(DEV) || "null"); } catch (_) {}
    if (!rec || !rec.installation_id) rec = { installation_id: uuid() };
    if (!rec.gpu) rec.gpu = gpu();
    try { localStorage.setItem(DEV, JSON.stringify(rec)); } catch (_) {}

    const fp = {
      installation_id: rec.installation_id,
      platform: navigator.platform || (navigator.userAgentData && navigator.userAgentData.platform),
      user_agent_brand: (navigator.userAgentData && navigator.userAgentData.brands
        ? navigator.userAgentData.brands.map((b) => b.brand + " " + b.version).join("|")
        : navigator.userAgent || "").slice(0, 200),
      screen: `${screen.width}x${screen.height}x${screen.colorDepth}`,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      language: navigator.language,
      hardware_concurrency: navigator.hardwareConcurrency,
      device_memory: navigator.deviceMemory,
      gpu: rec.gpu,
    };
    const out = {};
    for (const k in fp) if (fp[k] !== undefined && fp[k] !== null && fp[k] !== "") out[k] = fp[k];
    return out;
  }

  // =====================================================================
  // Auth
  // =====================================================================
  const tokens = {
    get() { try { return JSON.parse(localStorage.getItem(TOK) || "null"); } catch (_) { return null; } },
    set(t) {
      localStorage.setItem(TOK, JSON.stringify({
        access_token: t.access_token,
        refresh_token: t.refresh_token,
        expires_at: Date.now() + (t.expires_in - 60) * 1000,
      }));
    },
    clear() { localStorage.removeItem(TOK); },
  };

  const signedIn = () => tokens.get() !== null;
  let refreshing = null;

  async function refreshTokens() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const t = tokens.get();
      if (!t) return null;
      try {
        const r = await fetch(`${API}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: t.refresh_token }),
        });
        if (!r.ok) { tokens.clear(); return null; }
        const fresh = await r.json();
        tokens.set(fresh);
        return fresh;
      } catch (_) { return null; }
      finally { refreshing = null; }
    })();
    return refreshing;
  }

  async function authHeader() {
    const t = tokens.get();
    if (!t) return null;
    if (Date.now() < t.expires_at) return `Bearer ${t.access_token}`;
    const fresh = await refreshTokens();
    return fresh ? `Bearer ${fresh.access_token}` : null;
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
    if (opts.auth !== false) {
      const h = await authHeader();
      if (!h) throw err(401, "Not signed in");
      headers.Authorization = h;
    }
    const init = { method: opts.method || "GET", headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body) };

    let res = await fetch(API + path, init);
    if (res.status === 401 && opts.auth !== false) {
      const fresh = await refreshTokens();
      if (fresh) { headers.Authorization = `Bearer ${fresh.access_token}`; res = await fetch(API + path, init); }
    }
    if (opts.raw) return res;

    const text = await res.text();
    let json = null; try { json = text ? JSON.parse(text) : null; } catch (_) {}
    if (!res.ok) { if (res.status === 401) tokens.clear(); throw err(res.status, json ? json.detail : text); }
    return json;
  }

  function err(status, detail) {
    const e = new Error(typeof detail === "string" ? detail : (detail && detail.message) || "Request failed");
    e.status = status; e.detail = detail;
    e.entitlement = detail && detail.entitlement;
    return e;
  }

  // =====================================================================
  // Trials chip
  // =====================================================================
  function paintChip(ent) {
    const chip = $("trialChip");
    $("accountBtn").textContent = signedIn() ? "Account" : "Sign in";
    if (!signedIn() || !ent) { chip.classList.add("hidden"); return; }
    chip.classList.remove("hidden");
    chip.classList.remove("pro", "out");
    if (ent.plan === "subscription") { chip.classList.add("pro"); chip.textContent = "★ Pro"; return; }
    const left = Math.min(ent.trials_remaining, ent.device_trials_remaining);
    if (left <= 0) chip.classList.add("out");
    chip.textContent = `${left} free left`;
  }

  async function refreshEntitlement() {
    if (!signedIn()) { paintChip(null); return null; }
    try {
      const ent = await api("/entitlement/check", { method: "POST", body: { device: device() } });
      paintChip(ent);
      return ent;
    } catch (_) { paintChip(null); return null; }
  }

  // =====================================================================
  // Markdown (small, safe: escape first, then re-introduce structure)
  // =====================================================================
  function md2html(md) {
    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>");
    let html = "", list = null;
    const close = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const raw of String(md || "").split("\n")) {
      const line = raw.trim();
      if (!line) { close(); continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        close();
        const lvl = Math.min(m[1].length, 3);
        html += `<h${lvl}>${inline(m[2])}</h${lvl}>`;
      } else if (/^\d+[.)]\s+/.test(line)) {
        if (list !== "ol") { close(); html += "<ol>"; list = "ol"; }
        html += `<li>${inline(line.replace(/^\d+[.)]\s+/, ""))}</li>`;
      } else if (/^[-*•]\s+/.test(line)) {
        if (list !== "ul") { close(); html += "<ul>"; list = "ul"; }
        html += `<li>${inline(line.replace(/^[-*•]\s+/, ""))}</li>`;
      } else {
        close();
        html += `<p>${inline(line)}</p>`;
      }
    }
    close();
    return html;
  }

  // =====================================================================
  // Result rendering
  // =====================================================================
  const R = () => $("result");

  function shell(video, extraTag) {
    const tags = [];
    if (video && video.author) tags.push(`<span class="tag">${escapeAttr(video.author)}</span>`);
    if (extraTag) tags.push(extraTag);
    R().classList.remove("hidden");
    R().innerHTML = `
      <div class="card">
        <div class="vidrow">
          <img id="rThumb" alt="" src="${video ? escapeAttr(video.thumbnail) : ""}"
               onerror="this.style.visibility='hidden'" onload="this.style.visibility='visible'" />
          <div>
            <div class="t" id="rTitle">${video ? escapeAttr(video.title) : "Loading…"}</div>
            <div class="m" id="rMeta">${tags.join("")}</div>
          </div>
        </div>
        <div class="toolbar" id="rTools"></div>
        <div class="body">
          <div id="rNote"></div>
          <div id="rPrint"></div>
          <div id="rProgress" class="hidden"><div class="progress"><i id="rBar"></i></div></div>
          <div id="rStatus" class="status"></div>
          <div class="md" id="rBody"></div>
        </div>
      </div>`;
    R().scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function escapeAttr(s) {
    return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function status(text, spinning) {
    const el = $("rStatus");
    if (!el) return;
    el.innerHTML = text ? `${spinning ? '<span class="spin"></span>' : ""}<span>${escapeAttr(text)}</span>` : "";
  }

  function note(kind, html) {
    const el = $("rNote");
    if (el) el.innerHTML = html ? `<div class="note ${kind}">${html}</div>` : "";
  }

  function tools(items) {
    const bar = $("rTools");
    if (!bar) return;
    bar.innerHTML = "";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "tool " + (it.tone || "t-copy");
      b.innerHTML = it.icon + " " + it.label;
      b.onclick = it.onClick;
      bar.appendChild(b);
    });
  }

  const ICONS = {
    copy: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    pdf: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>',
    md: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6"/></svg>',
    notes: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h4"/></svg>',
    open: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14L21 3"/></svg>',
  };

  // =====================================================================
  // The main flow
  // =====================================================================
  function setBusy(on) {
    busy = on;
    ["goBtn", "summaryBtn", "pdfBtn"].forEach((id) => { $(id).disabled = on; });
    $("goBtn").textContent = on ? "Working…" : "Summarize";
  }

  async function run(requestedMode, targetOverride) {
    if (busy) return;
    const url = $("url").value.trim();
    if (!url) { $("url").focus(); return; }

    if (!signedIn()) { openAuth("signup", "Create a free account to summarize — 5 videos free."); return; }

    const wantNotes = requestedMode === "notes";
    const target = targetOverride === undefined ? outLang() : targetOverride;
    setBusy(true);
    shell(null);
    status("Reading the video…", true);
    note("", "");

    // Show the thumbnail straight away — it costs nothing and makes the wait
    // feel much shorter.
    try {
      const info = await api("/video/info", { method: "POST", body: { url, device: device() } });
      $("rThumb").src = info.thumbnail;
      $("rTitle").textContent = info.title;
      $("rMeta").innerHTML = info.author ? `<span class="tag">${escapeAttr(info.author)}</span>` : "";
      lastNotes = { videoId: info.video_id, title: info.title, url: info.url, markdown: "" };
    } catch (e) {
      if (e.status === 401) { setBusy(false); openAuth("login"); return; }
      if (e.status === 400) { setBusy(false); status(""); note("err", escapeAttr(e.message)); return; }
    }

    try {
      if (wantNotes) await streamNotes(url, target);
      else await streamSummary(url, requestedMode, target);
    } catch (e) {
      status("");
      if (e.status === 402) {
        const ent = e.entitlement;
        paintChip(ent);
        note("warn",
          `<b>${escapeAttr(e.message)}</b><br>` +
          `Your free videos are used up. <button class="linkbtn" onclick="document.getElementById('accountBtn').click()">Subscribe for $5/month →</button>`);
      } else if (e.status === 401) {
        openAuth("login");
      } else if (e.status === 422) {
        note("err", escapeAttr(e.message));
      } else {
        note("err", escapeAttr(e.message || "Something went wrong."));
      }
    } finally {
      setBusy(false);
      refreshEntitlement();
    }
  }

  /** Read an NDJSON stream, calling onEvent for each line as it arrives. */
  // --- Extension bridge ----------------------------------------------------
  // When the TubeNotes extension is installed it can read the transcript from
  // this user's own browser, so YouTube sees a person rather than our server.
  // Free, unlimited, never rate-limited. With no extension we send nothing and
  // the server fetches it the old way - so nothing here can break a visitor
  // who does not have it.
  let extReady = false;
  const extWaiters = new Map();

  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d.source !== "tubenotes-ext") return;
    if (d.type === "READY") { extReady = true; return; }
    if (d.type === "TRANSCRIPT") {
      const done = extWaiters.get(d.reqId);
      if (done) { extWaiters.delete(d.reqId); done(d); }
    }
  });

  // The content script may have loaded before this file; a ping makes sure we
  // hear its READY either way.
  try { window.postMessage({ source: "tubenotes-page", type: "PING" }, window.location.origin); } catch (_) {}

  function extAsk(videoId, ms) {
    return new Promise((resolve) => {
      const reqId = "r" + Math.random().toString(36).slice(2);
      const timer = setTimeout(() => { extWaiters.delete(reqId); resolve(null); }, ms);
      extWaiters.set(reqId, (d) => { clearTimeout(timer); resolve(d && d.ok ? d : null); });
      window.postMessage(
        { source: "tubenotes-page", type: "GET_TRANSCRIPT", videoId, reqId },
        window.location.origin
      );
    });
  }

  function videoIdFrom(input) {
    const v = (input || "").trim();
    if (/^[A-Za-z0-9_-]{11}$/.test(v)) return v;
    try {
      const u = new URL(v.includes("://") ? v : "https://" + v);
      const host = u.hostname.replace(/^www\.|^m\./, "");
      if (host === "youtu.be") return u.pathname.slice(1).split("/")[0] || null;
      if (u.pathname === "/watch") return u.searchParams.get("v");
      const parts = u.pathname.split("/").filter(Boolean);
      if (parts.length >= 2 && ["shorts", "embed", "live", "v"].includes(parts[0])) return parts[1];
    } catch (_) {}
    return null;
  }

  async function extraFromExtension(url) {
    // BAND. Ye pul extension se transcript maangta tha aur 30 second tak uska
    // intezaar karta tha. Wo raasta kabhi kaam nahi kiya: YouTube ab
    // api/timedtext par HTTP 200 ke saath KHAALI body lautata hai - server se,
    // extension ke service worker se, aur khud YouTube ke page ke andar se bhi.
    //
    // Jab tak extension lagi nahi thi, extReady false rehta tha aur ye chupchap
    // lautta tha. Extension lagte hi wo READY bhejne lagi aur har summary se 21
    // second cheen liye. Naapa gaya, ek hi video par:
    //     extension lagi hui  ->  Queued at 21,300 ms
    //     incognito (band)    ->  Queued at    293 ms
    //
    // Backend ka `transcript` field waise hi hai - wo nuksaan nahi karta aur
    // kabhi mobile app banane par wahi raasta kaam aayega.
    return {};
  }

  async function streamNdjson(path, body, onEvent) {
    const res = await api(path, { method: "POST", body, raw: true });
    if (!res.ok) {
      const text = await res.text();
      let json = null; try { json = JSON.parse(text); } catch (_) {}
      if (res.status === 401) tokens.clear();
      throw err(res.status, json ? json.detail : text);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try { onEvent(JSON.parse(line)); } catch (_) {}
      }
    }
  }

  async function streamSummary(url, m, target) {
    let text = "";
    let failed = null;
    let langOut = null;
    const body = $("rBody");

    const extra = await extraFromExtension(url);
    await streamNdjson("/summarize", { url, device: device(), mode: m, target_lang: target || null, ...extra }, (ev) => {
      if (ev.type === "meta") {
        paintChip(ev.entitlement);
        langOut = ev.language;
        lastNotes = { videoId: ev.video.video_id, title: ev.video.title, url: ev.video.url, markdown: "", lang: ev.language };
        const translated = ev.detected_language && ev.detected_language !== ev.language;
        $("rMeta").innerHTML =
          (ev.video.author ? `<span class="tag">${escapeAttr(ev.video.author)}</span>` : "") +
          `<span class="tag" id="rLangTag">🌐 ${escapeAttr(ev.language_name)}</span>` +
          (translated ? `<span class="tag">video: ${escapeAttr(ev.detected_language_name)}</span>` : "") +
          `<span class="tag">${(ev.transcript_chars / 1000).toFixed(1)}k chars</span>`;
        status("Writing the summary…", true);
      } else if (ev.type === "delta") {
        text += ev.text;
        body.innerHTML = md2html(text) + '<span class="cursor"></span>';
      } else if (ev.type === "status") {
        status(ev.message, true);
      } else if (ev.type === "done") {
        text = ev.text || text;
        body.innerHTML = md2html(text);
        if (ev.language) langOut = ev.language;
        if (ev.partial) note("warn", "The model stopped early — this is what it produced.");
      } else if (ev.type === "error") {
        failed = ev.message;
      }
    });

    status("");
    if (failed) { note("err", escapeAttr(failed)); return; }
    if (lastNotes) { lastNotes.markdown = text; lastNotes.lang = langOut; }
    finishTools(text);
  }

  async function streamNotes(url, target) {
    let text = "";
    let failed = null;
    let langOut = null;
    let partsDone = 0;
    const warnings = [];
    $("rProgress").classList.remove("hidden");
    status("Reading the whole video…", true);

    const extra = await extraFromExtension(url);
    await streamNdjson("/notes", { url, device: device(), target_lang: target || null, ...extra }, (ev) => {
      if (ev.type === "meta") {
        langOut = ev.language;
        const translated = ev.detected_language && ev.detected_language !== ev.language;
        $("rMeta").innerHTML =
          (ev.video.author ? `<span class="tag">${escapeAttr(ev.video.author)}</span>` : "") +
          `<span class="tag" id="rLangTag">🌐 ${escapeAttr(ev.language_name)}</span>` +
          (translated ? `<span class="tag">video: ${escapeAttr(ev.detected_language_name)}</span>` : "");
        lastNotes = { videoId: ev.video.video_id, title: ev.video.title, url: ev.video.url, markdown: "", lang: ev.language };
      } else if (ev.type === "status") {
        status(ev.message, true);
      } else if (ev.type === "warning") {
        warnings.push(ev.message);
        note("warn", warnings.map(escapeAttr).join("<br>"));
      } else if (ev.type === "progress") {
        partsDone = ev.total;
        $("rBar").style.width = ev.percent + "%";
        status(`Writing detailed notes — part ${ev.done} of ${ev.total}…`, true);
      } else if (ev.type === "done") {
        text = ev.text || "";
        $("rBody").innerHTML = md2html(text);
      } else if (ev.type === "error") {
        failed = ev.message;
      }
    });

    $("rProgress").classList.add("hidden");
    if (failed) { status(""); note("err", escapeAttr(failed)); return; }
    if (lastNotes) { lastNotes.markdown = text; lastNotes.lang = langOut; }

    status(`All ${partsDone || "?"} parts written — building the PDF…`, true);
    finishTools(text, true);
    // Only now, with every part written, do we open the print dialog.
    openPrintView();
    status("");
  }

  /** Convert the summary that is already on screen into another language. */
  async function translateTo(code) {
    if (!lastNotes || !lastNotes.markdown || !code) return;
    const sel = $("rLang");
    if (sel) sel.disabled = true;
    status("Translating…", true);
    note("", "");
    try {
      const res = await api("/translate", {
        method: "POST",
        body: { text: lastNotes.markdown, target_lang: code },
      });
      lastNotes.markdown = res.text;
      lastNotes.lang = res.target_lang;
      $("rBody").innerHTML = md2html(res.text);
      const tag = $("rLangTag");
      if (tag) tag.textContent = `🌐 ${res.language_name}`;
      status("");
      // The PDF is built from lastNotes.markdown, so it now follows too.
    } catch (e) {
      status("");
      note("err", e.status === 503
        ? "Translation service is unreachable right now."
        : escapeAttr(e.message || "Translation failed."));
    } finally {
      if (sel) { sel.disabled = false; sel.value = ""; }
    }
  }

  function finishTools(text, isNotes) {
    tools([
      { label: "Copy", icon: ICONS.copy, tone: "t-copy", onClick: () => {
          navigator.clipboard.writeText(text).then(() => status("Copied to clipboard"));
        } },
      // Always print what is on screen. Earlier this regenerated the notes,
      // which silently threw away a translation the user had just applied.
      // Ask for the language first, then print. Earlier this printed straight
      // away, so there was no way to get the PDF in another language.
      { label: "Download PDF", icon: ICONS.pdf, tone: "t-pdf",
        onClick: () => pdfFlow({ full: false }) },
      ...(isNotes ? [] : [{ label: "Full notes → PDF", icon: ICONS.notes, tone: "t-notes",
                            onClick: () => pdfFlow({ full: true }) }]),
      { label: "Download .md", icon: ICONS.md, tone: "t-md",
        onClick: () => downloadMd(lastNotes ? lastNotes.markdown : text) },
      { label: "Open on YouTube", icon: ICONS.open, tone: "t-youtube", onClick: () => {
          if (lastNotes) window.open(lastNotes.url, "_blank", "noopener");
        } },
    ]);

    // Translate picker — applies to what is on screen AND to the PDF, since the
    // PDF is rendered from the same markdown.
    const sel = document.createElement("select");
    sel.className = "tool t-lang";
    sel.id = "rLang";
    sel.title = "Translate this summary (the PDF follows too)";
    fillLangSelect(sel, { includeAuto: false });
    sel.addEventListener("change", () => translateTo(sel.value));
    $("rTools").appendChild(sel);
  }

  function downloadMd(text) {
    if (!lastNotes) return;
    text = text || lastNotes.markdown;
    const md = `# ${lastNotes.title}\n\nSource: ${lastNotes.url}\n\n---\n\n${text}`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown;charset=utf-8" }));
    a.download = (lastNotes.title.replace(/[\\/:*?"<>|]/g, "").trim().slice(0, 90) || "summary") + ".md";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  /**
   * PDF = a print-ready page + the browser's "Save as PDF".
   * Deliberate: server-side PDF engines need Devanagari/Tamil font packaging to
   * avoid tofu boxes, while the browser already has those fonts and renders the
   * page perfectly.
   */
  function buildPrintHtml(standalone) {
    if (!lastNotes || !lastNotes.markdown) return null;
    let content = lastNotes.markdown;
    let heading = lastNotes.title;
    const m = content.match(/^\s*#\s+(.+)\s*(?:\n|$)/);
    if (m) { heading = m[1].trim(); content = content.slice(m[0].length); }

    return `<!doctype html><html lang="${escapeAttr(lastNotes.lang || "en")}"><head><meta charset="utf-8">
<title>${escapeAttr(heading)}</title>
<style>
:root{--ink:#1a1a1a;--muted:#6b7280;--line:#e5e7eb;
--c1:#4f46e5;--c1bg:#eef0ff;--c2:#0f9d58;--c2bg:#e7f6ee;--c3:#d97706;--c3bg:#fdf1de;
--c4:#db2777;--c4bg:#fce8f1;--c5:#0284c7;--c5bg:#e2f2fb}
*{box-sizing:border-box}
body{font-family:"Segoe UI","Noto Sans","Noto Sans Devanagari","Noto Sans Tamil","Nirmala UI",Arial,sans-serif;
max-width:780px;margin:0 auto 60px;padding:0 26px;color:var(--ink);line-height:1.85;font-size:16px}
.cover{margin:0 -26px 28px;padding:32px 34px 24px;
background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 50%,#db2777 100%);color:#fff;border-radius:0 0 20px 20px}
.cover .kicker{font-size:11px;letter-spacing:.14em;text-transform:uppercase;opacity:.9;font-weight:700;margin-bottom:10px}
.cover h1{font-size:25px;line-height:1.3;margin:0 0 14px;font-weight:800}
.cover .src{font-size:12px;word-break:break-all;background:rgba(255,255,255,.16);padding:7px 12px;border-radius:8px;display:inline-block;max-width:100%}
.cover .src a{color:#fff;text-decoration:none}
h2{font-size:19px;font-weight:800;margin:30px 0 12px;padding:11px 16px;border-radius:10px;
border-left:6px solid var(--c1);background:var(--c1bg)}
h2:nth-of-type(5n+2){border-left-color:var(--c2);background:var(--c2bg)}
h2:nth-of-type(5n+3){border-left-color:var(--c3);background:var(--c3bg)}
h2:nth-of-type(5n+4){border-left-color:var(--c4);background:var(--c4bg)}
h2:nth-of-type(5n+5){border-left-color:var(--c5);background:var(--c5bg)}
h3{font-size:16px;font-weight:700;margin:18px 0 6px;color:#374151}
p{margin:9px 0;text-align:justify}strong{color:#000;font-weight:700}
ul,ol{margin:8px 0 12px;padding-left:4px}
li{margin:6px 0;list-style:none;padding-left:24px;position:relative}
ul li::before{content:"";position:absolute;left:6px;top:10px;width:7px;height:7px;border-radius:50%;background:#4f46e5}
ol{counter-reset:item}ol li{counter-increment:item}
ol li::before{content:counter(item);position:absolute;left:0;top:2px;width:19px;height:19px;border-radius:50%;
background:#4f46e5;color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center}
.footer{margin-top:40px;padding-top:14px;border-top:2px solid var(--line);font-size:11px;color:var(--muted);text-align:center}
.hint{background:#fffbeb;border:1px solid #fcd34d;padding:11px 15px;border-radius:10px;font-size:13px;margin:18px 0 4px;color:#92400e}
@media print{.hint{display:none}body{margin:0;max-width:100%}.cover{border-radius:0}
*{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important}}
</style></head><body>
${standalone ? '<div class="hint">In the print dialog choose <b>Destination: Save as PDF</b>, then <b>Save</b>. Keep <b>More settings \u2192 Background graphics</b> ON so the colours print.</div>' : ""}
<div class="cover"><div class="kicker">\u2728 VIDEO NOTES</div>
<h1>${escapeAttr(heading)}</h1>
<div class="src">\ud83d\udd17 <a href="${escapeAttr(lastNotes.url)}">${escapeAttr(lastNotes.url)}</a></div></div>
${md2html(content)}
<div class="footer">Generated by TubeNotes</div>
${standalone ? '<scr' + 'ipt>setTimeout(function(){window.print()},450)</scr' + 'ipt>' : ""}
</body></html>`;
  }

  /**
   * Show the print dialog WITHOUT opening a popup window.
   *
   * window.open() is only permitted while a user gesture is still "live".
   * Generating full notes takes minutes, so by the time the document is ready
   * the gesture has long expired and Chrome blocks the window - which is the
   * "browser blocked the popup" message people were hitting. Printing from a
   * hidden same-origin iframe needs no popup permission at all, so it works
   * however long the generation took.
   */
  function openPrintView() {
    const html = buildPrintHtml(false);
    if (!html) return;

    const old = document.getElementById("tnPrintFrame");
    if (old) old.remove();

    const frame = document.createElement("iframe");
    frame.id = "tnPrintFrame";
    frame.setAttribute("aria-hidden", "true");
    frame.style.cssText =
      "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
    frame.srcdoc = html;                       // same-origin, so we may print it
    frame.onload = () => {
      // A beat for fonts (Devanagari, Tamil…) to lay out before the dialog.
      setTimeout(() => {
        try {
          frame.contentWindow.focus();
          frame.contentWindow.print();
          showPrintReady(false);
        } catch (e) {
          showPrintReady(true);
        }
      }, 350);
    };
    document.body.appendChild(frame);
  }

  /**
   * Shown once the document is fully built and the print dialog has been
   * triggered. The button is always present rather than only on failure -
   * there is no reliable way to detect that the dialog actually opened, and a
   * visible "Save as PDF" is more useful than a question about whether it did.
   */
  function showPrintReady(failed) {
    // Its own slot: a missing-section warning from the notes run must stay
    // visible next to this, not be overwritten by it.
    const el = $("rPrint");
    if (!el) return;
    const msg = failed
      ? "The document is ready, but the print dialog didn't open by itself."
      : "Ready — choose <b>Destination: Save as PDF</b> in the print dialog.";
    el.innerHTML =
      `<div class="note ${failed ? "warn" : "ok"}">` +
      `<b>${failed ? "\u26a0" : "\u2705"}</b> ${msg} ` +
      `<button class="linkbtn" id="tnPrintAgain">Open print view</button></div>`;
    const btn = $("tnPrintAgain");
    if (btn) btn.onclick = openPrintTab;      // a real click, so this is allowed
  }

  /** Opens the document in a normal tab. Safe: called straight from a click. */
  function openPrintTab() {
    const html = buildPrintHtml(true);   // with the hint + auto print dialog
    if (!html) return;
    const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }


  // =====================================================================
  // Language picker — shown when a PDF button is clicked, so the user
  // chooses the language of the document they are about to get.
  // =====================================================================
  const LANG_BY_CODE = Object.fromEntries(LANGS);

  function pickLanguage({ title, lead, firstOption }) {
    return new Promise((resolve) => {
      const overlay = $("langPicker");
      const list = $("pickList");
      const search = $("pickSearch");
      $("pickTitle").textContent = title;
      $("pickLead").textContent = lead;
      search.value = "";

      const close = (value) => {
        overlay.classList.add("hidden");
        overlay.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKey);
        resolve(value);
      };
      const onBackdrop = (e) => { if (e.target === overlay) close(null); };
      const onKey = (e) => { if (e.key === "Escape") close(null); };

      function render(filter) {
        const q = (filter || "").trim().toLowerCase();
        list.innerHTML = "";

        if (firstOption && !q) {
          const b = document.createElement("button");
          b.className = "pick-item suggested";
          b.innerHTML = `<span>${escapeAttr(firstOption.label)}</span>`;
          b.onclick = () => close(firstOption.value);
          list.appendChild(b);
        }

        const rows = LANGS.filter(([code, name]) =>
          !q || name.toLowerCase().includes(q) || code.includes(q)
        );
        if (!rows.length) {
          list.innerHTML = '<div class="pick-empty">No language matches that.</div>';
          return;
        }
        for (const [code, name] of rows) {
          const b = document.createElement("button");
          b.className = "pick-item";
          b.innerHTML = `<span>${escapeAttr(name)}</span><span class="code">${code}</span>`;
          b.onclick = () => close(code);
          list.appendChild(b);
        }
      }

      search.oninput = () => render(search.value);
      $("pickClose").onclick = () => close(null);
      overlay.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKey);

      render("");
      overlay.classList.remove("hidden");
      setTimeout(() => search.focus(), 40);
    });
  }

  /** Build the PDF the user asked for, in the language they just picked. */
  async function pdfFlow({ full }) {
    const url = $("url").value.trim();
    const haveCurrent =
      lastNotes && lastNotes.markdown && url.includes(lastNotes.videoId);

    // With nothing on screen there is nothing to print, so we generate the
    // full notes either way - say so, rather than promising a quick PDF.
    const willGenerate = full || !haveCurrent;

    const firstOption = willGenerate
      ? { value: "auto", label: "Same as the video" }
      : { value: "", label: `Keep current — ${LANG_BY_CODE[lastNotes.lang] || lastNotes.lang || "as shown"}` };

    const choice = await pickLanguage({
      title: willGenerate ? "Full notes PDF — language" : "PDF language",
      lead: willGenerate
        ? "Reads the whole video and writes complete notes in this language. Long videos take a few minutes."
        : "The PDF will be written in this language.",
      firstOption,
    });
    if (choice === null) return;          // cancelled

    if (willGenerate) {
      await run("notes", choice === "" ? null : choice);
      return;
    }

    // A summary is already on screen: keep it, translating only if needed.
    if (choice && choice !== "auto" && choice !== lastNotes.lang) {
      await translateTo(choice);
    }
    openPrintView();
  }

  // =====================================================================
  // Auth UI
  // =====================================================================
  let authMode = "login";

  function openAuth(which, lead) {
    $("authModal").classList.remove("hidden");
    $("authPane").classList.remove("hidden");
    $("acctPane").classList.add("hidden");
    setAuthMode(which === "signup" ? "signup" : "login");
    if (lead) $("authLead").textContent = lead;
  }

  function setAuthMode(m) {
    authMode = m;
    const up = m === "signup";
    $("tabIn").classList.toggle("on", !up);
    $("tabUp").classList.toggle("on", up);
    $("nameWrap").classList.toggle("hidden", !up);
    $("authTitle").textContent = up ? "Create your account" : "Welcome back";
    $("authLead").textContent = up
      ? "5 free videos, no card needed."
      : "Sign in to keep your credits and history.";
    $("authSubmit").textContent = up ? "Create account" : "Sign in";
    $("password").setAttribute("autocomplete", up ? "new-password" : "current-password");
    $("authMsg").textContent = "";
  }

  async function openAccount() {
    $("authModal").classList.remove("hidden");
    $("authPane").classList.add("hidden");
    $("acctPane").classList.remove("hidden");
    $("acctMsg").textContent = "";
    try {
      const [user, ent] = await Promise.all([api("/auth/me"), api("/entitlement/check", { method: "POST", body: { device: device() } })]);
      $("acctEmail").textContent = user.email;
      paintChip(ent);
      const pro = ent.plan === "subscription";
      const left = Math.min(ent.trials_remaining, ent.device_trials_remaining);
      $("acctStatus").textContent = pro
        ? "Subscription active"
        : left > 0 ? `${left} of ${ent.trials_limit} free videos left` : "Free videos used up";
      $("acctMeter").style.width = pro ? "100%" : `${((ent.trials_limit - left) / ent.trials_limit) * 100}%`;
      $("acctDetail").textContent = pro
        ? (ent.current_period_end ? "Renews " + new Date(ent.current_period_end).toLocaleDateString() : "Billed $5/month")
        : "One video = one credit. Re-running a video you already did is free.";
      $("upgradeBtn").classList.toggle("hidden", pro);
    } catch (e) {
      if (e.status === 401) { tokens.clear(); openAuth("login"); return; }
      $("acctMsg").textContent = "Can't reach the server.";
    }
  }

  function closeModal() { $("authModal").classList.add("hidden"); }

  // =====================================================================
  // Wiring
  // =====================================================================
  $("pills").addEventListener("click", (e) => {
    const b = e.target.closest(".pill");
    if (!b) return;
    [...$("pills").children].forEach((p) => p.classList.remove("on"));
    b.classList.add("on");
    mode = b.dataset.mode;
    $("heroHint").textContent = {
      summary: "A structured ~400-word summary with sections and takeaways.",
      key_points: "Just the main points, numbered, in the order they're discussed.",
      notes: "Reads the ENTIRE transcript, part by part. Nothing is dropped — a long lecture can run to dozens of pages.",
      transcript: "The raw transcript is shown with the summary — pick another mode to generate.",
    }[mode];
    if (mode === "transcript") { mode = "summary"; }
  });

  $("goBtn").onclick = () => run(mode);
  $("summaryBtn").onclick = () => run(mode === "notes" ? "summary" : mode);
  $("pdfBtn").onclick = () => pdfFlow({ full: mode === "notes" });
  $("url").addEventListener("keydown", (e) => { if (e.key === "Enter") run(mode); });

  $("pasteBtn").onclick = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) { $("url").value = text.trim(); $("url").focus(); }
    } catch (_) {
      $("url").focus();
      note("", "");
    }
  };

  $("themeBtn").onclick = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    $("themeBtn").textContent = dark ? "🌙" : "☀️";
    try { localStorage.setItem("tn_theme", dark ? "light" : "dark"); } catch (_) {}
  };

  $("accountBtn").onclick = () => (signedIn() ? openAccount() : openAuth("login"));
  $("tabIn").onclick = () => setAuthMode("login");
  $("tabUp").onclick = () => setAuthMode("signup");
  $("closeAuth").onclick = closeModal;
  $("closeAcct").onclick = closeModal;
  $("authModal").addEventListener("click", (e) => { if (e.target === $("authModal")) closeModal(); });

  $("authForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("authSubmit"), msg = $("authMsg");
    msg.textContent = ""; msg.className = "msg"; btn.disabled = true;
    try {
      const body = {
        email: $("email").value.trim(),
        password: $("password").value,
        device: device(),
      };
      if (authMode === "signup") body.full_name = $("fullName").value.trim() || null;
      const data = await api(authMode === "signup" ? "/auth/signup" : "/auth/login",
        { auth: false, method: "POST", body });
      tokens.set(data.tokens);
      paintChip(data.entitlement);
      closeModal();
      if ($("url").value.trim()) run(mode);
    } catch (e2) {
      msg.textContent = e2.status === 429
        ? "Too many attempts. Please wait a few minutes."
        : (e2.message || "Something went wrong.");
    } finally { btn.disabled = false; }
  });

  $("forgotBtn").onclick = async () => {
    const email = $("email").value.trim();
    const msg = $("authMsg");
    if (!email) { msg.textContent = "Enter your email first."; return; }
    try {
      const r = await api("/auth/password/forgot", { auth: false, method: "POST", body: { email } });
      msg.textContent = r.detail; msg.className = "msg ok";
    } catch (_) { msg.textContent = "Couldn't send the reset link."; }
  };

  $("signoutBtn").onclick = async () => {
    const t = tokens.get();
    try { if (t) await api("/auth/logout", { method: "POST", body: { refresh_token: t.refresh_token } }); } catch (_) {}
    tokens.clear(); paintChip(null); closeModal();
  };

  $("upgradeBtn").onclick = async () => {
    $("upgradeBtn").disabled = true;
    try {
      const s = await api("/billing/checkout", { method: "POST", body: {} });
      window.open(s.checkout_url, "_blank", "noopener");
      $("acctMsg").textContent = "Finish the payment in the new tab, then reopen this panel.";
      $("acctMsg").className = "msg ok";
    } catch (e) {
      $("acctMsg").textContent = e.status === 409 ? "You already have an active subscription." : "Couldn't start checkout.";
      $("acctMsg").className = "msg";
    } finally { $("upgradeBtn").disabled = false; }
  };

  // ---- Google sign-in -------------------------------------------------
  // Server /meta se batata hai ki feature chaalu hai ya nahi. Band ho to
  // yahan se aage kuch hota hi nahi aur purana form jaisa tha waisa rehta hai.
  async function initGoogle() {
    let meta;
    try { meta = await fetch(API + "/meta").then((r) => r.json()); }
    catch (_) { return; }
    if (!meta.google_login || !meta.google_client_id) return;

    // Google ka script async load hota hai - taiyaar hone ka intezaar.
    for (let i = 0; i < 40 && !(window.google && google.accounts && google.accounts.id); i++) {
      await new Promise((r) => setTimeout(r, 150));
    }
    if (!(window.google && google.accounts && google.accounts.id)) return;

    google.accounts.id.initialize({
      client_id: meta.google_client_id,
      callback: onGoogleCredential,
      auto_select: false,
      cancel_on_tap_outside: true,
    });
    google.accounts.id.renderButton($("gsiButton"), {
      theme: document.documentElement.dataset.theme === "dark" ? "filled_black" : "outline",
      size: "large",
      width: 280,
      text: "continue_with",
      shape: "pill",
    });

    $("googleWrap").classList.remove("hidden");
    // Password wala form chhupa dete hain - link se kabhi bhi khul jaata hai.
    $("pwdWrap").classList.add("hidden");
  }

  async function onGoogleCredential(resp) {
    const msg = $("googleMsg");
    msg.textContent = "Signing you in\u2026";
    msg.className = "msg";
    try {
      const data = await api("/auth/google", {
        auth: false,
        method: "POST",
        body: { credential: resp.credential, device: device() },
      });
      tokens.set(data.tokens);
      paintChip(data.entitlement);
      closeModal();
      if ($("url").value.trim()) run(mode);
    } catch (e) {
      msg.textContent = e.status === 429
        ? "Too many attempts. Please wait a few minutes."
        : (e.message || "Google sign-in failed.");
      msg.className = "msg";
    }
  }

  $("showPwd").onclick = () => {
    $("pwdWrap").classList.toggle("hidden");
  };

  // ---- boot ----
  fillLangSelect($("outLang"), { includeAuto: true });
  initGoogle();
  try {
    const t = localStorage.getItem("tn_theme");
    if (t) { document.documentElement.dataset.theme = t; $("themeBtn").textContent = t === "dark" ? "☀️" : "🌙"; }
  } catch (_) {}
  refreshEntitlement();
})();
