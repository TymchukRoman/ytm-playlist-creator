/* YT Music Sorter: the one-page UI over the local JSON API.
 *
 * Song titles, artists, reasons and every message from the server are untrusted text:
 * they only ever reach the page through textContent / DOM building, never innerHTML.
 */
"use strict";

// Screenshots for the YouTube Music sign-in guide, keyed by the step's data-slot.
// Put the file in static/guide/ and list it here, e.g. "chrome-3": "chrome-3.png".
// Steps without an entry simply show no picture (no broken images, no 404s).
const GUIDE_IMAGES = {};

const POLL_MS = 1000;
const ACTIVE = new Set(["running", "applying"]);
const OFFLINE_MSG = "Can't reach the app. Make sure its window is still open.";
const MISSING_KEY = "ytsorter.missingPlaylists";

const $ = (id) => document.getElementById(id);

const state = {
  status: null,        // GET /api/status
  job: null,           // GET /api/job
  playlists: [],       // GET /api/playlists
  reconnecting: false, // showing the YouTube guide from a paused job
  reconnectedJob: null, // id of the paused job whose sign-in was just renewed
  view: null,
};

let pollTimer = null;
let review = null; // {key, data, selected:Set, built:{exclude:bool}}

// --- small helpers --------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function plural(n, one, many) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

function showMessage(id, text) {
  const node = $(id);
  node.textContent = text || "";
  node.hidden = !text;
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    const err = new Error(OFFLINE_MSG);
    err.offline = true;
    throw err;
  }
  setOffline(false);
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) {
    throw new Error((data && data.error) || `Something went wrong (error ${res.status}). Please try again.`);
  }
  return data;
}

function setOffline(offline) {
  $("offline").hidden = !offline;
}

/** Run `fn` with `button` disabled; show any error in the element `errorId`. */
async function act(button, errorId, fn) {
  if (button.disabled) return;
  button.disabled = true;
  if (errorId) showMessage(errorId, "");
  try {
    await fn();
  } catch (err) {
    if (err.offline) setOffline(true);
    if (errorId) showMessage(errorId, err.message);
  } finally {
    button.disabled = false;
    // Disabling the pressed button drops keyboard focus to <body>; give it back.
    if (document.activeElement === document.body && !button.closest("[hidden]")) button.focus();
    if (state.view === "review") updateApprove();
  }
}

// Playlists found missing on YouTube Music, remembered so their row can offer "Forget".
function loadMissing() {
  try { return new Set(JSON.parse(localStorage.getItem(MISSING_KEY) || "[]")); } catch { return new Set(); }
}
function saveMissing(set) {
  try { localStorage.setItem(MISSING_KEY, JSON.stringify([...set])); } catch { /* storage unavailable */ }
}
const missing = loadMissing();

// --- loading data ---------------------------------------------------------------------------

async function loadStatus(refresh) {
  state.status = await api(refresh ? "/api/status?refresh=1" : "/api/status");
  $("demo-banner").hidden = !state.status.demo;
}

async function loadPlaylists() {
  try {
    state.playlists = await api("/api/playlists");
    showMessage("playlists-error", "");
  } catch (err) {
    showMessage("playlists-error", err.message);
  }
}

async function loadJob() {
  state.job = await api("/api/job");
  const j = state.job;
  if (j && j.missing_playlist_id) {
    missing.add(j.missing_playlist_id);
    saveMissing(missing);
  }
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, POLL_MS);
}

/** Fetch the job and redraw; keep polling while it works on its own. */
async function poll() {
  clearTimeout(pollTimer);
  const wasActive = state.job && ACTIVE.has(state.job.status);
  try {
    await loadJob();
  } catch (err) {
    if (err.offline) setOffline(true);
    schedulePoll();
    return;
  }
  const active = state.job && ACTIVE.has(state.job.status);
  if (!active && (wasActive || !state.job)) await loadPlaylists();
  render();
  if (active) schedulePoll();
}

// --- view switching -------------------------------------------------------------------------

function setupNeeded() {
  const s = state.status;
  return !s.claude.installed || !s.claude.logged_in || !s.youtube.connected;
}

function pickView() {
  const j = state.job;
  if (!state.status) return "loading";
  if (j) {
    if (ACTIVE.has(j.status)) return "progress";
    if (j.status === "review") return "review";
    if (j.status === "paused" || j.status === "failed") return state.reconnecting ? "setup" : "stopped";
    if (j.status === "done") return "done";
  }
  return setupNeeded() ? "setup" : "main";
}

function render() {
  const view = pickView();
  const changed = view !== state.view;
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.id !== `view-${view}`;
  }
  state.view = view;
  const fresh = ({ setup: renderSetup, main: renderMain, progress: renderProgress, review: renderReview,
    stopped: renderStopped, done: renderDone })[view]?.();
  if (changed || fresh) { // a new screen, or the next playlist's review
    window.scrollTo(0, 0);
    const heading = document.querySelector(`#view-${view} h1`);
    if (heading && view !== "loading") heading.focus({ preventScroll: true });
  }
}

// --- setup ----------------------------------------------------------------------------------

function setCheck(id, ok, stateText) {
  const row = $(id);
  row.classList.toggle("is-ok", ok);
  row.classList.toggle("is-bad", !ok);
  row.querySelector(".mark").textContent = ok ? "✓" : "✗";
  row.querySelector(".check-state").textContent = stateText;
  row.querySelector(".check-body").hidden = ok;
}

function renderSetup() {
  const { claude, youtube } = state.status;
  const claudeOk = claude.installed && claude.logged_in;
  setCheck("check-claude", claudeOk, claudeOk ? "Ready" : claude.installed ? "Not logged in" : "Not installed");
  $("claude-message").textContent = claudeOk ? "" : claude.message;

  const ytOk = youtube.connected && !state.reconnecting;
  setCheck("check-youtube", ytOk, ytOk ? "Connected" : state.reconnecting ? "Sign-in expired" : "Not connected");
  $("youtube-heading").textContent = state.reconnecting ? "Reconnect YouTube Music" : "YouTube Music";
  $("setup-lead").textContent = state.reconnecting
    ? "Your YouTube Music sign-in expired. Copy the details again to reconnect, then resume."
    : "This app needs two things before it can make playlists for you.";
  $("check-claude").hidden = state.reconnecting;
  $("setup-back").hidden = !state.reconnecting;
}

function selectTab(name) {
  for (const browser of ["chrome", "firefox"]) {
    const on = browser === name;
    const tab = $(`tab-${browser}`);
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(`guide-${browser}`).hidden = !on;
  }
}

function setupGuideImages() {
  for (const step of document.querySelectorAll("[data-slot]")) {
    const file = GUIDE_IMAGES[step.dataset.slot];
    if (!file) continue;
    const img = el("img", "guide-img");
    img.alt = "";
    img.loading = "lazy";
    img.hidden = true;
    img.addEventListener("load", () => { img.hidden = false; });
    img.addEventListener("error", () => img.remove());
    img.src = `guide/${file}`;
    step.append(img);
  }
}

// --- main -----------------------------------------------------------------------------------

function formatDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function renderMain() {
  const list = $("playlist-list");
  const items = state.playlists;
  $("btn-update-all").hidden = items.length === 0;
  $("playlists-hint").hidden = items.length === 0;
  $("playlists-empty").hidden = items.length > 0;
  const frag = document.createDocumentFragment();
  for (const p of items) {
    const li = el("li", "playlist");
    const info = el("div", "playlist-info");
    info.append(el("h3", "playlist-name", p.name));
    if (p.description) info.append(el("p", "playlist-desc", p.description));
    const date = formatDate(p.updated_at);
    info.append(el("p", "playlist-meta", plural(p.song_count, "song", "songs") + (date ? `, updated ${date}` : "")));
    const actions = el("div", "playlist-actions");
    if (missing.has(p.playlist_id)) {
      info.append(el("p", "playlist-missing", "Not found on YouTube Music. It may have been deleted there."));
      const forget = el("button", "link-btn", "Forget");
      forget.type = "button";
      forget.setAttribute("aria-label", `Forget “${p.name}”`);
      forget.addEventListener("click", () => act(forget, "playlists-error", () => forgetPlaylist(p.playlist_id)));
      actions.append(forget);
    }
    const update = el("button", "btn btn-secondary", "Update");
    update.type = "button";
    update.setAttribute("aria-label", `Update “${p.name}”`);
    update.addEventListener("click", () => act(update, "playlists-error", async () => {
      missing.delete(p.playlist_id); // if it is still gone, the job reports it again
      saveMissing(missing);
      await api(`/api/playlists/${encodeURIComponent(p.playlist_id)}/update`, { method: "POST" });
      await poll();
    }));
    actions.append(update);
    li.append(info, actions);
    frag.append(li);
  }
  list.replaceChildren(frag);
}

async function forgetPlaylist(id) {
  await api(`/api/playlists/${encodeURIComponent(id)}`, { method: "DELETE" });
  missing.delete(id);
  saveMissing(missing);
  await loadPlaylists();
}

// --- progress -------------------------------------------------------------------------------

function jobTitle(j, verb) {
  if (j.targets.length > 1) return `${verb} all playlists`;
  const name = j.targets[0] ? j.targets[0].name : "";
  return `${verb} “${name}”`;
}

function renderProgress() {
  const j = state.job;
  const p = j.progress;
  $("progress-title").textContent = jobTitle(j, j.kind === "create" ? "Creating" : "Updating");
  $("progress-message").textContent = p.message;
  const bar = $("progress-bar");
  const determinate = p.current !== null && p.total;
  bar.classList.toggle("indeterminate", !determinate);
  if (determinate) {
    const pct = Math.min(100, Math.round((p.current / p.total) * 100));
    bar.firstElementChild.style.width = `${pct}%`;
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", String(p.total));
    bar.setAttribute("aria-valuenow", String(p.current));
  } else {
    bar.firstElementChild.style.width = "";
    bar.removeAttribute("aria-valuenow");
    bar.removeAttribute("aria-valuemax");
  }
}

// --- review ---------------------------------------------------------------------------------

const SECTIONS = { include: "group-include", unsure: "group-unsure", exclude: "group-exclude" };

function songRow(song, checked, showReason) {
  const li = el("li");
  const label = el("label", "song");
  const box = el("input");
  box.type = "checkbox";
  box.dataset.vid = song.video_id;
  box.checked = checked;
  const text = el("span", "song-text");
  text.append(el("span", "song-title", song.title || "Untitled"));
  const by = [song.artists.join(", "), song.album].filter(Boolean).join(" · ");
  if (by) text.append(el("span", "song-by", by));
  if (showReason && song.reason) text.append(el("span", "song-reason", song.reason));
  label.append(box, text);
  li.append(label);
  return li;
}

function fillSection(name) {
  const songs = review.data[name];
  const frag = document.createDocumentFragment();
  for (const s of songs) frag.append(songRow(s, review.selected.has(s.video_id), name !== "include"));
  $(SECTIONS[name]).querySelector(".songs").replaceChildren(frag);
  review.built[name] = true;
}

function buildReview(data, key) {
  review = { key, data, selected: new Set(data.include.map((s) => s.video_id)), built: {} };
  for (const name of Object.keys(SECTIONS)) {
    const group = $(SECTIONS[name]);
    group.querySelector(".count").textContent = `(${data[name].length})`;
    group.querySelector(".songs").replaceChildren();
    review.built[name] = false;
  }
  $("group-include").classList.toggle("is-empty", data.include.length === 0);
  $("group-unsure").hidden = data.unsure.length === 0;
  $("group-exclude").hidden = data.exclude.length === 0;
  $("group-exclude").open = false; // built when opened: it can hold thousands of songs
  fillSection("include");
  fillSection("unsure");
  showMessage("review-error", "");
}

function renderReview() {
  const j = state.job;
  const data = j.review;
  const key = `${j.id}:${data.index}:${data.target_name}`;
  const fresh = !review || review.key !== key;
  if (fresh) buildReview(data, key);
  $("review-title").textContent = `Review “${data.target_name}”`;
  const multi = j.targets.length > 1;
  $("review-step").hidden = !multi;
  $("review-step").textContent = `Playlist ${data.index} of ${data.count}`;
  showMessage("review-notice", j.notice);
  $("btn-skip").hidden = !multi;
  updateApprove();
  return fresh;
}

function updateApprove() {
  if (!review || !state.job) return;
  const n = review.selected.size;
  const create = state.job.kind === "create";
  const btn = $("btn-approve");
  btn.textContent = create ? `Create playlist with ${plural(n, "song", "songs")}` : `Add ${plural(n, "song", "songs")}`;
  if (create) btn.disabled = n === 0;
  $("review-zero-hint").hidden = create || n > 0;
}

function onReviewChange(event) {
  const box = event.target;
  if (!review || !box.matches("input[type=checkbox][data-vid]")) return;
  if (box.checked) review.selected.add(box.dataset.vid);
  else review.selected.delete(box.dataset.vid);
  updateApprove();
}

function onSelectAllNone(event) {
  const btn = event.target.closest("[data-select]");
  if (!btn || !review) return;
  const group = btn.closest(".group");
  const name = Object.keys(SECTIONS).find((k) => SECTIONS[k] === group.id);
  const on = btn.dataset.select === "all";
  for (const s of review.data[name]) {
    if (on) review.selected.add(s.video_id);
    else review.selected.delete(s.video_id);
  }
  for (const box of group.querySelectorAll("input[data-vid]")) box.checked = on;
  updateApprove();
}

// --- paused / failed / done -----------------------------------------------------------------

function isSignInError(text) {
  return /YouTube Music sign-in/i.test(text || "");
}

function renderStopped() {
  const j = state.job;
  const paused = j.status === "paused";
  $("stopped-title").textContent = paused ? "Paused" : "This stopped before it finished";
  $("stopped-error").textContent = j.error || "Something went wrong.";
  const reconnect = isSignInError(j.error) && state.reconnectedJob !== j.id;
  $("btn-reconnect").hidden = !reconnect;
  $("btn-forget-missing").hidden = !j.missing_playlist_id;
  // Resume is the main action unless something has to be fixed first.
  $("btn-resume").className = `btn ${reconnect || j.missing_playlist_id ? "btn-secondary" : "btn-primary"}`;
  $("stopped-hint").textContent = j.missing_playlist_id
    ? "Forget it to remove it from your list here. Nothing on YouTube Music changes."
    : "The work done so far is saved.";
}

function renderDone() {
  const j = state.job;
  const nothingDone = Boolean(j.notice) && j.progress.message === j.notice; // stopped before any review
  const approved = j.targets.filter((t) => t.approved).map((t) => `“${t.name}”`);
  let title, message;
  if (nothingDone) {
    title = "Nothing to add";
    message = j.notice || j.progress.message;
  } else if (j.kind === "create") {
    title = "Your playlist is ready";
    message = `${approved[0] || "Your playlist"} is now on YouTube Music. It's private, so only you can see it.`;
  } else {
    title = "All done";
    message = approved.length ? `Updated ${approved.join(", ")} on YouTube Music.` : "No playlists were changed.";
  }
  $("done-title").textContent = title;
  $("done-message").textContent = message;
  showMessage("done-notice", nothingDone ? "" : j.notice);
}

/** Discard the current job (or a finished one) and go back. */
async function cancelJob() {
  await api("/api/job/cancel", { method: "POST" });
  state.reconnecting = false;
  review = null;
  await poll();
}

// --- wiring ---------------------------------------------------------------------------------

function wire() {
  $("btn-check-again").addEventListener("click", (e) => act(e.currentTarget, null, async () => {
    $("claude-message").textContent = "Checking…";
    try { await loadStatus(true); } catch (err) { $("claude-message").textContent = err.message; return; }
    render();
  }));

  for (const browser of ["chrome", "firefox"]) $(`tab-${browser}`).addEventListener("click", () => selectTab(browser));
  document.querySelector(".tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const next = $("tab-chrome").getAttribute("aria-selected") === "true" ? "firefox" : "chrome";
    selectTab(next);
    $(`tab-${next}`).focus();
  });

  $("connect-form").addEventListener("submit", (e) => {
    e.preventDefault();
    act($("btn-connect"), "connect-error", async () => {
      if (!$("headers").value.trim()) {
        $("headers").focus();
        throw new Error("Paste the request headers from your browser first.");
      }
      await api("/api/youtube/connect", { method: "POST", body: { headers: $("headers").value } });
      $("headers").value = "";
      const wasReconnecting = state.reconnecting;
      if (wasReconnecting && state.job) state.reconnectedJob = state.job.id;
      state.reconnecting = false;
      await loadStatus(false);
      render();
      showMessage("stopped-ok", wasReconnecting ? "YouTube Music is connected again. Press Resume to continue." : "");
    });
  });

  $("btn-setup-back").addEventListener("click", () => { state.reconnecting = false; render(); });

  $("create-form").addEventListener("submit", (e) => {
    e.preventDefault();
    act($("btn-create"), "create-error", async () => {
      const description = $("desc").value.trim();
      const name = $("name").value.trim();
      if (!description) { $("desc").focus(); throw new Error("Describe which songs belong in the playlist."); }
      if (!name) { $("name").focus(); throw new Error("Give the playlist a name."); }
      await api("/api/playlists", { method: "POST", body: { name, description } });
      $("desc").value = "";
      $("name").value = "";
      await poll();
    });
  });

  $("btn-update-all").addEventListener("click", (e) => act(e.currentTarget, "playlists-error", async () => {
    await api("/api/update-all", { method: "POST" });
    await poll();
  }));

  $("btn-progress-cancel").addEventListener("click", (e) => act(e.currentTarget, "progress-error", cancelJob));

  const reviewView = $("view-review");
  reviewView.addEventListener("change", onReviewChange);
  reviewView.addEventListener("click", onSelectAllNone);
  $("group-exclude").addEventListener("toggle", () => {
    if ($("group-exclude").open && review && !review.built.exclude) fillSection("exclude");
  });
  $("btn-approve").addEventListener("click", (e) => act(e.currentTarget, "review-error", async () => {
    await api("/api/job/approve", { method: "POST", body: { video_ids: [...review.selected] } });
    await poll();
  }));
  $("btn-skip").addEventListener("click", (e) => act(e.currentTarget, "review-error", async () => {
    await api("/api/job/skip", { method: "POST" });
    await poll();
  }));
  $("btn-review-cancel").addEventListener("click", (e) => act(e.currentTarget, "review-error", cancelJob));

  $("btn-reconnect").addEventListener("click", () => {
    showMessage("stopped-ok", "");
    state.reconnecting = true;
    render();
  });
  $("btn-forget-missing").addEventListener("click", (e) => act(e.currentTarget, "stopped-action-error", async () => {
    await forgetPlaylist(state.job.missing_playlist_id);
    await cancelJob();
  }));
  $("btn-resume").addEventListener("click", (e) => act(e.currentTarget, "stopped-action-error", async () => {
    showMessage("stopped-ok", "");
    await api("/api/job/resume", { method: "POST" });
    state.reconnectedJob = null; // if sign-in fails again, offer Reconnect again
    await poll();
  }));
  $("btn-stopped-cancel").addEventListener("click", (e) => act(e.currentTarget, "stopped-action-error", async () => {
    showMessage("stopped-ok", "");
    await cancelJob();
  }));

  $("btn-done").addEventListener("click", (e) => act(e.currentTarget, "done-error", cancelJob));
}

async function start() {
  wire();
  setupGuideImages();
  try {
    await Promise.all([loadStatus(false), loadJob(), loadPlaylists()]);
  } catch (err) {
    if (err.offline) setOffline(true);
    const p = el("p", "error", err.message);
    $("view-loading").append(p);
    return;
  }
  render();
  if (state.job && ACTIVE.has(state.job.status)) schedulePoll();
}

start();
