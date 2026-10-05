export type NoteEditBuffer = {
  noteId: string;
  versionId: string;
  version: number;
  text: string;
  savedAt: string;
  /** Identifies one editing session (one mounted editor). Two tabs — or two
   * visits to the same note — each keep their own recovery record, so a
   * refresh can offer BOTH drafts back instead of silently replacing one. */
  sessionId: string;
};

const KEY_PREFIX = "shen.note-buffer.v2.";
const LEGACY_KEY_PREFIX = "shen.note-buffer.v1.";
/** Synthetic session id for buffers stored by the pre-v2 single-record format;
 * restoring or discarding it clears the legacy key. */
export const LEGACY_SESSION_ID = "legacy";
/** Advisory only: when a note holds more records than this the UI suggests
 * tidying up. Records are NEVER auto-deleted — an old draft can still be the
 * only copy of unsaved work. */
export const NOTE_EDIT_BUFFER_SOFT_LIMIT = 20;

function bufferKey(noteId: string, versionId: string, sessionId: string) {
  return `${KEY_PREFIX}${noteId}.${versionId}.${sessionId}`;
}

function legacyBufferKey(noteId: string) {
  return `${LEGACY_KEY_PREFIX}${noteId}`;
}

// globalThis (not window) so the module is unit-testable outside a DOM;
// in the browser the two are the same, and on the server this is undefined.
function getStorage(): Storage | null {
  try {
    return (globalThis as { localStorage?: Storage }).localStorage ?? null;
  } catch {
    return null;
  }
}

function parseBuffer(
  raw: string | null,
  noteId: string,
  options: { legacy?: boolean } = {},
): NoteEditBuffer | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<NoteEditBuffer> | null;
    if (
      !parsed ||
      parsed.noteId !== noteId ||
      typeof parsed.versionId !== "string" ||
      !parsed.versionId ||
      typeof parsed.version !== "number" ||
      typeof parsed.text !== "string" ||
      typeof parsed.savedAt !== "string"
    ) {
      return null;
    }
    // Legacy (pre-v2) records carry no session id; v2 records must.
    if (options.legacy) {
      return {
        noteId: parsed.noteId,
        versionId: parsed.versionId,
        version: parsed.version,
        text: parsed.text,
        savedAt: parsed.savedAt,
        sessionId: LEGACY_SESSION_ID,
      };
    }
    if (typeof parsed.sessionId !== "string" || !parsed.sessionId) return null;
    return parsed as NoteEditBuffer;
  } catch {
    return null;
  }
}

/**
 * Local, per-browser recovery records for unsaved note edits, keyed by
 * note + base version + editing session. The app is local-first; records
 * never leave the user's machine and a record is cleared the moment its
 * edit is saved, restored, or explicitly discarded.
 */

/** All recovery records for one note, newest first. Includes a migrated view
 * of the legacy single-record format when present. */
export function listNoteEditBuffers(noteId: string): NoteEditBuffer[] {
  const storage = getStorage();
  if (!storage) return [];
  const found: NoteEditBuffer[] = [];
  try {
    const ownPrefix = `${KEY_PREFIX}${noteId}.`;
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key || !key.startsWith(ownPrefix)) continue;
      const parsed = parseBuffer(storage.getItem(key), noteId);
      if (parsed) found.push(parsed);
    }
    const legacy = parseBuffer(storage.getItem(legacyBufferKey(noteId)), noteId, {
      legacy: true,
    });
    if (legacy) found.push(legacy);
  } catch {
    // Storage unavailable or misbehaving; return what was collected so far.
  }
  return found.sort((a, b) => (a.savedAt < b.savedAt ? 1 : a.savedAt > b.savedAt ? -1 : 0));
}

/** Every recovery record across ALL notes (workbench "继续工作" aggregation),
 * newest first. Malformed entries are skipped, never thrown. */
export function listAllNoteEditBuffers(): NoteEditBuffer[] {
  const storage = getStorage();
  if (!storage) return [];
  const found: NoteEditBuffer[] = [];
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key || !key.startsWith(KEY_PREFIX)) continue;
      // Key layout: {noteId}.{versionId}.{sessionId} — noteId is UUID-based
      // (never contains dots), so the FIRST segment is the note id.
      const rest = key.slice(KEY_PREFIX.length);
      const noteId = rest.slice(0, rest.indexOf("."));
      const parsed = parseBuffer(storage.getItem(key), noteId);
      if (parsed) found.push(parsed);
    }
  } catch {
    // Storage unavailable or misbehaving; return what was collected so far.
  }
  return found.sort((a, b) => (a.savedAt < b.savedAt ? 1 : a.savedAt > b.savedAt ? -1 : 0));
}

export function readNoteEditBuffer(
  noteId: string,
  versionId: string,
  sessionId: string,
): NoteEditBuffer | null {
  const storage = getStorage();
  if (!storage) return null;
  const direct = parseBuffer(
    storage.getItem(bufferKey(noteId, versionId, sessionId)),
    noteId,
  );
  if (direct) return direct;
  // Legacy fallback: the pre-v2 buffer did not carry a session id, so accept
  // it only under the synthetic legacy session and only for matching version.
  if (sessionId === LEGACY_SESSION_ID) {
    const legacy = parseBuffer(storage.getItem(legacyBufferKey(noteId)), noteId, {
      legacy: true,
    });
    return legacy && legacy.versionId === versionId ? legacy : null;
  }
  return null;
}

/** Returns false when storage is unavailable, so callers can keep guards.
 * Never deletes other records: an old draft may be the only copy of
 * unsaved work; tidying up is the user's explicit decision. */
export function writeNoteEditBuffer(buffer: NoteEditBuffer): boolean {
  const storage = getStorage();
  if (!storage || !buffer.sessionId) return false;
  try {
    storage.setItem(
      bufferKey(buffer.noteId, buffer.versionId, buffer.sessionId),
      JSON.stringify(buffer),
    );
    return true;
  } catch {
    return false;
  }
}

export function clearNoteEditBuffer(
  noteId: string,
  versionId?: string,
  sessionId?: string,
): void {
  const storage = getStorage();
  if (!storage) return;
  try {
    if (versionId === undefined) {
      // Clear everything for the note (v2 records plus the legacy key).
      const ownPrefix = `${KEY_PREFIX}${noteId}.`;
      const doomed: string[] = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key && key.startsWith(ownPrefix)) doomed.push(key);
      }
      for (const key of doomed) storage.removeItem(key);
      storage.removeItem(legacyBufferKey(noteId));
      return;
    }
    if (sessionId === undefined) {
      // Clear every session's record for this note + base version.
      const ownPrefix = `${KEY_PREFIX}${noteId}.${versionId}.`;
      const doomed: string[] = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key && key.startsWith(ownPrefix)) doomed.push(key);
      }
      for (const key of doomed) storage.removeItem(key);
      const legacy = parseBuffer(storage.getItem(legacyBufferKey(noteId)), noteId, {
        legacy: true,
      });
      if (legacy && legacy.versionId === versionId) {
        storage.removeItem(legacyBufferKey(noteId));
      }
      return;
    }
    if (sessionId === LEGACY_SESSION_ID) {
      const legacy = parseBuffer(storage.getItem(legacyBufferKey(noteId)), noteId, {
        legacy: true,
      });
      if (legacy && legacy.versionId === versionId) {
        storage.removeItem(legacyBufferKey(noteId));
      }
      return;
    }
    storage.removeItem(bufferKey(noteId, versionId, sessionId));
  } catch {
    // Storage unavailable; nothing to clean up.
  }
}
