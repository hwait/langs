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
//
// A judge marks recorded and written answers later, in another process, and the server has
// no way to tell the page. So while anything waits on a judge the page re-reads `/screen`
// on a backoff, and a change it finds never disturbs the task the learner is on.

const TOKEN_HEADER = "X-LinguaWiki-Token";
const PENDING_KEY = "linguawiki.pending";
// The launch token and the run on screen, kept per tab so a reload can carry on. Not in
// the URL, which reaches history; sessionStorage is this origin's, this tab's, and goes
// when the tab does. A token from a newer launch URL replaces it, and a refused one is
// dropped.
const TOKEN_KEY = "linguawiki.token";
const RUN_KEY = "linguawiki.run";
// The batch being worked through: `{run, idempotency_key, batch_id, content_ids}`. Its own
// slot, not the pending one: the pending slot is cleared the moment an operation is
// answered, and a batch is a round of answers long. Written before the batch is sent, so a
// reload that lost the answer replays the same key rather than serving a second round.
const BATCH_KEY = "linguawiki.batch";
const STALE_TOKEN = new Set(["client_token_required", "client_token_invalid"]);
const MACHINE_RUN = { scoring: "machine", modalities: ["text", "audio"] };
// Where the track lets a judge mark something the learner makes here -- a recording it has
// both said it can make and agreed to keep, or a written answer it agrees to keep whole --
// the run is opened `machine+judged`, and those tasks wait for the judge. The server decides
// both (`recording.offered`, `written_offered`); the page only reads them. Speech is asked
// for only where a recording can be made, as it was under `machine+recorded`; writing
// always, so a track that keeps no whole answer is told so by name rather than by an
// unavailable modality.
function judgedRun(recording) {
  const modalities = ["text", "audio", "writing"];
  if (recording) modalities.push("speech");
  return { scoring: "machine+judged", modalities };
}
// `machine+recorded` is still drawn: a run opened under it keeps meaning what it meant.
const PAGE_SCORING = new Set(["machine", "machine+recorded", "machine+judged"]);
const JUDGED_SCORING = "machine+judged";
// How often the page asks whether a judge has delivered: from the first interval, doubling
// to the cap while nothing changes, and back to the first on any change.
const POLL_FIRST_MS = 2000;
const POLL_CAP_MS = 30000;

const state = {
  token: null,
  run: null, // the run id being drawn
  screen: null,
  busy: false, // an operation is in flight
  status: "", // "", "waiting", "offline", "unknown"
  error: null, // { code, message }
  recordings: new Map(), // content_id -> object URL
  // content_id -> what the learner has typed so far. Every redraw rebuilds the field, so
  // a draft that lived only in the DOM was erased by pressing Play or by any error.
  drafts: new Map(),
  stale: false,
  recording: null, // the track's recording policy, from discovery
  recorder: null, // { recorder, chunks, content_id } while the learner is speaking
  // content_id -> { capture_id, blob } of a recording made and not yet acknowledged, so a
  // retry resends the same bytes under the same identifier.
  takes: new Map(),
  // content_id -> { submission_key, response } of a written answer handed in and not yet
  // acknowledged. Pressing submit again with the same words resends them under the same
  // key, which the server answers as a replay rather than a second answer.
  answers: new Map(),
  written: false, // whether the track keeps a written answer whole, from discovery
  // Bumped whenever an operation starts. A poll that was sent before it is older than what
  // the operation will draw, and is dropped rather than drawn over it.
  epoch: 0,
  poll: { timer: null, delay: POLL_FIRST_MS },
  // A finalization refused because judgements are outstanding: the operation, kept so the
  // learner's "finish without these" is the same request with the flag added.
  finishing: null,
  // The last batch served nothing. A page that served again on its own would loop.
  servedNothing: false,
};

// --- the fragment ----------------------------------------------------------------------

function stored(name) {
  try {
    return sessionStorage.getItem(name);
  } catch {
    return null;
  }
}

function store(name, value) {
  try {
    if (value === null) sessionStorage.removeItem(name);
    else sessionStorage.setItem(name, value);
  } catch {
    // No storage: the page still works, it just cannot survive a reload.
  }
}

function readFragment() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const launched = params.get("token");
  if (launched) {
    store(TOKEN_KEY, launched);
    // A new launch is a new start: what this tab was showing belonged to the old one
    // unless the launch names it again.
    store(RUN_KEY, null);
  }
  state.token = launched || stored(TOKEN_KEY);
  const run = params.get("run") || stored(RUN_KEY);
  // Out of the address bar and out of history.
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

function showStatus(status) {
  if (state.status !== status) {
    state.status = status;
    drawStatus();
  }
}

// One request, retried only where a retry is a replay: a read, or a mutation that carries
// its key. A retryable refusal waits the server's `Retry-After` and sends the same body.
//
// `quiet` is for a poll: it never changes the status banner and never waits out a busy
// database. A poll the writer lock refuses -- usually the judge's own `record` -- is a poll
// to try at the next tick, not a wait to show the learner.
async function request(method, path, body, { binary = false, attempts = Infinity, quiet = false } = {}) {
  // A written answer's key is its `submission_key`, and it makes a resend a replay exactly
  // as `idempotency_key` does elsewhere.
  const keyed =
    body !== undefined && (typeof body.idempotency_key === "string" || typeof body.submission_key === "string");
  const mayRetry = method === "GET" || keyed;
  const setStatus = quiet ? () => {} : showStatus;
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
      if (attempt + 1 >= attempts) {
        setStatus("");
        throw new Refusal(0, {
          code: "client_unreachable",
          message: "The LinguaWiki server did not answer.",
        });
      }
      setStatus("offline");
      await sleep(Math.min(1000 * (attempt + 1), 5000));
      continue;
    }
    const type = response.headers.get("Content-Type") || "";
    // An answer that cannot be read is not an answer. The request may have landed -- a
    // response cut off mid-body is exactly that -- so it is handled like a connection that
    // dropped: a read or a keyed mutation is resent unchanged, which the key makes a
    // replay; a keyless mutation's outcome is reported as unknown. Treating it as a
    // refusal discarded the key, and the next press opened a second run.
    let envelope = null;
    try {
      if (binary && response.ok && !type.startsWith("application/json")) {
        const blob = await response.blob();
        setStatus("");
        return blob;
      }
      envelope = await response.json();
    } catch {
      envelope = null;
    }
    const readable =
      envelope !== null && typeof envelope === "object" && (envelope.ok === true || envelope.error);
    if (!readable) {
      if (!mayRetry || attempt + 1 >= attempts) {
        setStatus("unknown");
        throw new Refusal(response.status, {
          code: "client_unreachable",
          message: "The server's answer could not be read, so whether that landed is unknown.",
        });
      }
      setStatus("offline");
      await sleep(Math.min(1000 * (attempt + 1), 5000));
      continue;
    }
    if (response.ok && envelope.ok) {
      setStatus("");
      return envelope.data;
    }
    const error = envelope.error;
    if (STALE_TOKEN.has(error.code)) {
      state.stale = true;
      store(TOKEN_KEY, null);
      setStatus("");
      throw new Refusal(response.status, error);
    }
    if (response.status === 503 && error.retryable && mayRetry && !quiet) {
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

// One recording, sent as its own bytes under the identifier minted when the learner pressed
// stop. The identifier is the upload's idempotency key: a retry resends the same bytes
// under it, and the server answers a retry of a capture it already registered with that
// registration rather than a second one.
async function uploadTake(task, take) {
  const path = `/runs/${state.run}/tasks/${task.content_id}/captures/${take.capture_id}`;
  for (let attempt = 0; ; attempt += 1) {
    let response;
    try {
      response = await fetch(path, {
        method: "POST",
        headers: { [TOKEN_HEADER]: state.token, "Content-Type": take.blob.type || "audio/webm" },
        body: take.blob,
        cache: "no-store",
        credentials: "omit",
        referrerPolicy: "no-referrer",
      });
    } catch {
      showStatus("offline");
      await sleep(Math.min(1000 * (attempt + 1), 5000));
      continue;
    }
    let envelope = null;
    try {
      envelope = await response.json();
    } catch {
      envelope = null;
    }
    // An answer that cannot be read is not an answer -- the upload may have landed -- so it
    // is resent under the same identifier, which the server answers as a replay. Reading
    // `error.code` off an envelope that has none threw, and stranded the take.
    const readable =
      envelope !== null &&
      typeof envelope === "object" &&
      ((response.ok && envelope.ok === true) || (envelope.error && typeof envelope.error.code === "string"));
    if (!readable) {
      showStatus("offline");
      await sleep(Math.min(1000 * (attempt + 1), 5000));
      continue;
    }
    if (response.ok && envelope.ok) {
      showStatus("");
      state.takes.delete(task.content_id);
      return envelope.data;
    }
    const error = envelope.error;
    if (STALE_TOKEN.has(error.code)) {
      state.stale = true;
      store(TOKEN_KEY, null);
      showStatus("");
      throw new Refusal(response.status, error);
    }
    if (response.status === 503 && error.retryable) {
      showStatus("waiting");
      await sleep((Number(response.headers.get("Retry-After")) || 1) * 1000);
      continue;
    }
    showStatus("");
    // A refusal is an answer: the server removed the bytes and said why. This take is over.
    state.takes.delete(task.content_id);
    throw new Refusal(response.status, error);
  }
}

// --- actions ---------------------------------------------------------------------------

async function act(work) {
  if (state.busy) return; // two clicks are one answer
  state.busy = true;
  state.epoch += 1;
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
    const recording = Boolean(state.recording && state.recording.offered);
    const shape = recording || state.written ? judgedRun(recording) : MACHINE_RUN;
    const run = await keyed("POST", "/runs", shape);
    state.run = run.run_id;
  });
}

// Press to start, press to stop. No countdown: the learner decides when they have finished.
async function startRecording(task) {
  if (state.busy || state.recorder) return;
  state.error = null;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    state.error = {
      code: "client_microphone_unavailable",
      message: "The browser did not allow the microphone, so nothing was recorded.",
    };
    draw();
    return;
  }
  const recorder = new MediaRecorder(stream);
  const chunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  });
  state.recorder = { recorder, chunks, stream, content_id: task.content_id };
  recorder.start();
  draw();
}

// Stop the microphone and throw the take away. Leaving the task view for any reason --
// pausing, a refusal, the task settling -- must not leave a live microphone behind a screen
// with no stop button on it.
function discardRecording() {
  const active = state.recorder;
  if (!active) return;
  state.recorder = null;
  try {
    if (active.recorder.state !== "inactive") active.recorder.stop();
  } catch {
    /* already stopped */
  }
  active.stream.getTracks().forEach((track) => track.stop());
}

function stopRecording(task) {
  const active = state.recorder;
  if (!active || active.content_id !== task.content_id) return;
  active.recorder.addEventListener(
    "stop",
    () => {
      active.stream.getTracks().forEach((track) => track.stop());
      state.recorder = null;
      const blob = new Blob(active.chunks, { type: active.recorder.mimeType || "audio/webm" });
      state.takes.set(task.content_id, { capture_id: crypto.randomUUID(), blob });
      submitTake(task);
    },
    { once: true },
  );
  active.recorder.stop();
}

function submitTake(task) {
  const take = state.takes.get(task.content_id);
  if (!take) return Promise.resolve();
  return act(() => uploadTake(task, take));
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
  discardRecording();
  return act(() => request("POST", `/runs/${state.run}/status`, { status }));
}

// --- batches ---------------------------------------------------------------------------

function loadBatch() {
  try {
    const raw = sessionStorage.getItem(BATCH_KEY);
    const batch = raw ? JSON.parse(raw) : null;
    return batch && typeof batch.idempotency_key === "string" ? batch : null;
  } catch {
    return null;
  }
}

function saveBatch(batch) {
  store(BATCH_KEY, batch === null ? null : JSON.stringify(batch));
}

// What a batch report says the page holds: its tasks in the order the batch served them.
function remember(batch, report) {
  saveBatch({ ...batch, batch_id: report.batch_id, content_ids: report.tasks.map((task) => task.content_id) });
}

// Serve one task in every free dimension at once. The key is written down before it is
// sent, so a reload replays this round rather than serving a second one beside it.
async function serveBatch() {
  const batch = { run: state.run, idempotency_key: crypto.randomUUID(), batch_id: null, content_ids: [] };
  saveBatch(batch);
  let report;
  try {
    report = await request("POST", `/runs/${state.run}/batch`, { idempotency_key: batch.idempotency_key });
  } catch (refusal) {
    // A refusal served nothing, and the key is spent on it. Only an unreachable server
    // leaves the batch to replay, because then nobody knows whether it landed.
    if (refusal.code !== "client_unreachable") saveBatch(null);
    throw refusal;
  }
  // A batch that served nothing is settled the moment it is answered. Kept, it would be
  // replayed after every reload -- serving nothing again -- once dimensions had freed up.
  state.servedNothing = report.tasks.length === 0;
  if (state.servedNothing) saveBatch(null);
  else remember(batch, report);
}

// On a reload, before anything new is served: the stored batch, replayed by its key. The
// server hands back the same tasks with where each stands now, and serves nothing.
async function replayBatch() {
  const batch = loadBatch();
  if (!batch) return;
  if (batch.run !== state.run) {
    saveBatch(null);
    return;
  }
  try {
    const report = await request("POST", `/runs/${batch.run}/batch`, { idempotency_key: batch.idempotency_key });
    if (report.tasks.every((task) => task.state !== "served")) saveBatch(null);
    else remember(batch, report);
  } catch (refusal) {
    // The run moved on in a way the batch cannot follow -- closed, or the key refused. The
    // screen is what the page draws from either way, so the batch is let go and the
    // refusal said.
    saveBatch(null);
    throw refusal;
  }
}

// The batch is worked through until no task in it is still waiting for the learner. An
// answer handed to a judge is the learner's part done: holding the round open until the
// judge marked it would stop the learner working in every other dimension.
function settleBatch(screen) {
  const batch = loadBatch();
  if (!batch) return null;
  const waitingOnLearner = new Set(
    screen.outstanding.filter((task) => task.state !== "awaiting-judge").map((task) => task.content_id),
  );
  if (batch.run !== screen.run_id || !batch.content_ids.some((id) => waitingOnLearner.has(id))) {
    // A batch whose response never arrived has no members yet; it is kept for the replay.
    if (batch.run === screen.run_id && batch.batch_id === null) return batch;
    saveBatch(null);
    return null;
  }
  return batch;
}

function answer(task, response) {
  return act(() =>
    keyed("POST", `/runs/${state.run}/results`, { content_id: task.content_id, response }),
  );
}

// A written answer for a judge. The key is the page's own identifier for the answer, kept
// with the words until the server acknowledges them: the same words sent again go under the
// same key, so a response lost on the way back is replayed rather than handed in twice.
function submitWritten(task, text) {
  const kept = state.answers.get(task.content_id);
  const submission =
    kept && kept.response === text ? kept : { submission_key: crypto.randomUUID(), response: text };
  state.answers.set(task.content_id, submission);
  return act(async () => {
    try {
      await send({
        method: "POST",
        path: `/runs/${state.run}/tasks/${task.content_id}/submission`,
        body: { submission_key: submission.submission_key, response: submission.response },
      });
    } catch (refusal) {
      // A refusal is an answer; the next press is a new hand-in under a new key. Unknown
      // is not, and keeps the key.
      if (refusal.code !== "client_unreachable") state.answers.delete(task.content_id);
      throw refusal;
    }
    state.answers.delete(task.content_id);
    state.drafts.delete(task.content_id);
  });
}

function finish() {
  return act(async () => {
    const operation = {
      method: "POST",
      path: `/runs/${state.run}/finalization`,
      body: { reason: "completed", idempotency_key: crypto.randomUUID() },
    };
    state.finishing = null;
    try {
      await send(operation);
    } catch (refusal) {
      // Answers still waiting for a judge. Not an error to show: a choice to offer.
      if (refusal.code !== "assessment_judgement_outstanding") throw refusal;
      state.finishing = { operation };
    }
  });
}

// "Finish without these": the refused request again, under its key, with the flag the
// refusal named. The flag is part of the request's hash, so this is a new request rather
// than a replay of the refusal.
function finishWithout() {
  const { operation } = state.finishing;
  return act(async () => {
    await send({ ...operation, body: { ...operation.body, exclude_outstanding: true } });
    state.finishing = null;
  });
}

function keepWaiting() {
  state.finishing = null;
  draw();
}

// --- waiting for a judge ---------------------------------------------------------------

// What a poll compares: the run's and each dimension's standing, and every judgement it
// waits on. Claims change in place, so there is nothing to count -- equal means no redraw.
function signature(screen) {
  return JSON.stringify([
    screen.status,
    screen.progress,
    screen.dimensions.map((dimension) => [dimension.dimension, dimension.status, dimension.progress]),
    screen.outstanding_judgements,
  ]);
}

// Polling runs while something is outstanding on a run that is going, and stops when the
// page is refusing: a refusal that repeats must not loop.
function pollWanted() {
  const screen = state.screen;
  return Boolean(
    !state.stale &&
      state.run &&
      screen &&
      screen.run_id === state.run &&
      screen.status === "in-progress" &&
      screen.outstanding_judgements &&
      screen.outstanding_judgements.length &&
      !state.error,
  );
}

function stopPolling() {
  if (state.poll.timer !== null) clearTimeout(state.poll.timer);
  state.poll.timer = null;
}

function schedulePoll() {
  if (!pollWanted()) {
    stopPolling();
    state.poll.delay = POLL_FIRST_MS;
    return;
  }
  if (state.poll.timer !== null || document.visibilityState === "hidden") return;
  state.poll.timer = setTimeout(pollOnce, state.poll.delay);
}

function backOff() {
  state.poll.delay = Math.min(state.poll.delay * 2, POLL_CAP_MS);
}

async function pollOnce() {
  state.poll.timer = null;
  // An operation in flight reads the screen itself when it ends.
  if (!pollWanted() || state.busy || document.visibilityState === "hidden") return schedulePoll();
  const epoch = state.epoch;
  const before = signature(state.screen);
  let fresh;
  try {
    fresh = await request("GET", `/runs/${state.run}/screen`, undefined, { attempts: 1, quiet: true });
  } catch (refusal) {
    if (state.stale) return draw();
    // Busy (the writer lock), unreachable, or unreadable: keep the screen, try at the next
    // tick. Anything else is the page refusing, and stops the polling.
    const transient = refusal.code === "client_unreachable" || (refusal.status === 503 && refusal.retryable);
    if (!transient) {
      state.error = { code: refusal.code, message: refusal.message };
      if (!taskOnScreen()) draw();
      return schedulePoll();
    }
    backOff();
    return schedulePoll();
  }
  // Something the learner did since has drawn, or will draw, a newer screen.
  if (epoch !== state.epoch || state.busy) return schedulePoll();
  if (signature(fresh) === before) {
    backOff();
    return schedulePoll();
  }
  state.screen = fresh;
  state.poll.delay = POLL_FIRST_MS;
  state.servedNothing = false;
  if (taskOnScreen()) {
    // The learner is on a task. Only what surrounds it changes: their typing, a live
    // recording, and a playing recording are all inside the node that is kept. A dimension
    // this found open is served when they finish, through the normal post-answer draw.
    refreshSurroundings(fresh);
    schedulePoll();
  } else {
    draw();
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    stopPolling();
  } else if (pollWanted() && state.poll.timer === null) {
    // Back at once: a learner returning to the tab should not wait out a backoff.
    state.poll.timer = setTimeout(pollOnce, 0);
  }
});

async function play(task, player) {
  if (state.busy) return;
  state.busy = true;
  state.epoch += 1;
  state.error = null;
  draw();
  try {
    // The bytes first: fetching is not a play, so a recording that cannot be had costs the
    // learner nothing. Bounded, because this is a click waiting on an answer.
    let url = state.recordings.get(task.content_id);
    if (!url) {
      let blob;
      try {
        blob = await request("GET", `/runs/${state.run}/tasks/${task.content_id}/audio`, undefined, {
          binary: true,
          attempts: 3,
        });
      } catch (refusal) {
        throw new Refusal(refusal.status, {
          code: refusal.code,
          message: `The recording could not be loaded, so no play was used. ${refusal.message}`,
        });
      }
      url = URL.createObjectURL(blob);
      state.recordings.set(task.content_id, url);
    }
    // Recorded before it is heard, and playback starts only when the server agrees.
    await keyed("POST", `/runs/${state.run}/tasks/${task.content_id}/plays`);
    player.src = url;
    player.currentTime = 0;
    try {
      await player.play();
    } catch {
      // The play is already counted; saying so is the honest answer, and pressing play
      // again is the way forward while any remain.
      throw new Refusal(0, {
        code: "client_playback_failed",
        message: "The browser did not start the recording. That play was counted; press play again if any remain.",
      });
    }
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
    if (dimension.progress === "waiting") {
      parts.push(h("span", { class: "reason", dataset: { role: "dimension-waiting" } }, "waiting for a judge"));
    }
  }
  return h(
    "li",
    { dataset: { dimension: dimension.dimension, status: dimension.status, progress: dimension.progress || "" } },
    parts,
  );
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
  state.recording = listed.recording || null;
  state.written = listed.written_offered === true;
  const items = listed.runs.map((run) =>
    h(
      "li",
      {},
      h(
        "span",
        {},
        `${run.calibration_label} · ${run.status} · started ${run.started_at.slice(0, 10)}`,
        PAGE_SCORING.has(run.scoring) ? "" : " · opened elsewhere: continue it with the assess skill",
      ),
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
    state.recording && state.recording.offered
      ? h("p", { class: "note", dataset: { role: "retention" } }, retentionNote(state.recording))
      : null,
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
  if (task.answer_with === "recording") {
    const recording = state.recorder && state.recorder.content_id === task.content_id;
    const pending = state.takes.get(task.content_id);
    body.push(
      h(
        "div",
        { class: "speak" },
        recording
          ? button("Stop recording", () => stopRecording(task), { dataset: { role: "stop" }, disabled: false })
          : button("Start recording", () => startRecording(task), { dataset: { role: "record" } }),
        pending && !recording ? button("Send again", () => submitTake(task), { dataset: { role: "resend" } }) : null,
        h("span", { class: "recording-state", dataset: { role: "recording-state" } }, recording ? "Recording…" : ""),
      ),
      h("p", { class: "note", dataset: { role: "retention" } }, retentionNote(screen.recording)),
    );
  } else if (task.answer_with === "choice") {
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
  } else if (writable(screen, task)) {
    // Long writing for a judge: a box rather than a line, and Enter is a new line, not a
    // submission. Handed in, it waits for the judge like a recording does.
    const field = h("textarea", {
      name: "written",
      rows: 8,
      spellcheck: "false",
      "aria-label": "Your answer",
      disabled: state.busy,
    });
    field.value = state.drafts.get(task.content_id) || "";
    field.addEventListener("input", () => state.drafts.set(task.content_id, field.value));
    const submit = () => {
      const text = field.value;
      if (!text.trim()) {
        state.error = { code: "invalid_arguments", message: "Write an answer first." };
        draw();
        return;
      }
      submitWritten(task, text);
    };
    body.push(
      h(
        "form",
        { class: "written", onsubmit: (event) => (event.preventDefault(), submit()) },
        field,
        button("Hand in for marking", submit, { dataset: { role: "hand-in" } }),
      ),
      h("p", { class: "note" }, "A judge reads your answer to mark it, so it is kept whole."),
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
    field.value = state.drafts.get(task.content_id) || "";
    field.addEventListener("input", () => state.drafts.set(task.content_id, field.value));
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

// The track's retention policy as it applies to *this* recording, said rather than chosen:
// the page never overrides it, and it must not promise a deletion the sweep never does.
// `delete-after-ingestion` is about recordings a package brought in; a recording made here
// is not one, so under it this recording is kept until the learner removes it. Under a
// rolling window, a recording a judge has not heard yet is held until it is heard.
function retentionNote(policy) {
  if (!policy) return "";
  const kept = {
    keep: "Your recording is kept until you remove it.",
    "rolling-days": `Your recording is removed ${policy.retention_days || "a set number of"} day(s) after you make it, though not before a judge has heard it.`,
    "delete-after-ingestion":
      "Your recording is kept until you remove it: this track removes imported recordings after they are processed, and a recording made here is not one.",
  }[policy.retention_policy];
  return `${kept || ""} A judge listens to it to mark your answer.`;
}

// A written task a judge marks from what the learner types. Only a `machine+judged` run
// serves one, and only where the track keeps the answer whole -- the server decided that
// when it served the task.
function writable(screen, task) {
  return (
    screen.scoring === JUDGED_SCORING &&
    task.needs_judge &&
    task.answer_with === "text" &&
    task.modality === "writing"
  );
}

function when(stamp) {
  const time = new Date(stamp);
  return Number.isNaN(time.getTime()) ? String(stamp) : time.toLocaleString();
}

// What each answer is waiting *on*: a queue nobody has picked up reads differently from one
// being worked, and a verdict held for a resume differently again.
function judgementLine(judgement) {
  const what = judgement.kind === "text" ? "Written answer" : "Recorded answer";
  const where = judgement.dimension ? ` (${judgement.dimension})` : "";
  const standing = {
    unclaimed: judgement.unclaimed_since
      ? `waiting for a judge since ${when(judgement.unclaimed_since)}`
      : "waiting for a judge",
    claimed: `being marked by ${judgement.claimed_by || "a judge"}${judgement.claimed_until ? ` until ${when(judgement.claimed_until)}` : ""}`,
    held: "marked; the mark is held until the run resumes",
    lapsed: "no judge could mark it, so it will be set aside",
  }[judgement.claim_state];
  return h(
    "li",
    { dataset: { submission: judgement.submission_id, claimState: judgement.claim_state } },
    `${what}${where}: ${standing || judgement.claim_state}`,
  );
}

function waitingNote(screen) {
  const judgements = screen.outstanding_judgements || [];
  if (!judgements.length) return null;
  const count = judgements.length;
  return h(
    "div",
    { class: "note", dataset: { role: "awaiting-judge" } },
    h("p", {}, `${count} answer${count === 1 ? " is" : "s are"} waiting for a judge to mark.`),
    h("ul", { class: "judgements" }, judgements.map(judgementLine)),
  );
}

// The slot the waiting note lives in, present on every run screen so a poll has one place
// to put it without touching anything around it.
function judgementSlot(screen) {
  return h("div", { dataset: { role: "judgements" } }, waitingNote(screen) || "");
}

function taskOnScreen() {
  return Boolean(document.querySelector("#app section.task"));
}

// A changed poll while a task is on screen: the progress and the waiting note are redrawn,
// and nothing else is touched -- the task node, with whatever the learner has typed, is
// recording, or is listening to, stays exactly where it is.
function refreshSurroundings(screen) {
  const root = app();
  const shown = root.querySelector("section.progress");
  if (shown) shown.replaceWith(progress(screen));
  const slot = root.querySelector("[data-role=judgements]");
  if (slot) slot.replaceWith(judgementSlot(screen));
}

function finishChoice(screen) {
  const count = (screen.outstanding_judgements || []).length;
  return message(
    `${count} answer${count === 1 ? " is" : "s are"} still waiting for a judge. Wait for the marks, or finish now without ${count === 1 ? "it" : "them"}: an answer left out is withdrawn and does not count towards the estimates.`,
    button("Wait for the judge", keepWaiting, { dataset: { role: "keep-waiting" } }),
    button("Finish without these", finishWithout, { dataset: { role: "finish-without" } }),
  );
}

function results(screen) {
  const closed = screen.status === "finalized" || screen.status === "abandoned";
  return message(
    closed ? `This calibration is ${screen.status}.` : "Every dimension that could be tested has finished.",
    closed ? button("Back to the start", () => ((state.run = null), draw())) : button("Finish and save the estimates", finish),
  );
}

function waitingPanel() {
  return message(
    "Everything left in this calibration is waiting for a judge to mark an answer. This page checks for the marks on its own and carries on when one arrives. You can also pause here and come back, or finish now.",
    button("Pause", () => setRunStatus("paused")),
    button("Finish now", finish, { dataset: { role: "finish-now" } }),
    state.error ? button("Check again", () => act(async () => {})) : null,
  );
}

async function draw(options = {}) {
  if (state.stale || !state.run || !state.screen) discardRecording();
  if (state.stale) {
    stopPolling();
    return drawStale();
  }
  store(RUN_KEY, state.run);
  if (!state.run || !state.screen) {
    stopPolling();
    return drawPicker();
  }
  const screen = state.screen;
  let content;
  // A spoken task is answered here by recording it, when the track permits that; a written
  // task for a judge by typing it, under a run opened for that. A judge marks both later.
  // Everything else answerable here is scored by the server.
  const recordable = (task) =>
    task.answer_with === "recording" && screen.recording && screen.recording.offered;
  const open = screen.outstanding.filter((task) => task.state !== "awaiting-judge");
  const canAnswer = (task) =>
    (!task.needs_judge && !task.missing_recording) || recordable(task) || writable(screen, task);
  // The batch being worked through goes first, in the order it was served; anything else
  // outstanding -- a task served before batches, or by another surface -- after it.
  const batch = settleBatch(screen);
  const inBatch = batch ? batch.content_ids.map((id) => open.find((task) => task.content_id === id)) : [];
  const answerable = inBatch.find((task) => task && canAnswer(task)) || open.find(canAnswer);
  const judged = open.find((task) => !canAnswer(task));
  // Only a run this page shaped is served from it. Under `any` the next task can need a
  // judge or a recording this page does not have, and serving it would spend an exposure
  // on a question the learner cannot answer here.
  const servable = PAGE_SCORING.has(screen.scoring);
  // A dimension is free when it is open as the learner meets it and no task in it is in
  // the learner's hands. One waiting on a judge is `waiting`, not `open`, and is not served
  // in; the run may still serve the others.
  const holding = new Set(open.map((task) => task.dimension));
  const free = screen.dimensions.some(
    (dimension) => dimension.status === "open" && dimension.progress === "open" && !holding.has(dimension.dimension),
  );
  const outstanding = (screen.outstanding_judgements || []).length > 0;
  // The choice between waiting and finishing is about a run with nothing else to do. Once
  // the judge has delivered, or there is work again, it no longer describes anything.
  if (!outstanding || screen.status !== "in-progress" || answerable || free) state.finishing = null;
  if (!free) state.servedNothing = false;
  // The only screen a live recording may sit behind is the task it is recording.
  if (
    state.recorder &&
    (screen.status !== "in-progress" || !answerable || answerable.content_id !== state.recorder.content_id)
  ) {
    discardRecording();
  }
  if (screen.status === "paused") {
    content = message("This calibration is paused.", button("Resume", () => resumeRun(screen.run_id)));
  } else if (screen.status !== "in-progress") {
    content = results(screen);
  } else if (state.finishing) {
    content = finishChoice(screen);
  } else if (answerable) {
    content = taskView(screen, answerable);
    if (options.keepPlayer) {
      const fresh = content.querySelector("audio");
      fresh.replaceWith(options.keepPlayer);
    }
  } else if (judged) {
    content = message(
      judged.needs_judge
        ? "The next task in this run needs a judge to mark it, which this page cannot do. Continue it with the assess skill, or pause it here."
        : "The next task in this run is a listening task with no recording, which this page cannot play. Continue it with the assess skill, or pause it here.",
      button("Pause", () => setRunStatus("paused")),
    );
  } else if (!servable && screen.dimensions.some((dimension) => dimension.status === "open")) {
    content = message(
      "This calibration was opened outside this page, so its next task may need a judge or a recording this page does not have. Continue it with the assess skill, or pause it here.",
      button("Pause", () => setRunStatus("paused")),
    );
  } else if (free) {
    if (state.error || state.servedNothing) {
      // Never serve again on its own after a refusal, or after a round that served
      // nothing: either one repeated would loop. The learner reads the message and decides.
      content = message(
        state.error ? "The next task could not be served." : "Nothing could be served just now.",
        button("Try again", () => act(serveBatch)),
      );
    } else if (!state.busy) {
      act(serveBatch);
      return;
    } else {
      content = message("Choosing the next task…");
    }
  } else if (screen.progress === "waiting" || outstanding) {
    // Waiting is a state, and said: what is left is a judge's to do, and the page asks
    // after it on its own.
    content = waitingPanel();
  } else {
    content = results(screen);
  }
  app().replaceChildren(header(), errorBanner() || "", progress(screen), judgementSlot(screen), content);
  drawStatus();
  schedulePoll();
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
  // A round a reload interrupted is replayed by its key before anything new is served, so
  // the tasks it served are worked through rather than joined by a second round.
  if (state.run && state.screen && state.screen.status === "in-progress") {
    try {
      await replayBatch();
      state.screen = await request("GET", `/runs/${state.run}/screen`);
    } catch (refusal) {
      state.error = state.error || { code: refusal.code, message: refusal.message };
    }
  }
  draw();
}

boot();
