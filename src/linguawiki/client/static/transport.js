// How a LinguaWiki page talks to its server. One module for every page, so the rule that
// decides when a request may be sent again is written once.
//
// Three rules shape it:
//   * A read, or a mutation that carries its key, is retried: the key makes a second send a
//     replay. A keyless mutation is never retried, because a second attempt could repeat
//     work that landed; its outcome is reported as unknown instead.
//   * The key is found where the caller says it is (`key`), or at the top of the body. An
//     imported batch carries its producer's key *inside* the batch, and copying it to the top
//     would make two copies that could disagree.
//   * A persisted operation is written to sessionStorage before it is sent and cleared on a
//     definitive answer -- a refusal is an answer too -- so a reload resends the same request
//     under the same key rather than a new one.

export const TOKEN_HEADER = "X-LinguaWiki-Token";
// The launch token, kept per tab so a reload can carry on. Not in the URL, which reaches
// history; sessionStorage is this origin's, this tab's, and goes when the tab does.
export const TOKEN_KEY = "linguawiki.token";
export const STALE_TOKEN = new Set(["client_token_required", "client_token_invalid"]);

export class Refusal extends Error {
  constructor(status, error) {
    super(error.message);
    this.status = status;
    this.code = error.code;
    this.retryable = Boolean(error.retryable);
    this.details = error.details || [];
  }
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function stored(name) {
  try {
    return sessionStorage.getItem(name);
  } catch {
    return null;
  }
}

export function store(name, value) {
  try {
    if (value === null) sessionStorage.removeItem(name);
    else sessionStorage.setItem(name, value);
  } catch {
    // No storage: the page still works, it just cannot survive a reload.
  }
}

export function storedJson(name) {
  const raw = stored(name);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function storeJson(name, value) {
  store(name, value === null ? null : JSON.stringify(value));
}

// `token` reads the current launch token; `onStatus` shows "", "waiting", "offline", or
// "unknown"; `onStale` hears that the token was refused. `pendingKey` is this page's own
// pending slot, so one page never resends another page's operation.
export function createTransport({ token, onStatus, onStale, pendingKey }) {
  // One request, retried only where a retry is a replay. A retryable refusal waits the
  // server's `Retry-After` and sends the same body.
  //
  // `quiet` is for a poll: it never changes the status banner and never waits out a busy
  // database. `key` names the operation's key when it is not at the top of the body.
  async function request(
    method,
    path,
    body,
    { binary = false, attempts = Infinity, quiet = false, key = undefined } = {},
  ) {
    // A written answer's key is its `submission_key`, and it makes a resend a replay exactly
    // as `idempotency_key` does elsewhere.
    const found =
      key !== undefined
        ? key
        : body === undefined
          ? undefined
          : typeof body.idempotency_key === "string"
            ? body.idempotency_key
            : body.submission_key;
    const keyed = typeof found === "string" && found.trim() !== "";
    const mayRetry = method === "GET" || keyed;
    const setStatus = quiet ? () => {} : onStatus;
    for (let attempt = 0; ; attempt += 1) {
      let response;
      try {
        response = await fetch(path, {
          method,
          headers: {
            [TOKEN_HEADER]: token(),
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
      // replay; a keyless mutation's outcome is reported as unknown.
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
        onStale();
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

  function savePending(operation) {
    storeJson(pendingKey, operation);
  }

  function clearPending() {
    store(pendingKey, null);
  }

  function loadPending() {
    return storedJson(pendingKey);
  }

  // A persisted operation: written before it is sent, cleared on a definitive answer.
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

  // A keyed operation under a fresh key, minted now.
  async function keyed(method, path, fields = {}) {
    return send({ method, path, body: { ...fields, idempotency_key: crypto.randomUUID() } });
  }

  return { request, send, keyed, savePending, clearPending, loadPending };
}
