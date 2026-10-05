/**
 * Local recovery records for the quick 学生补录 form, keyed per encounter +
 * editing session — the same isolation the note editor uses, so two tabs
 * taking notes on the same encounter keep separate drafts instead of
 * overwriting each other. The timeline confirms before discarding, but a
 * confirmed discard on a closed tab or an expired browser session would
 * still lose the sentence — records keep it recoverable on the next visit
 * to that encounter. Records never leave the user's machine and are never
 * auto-deleted.
 */
export type StudentNoteBuffer = {
  encounterId: string;
  noteKind: string;
  content: string;
  savedAt: string;
  /** Identifies one editing session (one mounted timeline form). */
  sessionId: string;
  /**
   * Work-positioning only: lets the workbench deep-link straight to
   * `/patients/{id}?encounter=…`. Records written before this field existed
   * lack it; the workbench resolves their owner through the authenticated
   * encounter-owner lookup and says so when that fails.
   */
  patientId?: string;
};

const KEY_PREFIX = "shen.student-note-buffer.v2.";
const LEGACY_KEY_PREFIX = "shen.student-note-buffer.v1.";
/** Synthetic session id for buffers stored by the pre-v2 per-encounter
 * single-record format; restoring or discarding it clears the legacy key. */
export const LEGACY_SESSION_ID = "legacy";

function bufferKey(encounterId: string, sessionId: string) {
  return `${KEY_PREFIX}${encounterId}.${sessionId}`;
}

function legacyBufferKey(encounterId: string) {
  return `${LEGACY_KEY_PREFIX}${encounterId}`;
}

function getStorage(): Storage | null {
  try {
    return (globalThis as { localStorage?: Storage }).localStorage ?? null;
  } catch {
    return null;
  }
}

function parseBuffer(
  raw: string | null,
  encounterId: string,
  options: { legacy?: boolean } = {},
): StudentNoteBuffer | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<StudentNoteBuffer> | null;
    if (
      !parsed ||
      parsed.encounterId !== encounterId ||
      typeof parsed.noteKind !== "string" ||
      !parsed.noteKind ||
      typeof parsed.content !== "string" ||
      typeof parsed.savedAt !== "string"
    ) {
      return null;
    }
    // Legacy (pre-v2) records carry no session id; v2 records must.
    if (options.legacy) {
      return {
        encounterId: parsed.encounterId,
        noteKind: parsed.noteKind,
        content: parsed.content,
        savedAt: parsed.savedAt,
        sessionId: LEGACY_SESSION_ID,
      };
    }
    if (typeof parsed.sessionId !== "string" || !parsed.sessionId) return null;
    // patientId is optional positioning data (added 2026-09-09); keep it only
    // when well-formed so pre-positioning v2 records stay readable.
    return {
      ...(typeof parsed.patientId === "string" && parsed.patientId
        ? { patientId: parsed.patientId }
        : {}),
      encounterId: parsed.encounterId,
      noteKind: parsed.noteKind,
      content: parsed.content,
      savedAt: parsed.savedAt,
      sessionId: parsed.sessionId,
    };
  } catch {
    return null;
  }
}

/** All recovery records for one encounter, newest first. Includes a migrated
 * view of the legacy single-record format when present. */
export function listStudentNoteBuffers(encounterId: string): StudentNoteBuffer[] {
  const storage = getStorage();
  if (!storage) return [];
  const found: StudentNoteBuffer[] = [];
  try {
    const ownPrefix = `${KEY_PREFIX}${encounterId}.`;
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key || !key.startsWith(ownPrefix)) continue;
      const parsed = parseBuffer(storage.getItem(key), encounterId);
      if (parsed) found.push(parsed);
    }
    const legacy = parseBuffer(storage.getItem(legacyBufferKey(encounterId)), encounterId, {
      legacy: true,
    });
    if (legacy) found.push(legacy);
  } catch {
    // Storage unavailable or misbehaving; return what was collected so far.
  }
  return found.sort((a, b) => (a.savedAt < b.savedAt ? 1 : a.savedAt > b.savedAt ? -1 : 0));
}

/** Every recovery record across ALL encounters (workbench aggregation),
 * newest first. Malformed entries are skipped, never thrown. */
export function listAllStudentNoteBuffers(): StudentNoteBuffer[] {
  const storage = getStorage();
  if (!storage) return [];
  const found: StudentNoteBuffer[] = [];
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key || !key.startsWith(KEY_PREFIX)) continue;
      const rest = key.slice(KEY_PREFIX.length);
      const encounterId = rest.slice(0, rest.lastIndexOf("."));
      const parsed = parseBuffer(storage.getItem(key), encounterId);
      if (parsed) found.push(parsed);
    }
  } catch {
    // Storage unavailable or misbehaving; return what was collected so far.
  }
  return found.sort((a, b) => (a.savedAt < b.savedAt ? 1 : a.savedAt > b.savedAt ? -1 : 0));
}

export function readStudentNoteBuffer(
  encounterId: string,
  sessionId: string,
): StudentNoteBuffer | null {
  const storage = getStorage();
  if (!storage) return null;
  const direct = parseBuffer(storage.getItem(bufferKey(encounterId, sessionId)), encounterId);
  if (direct) return direct;
  if (sessionId === LEGACY_SESSION_ID) {
    return parseBuffer(storage.getItem(legacyBufferKey(encounterId)), encounterId, {
      legacy: true,
    });
  }
  return null;
}

/** Returns false when storage is unavailable, so callers can keep guards. */
export function writeStudentNoteBuffer(buffer: StudentNoteBuffer): boolean {
  const storage = getStorage();
  if (!storage || !buffer.sessionId) return false;
  try {
    storage.setItem(
      bufferKey(buffer.encounterId, buffer.sessionId),
      JSON.stringify(buffer),
    );
    return true;
  } catch {
    return false;
  }
}

export function clearStudentNoteBuffer(
  encounterId: string,
  sessionId?: string,
): void {
  const storage = getStorage();
  if (!storage) return;
  try {
    if (sessionId === undefined) {
      const ownPrefix = `${KEY_PREFIX}${encounterId}.`;
      const doomed: string[] = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key && key.startsWith(ownPrefix)) doomed.push(key);
      }
      for (const key of doomed) storage.removeItem(key);
      storage.removeItem(legacyBufferKey(encounterId));
      return;
    }
    if (sessionId === LEGACY_SESSION_ID) {
      const legacy = parseBuffer(storage.getItem(legacyBufferKey(encounterId)), encounterId, {
        legacy: true,
      });
      if (legacy) storage.removeItem(legacyBufferKey(encounterId));
      return;
    }
    storage.removeItem(bufferKey(encounterId, sessionId));
  } catch {
    // Storage unavailable; nothing to clean up.
  }
}
