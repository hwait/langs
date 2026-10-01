// The LinguaWiki assessment screen.
//
// A drawing of what the server says, and nothing more. Selection, scoring, the stop rule,
// the estimate and every refusal stay in Python: this page sends the learner's choice
// `value` or typed text, and draws `/screen`. It never computes an estimate, rounds a
// range into a level, or decides whether an answer was right.
//
// Three rules shape it:
//   * Every keyed operation gets one key, minted when the learner acts, and a retry
//     resends that key with the identical body -- across a reload too, which is why the
//     pending operation is written to sessionStorage before it is sent.
//   * One operation is in flight at a time. Buttons disable on the first press, so two
//     clicks are one answer.
//   * State is read back from `/screen` after every change. Nothing about a run lives
//     only here, so a reload, a resume, or a different sitting draws the same thing.

const TOKEN_HEADER = "X-LinguaWiki-Token";
const PENDING_KEY = "linguawiki.pending";
const STALE_TOKEN = new Set(["client_token_required", "client_token_invalid"]);
const MACHINE_RUN = { scoring: "machine", modalities: ["text", "audio"] };

const state = {
  token: null,
  run: null, // the run id being drawn
  screen: null,
  busy: false, // an operation is in flight
  status: "", // "", "waiting", "offline", "unknown"
  error: null, // { code, message }
  recordings: new Map(), // content_id -> object URL
  stale: false,
};

// --- the fragment ----------------------------------------------------------------------

function readFragment() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  state.token = params.get("token");
  const run = params.get("run");
  // Out of the address bar and out of history: the token is in memory from here on.
  history.replaceState(null, "", location.pathname);
  return run;
}

// --- the transport ---------------------------------------------------------------------

class Refusal extends Error {
  constructor(status, error) {
    super(error.message);
    this.status = status;
    this.code = error.code;
    this.retryable = Boolean(error.retryable);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function savePending(operation) {
  try {
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(operation));
  } catch {
    // No storage: the operation still runs, it just cannot survive a reload.
  }
}

function clearPending() {
  try {
    sessionStorage.removeItem(PENDING_KEY);
  } catch {
    /* nothing to clear */
  }
}

function loadPending() {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function setStatus(status) {
  if (state.status !== status) {
    state.status = status;
    drawStatus();
  }
}

// One request, retried only where a retry is a replay: a read, or a mutation that carries
// its key. A retryable refusal waits the server's `Retry-After` and sends the same body.
async function request(method, path, body, { binary = false } = {}) {
  const keyed = body !== undefined && typeof body.idempotency_key === "string";
  const mayRetry = method === "GET" || keyed;
  for (let attempt = 0; ; attempt += 1) {
    let response;
    try {
      response = await fetch(path, {
        method,
        headers: {
          [TOKEN_HEADER]: state.token,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        credentials: "omit",
        referrerPolicy: "no-referrer",
      });
    } catch {
      if (!mayRetry) {
        setStatus("unknown");
        throw new Refusal(0, {
          code: "client_unreachable",
          message: "The LinguaWiki server did not answer, so whether that landed is unknown.",
        });
      }
      setStatus("offline");
      await sleep(Math.min(1000 * (attempt + 1), 5000));
      continue;
    }
    const type = response.headers.get("Content-Type") || "";
    if (binary && response.ok && !type.startsWith("application/json")) {
      setStatus("");
      return response.blob();
    }
    const envelope = await response.json().catch(() => ({}));
    if (response.ok && envelope.ok) {
      setStatus("");
      return envelope.data;
    }
    const error = envelope.error || { code: "client_unreadable", message: "The server's answer could not be read." };
    if (STALE_TOKEN.has(error.code)) {
      state.stale = true;
      setStatus("");
      throw new Refusal(response.status, error);
    }
    if (response.status === 503 && error.retryable && mayRetry) {
      setStatus("waiting");
      const after = Number(response.headers.get("Retry-After")) || 1;
      await sleep(after * 1000);
      continue;
    }
    if (response.status === 503) {
      // A keyless mutation whose outcome the server cannot vouch for. Not retried: a
      // second attempt could repeat work that landed. The screen is re-read instead.
      setStatus("unknown");
    } else {
      setStatus("");
    }
    throw new Refusal(response.status, error);
  }
}

// A keyed operation: persisted before it is sent, cleared on a definitive answer.
async function keyed(method, path, fields = {}) {
  const operation = { method, path, body: { ...fields, idempotency_key: crypto.randomUUID() } };
  return send(operation);
}

async function send(operation) {
  savePending(operation);
  try {
    const data = await request(operation.method, operation.path, operation.body);
    clearPending();
    return data;
  } catch (refusal) {
    // A refusal is an answer, and the operation is over. Only an unreachable server
    // leaves it pending, because then nobody knows whether it landed.
    if (refusal.code !== "client_unreachable") clearPending();
    throw refusal;
  }
}

// --- actions ---------------------------------------------------------------------------

async function act(work) {
  if (state.busy) return; // two clicks are one answer
  state.busy = true;
  state.error = null;
  draw();
  try {
    await work();
  } catch (refusal) {
    state.error = { code: refusal.code, message: refusal.message };
  } finally {
    state.busy = false;
  }
  if (!state.stale && state.run) {
    try {
      state.screen = await request("GET", `/runs/${state.run}/screen`);
    } catch (refusal) {
      state.error = state.error || { code: refusal.code, message: refusal.message };
    }
  }
  draw();
}

async function openRun(runId) {
  state.run = runId;
  state.screen = await request("GET", `/runs/${runId}/screen`);
}

function startRun() {
  return act(async () => {
    const run = await keyed("POST", "/runs", MACHINE_RUN);
    state.run = run.run_id;
  });
}

function resumeRun(runId) {
  return act(async () => {
    state.run = runId;
    const screen = await request("GET", `/runs/${runId}/screen`);
    if (screen.status === "paused") {
      await request("POST", `/runs/${runId}/status`, { status: "in-progress" });
    }
  });
}

function setRunStatus(status) {
  return act(() => request("POST", `/runs/${state.run}/status`, { status }));
}

// Serve the next task. A replayed serve can hand back a task already answered; it is never
// drawn, and the next serve goes out under a new key.
async function serveNext() {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const served = await keyed("POST", `/runs/${state.run}/tasks`);
    if (!("content_id" in served) || served.status === "served") return;
  }
}

function answer(task, response) {
  return act(() =>
    keyed("POST", `/runs/${state.run}/results`, { content_id: task.content_id, response }),
  );
}

function finish() {
  return act(() => keyed("POST", `/runs/${state.run}/finalization`, { reason: "completed" }));
}

async function play(task, player) {
  if (state.busy) return;
  state.busy = true;
  state.error = null;
  draw();
  try {
    // Recorded before it is heard, and playback starts only when the server agrees.
    await keyed("POST", `/runs/${state.run}/tasks/${task.content_id}/plays`);
    let url = state.recordings.get(task.content_id);
    if (!url) {
      const blob = await request("GET", `/runs/${state.run}/tasks/${task.content_id}/audio`, undefined, {
        binary: true,
      });
      url = URL.createObjectURL(blob);
      state.recordings.set(task.content_id, url);
    }
    player.src = url;
    player.currentTime = 0;
    await player.play().catch(() => {});
  } catch (refusal) {
    state.error = { code: refusal.code, message: refusal.message };
  } finally {
    state.busy = false;
  }
  try {
    state.screen = await request("GET", `/runs/${state.run}/screen`);
  } catch {
    /* drawn from what we have */
  }
  draw({ keepPlayer: player });
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
  return h(
    "header",
    {},
    h("h1", {}, "LinguaWiki"),
    h("p", { id: "status", role: "status", hidden: true }),
  );
}

function errorBanner() {
  if (!state.error) return null;
  return h("p", { class: "error", role: "alert", dataset: { code: state.error.code } }, state.error.message);
}

function dimensionRow(dimension) {
  const range = `${dimension.minimum_tasks}–${dimension.maximum_tasks}`;
  const parts = [h("span", { class: "name" }, dimension.dimension)];
  if (dimension.status === "not-tested") {
    parts.push(h("span", { class: "count" }, "not-tested"));
    if (dimension.unavailable_reason) parts.push(h("span", { class: "reason" }, dimension.unavailable_reason));
  } else {
    parts.push(
      h("span", { class: "count" }, `${dimension.tasks_used} of ${range} tasks`),
      h("progress", { max: dimension.maximum_tasks, value: dimension.tasks_used, "aria-label": `${dimension.dimension} progress` }),
    );
    // Exactly what the CLI reports: the level it claimed, the range it gave, and its
    // confidence label. Nothing is rounded or inferred here.
    if (dimension.estimated_level) {
      const span = dimension.credible_low && dimension.credible_high ? ` (${dimension.credible_low}–${dimension.credible_high})` : "";
      parts.push(h("span", { class: "estimate" }, `${dimension.estimated_level}${span}`));
    }
    parts.push(h("span", { class: "confidence" }, dimension.confidence));
  }
  return h("li", { dataset: { dimension: dimension.dimension, status: dimension.status } }, parts);
}

function progress(screen) {
  return h("section", { class: "progress", "aria-label": "Progress" }, h("ul", {}, screen.dimensions.map(dimensionRow)));
}

function message(text, ...actions) {
  return h("section", { class: "panel" }, h("p", {}, text), actions.length ? h("div", { class: "actions" }, actions) : null);
}

function button(label, onclick, attributes = {}) {
  return h("button", { type: "button", onclick, disabled: state.busy, ...attributes }, label);
}

function drawStale() {
  app().replaceChildren(
    header(),
    message(
      "The LinguaWiki server was restarted, so this page's key no longer works. Open the page again from the address the launch command prints (linguawiki client serve).",
    ),
  );
}

async function drawPicker() {
  let listed;
  try {
    listed = await request("GET", "/runs");
  } catch (refusal) {
    state.error = { code: refusal.code, message: refusal.message };
    listed = { runs: [] };
  }
  const items = listed.runs.map((run) =>
    h(
      "li",
      {},
      h("span", {}, `${run.calibration_label} · ${run.status} · started ${run.started_at.slice(0, 10)}`),
      button(run.status === "paused" ? "Resume" : "Continue", () => resumeRun(run.run_id), {
        dataset: { run: run.run_id },
      }),
    ),
  );
  app().replaceChildren(
    header(),
    errorBanner() || "",
    items.length
      ? h("section", { class: "panel" }, h("h2", {}, "Pick up where you left off"), h("ul", { class: "runs" }, items))
      : "",
    message(items.length ? "Or begin again:" : "No calibration is open.", button("Start a calibration", startRun)),
  );
  drawStatus();
}

function taskView(screen, task) {
  const player = h("audio", { preload: "none" });
  const body = [h("p", { class: "dimension" }, task.dimension), h("p", { class: "prompt" }, task.prompt || "")];
  if (task.plays_audio) {
    const left = task.plays_remaining;
    const exhausted = left === 0;
    body.push(
      h(
        "div",
        { class: "listen" },
        button(task.plays_used ? "Play again" : "Play", () => play(task, player), {
          disabled: state.busy || exhausted,
          dataset: { role: "play" },
        }),
        h(
          "span",
          { class: "plays", dataset: { role: "plays" } },
          left === null || left === undefined ? `played ${task.plays_used}×` : `${left} play${left === 1 ? "" : "s"} left`,
        ),
        player,
      ),
    );
  }
  if (task.answer_with === "choice") {
    body.push(
      h(
        "div",
        { class: "choices", role: "group", "aria-label": "Answers" },
        task.presentation.choices.map((choice) =>
          // `display` is drawn; `value` is what is submitted, verbatim.
          button(choice.display || choice.value, () => answer(task, choice.value), {
            dataset: { value: choice.value },
          }),
        ),
      ),
    );
  } else {
    const field = h("input", {
      type: "text",
      name: "response",
      autocomplete: "off",
      spellcheck: "false",
      "aria-label": "Your answer",
      disabled: state.busy,
    });
    const shape = task.presentation && task.presentation.response_shape;
    const submit = () => {
      const text = field.value;
      if (!text.trim()) {
        state.error = { code: "invalid_arguments", message: "Type an answer first." };
        draw();
        return;
      }
      answer(task, text);
    };
    field.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submit();
    });
    body.push(
      h(
        "form",
        { class: "written", onsubmit: (event) => (event.preventDefault(), submit()) },
        shape ? h("label", { class: "shape" }, shape) : null,
        field,
        button("Submit", submit, { dataset: { role: "submit" } }),
      ),
    );
  }
  body.push(h("div", { class: "actions secondary" }, button("Pause", () => setRunStatus("paused"))));
  return h("section", { class: "task", dataset: { content: task.content_id } }, body);
}

function results(screen) {
  const closed = screen.status === "finalized" || screen.status === "abandoned";
  return message(
    closed ? `This calibration is ${screen.status}.` : "Every dimension that could be tested has finished.",
    closed ? button("Back to the start", () => ((state.run = null), draw())) : button("Finish and save the estimates", finish),
  );
}

async function draw(options = {}) {
  if (state.stale) return drawStale();
  if (!state.run || !state.screen) return drawPicker();
  const screen = state.screen;
  let content;
  const answerable = screen.outstanding.find((task) => !task.needs_judge);
  const judged = screen.outstanding.find((task) => task.needs_judge);
  if (screen.status === "paused") {
    content = message("This calibration is paused.", button("Resume", () => resumeRun(screen.run_id)));
  } else if (screen.status !== "in-progress") {
    content = results(screen);
  } else if (answerable) {
    content = taskView(screen, answerable);
    if (options.keepPlayer) {
      const fresh = content.querySelector("audio");
      fresh.replaceWith(options.keepPlayer);
    }
  } else if (judged) {
    content = message(
      "The next task in this run needs a judge to mark it, which this page cannot do. Continue it with the assess skill, or pause it here.",
      button("Pause", () => setRunStatus("paused")),
    );
  } else if (screen.dimensions.some((dimension) => dimension.status === "open")) {
    if (state.error) {
      // Never serve again on its own after a refusal: a refusal that repeats would loop.
      // The learner reads the message and decides.
      content = message("The next task could not be served.", button("Try again", () => act(serveNext)));
    } else if (!state.busy) {
      act(serveNext);
      return;
    } else {
      content = message("Choosing the next task…");
    }
  } else {
    content = results(screen);
  }
  app().replaceChildren(header(), errorBanner() || "", progress(screen), content);
  drawStatus();
}

// --- start -----------------------------------------------------------------------------

async function boot() {
  const run = readFragment();
  if (!state.token) {
    app().replaceChildren(header(), message("Open this page from the address that linguawiki client serve prints."));
    return;
  }
  // An operation a reload interrupted goes first, unchanged, so a lost response is
  // answered as a replay rather than repeated under a new key.
  const pending = loadPending();
  if (pending) {
    try {
      const data = await send(pending);
      if (pending.path === "/runs" && data && data.run_id) state.run = data.run_id;
      const match = /^\/runs\/(asm_[0-9A-Z]+)/.exec(pending.path);
      if (match) state.run = match[1];
    } catch (refusal) {
      state.error = { code: refusal.code, message: refusal.message };
    }
  }
  try {
    if (run && !state.run) {
      await openRun(run);
      if (!["in-progress", "paused"].includes(state.screen.status)) {
        state.error = { code: "assessment_run_closed", message: `That run is ${state.screen.status}, so it cannot be resumed.` };
        state.run = null;
        state.screen = null;
      }
    } else if (state.run) {
      await openRun(state.run);
    }
  } catch (refusal) {
    state.error = { code: refusal.code, message: refusal.message };
    state.run = null;
    state.screen = null;
  }
  draw();
}

boot();
