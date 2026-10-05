import assert from "node:assert/strict";
import test from "node:test";

import {
  LEGACY_SESSION_ID,
  clearNoteEditBuffer,
  listNoteEditBuffers,
  readNoteEditBuffer,
  writeNoteEditBuffer,
} from "../lib/note-edit-buffer.ts";

/** Minimal localStorage stub so the module thinks it runs in a browser. */
function withStorage(run) {
  const store = new Map();
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (name) => (store.has(name) ? store.get(name) : null),
      setItem: (name, value) => void store.set(name, value),
      removeItem: (name) => void store.delete(name),
      key: (index) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
  });
  try {
    run(store);
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous);
    else delete globalThis.localStorage;
  }
}

function buffer(overrides = {}) {
  return {
    noteId: "note-1",
    versionId: "v2",
    version: 2,
    text: "未保存的修改。",
    savedAt: "2026-09-07T10:00:00Z",
    sessionId: "session-a",
    ...overrides,
  };
}

test("buffer round-trips per note + version + session", () => {
  withStorage((store) => {
    assert.equal(readNoteEditBuffer("note-1", "v2", "session-a"), null);
    const record = buffer();
    assert.equal(writeNoteEditBuffer(record), true);
    assert.deepEqual(readNoteEditBuffer("note-1", "v2", "session-a"), record);
    // A different session (another tab) on the SAME base version reads null.
    assert.equal(readNoteEditBuffer("note-1", "v2", "session-b"), null);
    assert.equal(readNoteEditBuffer("note-2", "v2", "session-a"), null);
    assert.equal(store.size, 1);
  });
});

test("two tabs editing the same note keep separate records", () => {
  withStorage(() => {
    writeNoteEditBuffer(buffer({ sessionId: "session-a", text: "标签页甲的编辑。" }));
    writeNoteEditBuffer(
      buffer({ sessionId: "session-b", text: "标签页乙的编辑。", savedAt: "2026-09-07T10:01:00Z" }),
    );
    const listed = listNoteEditBuffers("note-1");
    assert.equal(listed.length, 2);
    // Newest first.
    assert.equal(listed[0].sessionId, "session-b");
    assert.equal(listed[1].sessionId, "session-a");
    assert.deepEqual(readNoteEditBuffer("note-1", "v2", "session-b").text, "标签页乙的编辑。");
  });
});

test("corrupt or mismatched payloads read as absent", () => {
  withStorage((store) => {
    const key = "shen.note-buffer.v2.note-1.v2.session-a";
    store.set(key, "{not json");
    assert.equal(readNoteEditBuffer("note-1", "v2", "session-a"), null);
    store.set(key, JSON.stringify({ noteId: "other-note", text: "x", versionId: "v1", savedAt: "t", sessionId: "s" }));
    assert.equal(readNoteEditBuffer("note-1", "v2", "session-a"), null);
    store.set(key, JSON.stringify({ noteId: "note-1", text: "x", versionId: "v2", savedAt: "t" }));
    // Missing sessionId: a v2 record must carry one.
    assert.equal(readNoteEditBuffer("note-1", "v2", "session-a"), null);
    // A record without version number is rejected too.
    store.set(key, JSON.stringify({ noteId: "note-1", text: "x", versionId: "v2", savedAt: "t", sessionId: "session-a" }));
    assert.equal(readNoteEditBuffer("note-1", "v2", "session-a"), null);
  });
});

test("clear removes only the targeted record", () => {
  withStorage(() => {
    writeNoteEditBuffer(buffer({ sessionId: "session-a" }));
    writeNoteEditBuffer(buffer({ sessionId: "session-b", text: "b" }));
    writeNoteEditBuffer(buffer({ noteId: "note-2", sessionId: "session-a", text: "c" }));
    clearNoteEditBuffer("note-1", "v2", "session-a");
    assert.equal(readNoteEditBuffer("note-1", "v2", "session-a"), null);
    assert.notEqual(readNoteEditBuffer("note-1", "v2", "session-b"), null);
    assert.notEqual(readNoteEditBuffer("note-2", "v2", "session-a"), null);
    // Clearing everything for note-1 leaves note-2 alone.
    clearNoteEditBuffer("note-1");
    assert.deepEqual(listNoteEditBuffers("note-1"), []);
    assert.notEqual(readNoteEditBuffer("note-2", "v2", "session-a"), null);
  });
});

test("legacy v1 single-record buffers are listed, restorable, and clearable", () => {
  withStorage((store) => {
    store.set(
      "shen.note-buffer.v1.note-1",
      JSON.stringify({
        noteId: "note-1",
        versionId: "v3",
        version: 3,
        text: "旧版缓冲内容。",
        savedAt: "2026-09-01T08:00:00Z",
      }),
    );
    const listed = listNoteEditBuffers("note-1");
    assert.equal(listed.length, 1);
    assert.equal(listed[0].sessionId, LEGACY_SESSION_ID);
    assert.equal(listed[0].text, "旧版缓冲内容。");
    assert.deepEqual(readNoteEditBuffer("note-1", "v3", LEGACY_SESSION_ID).text, "旧版缓冲内容。");
    // A non-legacy session id never sees the legacy record.
    assert.equal(readNoteEditBuffer("note-1", "v3", "session-a"), null);
    // Restoring/discarding the legacy record clears the legacy key.
    clearNoteEditBuffer("note-1", "v3", LEGACY_SESSION_ID);
    assert.deepEqual(listNoteEditBuffers("note-1"), []);
    assert.equal(store.get("shen.note-buffer.v1.note-1"), undefined);
  });
});

test("legacy buffer is not offered for a mismatched base version", () => {
  withStorage((store) => {
    store.set(
      "shen.note-buffer.v1.note-1",
      JSON.stringify({
        noteId: "note-1",
        versionId: "v9",
        version: 9,
        text: "x",
        savedAt: "2026-09-01T08:00:00Z",
      }),
    );
    assert.equal(readNoteEditBuffer("note-1", "v3", LEGACY_SESSION_ID), null);
  });
});

test("records are never auto-deleted, whatever the count", () => {
  withStorage(() => {
    for (let index = 0; index < 30; index += 1) {
      writeNoteEditBuffer(
        buffer({ sessionId: `s-${index}`, savedAt: `2026-09-07T10:${String(index).padStart(2, "0")}:00Z` }),
      );
    }
    const listed = listNoteEditBuffers("note-1");
    // An old draft may be the only copy of unsaved work: no cap-based
    // deletion, ever. Tidying up is the user's explicit decision.
    assert.equal(listed.length, 30);
    assert.equal(listed[0].sessionId, "s-29");
    assert.equal(listed.at(-1).sessionId, "s-0");
  });
});
