/* ============================================================
   Find Your Mood — Anime Chat (live AniList edition)
   Vanilla JS. Talks to /api/mood-picks (see find_mood_route.py).
   ============================================================ */
(() => {
  "use strict";

  const MAX_MOODS = 3;
  const FAV_KEY = "fm_favs";
  const HIST_KEY = "fm_history";

  const MOODS = {
    happy:   { label: "Happy",        emoji: "😊", color: "#ffcb29", desc: "Feel-good adventures, wholesome friendships and pure joy." },
    sad:     { label: "Sad",          emoji: "😢", color: "#59a9ff", desc: "Emotional journeys and unforgettable endings that hit hard." },
    action:  { label: "Action",       emoji: "⚔️", color: "#ff5454", desc: "Epic battles, powerful heroes and nonstop excitement." },
    romance: { label: "Romance",      emoji: "💕", color: "#ff5db5", desc: "Sweet love stories, unforgettable couples and butterflies." },
    horror:  { label: "Horror",       emoji: "👻", color: "#945dff", desc: "Chills, thrills, monsters and dark, twisted worlds." },
    fantasy: { label: "Fantasy",      emoji: "🐉", color: "#42dcb4", desc: "Magic, dragons, kingdoms and adventures beyond imagination." },
    chill:   { label: "Relax",        emoji: "☕", color: "#6fd8ff", desc: "Slice-of-life comfort, peaceful scenery and warm stories." },
    mystery: { label: "Mystery",      emoji: "🕵️", color: "#7f83ff", desc: "Investigations, secrets, puzzles and brilliant twists." },
    comedy:  { label: "Comedy",       emoji: "😂", color: "#ffb431", desc: "Laugh-out-loud characters and hilarious unforgettable moments." },
    scifi:   { label: "Sci-Fi",       emoji: "🚀", color: "#2fe7ff", desc: "Time travel, futuristic cities, AI and space adventures." },
    sports:  { label: "Sports",       emoji: "🏐", color: "#49df82", desc: "Competition, teamwork and inspiring victories." },
    mind:    { label: "Mind-Bending", emoji: "🧠", color: "#ff55cb", desc: "Complex stories, psychological themes and unforgettable twists." },
  };
  const MOOD_IDS = Object.keys(MOODS);

  /* ---------- tiny helpers ---------- */

  const $ = (sel) => document.querySelector(sel);
  const num = (v) => Number.parseFloat(String(v ?? 0)) || 0;

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function hexToRgba(hex, alpha) {
    const h = hex.replace("#", "");
    const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
    const n = Number.parseInt(full, 16);
    if (Number.isNaN(n)) return `rgba(255,255,255,${alpha})`;
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
  }

  function readLocal(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch {
      return fallback;
    }
  }

  function writeLocal(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
  }

  function stars(rating) {
    const n = num(rating);
    if (!n) return `<span class="fm-stars">☆☆☆☆☆</span>`;
    const filled = Math.round(n);
    return `<span class="fm-stars">${"★".repeat(filled)}${"☆".repeat(Math.max(0, 5 - filled))}<b>${esc(rating)}</b></span>`;
  }

  function fallbackImage(title) {
    const letter = String(title || "A").trim().charAt(0).toUpperCase();
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="560"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b2f7a"/><stop offset="1" stop-color="#12224a"/></linearGradient></defs><rect width="400" height="560" fill="url(#g)"/><text x="200" y="300" font-family="sans-serif" font-size="140" font-weight="800" fill="#ffffff" text-anchor="middle" opacity="0.85">${letter}</text></svg>`;
    return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  }
  window.fmFallback = (img, title) => { img.onerror = null; img.src = fallbackImage(title); };

  let toastTimer;
  function toast(msg) {
    let el = document.getElementById("fm-toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "fm-toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  /* ---------- state ---------- */

  const state = {
    selected: [],
    favs: readLocal(FAV_KEY, {}),
    history: readLocal(HIST_KEY, []),
    query: "",
    genre: "all",
    sort: "rating",
    favOnly: false,
    picks: [],
    hero: null,
    source: "idle",
    note: "",
    loading: false,
    modal: null,
  };

  const els = {
    moodsGrid: $("#fm-moods-grid"),
    selected: $("#fm-selected"),
    history: $("#fm-history"),
    resultsGrid: $("#fm-results-grid"),
    sourceNote: $("#fm-source-note"),
    count: $("#fm-count"),
    search: $("#fm-search"),
    genre: $("#fm-genre"),
    sort: $("#fm-sort"),
    favToggle: $("#fm-fav-toggle"),
    heroPick: $("#fm-hero-pick"),
    modal: $("#fm-modal"),
    modalBody: $("#fm-modal-body"),
  };

  /* ---------- data ---------- */

  async function fetchPicks(moods) {
    const url = new URL("/api/mood-picks", window.location.origin);
    if (moods.length) url.searchParams.set("moods", moods.join(","));
    const res = await fetch(url.toString());
    if (!res.ok) throw new Error(`Request failed (${res.status})`);
    return res.json();
  }

  /* ---------- mood grid ---------- */

  function renderMoods() {
    els.moodsGrid.innerHTML = MOOD_IDS.map((id) => {
      const m = MOODS[id];
      const active = state.selected.includes(id);
      return `
        <button class="fm-mood-card${active ? " active" : ""}" type="button" data-mood="${id}"
          style="--mood-color:${m.color};--mood-bg:${hexToRgba(m.color, 0.15)};--mood-border:${hexToRgba(m.color, 0.4)};"
          aria-pressed="${active}">
          ${active ? `<span class="fm-mood-check">✓</span>` : ""}
          <span class="fm-mood-emoji">${m.emoji}</span>
          <span class="fm-mood-label">${m.label}</span>
          <span class="fm-mood-desc">${esc(m.desc)}</span>
          <span class="fm-mood-action">${active ? "Selected ✓" : "Select Mood"}</span>
        </button>`;
    }).join("");
  }

  function toggleMood(id) {
    const idx = state.selected.indexOf(id);
    if (idx !== -1) {
      state.selected.splice(idx, 1);
    } else {
      if (state.selected.length >= MAX_MOODS) {
        toast("You can blend up to 3 moods at a time 💫");
        return;
      }
      state.selected.push(id);
      state.favOnly = false;
      state.history = [id, ...state.history.filter((h) => h !== id)].slice(0, 6);
      writeLocal(HIST_KEY, state.history);
    }
    renderMoods();
    renderSelected();
    renderHistory();
    refreshPicks();
  }

  function surprise() {
    const pool = MOOD_IDS.filter((id) => !state.selected.includes(id));
    const pick = pool[Math.floor(Math.random() * pool.length)] ?? MOOD_IDS[0];
    if (state.selected.length >= MAX_MOODS) state.selected.shift();
    state.selected.push(pick);
    state.favOnly = false;
    state.history = [pick, ...state.history.filter((h) => h !== pick)].slice(0, 6);
    writeLocal(HIST_KEY, state.history);
    renderMoods();
    renderSelected();
    renderHistory();
    refreshPicks();
    toast(`${MOODS[pick].emoji} Feeling ${MOODS[pick].label} today?`);
  }

  function resetAll() {
    state.selected = [];
    state.query = "";
    state.genre = "all";
    state.sort = "rating";
    state.favOnly = false;
    els.search.value = "";
    els.genre.value = "all";
    els.sort.value = "rating";
    renderMoods();
    renderSelected();
    renderHistory();
    refreshPicks();
  }

  async function share() {
    const url = new URL(window.location.href);
    if (state.selected.length) url.searchParams.set("moods", state.selected.join(","));
    else url.searchParams.delete("moods");
    window.history.replaceState({}, "", url);
    try {
      await navigator.clipboard.writeText(url.toString());
      toast("Link copied — share your vibe! 🔗");
    } catch {
      window.prompt("Copy this link to share your mood picks:", url.toString());
    }
  }

  function renderSelected() {
    if (!state.selected.length) {
      els.selected.innerHTML = `<span class="fm-selected-empty">No mood selected — pick one above</span>`;
      return;
    }
    els.selected.innerHTML = state.selected.map((id) => {
      const m = MOODS[id];
      return `<button class="fm-chip" type="button" data-unselect="${id}"
        style="--chip-bg:${hexToRgba(m.color, 0.15)};--chip-border:${hexToRgba(m.color, 0.45)};">
        ${m.emoji} ${m.label} ✕</button>`;
    }).join("");
  }

  function renderHistory() {
    if (!state.history.length) {
      els.history.hidden = true;
      return;
    }
    els.history.hidden = false;
    els.history.innerHTML = `<span class="fm-history-label">Recently felt:</span>` +
      state.history.map((id) => {
        const m = MOODS[id];
        return `<button class="fm-chip" type="button" data-mood="${id}"
          style="--chip-bg:${hexToRgba(m.color, 0.1)};--chip-border:${hexToRgba(m.color, 0.4)};">
          ${m.emoji} ${m.label}</button>`;
      }).join("");
  }

  /* ---------- results ---------- */

  function visiblePicks() {
    const q = state.query.trim().toLowerCase();
    let out = state.favOnly
      ? Object.values(state.favs)
      : state.picks.slice();
    out = out.filter((a) => {
      if (state.genre !== "all" && !(a.g || "").includes(state.genre)) return false;
      if (q && !(`${a.t} ${a.g} ${a.d}`.toLowerCase().includes(q))) return false;
      return true;
    });
    const s = state.sort;
    out.sort((a, b) => {
      if (s === "title") return String(a.t).localeCompare(String(b.t));
      if (s === "newest") return num(b.y) - num(a.y);
      if (s === "episodes") return num(b.e) - num(a.e);
      return num(b.r) - num(a.r);
    });
    return out;
  }

  function cardHTML(a) {
    const genres = (a.g || "").split("•").map((x) => x.trim()).filter(Boolean);
    const fav = Boolean(state.favs[a.s]);
    const srcTag = a.src === "anilist"
      ? `<span class="fm-src-tag">🌐 AniList</span>`
      : `<span class="fm-src-tag">⭐ In your catalog</span>`;
    return `
      <article class="fm-card">
        <div class="fm-card-img-wrap">
          <img class="fm-card-img" src="${esc(a.i)}" alt="${esc(a.t)}" loading="lazy"
            onerror="fmFallback(this, '${esc(a.t)}')">
          <span class="fm-card-rating">★ ${esc(a.r)}</span>
          <button class="fm-card-fav${fav ? " on" : ""}" type="button" data-fav="${esc(a.s)}" aria-label="Favourite">${fav ? "♥" : "♡"}</button>
          ${srcTag}
        </div>
        <div class="fm-card-body">
          <h3 class="fm-card-title"><button type="button" data-open="${esc(a.s)}">${esc(a.t)}</button></h3>
          <div class="fm-card-genres">${genres.slice(0, 3).map((g) => `<span class="fm-badge">${esc(g)}</span>`).join("")}</div>
          <p class="fm-card-desc">${esc(a.d)}</p>
          <div class="fm-card-meta"><span>📺 ${esc(String(a.e))}</span><span>🗓️ ${esc(a.y || "—")}</span></div>
          <div class="fm-card-actions">
            <button class="fm-btn fm-btn-primary fm-btn-small" type="button" data-visit="${esc(a.s)}">View Anime</button>
            <button class="fm-btn fm-btn-small fm-btn-details" type="button" data-open="${esc(a.s)}">Details</button>
          </div>
        </div>
      </article>`;
  }

  function renderResults() {
    if (state.loading) {
      els.resultsGrid.innerHTML = `
        <div class="fm-state fm-loading">
          <div class="fm-loading-dots"><span></span><span></span><span></span></div>
          <p><b>Fetching real picks from AniList…</b></p>
        </div>`;
      els.sourceNote.hidden = true;
      els.count.hidden = true;
      return;
    }

    const list = visiblePicks();
    const showEmpty = !state.selected.length && !state.favOnly;

    if (showEmpty) {
      els.resultsGrid.innerHTML = `
        <div class="fm-state">
          <div class="fm-state-emoji">🌙</div>
          <h3>Your recommendations will appear here</h3>
          <p>Choose a mood above and we'll scan the live anime database for series that match how you're feeling.</p>
        </div>`;
    } else if (!list.length) {
      els.resultsGrid.innerHTML = `
        <div class="fm-state">
          <div class="fm-state-emoji">${state.favOnly ? "💜" : "🔍"}</div>
          <h3>Nothing found</h3>
          <p>${state.favOnly
            ? "You haven't saved any favourites yet — tap the ♥ on a card to keep it here."
            : "No anime match those filters. Try a different mood or clear the search."}</p>
        </div>`;
    } else {
      els.resultsGrid.innerHTML = list.map(cardHTML).join("");
    }

    // source note + count
    els.sourceNote.hidden = !state.selected.length || !state.note;
    els.sourceNote.innerHTML = state.note
      ? `<b>⚡</b><span>${esc(state.note)}</span>`
      : "";
    els.count.hidden = showEmpty;
    els.count.innerHTML = showEmpty
      ? ""
      : `<b>${list.length}</b> ${list.length === 1 ? "anime matches" : "anime match"} your vibe`;

    // rebuild genre filter options
    const allGenres = Array.from(
      new Set(Object.values(state.favs).concat(state.picks).flatMap((a) =>
        (a.g || "").split("•").map((x) => x.trim()).filter(Boolean),
      )),
    ).sort();
    const prev = els.genre.value;
    els.genre.innerHTML = `<option value="all">All genres</option>` +
      allGenres.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
    if (allGenres.includes(prev)) els.genre.value = prev;
    else { state.genre = "all"; els.genre.value = "all"; }
  }

  async function refreshPicks() {
    if (!state.selected.length) {
      state.picks = [];
      state.source = "idle";
      state.note = "";
      state.loading = false;
      renderResults();
      // fetch today's hero once
      if (!state.hero) {
        fetchPicks([])
          .then((json) => { state.hero = json.hero || null; renderHero(); })
          .catch((err) => {
            console.error("[find-mood] /api/mood-picks failed:", err);
            renderHeroError();
          });
      }
      return;
    }
    state.loading = true;
    renderResults();
    try {
      const json = await fetchPicks(state.selected);
      if (!state.hero && json.hero) { state.hero = json.hero; renderHero(); }
      state.picks = json.picks || [];
      state.source = json.source || "catalog";
      state.note = json.note || "";
    } catch (err) {
      console.error("[find-mood] fetch picks failed:", err);
      state.picks = [];
      state.source = "catalog";
      state.note = "Couldn't fetch recommendations — check the browser console (F12).";
    }
    state.loading = false;
    renderResults();
  }

  function renderHero() {
    const h = state.hero;
    if (!h) return;
    els.heroPick.innerHTML = `
      <img src="${esc(h.i)}" alt="${esc(h.t)}" onerror="fmFallback(this, '${esc(h.t)}')">
      <div class="fm-hero-pick-info">
        <p class="fm-hero-pick-title">${esc(h.t)}</p>
        <p class="fm-hero-pick-meta">${esc((h.g || "").split("•")[0].trim())} • ★ ${esc(h.r)}</p>
      </div>`;
  }

  function renderHeroError() {
    els.heroPick.innerHTML = `
      <div style="padding:1.2rem;color:#f87171;font-size:0.85rem;line-height:1.7">
        ⚠️ Couldn't reach <code>/api/mood-picks</code>.<br><br>
        Make sure you opened the page through your running Flask server
        (<b>http://127.0.0.1:5000/find-mood</b>), not by double-clicking the
        HTML file, and that you restarted <code>python app.py</code> after
        saving. Open the browser console (F12) for the exact error.
      </div>`;
  }

  /* ---------- favourites ---------- */

  function toggleFav(a) {
    const has = Boolean(state.favs[a.s]);
    if (has) delete state.favs[a.s];
    else state.favs[a.s] = a;
    writeLocal(FAV_KEY, state.favs);
    toast(has ? "Removed from favourites" : "Saved to favourites 💜");
    renderResults();
  }

  /* ---------- modal ---------- */

  function openModal(a) {
    state.modal = a;
    const genres = (a.g || "").split("•").map((x) => x.trim()).filter(Boolean);
    const fav = Boolean(state.favs[a.s]);
    const visitLabel = a.src === "catalog" ? "View on Anime Chat" : "View on AniList";
    els.modalBody.innerHTML = `
      <div class="fm-modal-grid">
        <img class="fm-modal-img" src="${esc(a.i)}" alt="${esc(a.t)}" onerror="fmFallback(this, '${esc(a.t)}')">
        <div>
          <h3 class="fm-modal-title">${esc(a.t)}</h3>
          <div class="fm-modal-genres">${genres.map((g) => `<span class="fm-badge">${esc(g)}</span>`).join("")}</div>
          <div class="fm-modal-meta">
            ${stars(a.r)}
            <span>📺 ${esc(String(a.e))} episodes</span>
            <span>🗓️ ${esc(a.y || "—")}</span>
          </div>
          <p class="fm-modal-desc">${esc(a.d)}</p>
          <div class="fm-modal-actions">
            <button class="fm-btn fm-btn-primary fm-btn-small fm-modal-save${fav ? " on" : ""}" type="button" id="fm-modal-fav">${fav ? "♥ Saved" : "♡ Save"}</button>
            <a class="fm-btn fm-btn-small fm-btn-details fm-modal-anilist" href="${a.src === "catalog" ? "/anime/" + encodeURIComponent(a.s) : "https://anilist.co/anime/" + encodeURIComponent(String(a.id || ""))}" target="${a.src === "catalog" ? "_self" : "_blank"}" rel="noopener">${visitLabel} ↗</a>
          </div>
        </div>
      </div>`;
    els.modal.hidden = false;
    document.body.style.overflow = "hidden";
    const favBtn = $("#fm-modal-fav");
    if (favBtn) favBtn.addEventListener("click", () => { toggleFav(a); openModal(a); });
  }

  function closeModal() {
    els.modal.hidden = true;
    state.modal = null;
    document.body.style.overflow = "";
  }

  function findPick(slug) {
    return state.picks.find((p) => p.s === slug) || state.favs[slug] || null;
  }

  /* ---------- events ---------- */

  function bindEvents() {
    els.moodsGrid.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-mood]");
      if (btn) toggleMood(btn.dataset.mood);
    });

    els.selected.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-unselect]");
      if (btn) toggleMood(btn.dataset.unselect);
    });

    els.history.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-mood]");
      if (btn) toggleMood(btn.dataset.mood);
    });

    els.resultsGrid.addEventListener("click", (e) => {
      const openBtn = e.target.closest("[data-open]");
      if (openBtn) { const a = findPick(openBtn.dataset.open); if (a) openModal(a); return; }
      const visitBtn = e.target.closest("[data-visit]");
      if (visitBtn) {
        const a = findPick(visitBtn.dataset.visit);
        if (a) {
          if (a.src === "catalog") window.location.href = "/anime/" + encodeURIComponent(a.s);
          else window.open("https://anilist.co/anime/" + encodeURIComponent(String(a.id || "")), "_blank", "noopener");
        }
        return;
      }
      const favBtn = e.target.closest("[data-fav]");
      if (favBtn) { const a = findPick(favBtn.dataset.fav); if (a) toggleFav(a); }
    });

    $("#fm-see-results").addEventListener("click", () => {
      document.getElementById("fm-results").scrollIntoView({ behavior: "smooth" });
    });
    $("#fm-surprise").addEventListener("click", surprise);
    $("#fm-share").addEventListener("click", share);
    $("#fm-reset").addEventListener("click", resetAll);

    els.search.addEventListener("input", (e) => { state.query = e.target.value; renderResults(); });
    els.genre.addEventListener("change", (e) => { state.genre = e.target.value; renderResults(); });
    els.sort.addEventListener("change", (e) => { state.sort = e.target.value; renderResults(); });
    els.favToggle.addEventListener("click", () => {
      state.favOnly = !state.favOnly;
      els.favToggle.classList.toggle("fm-fav-active", state.favOnly);
      renderResults();
    });

    $("#fm-modal-close").addEventListener("click", closeModal);
    $("#fm-modal-backdrop").addEventListener("click", closeModal);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  }

  /* ---------- scroll reveal ---------- */

  function initReveal() {
    const items = document.querySelectorAll(".fm-section");
    if (!("IntersectionObserver" in window)) {
      items.forEach((el) => el.classList.add("in"));
      return;
    }
    items.forEach((el) => el.classList.add("reveal"));
    const io = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) {
          en.target.classList.add("in");
          io.unobserve(en.target);
        }
      });
    }, { threshold: 0.08 });
    items.forEach((el) => io.observe(el));
  }

  /* ---------- init ---------- */

  function init() {
    const params = new URLSearchParams(window.location.search);
    state.selected = (params.get("moods") || "")
      .split(",").map((s) => s.trim())
      .filter((m) => MOODS[m])
      .slice(0, MAX_MOODS);
    renderMoods();
    renderSelected();
    renderHistory();
    renderHero();
    bindEvents();
    initReveal();
    refreshPicks();
  }

  init();
})();