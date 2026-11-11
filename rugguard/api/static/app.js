// RugGuard UI. Every value that comes from the chain (token IDs, names, evidence) is
// inserted with textContent, never innerHTML, so hostile token metadata cannot run here.
"use strict";

const ACCOUNT_ID = /^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$/;
const BAND_WORD = { low: "Low", medium: "Medium", high: "High", unknown: "Unknown" };
const SCORE_NAME = { upgrade_surface: "Who can change the code", concentration: "Who holds the supply" };
const OUTCOME_WORD = {
  rugged: "Rugged", faded: "Faded", alive: "Still trading", spoiled: "Never launched",
  pending: "Too early to judge", needs_review: "Needs review",
};
const LABELS = Object.keys(OUTCOME_WORD);

const $ = (id) => document.getElementById(id);

// The DOM replaceChildren method inserts null/undefined as the text "null"; every call goes through here.
function setChildren(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
}
let current = null;          // token on screen
let adminKey = "";           // kept in memory only
let watching = new Set();    // job ids started from this page
let pollTimer = null;

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null) continue;
    if (k === "text") node.textContent = v;
    else if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) node.append(c);
  return node;
}

async function api(path, opts = {}) {
  const headers = { Accept: "application/json", ...(opts.body ? { "Content-Type": "application/json" } : {}) };
  if (adminKey) headers.Authorization = `Bearer ${adminKey}`;
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : data.detail;
    const err = new Error(detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(typeof iso === "number" ? iso * 1000 : iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// ------------------------------------------------------------------ report
function verdict(name, band, reasons, source, notes) {
  return el("article", { class: "verdict", "data-band": band || "unknown" }, [
    el("div", { class: "verdict-head" }, [
      el("h2", { class: "verdict-name", text: name }),
      el("p", { class: "verdict-band", text: BAND_WORD[band] || "Unknown" }),
      el("p", { class: "verdict-source", text: source }),
    ]),
    el("ul", { class: "reasons" }, (reasons || []).map((r) => el("li", { text: r }))),
    notes && notes.length ? el("p", { class: "notes", text: notes.join(" ") }) : null,
  ]);
}

async function lookup(token) {
  token = token.trim();
  if (!ACCOUNT_ID.test(token) || token.length < 2 || token.length > 64) {
    showEmpty(`"${token}" is not a NEAR account ID. Use lowercase letters, digits, and - _ . between them.`);
    return;
  }
  current = token;
  $("token").value = token;
  if (location.hash.slice(1) !== token) history.replaceState(null, "", `#${token}`);
  $("empty").hidden = true;
  $("token-report").hidden = false;
  $("r-id").textContent = token;
  $("r-meta").textContent = "Checking…";
  setChildren($("r-verdicts"), );
  $("r-postmortem").hidden = true;
  $("action-note").textContent = "";

  const [stored, live] = await Promise.allSettled([
    api(`/api/tokens/${encodeURIComponent(token)}`),
    api(`/api/tokens/${encodeURIComponent(token)}/surface`),
  ]);
  if (current !== token) return;
  renderReport(token, stored.value, live.status === "fulfilled" ? live.value : null,
    live.status === "rejected" ? live.reason.message : null);
}

function renderReport(token, stored, live, liveError) {
  const meta = stored && stored.metadata;
  $("r-meta").textContent = meta && meta.name
    ? `${meta.name}${meta.symbol ? ` (${meta.symbol})` : ""}`
    : stored && stored.snapshot ? "" : "No snapshot collected yet";

  const blocks = [];
  if (live) {
    blocks.push(verdict(SCORE_NAME.upgrade_surface, live.band, live.reasons, "Checked just now",
      (live.facts && live.facts.notes) || []));
  } else {
    blocks.push(verdict(SCORE_NAME.upgrade_surface, "unknown",
      [liveError || "The live check could not reach NEAR."], "Live check failed"));
  }
  const scores = (stored && stored.scores) || [];
  const conc = scores.find((s) => s.kind === "concentration");
  if (conc) {
    blocks.push(verdict(SCORE_NAME.concentration, conc.band, conc.reasons,
      `From snapshot of ${when(stored.snapshot.taken_at)}`));
  } else {
    blocks.push(verdict(SCORE_NAME.concentration, "unknown",
      ["Not measured yet. Collect a full snapshot to see how the supply is spread."], "No snapshot"));
  }
  setChildren($("r-verdicts"), ...blocks);
  renderScore(scores.find((s) => s.kind === "total"));
  renderOutcome(stored);
}

const SIGNAL_NAME = {
  upgrade: "Code can be changed", lp_control: "A wallet controls the liquidity",
  concentration: "Supply in few wallets", liquidity: "Thin pool",
  early_buyers: "Early buyers linked to deployer", deployer: "Deployer still holds supply",
  owner_powers: "Owner powers in the code",
};
const SCORE_BAND = { low: "Low risk", elevated: "Elevated risk", high: "High risk", unknown: "Not enough data" };

function renderScore(total) {
  const box = $("r-score");
  if (!total) { box.hidden = true; return; }
  const comps = Object.entries(total.facts.components || {});
  const value = total.score === null ? "–" : Math.round(total.score);
  box.dataset.band = total.band;
  setChildren(box, 
    el("div", { class: "score-head" }, [
      el("p", { class: "score-value" }, [el("span", { class: "score-number", text: String(value) }),
        el("span", { class: "score-max", text: "/100" })]),
      el("div", {}, [
        el("p", { class: "score-band", text: SCORE_BAND[total.band] || total.band }),
        el("p", { class: "score-sub", text: `Using ${Math.round(total.facts.coverage * 100)}% of signals, from data up to `
          + `${total.facts.cut_hours} h after launch. Scored ${when(total.taken_at)}.` }),
      ]),
    ]),
    el("table", { class: "signals" }, [
      el("thead", {}, el("tr", {}, [el("th", { scope: "col", text: "Signal" }), el("th", { scope: "col", text: "Weight" }),
        el("th", { scope: "col", text: "Risk" }), el("th", { scope: "col", text: "Why" })])),
      el("tbody", {}, comps.map(([name, c]) => el("tr", { class: c.subscore === null ? "na" : "" }, [
        el("th", { scope: "row", text: SIGNAL_NAME[name] || name }),
        el("td", { class: "num", text: String(c.weight) }),
        el("td", { class: "num" }, c.subscore === null ? "not measured" : el("meter", {
          min: "0", max: "1", low: "0.3", high: "0.6", optimum: "0", value: String(c.subscore),
          "aria-label": `${Math.round(c.subscore * 100)}% risk` })),
        el("td", { text: c.reason }),
      ]))),
    ]),
    total.facts.note ? el("p", { class: "notes", text: total.facts.note }) : null,
  );
  box.hidden = false;
}

function renderOutcome(stored) {
  const o = stored && stored.outcome;
  if (!o) { $("r-postmortem").hidden = true; return; }
  const review = stored.review;
  // replaceChildren() turns a null argument into the text "null", so drop empties first.
  setChildren($("r-outcome"), [
    el("p", { class: "outcome-label", "data-label": o.label, text: OUTCOME_WORD[o.label] || o.label }),
    el("p", { class: "outcome-sub", text: `Rules ${o.rules_version}, run ${when(o.computed_at)}` }),
    el("ul", { class: "reasons" }, o.evidence.map((e) => el("li", { text: e }))),
    review ? el("p", { class: "reviewed",
      text: `Reviewer label: ${OUTCOME_WORD[review.label] || review.label}. ${review.reason}` }) : null,
  ]);
  $("r-postmortem").hidden = false;
}

function showEmpty(message) {
  current = null;
  $("token-report").hidden = true;
  $("empty").hidden = false;
  if (message) $("empty").querySelector(".hint").textContent = message;
}

// ------------------------------------------------------------------ jobs
async function startJob(kind) {
  if (!current) return;
  const btn = $(`btn-${kind}`);
  btn.disabled = true;
  try {
    const { job, created } = await api(`/api/tokens/${encodeURIComponent(current)}/${kind}`, { method: "POST" });
    watching.add(job.id);
    $("action-note").textContent = created
      ? `Queued as job ${job.id}. This page updates when it finishes; NearBlocks pacing can make it take minutes.`
      : `Already queued as job ${job.id}.`;
    schedulePoll(500);
  } catch (err) {
    $("action-note").textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

const JOB_WORD = { collect: "Snapshot", outcome: "Post-mortem", discover: "Pool scan", score: "Score" };
const STATE_WORD = { queued: "Waiting", running: "Running", done: "Done", failed: "Failed" };

async function refreshJobs() {
  let data;
  try { data = await api("/api/jobs"); } catch { return false; }
  setChildren($("jobs"), ...data.jobs.slice(0, 12).map((j) =>
    el("li", { "data-status": j.status }, [
      el("div", {}, [el("strong", { text: `${JOB_WORD[j.kind] || j.kind} ` }),
        el("span", { class: "job-target", text: j.kind === "discover" ? `newest ${j.target} pools` : j.target })]),
      el("div", { class: "job-state",
        text: `${STATE_WORD[j.status]}${j.error ? `: ${j.error}` : ""}${j.result && j.result.rejected ? `: ${j.result.rejected}` : ""}` }),
    ])));
  for (const j of data.jobs) {
    if (!watching.has(j.id) || (j.status !== "done" && j.status !== "failed")) continue;
    watching.delete(j.id);
    if (j.kind === "discover" && j.status === "done") renderDiscovered(j.result.tokens || []);
    else if (j.target === current) { $("action-note").textContent = `Job ${j.id} ${j.status}.`; lookup(current); }
    refreshOutcomes();
  }
  return data.jobs.some((j) => j.status === "queued" || j.status === "running");
}

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const busy = await refreshJobs();
    refreshLog();
    schedulePoll(busy || watching.size ? 2500 : 15000);
  }, delay);
}

async function refreshLog() {
  try {
    const { entries } = await api("/api/logs?n=40");
    $("activity-section").hidden = false;
    // Show what RugGuard did (source calls, jobs, reviews), not this page's own polling.
    const shown = entries.filter((e) => e.event !== "http" || e.status >= 400).slice(-25).reverse();
    setChildren($("log"), ...shown.map((e) => {
      const detail = e.event === "fetch" ? `${e.source}: ${e.what} → ${e.status ?? "no response"} in ${e.ms ?? "?"} ms`
        : e.event === "http" ? `${e.method} ${e.path} → ${e.status}`
        : e.event === "review" ? `review saved: ${e.token} as ${e.label}`
        : `${e.event.replace("job.", "job ")} ${e.job ?? ""} ${e.kind ?? ""} ${e.target ?? e.status ?? ""}`;
      return el("li", { "data-level": e.level, text: `${e.ts.slice(11, 19)} ${detail}` });
    }));
  } catch (err) {
    if (err.status === 401) $("activity-section").hidden = true;
  }
}

// ------------------------------------------------------------------ outcomes + review
async function refreshOutcomes() {
  let data;
  try { data = await api("/api/outcomes"); } catch { return; }
  setChildren($("outcomes"), ...data.rows.map((r) => {
    const match = !r.human_label ? "" : r.human_label === r.auto_label ? "Yes" : "No";
    return el("tr", {}, [
      el("td", {}, el("button", { type: "button", text: r.token_id,
        onclick: () => { lookup(r.token_id); openReview(r.token_id); window.scrollTo({ top: 0 }); } })),
      el("td", { text: OUTCOME_WORD[r.auto_label] || r.auto_label }),
      el("td", { text: r.human_label ? OUTCOME_WORD[r.human_label] : "Not reviewed" }),
      el("td", { class: match === "No" ? "no" : "", text: match }),
    ]);
  }));
  $("agreement").textContent = data.reviewed
    ? `${data.reviewed} reviewed. Agreement ${Math.round(data.agreement * 100)}%, Cohen's kappa ${data.kappa.toFixed(2)}.`
    : data.rows.length ? "No reviews yet. Open a token to review its label." : "";
  try {
    const ev = await api("/api/evaluation");
    $("evaluation").textContent = ev.rugged && ev.judged > ev.rugged
      ? `Detection so far: caught ${ev.caught} of ${ev.rugged} rugs at a score of ${ev.threshold} or more, `
        + `with ${ev.false_alarms} false alarm${ev.false_alarms === 1 ? "" : "s"}. AUC ${ev.auc.toFixed(2)} across `
        + `${ev.judged} judged tokens.`
      : ev.judged ? `${ev.judged} token(s) scored and judged; detection needs at least one rug and one non-rug.` : "";
  } catch { /* evaluation is optional */ }
}

function openReview(token) {
  $("review").hidden = false;
  $("review").dataset.token = token;
  $("review-title").textContent = `Review the post-mortem for ${token}`;
  $("review-note").textContent = "";
}

async function submitReview(event) {
  event.preventDefault();
  const token = $("review").dataset.token;
  if ($("review-key").value) adminKey = $("review-key").value;
  try {
    await api(`/api/tokens/${encodeURIComponent(token)}/review`, {
      method: "POST",
      body: JSON.stringify({ label: $("review-label").value, reason: $("review-reason").value }),
    });
    $("review-note").textContent = `Review saved for ${token}.`;
    $("review-reason").value = "";
    refreshOutcomes();
    if (token === current) lookup(current);
  } catch (err) {
    $("review-note").textContent = err.message;
  }
}

// ------------------------------------------------------------------ discover
async function startDiscover(event) {
  event.preventDefault();
  const n = Number($("discover-n").value) || 300;
  try {
    const { job } = await api(`/api/discover?recent=${n}`, { method: "POST" });
    watching.add(job.id);
    setChildren($("discovered"), el("li", { text: `Scanning pools as job ${job.id}…` }));
    schedulePoll(500);
  } catch (err) {
    setChildren($("discovered"), el("li", { text: err.message }));
  }
}

function renderDiscovered(tokens) {
  if (!tokens.length) { setChildren($("discovered"), el("li", { text: "No wNEAR pools in that range." })); return; }
  setChildren($("discovered"), ...tokens.map((t) => el("li", {}, [
    el("button", { type: "button", text: t.token_id, onclick: () => { lookup(t.token_id); window.scrollTo({ top: 0 }); } }),
    el("span", { class: t.wnear < 1 ? "depth thin" : "depth",
      text: t.wnear < 1 ? "empty pool" : `${t.wnear.toLocaleString(undefined, { maximumFractionDigits: 1 })} wNEAR` }),
  ])));
}

// ------------------------------------------------------------------ start
async function start() {
  for (const l of LABELS) $("review-label").append(el("option", { value: l, text: OUTCOME_WORD[l] }));
  $("lookup").addEventListener("submit", (e) => { e.preventDefault(); lookup($("token").value); });
  $("btn-score").addEventListener("click", () => startJob("score"));
  $("btn-collect").addEventListener("click", () => startJob("collect"));
  $("btn-outcome").addEventListener("click", () => startJob("outcome"));
  $("review").addEventListener("submit", submitReview);
  $("discover").addEventListener("submit", startDiscover);
  try {
    const h = await api("/api/health");
    $("net").textContent = `NEAR ${h.network}`;
    $("review-key-wrap").hidden = !h.writes_need_key;
  } catch {
    $("net").textContent = "Service unreachable";
  }
  refreshOutcomes();
  schedulePoll(0);
  const fromHash = decodeURIComponent(location.hash.slice(1));
  if (fromHash) lookup(fromHash);
}

document.addEventListener("DOMContentLoaded", start);
