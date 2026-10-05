// In-memory identity for one manual save. Retrying an uncertain response
// keeps the key; changing the payload or completing a save starts a new one.
// `continueId` lets a FRESH editor (after a refresh) adopt the request id of
// an unresolved pending save whose payload it is re-saving unchanged, so the
// plain save button continues that original request instead of minting a
// parallel one that would bypass its idempotency.
export class SaveRequest {
  private payload = "";
  private id = "";

  forPayload(payload: string, continueId?: string): string {
    if (!this.id || this.payload !== payload) {
      this.payload = payload;
      this.id = continueId ?? crypto.randomUUID();
    }
    return this.id;
  }

  complete(): void {
    this.payload = "";
    this.id = "";
  }
}

/**
 * Local registry of saves whose outcome was never confirmed. A record is
 * written synchronously BEFORE the request leaves the browser and removed
 * when the outcome is known (success, or a definitive 4xx rejection). On
 * refresh/relogin the editor looks each pending save up by its request id —
 * which the API stores as the created row's id — and only replays when the
 * lookup 404s, so an ambiguous save can converge to exactly one row.
 */
export type PendingSaveKind = "note_revision" | "student_note";

export type PendingSave = {
  requestId: string;
  kind: PendingSaveKind;
  /** Exact JSON body to replay byte-for-byte (request id included). */
  body: string;
  savedAt: string;
  noteId?: string;
  encounterId?: string;
  /**
   * Work-positioning only (never clinical content): the owning patient and
   * the editing session, recorded so the workbench can group the pending
   * save with its own buffer and deep-link to the exact object. Records
   * written before these fields existed simply lack them and stay valid.
   */
  patientId?: string;
  sessionId?: string;
};

/**
 * One pending save as the UI shows it: every record is listed IMMEDIATELY
 * as `verifying`, then marked per entry as its lookup returns — `unresolved`
 * (server answered "not found yet") or `failed` (cannot ask the server now).
 * A verified save leaves the list entirely. Records are never hidden while
 * their outcome is unknown, whatever the network does.
 */
export type { PendingSaveCheck, PendingSaveCheckStatus } from "@/lib/pending-save-checks";

const PENDING_KEY_PREFIX = "shen.pending-save.v1.";

function getStorage(): Storage | null {
  try {
    return (globalThis as { localStorage?: Storage }).localStorage ?? null;
  } catch {
    return null;
  }
}

function parsePendingSave(raw: string | null): PendingSave | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<PendingSave> | null;
    if (
      !parsed ||
      typeof parsed.requestId !== "string" ||
      !parsed.requestId ||
      (parsed.kind !== "note_revision" && parsed.kind !== "student_note") ||
      typeof parsed.body !== "string" ||
      !parsed.body ||
      typeof parsed.savedAt !== "string"
    ) {
      return null;
    }
    if (parsed.kind === "note_revision" && typeof parsed.noteId !== "string") return null;
    if (parsed.kind === "student_note" && typeof parsed.encounterId !== "string") return null;
    // Positioning fields are optional: pre-positioning records lack them, and
    // malformed values are dropped rather than invalidating the record.
    if (typeof parsed.patientId !== "string") delete parsed.patientId;
    if (typeof parsed.sessionId !== "string") delete parsed.sessionId;
    return parsed as PendingSave;
  } catch {
    return null;
  }
}

export function recordPendingSave(entry: PendingSave): boolean {
  const storage = getStorage();
  if (!storage) return false;
  try {
    storage.setItem(PENDING_KEY_PREFIX + entry.requestId, JSON.stringify(entry));
    return true;
  } catch {
    return false;
  }
}

export function listPendingSaves(filter: { kind?: PendingSaveKind; noteId?: string; encounterId?: string } = {}): PendingSave[] {
  const storage = getStorage();
  if (!storage) return [];
  const found: PendingSave[] = [];
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key || !key.startsWith(PENDING_KEY_PREFIX)) continue;
      const parsed = parsePendingSave(storage.getItem(key));
      if (!parsed) continue;
      if (filter.kind && parsed.kind !== filter.kind) continue;
      if (filter.noteId && parsed.noteId !== filter.noteId) continue;
      if (filter.encounterId && parsed.encounterId !== filter.encounterId) continue;
      found.push(parsed);
    }
  } catch {
    // Storage misbehaving; return what was collected so far.
  }
  return found.sort((a, b) => (a.savedAt < b.savedAt ? 1 : a.savedAt > b.savedAt ? -1 : 0));
}

export function clearPendingSave(requestId: string): void {
  const storage = getStorage();
  if (!storage) return;
  try {
    storage.removeItem(PENDING_KEY_PREFIX + requestId);
  } catch {
    // Storage unavailable; nothing to clean up.
  }
}

/** One pending record by request id, or null when absent/malformed. */
export function getPendingSave(requestId: string): PendingSave | null {
  const storage = getStorage();
  if (!storage) return null;
  try {
    return parsePendingSave(storage.getItem(PENDING_KEY_PREFIX + requestId));
  } catch {
    return null;
  }
}

function samePlainObject(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const aKeys = Object.keys(a);
  const bKeys = Object.keys(b);
  if (aKeys.length !== bKeys.length) return false;
  return aKeys.every((key) => a[key] === b[key]);
}

/**
 * The pending save whose payload a fresh editor is about to re-save
 * unchanged, if any. After a refresh the edit revision only survives
 * through the recovery buffer, so an exact match against a still-unresolved
 * request's recorded body (everything but its request id) IS the same
 * revision continuing — not a text coincidence — and the caller must reuse
 * that request id so the server's idempotency keeps covering the save.
 * Records are scanned newest-first; malformed bodies never match.
 */
export function findContinuablePendingSave(
  filter: { kind: PendingSaveKind; noteId?: string; encounterId?: string },
  payload: Record<string, unknown>,
): PendingSave | null {
  for (const entry of listPendingSaves(filter)) {
    try {
      const parsed = JSON.parse(entry.body) as Record<string, unknown> | null;
      if (!parsed || typeof parsed !== "object") continue;
      const recordedPayload: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(parsed)) {
        if (key !== "request_id") recordedPayload[key] = value;
      }
      if (samePlainObject(recordedPayload, payload)) return entry;
    } catch {
      continue;
    }
  }
  return null;
}

export type SaveSubmission = {
  requestId: string;
  /** Exact JSON body to send byte-for-byte (request id included). */
  body: string;
  /** True when an unresolved pending record was adopted: its storage entry stays as-is and must NOT be re-registered. */
  continued: boolean;
  /** False when the pending record could not be written locally (degraded cross-refresh protection; the save still proceeds). */
  recorded: boolean;
};

/**
 * The 编辑与保存 module's single submission path, shared by both editing
 * surfaces. ONE call does everything that must happen before a save leaves
 * the browser: continue the unresolved pending request when the exact same
 * revision is re-saved (cross-refresh continuation), otherwise mint a fresh
 * request id, and register the pending record BEFORE the request leaves.
 * Editors only decide what to save; the identity, the byte-for-byte body and
 * the local record live here.
 */
export function beginSaveSubmission(params: {
  kind: PendingSaveKind;
  filter: { noteId?: string; encounterId?: string; patientId?: string };
  /** The compared/recorded payload WITHOUT the request id. */
  payload: Record<string, unknown>;
  /** The editor's in-memory identity; adopts the continuation's id when minting. */
  inMemory: SaveRequest;
  /** Distinct in-memory identity string when an editor keys it differently (timeline includes the encounter id). */
  identityPayload?: string;
  /** Editing-session id so the workbench can group this save with the buffer it belongs to. */
  sessionId?: string;
}): SaveSubmission {
  const payloadJson = JSON.stringify(params.payload);
  const continuation = findContinuablePendingSave(
    { kind: params.kind, ...params.filter },
    params.payload,
  );
  const requestId = params.inMemory.forPayload(
    params.identityPayload ?? payloadJson,
    continuation?.requestId,
  );
  const body = JSON.stringify({ ...params.payload, request_id: requestId });
  const recorded = continuation
    ? true
    : recordPendingSave({
        requestId,
        kind: params.kind,
        ...params.filter,
        sessionId: params.sessionId,
        body,
        savedAt: new Date().toISOString(),
      });
  return { requestId, body, continued: Boolean(continuation), recorded };
}

/**
 * Classification for pending-record cleanup after a failed save.
 *
 * Only CONTENT-level rejections let the pending record go: the server
 * processed this exact request and refused the payload, so replaying it
 * cannot help (400/413/422 validation, 404 the note itself is gone).
 *
 * Everything else is NOT safe to treat as "never submitted": 401 (rejected
 * before processing — the save may deserve a retry after re-login), 408/429
 * (temporary), network failure (status 0), 5xx, an unreadable success
 * response, or a non-ApiError. The record must survive those so the next
 * mount can verify what actually happened before any retry.
 *
 * 409 splits into two very different cases. The replay conflict (same
 * request id already carries other content) actually PROVES a version with
 * this id exists — the original save may well have landed — so the record
 * stays for verification. Every other 409 on the save endpoints (preview
 * token mismatch, ambiguous prescription diff) is rejected BEFORE any write
 * with a fresh request id: nothing landed, definitive, record cleared.
 *
 * Recognized structurally (an Error carrying a numeric `status`) so this
 * module stays dependency-free and testable outside the bundler.
 */
const DEFINITIVE_REJECTION_STATUSES = new Set([400, 404, 413, 422]);

/** 服务端重放冲突的固定 detail（notes 与 student-notes 两个保存端点同文案）。 */
const REPLAY_CONFLICT_DETAIL = "保存标识已用于其他内容";

export function isDefinitiveSaveRejection(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const status = (error as { status?: unknown }).status;
  if (typeof status !== "number") return false;
  if (status === 409) {
    return !error.message.includes(REPLAY_CONFLICT_DETAIL);
  }
  return DEFINITIVE_REJECTION_STATUSES.has(status);
}

export function leavesCurrentPage(
  event: { button: number; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean },
  href: string,
  currentHref: string,
  target: string | null,
  download: boolean,
): boolean {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || download) return false;
  if (target && target !== "_self") return false;
  const next = new URL(href, currentHref);
  const current = new URL(currentHref);
  return next.origin !== current.origin || next.pathname !== current.pathname || next.search !== current.search;
}
