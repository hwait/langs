// The LinguaWiki session page: plan a session, see what is staged, close it, recover what an
// abandoned session left behind.
//
// It does not teach. Core holds no teaching content, so this page never shows an exercise,
// produces a score, or says anything was assessed: work is done in the skill or a session
// package, and arrives here as staged observations, uncredited until the close.
//
// Rules this page keeps:
//   * Every keyed operation is persisted before it is sent and resent unchanged on reload,
//     except an import: its body can hold the learner's words, which retention has not yet
//     seen, so only a marker of identifiers is stored and the file is asked for again.
//   * A close sends the staging digest it was confirmed against, and a changed staging is
//     refused by the server and re-confirmed here, never closed over.
//   * A recovery is a workflow of several requests, kept in its own record so a reload
//     resumes it at the first step that has not been acknowledged.
//   * Everything drawn is read back from the server. Nothing about a session lives only here.

import {
  Refusal,
  TOKEN_KEY,
  createTransport,
  store,
  storeJson,
  stored,
  storedJson,
} from "/transport.js";

const PENDING_KEY = "linguawiki.sessions.pending";
const SESSION_KEY = "linguawiki.session";
const TRACK_KEY = "linguawiki.track";
// `{session_id, idempotency_key, sequence, event_count, file_sha256}`: identifiers and a
// digest of the file's bytes, never a payload, a response, or a file name.
const IMPORT_KEY = "linguawiki.import";
// `{track_id, source_session_id, selected_event_ids, destination, keys, steps}`.
const RECOVERY_KEY = "linguawiki.recovery";
const PAGE = 100;

const state = {
  token: null,
  stale: false,
  busy: false,
  status: "",
  error: null,
  notice: null,
  tracks: null,
  track: null,
  listing: null,
  session: null,
  screen: null,
  extra: [], // further pages of the staged listing
  view: "loading", // loading | picker | home | session | review
  confirm: null, // a close being confirmed
  abandoning: false,
  closeReport: null,
  review: null, // a recovery being reviewed
  importPrompt: null, // an import a reload interrupted
  planForm: null,
};

const { request, send, keyed, loadPending } = createTransport({
  token: () => state.token,
  onStatus: (status) => showStatus(status),
  onStale: () => {
    state.stale = true;
    store(TOKEN_KEY, null);
  },
  pendingKey: PENDING_KEY,
});

// --- the fragment ----------------------------------------------------------------------

function readFragment() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const launched = params.get("token");
  if (launched) store(TOKEN_KEY, launched);
  state.token = launched || stored(TOKEN_KEY);
  history.replaceState(null, "", location.pathname);
}

// --- operations ------------------------------------------------------------------------

async function act(work) {
  if (state.busy) return;
  state.busy = true;
  state.error = null;
  draw();
  try {
    await work();
  } catch (refusal) {
    state.error = { code: refusal.code || "client_error", message: refusal.message };
  } finally {
    state.busy = false;
    draw();
  }
}

async function chooseTrack() {
  const listing = await request("GET", "/tracks");
  state.tracks = listing.tracks;
  const record = storedJson(RECOVERY_KEY);
  if (record) {
    // A recovery belongs to the source's track, whatever this tab was last showing.
    state.track = record.track_id;
    store(TRACK_KEY, record.track_id);
    return;
  }
  const kept = stored(TRACK_KEY);
  if (kept) {
    const entry = listing.tracks.find((track) => track.track_id === kept);
    if (entry && entry.selectable) {
      state.track = kept;
      return;
    }
    store(TRACK_KEY, null);
    store(SESSION_KEY, null);
    state.notice = `The track this page was showing (${kept}) is no longer available, so choose one.`;
    state.view = "picker";
    return;
  }
  if (listing.default_track_id) {
    state.track = listing.default_track_id;
    store(TRACK_KEY, state.track);
    return;
  }
  state.view = "picker";
}

function pickTrack(trackId) {
  return act(async () => {
    state.track = trackId;
    store(TRACK_KEY, trackId);
    store(SESSION_KEY, null);
    state.session = null;
    state.screen = null;
    state.closeReport = null;
    await loadHome();
  });
}

function switchTrack() {
  if (storedJson(RECOVERY_KEY)) {
    state.notice = "Finish or cancel the recovery in progress before switching track.";
    state.view = "review";
    draw();
    return;
  }
  state.view = "picker";
  draw();
}

async function loadHome({ open = true } = {}) {
  if (!state.track) {
    state.view = "picker";
    return;
  }
  state.listing = await request("GET", `/sessions?track=${encodeURIComponent(state.track)}`);
  const openSessions = state.listing.sessions.filter((entry) => entry.open);
  const kept = stored(SESSION_KEY);
  if (open && kept && openSessions.some((entry) => entry.session_id === kept)) return openSession(kept);
  if (open && openSessions.length === 1) return openSession(openSessions[0].session_id);
  state.view = "home";
}

async function openSession(sessionId) {
  store(SESSION_KEY, sessionId);
  state.session = sessionId;
  state.extra = [];
  state.screen = await request("GET", `/sessions/${sessionId}/screen`);
  state.view = "session";
}

async function refresh() {
  if (state.session) state.screen = await request("GET", `/sessions/${state.session}/screen`);
}

function planSession(form) {
  return act(async () => {
    const fields = {
      track: state.track,
      minutes: Number(form.minutes),
      mode: form.mode,
      energy: form.energy,
    };
    if (form.intent) fields.intent = form.intent;
    const report = await keyed("POST", "/sessions", fields);
    state.closeReport = null;
    await openSession(report.session_id);
  });
}

function startSession() {
  return act(async () => {
    await send({ method: "POST", path: `/sessions/${state.session}/start`, body: {} });
    await refresh();
  });
}

function showMore() {
  return act(async () => {
    const offset = state.screen.staged.events.length + state.extra.length;
    const page = await request("GET", `/sessions/${state.session}/staged?limit=${PAGE}&offset=${offset}`);
    state.extra.push(...page.events);
  });
}

// --- import ----------------------------------------------------------------------------

async function sha256(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function readBatchFile(file) {
  const bytes = await file.arrayBuffer();
  const digest = await sha256(bytes);
  let batch;
  try {
    batch = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    batch = null;
  }
  if (batch === null || typeof batch !== "object" || Array.isArray(batch)) {
    throw new Refusal(0, { code: "client_invalid_file", message: "That file is not a session batch." });
  }
  return { batch, digest };
}

// The body lives in this function's scope for the life of the request and nowhere else.
async function importBatch(sessionId, batch, digest, { confirming = false } = {}) {
  const key = typeof batch.idempotency_key === "string" ? batch.idempotency_key : undefined;
  storeJson(IMPORT_KEY, {
    session_id: sessionId,
    idempotency_key: key ?? null,
    sequence: batch.sequence ?? null,
    event_count: Array.isArray(batch.events) ? batch.events.length : 0,
    file_sha256: digest,
  });
  try {
    const report = await request("POST", `/sessions/${sessionId}/batches`, { batch }, { key });
    store(IMPORT_KEY, null);
    state.importPrompt = null;
    if (report.duplicate) {
      state.notice = confirming
        ? `The import was confirmed: batch ${report.sequence} is the one on record, and nothing was staged twice.`
        : `Batch ${report.sequence} was already staged; nothing was staged twice.`;
    } else {
      state.notice = `Batch ${report.sequence} staged: ${report.event_count} event(s), not yet credited.`;
    }
    for (const warning of report.warnings || []) state.notice += ` ${warning}.`;
  } catch (refusal) {
    if (refusal.code !== "client_unreachable") {
      store(IMPORT_KEY, null);
      state.importPrompt = null;
    }
    if (refusal.code === "idempotency_conflict") {
      throw new Refusal(refusal.status, {
        code: refusal.code,
        message: `The import was refused: ${refusal.message}`,
      });
    }
    throw refusal;
  }
}

function importFile(file) {
  return act(async () => {
    const { batch, digest } = await readBatchFile(file);
    await importBatch(state.session, batch, digest);
    await refresh();
  });
}

// After a reload: a key on record proves only that *some* batch owns it, so content is
// verified by sending the file again and letting the server compare -- never from the key.
async function settleImportMarker() {
  const marker = storedJson(IMPORT_KEY);
  if (!marker) return;
  let onRecord = null;
  try {
    const screen = await request("GET", `/sessions/${marker.session_id}/screen`);
    onRecord = screen.batches.some((batch) => batch.idempotency_key === marker.idempotency_key);
  } catch {
    onRecord = null;
  }
  state.importPrompt = { marker, onRecord, mismatch: null };
}

function reimport(file) {
  return act(async () => {
    const prompt = state.importPrompt;
    const { batch, digest } = await readBatchFile(file);
    if (digest !== prompt.marker.file_sha256) {
      prompt.mismatch = { batch, digest };
      return;
    }
    await importBatch(prompt.marker.session_id, batch, digest, { confirming: true });
    await refresh();
  });
}

function importMismatchAsNew() {
  return act(async () => {
    const prompt = state.importPrompt;
    store(IMPORT_KEY, null);
    state.importPrompt = null;
    await importBatch(prompt.marker.session_id, prompt.mismatch.batch, prompt.mismatch.digest);
    await refresh();
  });
}

function dismissImport() {
  store(IMPORT_KEY, null);
  state.importPrompt = null;
  state.notice = "The interrupted import was set aside; whether it landed was never established.";
  draw();
}

// --- close and abandon -----------------------------------------------------------------

function openConfirm(outcome) {
  state.confirm = {
    outcome,
    digest: state.screen.staging.digest,
    count: state.screen.staging.count,
    changedFrom: null,
    discard: new Set(),
    minutes: "",
    summary: "",
  };
  state.abandoning = false;
  draw();
}

function confirmClose() {
  return act(async () => {
    const confirm = state.confirm;
    const fields = { outcome: confirm.outcome, expected_staging: confirm.digest };
    if (confirm.discard.size) fields.discard_blocks = [...confirm.discard].sort();
    if (confirm.minutes !== "") fields.actual_minutes = Number(confirm.minutes);
    if (confirm.summary) fields.summary = confirm.summary;
    try {
      state.closeReport = await keyed("POST", `/sessions/${state.session}/close`, fields);
      state.confirm = null;
    } catch (refusal) {
      if (refusal.code !== "session_staging_changed") throw refusal;
      // The staged work moved under the confirmation. Read it again and ask again; nothing
      // is sent until the learner has seen what will now be credited.
      await refresh();
      state.confirm = {
        ...confirm,
        changedFrom: confirm.count,
        count: state.screen.staging.count,
        digest: state.screen.staging.digest,
      };
      return;
    }
    await refresh();
  });
}

function abandonSession(reason) {
  return act(async () => {
    const fields = reason ? { reason } : {};
    await keyed("POST", `/sessions/${state.session}/abandon`, fields);
    state.abandoning = false;
    store(SESSION_KEY, null);
    await refresh();
  });
}

// --- recovery --------------------------------------------------------------------------

function reviewRecovery(sourceId) {
  return act(async () => {
    const events = [];
    let total = null;
    // Every still-recoverable event, page by page, before anything can be selected: a
    // selection made from half a list would leave the rest behind without saying so.
    while (total === null || events.length < total) {
      const page = await request(
        "GET",
        `/sessions/${sourceId}/staged?status=staged&limit=${PAGE}&offset=${events.length}`,
      );
      total = page.total;
      events.push(...page.events);
      if (!page.events.length) break;
    }
    const listing = await request("GET", `/sessions?track=${encodeURIComponent(state.track)}&state=open`);
    const destinations = listing.sessions.filter((entry) => ["planned", "active"].includes(entry.status));
    state.review = {
      source: sourceId,
      events,
      total,
      selected: new Set(events.map((event) => event.staged_event_id)),
      destinations,
      destination: destinations.length ? destinations[0].session_id : "new",
      plan: { minutes: "30", mode: state.listing?.modes?.[0] || "mixed", energy: "normal" },
      modes: listing.modes,
      energies: listing.energy_levels,
    };
    state.view = "review";
  });
}

function beginRecovery() {
  const review = state.review;
  const fresh = review.destination === "new";
  const record = {
    track_id: state.track,
    source_session_id: review.source,
    selected_event_ids: [...review.selected].sort(),
    destination: fresh
      ? { kind: "new", session_id: null, plan: { ...review.plan, minutes: Number(review.plan.minutes) } }
      : { kind: "existing", session_id: review.destination, plan: null },
    keys: { plan: crypto.randomUUID(), recover: crypto.randomUUID() },
    steps: { planned: !fresh, started: false, recovered: false },
  };
  storeJson(RECOVERY_KEY, record);
  return act(() => runRecovery());
}

function saveRecord(record) {
  storeJson(RECOVERY_KEY, record);
}

// Each step under its stored key, from the first one not acknowledged. A step marked done
// is never re-sent; the target's state is read again before the next one.
async function runRecovery() {
  const record = storedJson(RECOVERY_KEY);
  if (!record) return;
  state.track = record.track_id;
  store(TRACK_KEY, record.track_id);
  if (!record.steps.planned) {
    const plan = record.destination.plan;
    const report = await send({
      method: "POST",
      path: "/sessions",
      body: {
        track: record.track_id,
        minutes: plan.minutes,
        mode: plan.mode,
        energy: plan.energy,
        idempotency_key: record.keys.plan,
      },
    });
    record.destination.session_id = report.session_id;
    record.steps.planned = true;
    saveRecord(record);
  }
  const target = record.destination.session_id;
  if (!record.steps.started) {
    const screen = await request("GET", `/sessions/${target}/screen`);
    if (screen.session.status === "planned") {
      await send({ method: "POST", path: `/sessions/${target}/start`, body: {} });
    } else if (screen.session.status !== "active") {
      throw await recoveryNeeds("destination", `Session ${target} is ${screen.session.status} and cannot take recovered work; choose another destination.`);
    }
    record.steps.started = true;
    saveRecord(record);
  }
  if (!record.steps.recovered) {
    let report;
    try {
      report = await send({
        method: "POST",
        path: `/sessions/${record.source_session_id}/recover`,
        body: { into: target, events: record.selected_event_ids, idempotency_key: record.keys.recover },
      });
    } catch (refusal) {
      if (refusal.code === "staged_event_not_recoverable") {
        throw await recoveryNeeds("selection", "Some of those events have already moved elsewhere. Review what is left and choose again.");
      }
      if (["session_not_active", "session_not_started", "session_not_found"].includes(refusal.code)) {
        throw await recoveryNeeds("destination", refusal.message);
      }
      throw refusal;
    }
    record.steps.recovered = true;
    saveRecord(record);
    state.notice =
      `Recovered ${report.recovered} event(s) into session ${report.target_session_id}. ` +
      "They are staged and credited to nothing until that session closes." +
      (report.replayed ? " (Confirmed after a reload: this recovery had already happened.)" : "");
  }
  store(RECOVERY_KEY, null);
  state.review = null;
  await openSession(target);
}

// A refusal mid-flow keeps the learner's choices: a new selection keeps the destination, a
// new destination keeps the selection. Either is a new request, so the recover key is new.
async function recoveryNeeds(what, message) {
  const record = storedJson(RECOVERY_KEY);
  store(RECOVERY_KEY, null);
  await reviewRecovery(record.source_session_id);
  if (state.review) {
    if (what === "selection") {
      if (state.review.destinations.some((entry) => entry.session_id === record.destination.session_id)) {
        state.review.destination = record.destination.session_id;
      }
    } else {
      const kept = new Set(record.selected_event_ids);
      state.review.selected = new Set(state.review.events.map((event) => event.staged_event_id).filter((id) => kept.has(id)));
    }
  }
  return new Refusal(0, { code: `recovery_${what}_needed`, message });
}

function cancelRecovery() {
  const record = storedJson(RECOVERY_KEY);
  store(RECOVERY_KEY, null);
  state.review = null;
  if (record && record.destination.kind === "new" && record.steps.planned) {
    state.notice = `The session planned for this recovery (${record.destination.session_id}) is left in place.`;
  }
  return act(() => loadHome({ open: false }));
}

// --- drawing ---------------------------------------------------------------------------

function h(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === false || value === null || value === undefined) continue;
    if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
    else if (name === "dataset") Object.assign(element.dataset, value);
    else element.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

const app = () => document.getElementById("app");

function button(label, onclick, attributes = {}) {
  return h("button", { type: "button", onclick, disabled: state.busy, ...attributes }, label);
}

function showStatus(status) {
  if (state.status !== status) {
    state.status = status;
    drawStatus();
  }
}

function drawStatus() {
  const banner = document.getElementById("status");
  if (!banner) return;
  const text = {
    waiting: "Another LinguaWiki command is using the database. Waiting, then retrying…",
    offline: "Cannot reach the LinguaWiki server. Retrying…",
    unknown: "The server could not confirm that action. Reloading the current state.",
  }[state.status];
  banner.hidden = !text;
  banner.textContent = text || "";
  banner.dataset.state = state.status || "idle";
}

function header() {
  const track = state.tracks?.find((entry) => entry.track_id === state.track);
  return h(
    "header",
    {},
    h("h1", {}, "LinguaWiki sessions"),
    track
      ? h(
          "p",
          { class: "track", dataset: { role: "track", track: track.track_id } },
          `${track.display_name} · ${track.target_language} · ${track.proficiency_framework} `,
          button("Switch track", switchTrack, { dataset: { role: "switch-track" } }),
        )
      : null,
    h("p", { id: "status", role: "status", hidden: true }),
  );
}

function teachingElsewhere() {
  return h(
    "p",
    { class: "note", dataset: { role: "teaching-elsewhere" } },
    "Teaching happens elsewhere: in your LinguaWiki skill, or in a session package. This page plans " +
      "sessions, shows the work they have staged, and closes them. It gives no exercises and judges nothing.",
  );
}

function banners() {
  return [
    state.error
      ? h("p", { class: "error", role: "alert", dataset: { code: state.error.code } }, state.error.message)
      : null,
    state.notice ? h("p", { class: "note", dataset: { role: "notice" } }, state.notice) : null,
  ];
}

function importPromptPanel() {
  const prompt = state.importPrompt;
  if (!prompt) return null;
  const { marker } = prompt;
  const question =
    prompt.onRecord === true
      ? `A batch under this import's key is on record for session ${marker.session_id}. ` +
        "Choose the file again to confirm it is the one you imported."
      : prompt.onRecord === false
        ? `The import of batch ${marker.sequence} did not land. Choose the file again to send it.`
        : `Whether the import of batch ${marker.sequence} landed could not be read. Choose the file again to settle it.`;
  return h(
    "section",
    { class: "panel", dataset: { role: "import-prompt", onRecord: String(prompt.onRecord) } },
    h("p", {}, question),
    prompt.mismatch
      ? [
          h(
            "p",
            { dataset: { role: "import-mismatch" } },
            "That is a different file. Whether the original import landed is still unknown.",
          ),
          h(
            "div",
            { class: "actions" },
            button("Import this file as new", importMismatchAsNew, { dataset: { role: "import-as-new" } }),
          ),
        ]
      : h("input", {
          type: "file",
          accept: "application/json,.json",
          disabled: state.busy,
          dataset: { role: "import-again" },
          onchange: (event) => event.target.files[0] && reimport(event.target.files[0]),
        }),
    h("div", { class: "actions secondary" }, button("Set it aside", dismissImport, { dataset: { role: "import-dismiss" } })),
  );
}

function pickerView() {
  return h(
    "section",
    { class: "panel", dataset: { role: "track-picker" } },
    h("p", {}, "Choose the track to work on."),
    h(
      "ul",
      { class: "runs" },
      (state.tracks || []).map((track) =>
        h(
          "li",
          { dataset: { role: "track-option", track: track.track_id } },
          `${track.display_name} · ${track.target_language} · ${track.proficiency_framework} · ${track.status} `,
          track.selectable
            ? button("Use this track", () => pickTrack(track.track_id), { dataset: { role: "pick-track" } })
            : h("span", { class: "reason" }, "not selectable"),
        ),
      ),
    ),
  );
}

function homeView() {
  const listing = state.listing || { sessions: [], modes: [], energy_levels: [] };
  // Kept on the state, not rebuilt per draw: every redraw rebuilds the fields, and a form
  // that lived only in the DOM would lose what the learner typed.
  state.planForm ||= { minutes: "30", mode: listing.modes[0] || "mixed", energy: "normal", intent: "" };
  const form = state.planForm;
  const openSessions = listing.sessions.filter((entry) => entry.open);
  const recoverable = listing.sessions.filter((entry) => entry.recoverable);
  return [
    h(
      "section",
      { class: "panel", dataset: { role: "plan-form" } },
      h("p", {}, "Plan a session"),
      h(
        "label",
        {},
        "Minutes ",
        h("input", { type: "number", min: "5", value: form.minutes, dataset: { role: "minutes" }, oninput: (e) => (form.minutes = e.target.value) }),
      ),
      h(
        "label",
        {},
        " Mode ",
        h("select", { dataset: { role: "mode" }, onchange: (e) => (form.mode = e.target.value) }, listing.modes.map((mode) => h("option", { value: mode, selected: mode === form.mode }, mode))),
      ),
      h(
        "label",
        {},
        " Energy ",
        h(
          "select",
          { dataset: { role: "energy" }, onchange: (e) => (form.energy = e.target.value) },
          listing.energy_levels.map((energy) => h("option", { value: energy, selected: energy === form.energy }, energy)),
        ),
      ),
      h("label", {}, " Intent ", h("input", { type: "text", value: form.intent, dataset: { role: "intent" }, oninput: (e) => (form.intent = e.target.value) })),
      h("div", { class: "actions" }, button("Plan", () => planSession(form), { dataset: { role: "plan" } })),
    ),
    openSessions.length
      ? h(
          "section",
          { class: "panel", dataset: { role: "open-sessions" } },
          h("p", {}, "Open sessions"),
          h(
            "ul",
            { class: "runs" },
            openSessions.map((entry) =>
              h(
                "li",
                { dataset: { session: entry.session_id } },
                `${entry.session_id} · ${entry.status} · ${entry.staged_events} staged `,
                button("Open", () => act(() => openSession(entry.session_id)), { dataset: { role: "open-session" } }),
              ),
            ),
          ),
        )
      : null,
    recoverable.length
      ? h(
          "section",
          { class: "panel", dataset: { role: "recoverable-sessions" } },
          h("p", {}, "Sessions holding work that was never credited"),
          h(
            "ul",
            { class: "runs" },
            recoverable.map((entry) =>
              h(
                "li",
                { dataset: { session: entry.session_id } },
                `${entry.session_id} · ${entry.status} · ${entry.staged_events} staged event(s) `,
                button("Review and recover", () => reviewRecovery(entry.session_id), { dataset: { role: "review" } }),
              ),
            ),
          ),
        )
      : null,
  ];
}

const STAGED_LABEL = {
  staged: "not yet credited",
  materialized: "credited",
  discarded: "discarded",
  rejected: "rejected",
};

function stagedRow(event) {
  return h(
    "li",
    { dataset: { role: "staged-event", status: event.status, event: event.staged_event_id } },
    `${event.batch_sequence}.${event.sequence} ${event.kind} · ${event.summary}` +
      (event.assessor_kind ? ` · assessed by: ${event.assessor_kind} (as the producer says)` : "") +
      ` · ${event.evidence_basis} · ${STAGED_LABEL[event.status] || event.status}` +
      (event.discard_reason ? ` (${event.discard_reason})` : ""),
  );
}

function blockName(blockId) {
  const block = state.screen.session.blocks.find((entry) => entry.block_id === blockId);
  return block ? `${block.sequence}. ${block.block_type}` : blockId;
}

function closeReportPanel() {
  const report = state.closeReport;
  if (!report) return null;
  return h(
    "section",
    { class: "panel", dataset: { role: "close-report", replayed: String(report.replayed) } },
    h(
      "p",
      {},
      `Closed as ${report.outcome}: ${report.attempts_written} attempt(s), ${report.evidence_written} evidence row(s), ` +
        `${report.errors_written} error occurrence(s), ${report.followups_written} follow-up(s) credited.` +
        (report.replayed ? " This close had already happened; nothing was credited twice." : ""),
    ),
    report.stage_changes.length
      ? h(
          "ul",
          { dataset: { role: "stage-changes" } },
          report.stage_changes.map((change) =>
            h("li", {}, `${change.title}: ${change.stage_before || "new"} → ${change.stage_after}`, h("ul", {}, change.explanation.map((line) => h("li", {}, line)))),
          ),
        )
      : null,
    report.warnings.length ? h("ul", { dataset: { role: "close-warnings" } }, report.warnings.map((warning) => h("li", {}, warning))) : null,
  );
}

function confirmPanel() {
  const confirm = state.confirm;
  if (!confirm) return null;
  const screen = state.screen;
  const partial = confirm.outcome === "partial";
  return h(
    "section",
    { class: "panel", dataset: { role: "close-confirmation", outcome: confirm.outcome } },
    confirm.changedFrom !== null
      ? h(
          "p",
          { class: "error", dataset: { role: "staging-changed" } },
          `The staged work changed while you were confirming: ${confirm.changedFrom} event(s) became ${confirm.count}. Review it and confirm again.`,
        )
      : null,
    h(
      "p",
      { dataset: { role: "close-count", count: String(confirm.count) } },
      `This close credits ${confirm.count} staged event(s)` +
        (confirm.discard.size ? `, except those in the ${confirm.discard.size} block(s) you exclude` : "") +
        ". Nothing about the learner changes before it, and this is the moment it does.",
    ),
    partial
      ? [
          h(
            "ul",
            { dataset: { role: "discard-blocks" } },
            Object.entries(screen.staged_by_block).map(([blockId, number]) =>
              h(
                "li",
                {},
                h(
                  "label",
                  {},
                  h("input", {
                    type: "checkbox",
                    checked: confirm.discard.has(blockId),
                    dataset: { role: "discard-block", block: blockId },
                    onchange: (event) => {
                      if (event.target.checked) confirm.discard.add(blockId);
                      else confirm.discard.delete(blockId);
                      draw();
                    },
                  }),
                  ` exclude ${blockName(blockId)} (${number} event(s))`,
                ),
              ),
            ),
          ),
          screen.unattributed
            ? h(
                "p",
                { dataset: { role: "unattributed", count: String(screen.unattributed) } },
                `${screen.unattributed} event(s) belong to no block (recovered events among them) and cannot be excluded here; ` +
                  "they will be credited. To leave them out, abandon instead and recover a reviewed subset.",
              )
            : null,
          h(
            "p",
            { class: "note" },
            "An excluded block's events are kept for audit and cannot be recovered later.",
          ),
        ]
      : null,
    h(
      "label",
      {},
      "Minutes actually spent ",
      h("input", { type: "number", min: "0", value: confirm.minutes, dataset: { role: "actual-minutes" }, oninput: (e) => (confirm.minutes = e.target.value) }),
    ),
    h("label", {}, " Summary ", h("input", { type: "text", value: confirm.summary, dataset: { role: "summary" }, oninput: (e) => (confirm.summary = e.target.value) })),
    h(
      "div",
      { class: "actions" },
      button(partial ? "Close as partial" : "Close and credit", confirmClose, { dataset: { role: "confirm-close" } }),
      button("Not yet", () => {
        state.confirm = null;
        draw();
      }, { dataset: { role: "cancel-close" } }),
    ),
  );
}

function sessionView() {
  const screen = state.screen;
  const session = screen.session;
  const actions = new Set(screen.actions);
  const events = [...screen.staged.events, ...state.extra];
  const controls = [];
  if (actions.has("session.start")) controls.push(button("Start", startSession, { dataset: { role: "start" } }));
  if (actions.has("session.import")) {
    controls.push(
      h(
        "label",
        { class: "import" },
        "Import a batch file ",
        h("input", {
          type: "file",
          accept: "application/json,.json",
          disabled: state.busy,
          dataset: { role: "import" },
          onchange: (event) => event.target.files[0] && importFile(event.target.files[0]),
        }),
      ),
    );
  }
  if (actions.has("session.close")) {
    controls.push(
      button(screen.closing_interrupted ? "Finish closing" : "Close session", () => openConfirm("completed"), { dataset: { role: "close" } }),
    );
  }
  if (actions.has("session.partial-close") || screen.closing_interrupted) {
    controls.push(button("Close as partial", () => openConfirm("partial"), { dataset: { role: "partial-close" } }));
  }
  if (actions.has("session.abandon")) {
    controls.push(
      button("Abandon", () => {
        state.abandoning = true;
        state.confirm = null;
        draw();
      }, { dataset: { role: "abandon" } }),
    );
  }
  return [
    teachingElsewhere(),
    h(
      "section",
      { class: "panel", dataset: { role: "session", session: session.session_id, status: session.status } },
      h("p", {}, `Session ${session.session_id} · ${session.status} · ${session.mode} · ${session.planned_minutes} minute(s) planned`),
      screen.closing_interrupted
        ? h("p", { class: "error", dataset: { role: "closing-interrupted" } }, "This session was being closed when it stopped, and nothing was credited. Finish the close, or abandon it.")
        : null,
      h(
        "ol",
        { class: "progress", dataset: { role: "blocks" } },
        session.blocks.map((block) =>
          h(
            "li",
            { dataset: { block: block.block_id, status: block.status } },
            `${block.block_type} (${block.role}, ${block.planned_minutes} min): ${block.objective}`,
            h("ul", { dataset: { role: "rationale" } }, block.rationale.map((reason) => h("li", {}, reason))),
          ),
        ),
      ),
      session.omissions.length
        ? h(
            "div",
            { dataset: { role: "omissions" } },
            h("p", {}, "Ranked highly and missed out:"),
            h("ul", {}, session.omissions.map((omission) => h("li", {}, `${omission.block_type} (${omission.score.toFixed(2)}): ${omission.reason}`))),
          )
        : null,
    ),
    closeReportPanel(),
    h(
      "section",
      { class: "panel", dataset: { role: "staged", count: String(screen.staging.count) } },
      h("p", {}, `${screen.staging.count} staged event(s), not yet credited. Nothing here has changed the learner's record.`),
      screen.missing_batch_sequences.length
        ? h(
            "p",
            { class: "error", dataset: { role: "batch-gap" } },
            `Batch sequence(s) ${screen.missing_batch_sequences.join(", ")} never arrived, so a complete close will be refused. Close as partial once you have reviewed the rest.`,
          )
        : null,
      h("ul", {}, events.map(stagedRow)),
      events.length < screen.staged.total ? button("Show more", showMore, { dataset: { role: "more" } }) : null,
    ),
    confirmPanel(),
    state.abandoning
      ? h(
          "section",
          { class: "panel", dataset: { role: "abandon-confirmation" } },
          h("p", {}, "Abandoning credits nothing and keeps the staged work, so it can be recovered into another session later."),
          h(
            "div",
            { class: "actions" },
            button("Abandon this session", () => abandonSession(""), { dataset: { role: "confirm-abandon" } }),
            button("Keep it", () => {
              state.abandoning = false;
              draw();
            }),
          ),
        )
      : null,
    h(
      "div",
      { class: "actions" },
      controls,
      button("All sessions", () => act(() => loadHome({ open: false })), { dataset: { role: "home" } }),
    ),
  ];
}

function reviewView() {
  const record = storedJson(RECOVERY_KEY);
  if (record && !state.review) {
    return h(
      "section",
      { class: "panel", dataset: { role: "recovery-in-progress" } },
      h("p", {}, `A recovery from session ${record.source_session_id} is in progress.`),
      h(
        "div",
        { class: "actions" },
        button("Finish it", () => act(() => runRecovery()), { dataset: { role: "finish-recovery" } }),
        button("Cancel it", cancelRecovery, { dataset: { role: "cancel-recovery" } }),
      ),
    );
  }
  const review = state.review;
  if (!review) return null;
  const complete = review.events.length === review.total;
  return h(
    "section",
    { class: "panel", dataset: { role: "recovery-review", source: review.source } },
    h("p", {}, `Session ${review.source} holds ${review.total} event(s) that were never credited. Choose what to carry over.`),
    h(
      "ul",
      {},
      review.events.map((event) =>
        h(
          "li",
          {},
          h(
            "label",
            {},
            h("input", {
              type: "checkbox",
              checked: review.selected.has(event.staged_event_id),
              dataset: { role: "recover-event", event: event.staged_event_id },
              onchange: (change) => {
                if (change.target.checked) review.selected.add(event.staged_event_id);
                else review.selected.delete(event.staged_event_id);
                draw();
              },
            }),
            ` ${event.kind} · ${event.summary}` + (event.assessor_kind ? ` · assessed by: ${event.assessor_kind} (as the producer says)` : ""),
          ),
        ),
      ),
    ),
    h("p", {}, "Into:"),
    h(
      "ul",
      { dataset: { role: "destinations" } },
      review.destinations.map((entry) =>
        h(
          "li",
          {},
          h(
            "label",
            {},
            h("input", {
              type: "radio",
              name: "destination",
              value: entry.session_id,
              checked: review.destination === entry.session_id,
              dataset: { role: "destination", session: entry.session_id },
              onchange: () => {
                review.destination = entry.session_id;
                draw();
              },
            }),
            ` session ${entry.session_id} (${entry.status})`,
          ),
        ),
      ),
      h(
        "li",
        {},
        h(
          "label",
          {},
          h("input", {
            type: "radio",
            name: "destination",
            value: "new",
            checked: review.destination === "new",
            dataset: { role: "destination", session: "new" },
            onchange: () => {
              review.destination = "new";
              draw();
            },
          }),
          " a new session of ",
          h("input", { type: "number", min: "5", value: review.plan.minutes, dataset: { role: "recovery-minutes" }, oninput: (e) => (review.plan.minutes = e.target.value) }),
          " minutes, mode ",
          h(
            "select",
            { dataset: { role: "recovery-mode" }, onchange: (e) => (review.plan.mode = e.target.value) },
            review.modes.map((mode) => h("option", { value: mode, selected: mode === review.plan.mode }, mode)),
          ),
        ),
      ),
    ),
    h(
      "div",
      { class: "actions" },
      button("Recover selected", beginRecovery, {
        dataset: { role: "recover" },
        disabled: state.busy || !complete || review.selected.size === 0,
      }),
      button("Cancel", cancelRecovery, { dataset: { role: "cancel-recovery" } }),
    ),
  );
}

function draw() {
  const root = app();
  if (state.stale || !state.token) {
    root.replaceChildren(
      header(),
      h("section", { class: "panel" }, h("p", {}, "Open this page from the address that linguawiki client serve prints.")),
    );
    return;
  }
  const body = {
    loading: () => h("p", {}, "Loading…"),
    picker: pickerView,
    home: homeView,
    session: sessionView,
    review: reviewView,
  }[state.view]();
  root.replaceChildren(header(), ...banners(), importPromptPanel(), ...[body].flat());
  drawStatus();
  root.dataset.view = state.view;
  root.dataset.busy = String(state.busy);
}

// --- start -----------------------------------------------------------------------------

async function boot() {
  readFragment();
  if (!state.token) {
    draw();
    return;
  }
  try {
    // An operation a reload interrupted goes first, unchanged, so a lost response is
    // answered as a replay rather than repeated under a new key.
    let reopen = null;
    const pending = loadPending();
    if (pending) {
      try {
        const data = await send(pending);
        const closed = /^\/sessions\/(ses_[0-9A-Z]+)\/close$/.exec(pending.path);
        if (closed) {
          // The replayed close is drawn on its own session, which is no longer open and so
          // would not be chosen by discovery.
          state.closeReport = data;
          reopen = closed[1];
        }
      } catch (refusal) {
        state.error = { code: refusal.code, message: refusal.message };
      }
    }
    await settleImportMarker();
    await chooseTrack();
    if (storedJson(RECOVERY_KEY)) {
      try {
        await runRecovery();
      } catch (refusal) {
        state.error = { code: refusal.code, message: refusal.message };
        if (state.view !== "review") state.view = "review";
      }
    } else if (reopen && state.view !== "picker") {
      await openSession(reopen);
    } else if (state.view !== "picker") {
      await loadHome();
    }
  } catch (refusal) {
    state.error = { code: refusal.code || "client_error", message: refusal.message };
    if (state.view === "loading") state.view = state.track ? "home" : "picker";
  }
  draw();
}

boot();
