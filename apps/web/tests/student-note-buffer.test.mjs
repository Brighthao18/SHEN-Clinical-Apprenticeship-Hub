import assert from "node:assert/strict";
import test from "node:test";

import {
  LEGACY_SESSION_ID,
  clearStudentNoteBuffer,
  listStudentNoteBuffers,
  readStudentNoteBuffer,
  writeStudentNoteBuffer,
} from "../lib/student-note-buffer.ts";

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

function draft(overrides = {}) {
  return {
    encounterId: "enc-1",
    noteKind: "teacher_oral",
    content: "老师提到……",
    savedAt: "2026-09-08T09:00:00Z",
    sessionId: "session-a",
    ...overrides,
  };
}

test("student-note drafts round-trip per encounter + session", () => {
  withStorage(() => {
    assert.equal(readStudentNoteBuffer("enc-1", "session-a"), null);
    const record = draft();
    assert.equal(writeStudentNoteBuffer(record), true);
    assert.deepEqual(readStudentNoteBuffer("enc-1", "session-a"), record);
    // Another session (another tab) on the SAME encounter reads null.
    assert.equal(readStudentNoteBuffer("enc-1", "session-b"), null);
    assert.equal(readStudentNoteBuffer("enc-2", "session-a"), null);
  });
});

test("two tabs taking notes on the same encounter keep separate records", () => {
  withStorage(() => {
    writeStudentNoteBuffer(draft({ sessionId: "session-a", content: "标签页甲的补录。" }));
    writeStudentNoteBuffer(
      draft({ sessionId: "session-b", content: "标签页乙的补录。", savedAt: "2026-09-08T09:01:00Z" }),
    );
    const listed = listStudentNoteBuffers("enc-1");
    assert.equal(listed.length, 2);
    // Newest first.
    assert.equal(listed[0].sessionId, "session-b");
    assert.equal(listed[1].sessionId, "session-a");
  });
});

test("corrupt or mismatched payloads read as absent", () => {
  withStorage((store) => {
    const key = "shen.student-note-buffer.v2.enc-1.session-a";
    store.set(key, "{not json");
    assert.equal(readStudentNoteBuffer("enc-1", "session-a"), null);
    store.set(key, JSON.stringify({ encounterId: "enc-2", noteKind: "pulse", content: "x", savedAt: "t", sessionId: "s" }));
    assert.equal(readStudentNoteBuffer("enc-1", "session-a"), null);
    // A v2 record must carry a session id.
    store.set(key, JSON.stringify({ encounterId: "enc-1", noteKind: "pulse", content: "x", savedAt: "t" }));
    assert.equal(readStudentNoteBuffer("enc-1", "session-a"), null);
  });
});

test("clear removes only the targeted record", () => {
  withStorage(() => {
    writeStudentNoteBuffer(draft({ sessionId: "session-a" }));
    writeStudentNoteBuffer(draft({ sessionId: "session-b", content: "b" }));
    writeStudentNoteBuffer(draft({ encounterId: "enc-2", sessionId: "session-a", content: "c" }));
    clearStudentNoteBuffer("enc-1", "session-a");
    assert.equal(readStudentNoteBuffer("enc-1", "session-a"), null);
    assert.notEqual(readStudentNoteBuffer("enc-1", "session-b"), null);
    assert.notEqual(readStudentNoteBuffer("enc-2", "session-a"), null);
    clearStudentNoteBuffer("enc-1");
    assert.deepEqual(listStudentNoteBuffers("enc-1"), []);
    assert.notEqual(readStudentNoteBuffer("enc-2", "session-a"), null);
  });
});

test("legacy v1 per-encounter records are listed, restorable, and clearable", () => {
  withStorage((store) => {
    store.set(
      "shen.student-note-buffer.v1.enc-1",
      JSON.stringify({
        encounterId: "enc-1",
        noteKind: "pulse",
        content: "旧版补录草稿。",
        savedAt: "2026-09-01T08:00:00Z",
      }),
    );
    const listed = listStudentNoteBuffers("enc-1");
    assert.equal(listed.length, 1);
    assert.equal(listed[0].sessionId, LEGACY_SESSION_ID);
    assert.equal(listed[0].content, "旧版补录草稿。");
    assert.equal(readStudentNoteBuffer("enc-1", LEGACY_SESSION_ID).content, "旧版补录草稿。");
    clearStudentNoteBuffer("enc-1", LEGACY_SESSION_ID);
    assert.deepEqual(listStudentNoteBuffers("enc-1"), []);
    assert.equal(store.get("shen.student-note-buffer.v1.enc-1"), undefined);
  });
});

test("patientId positioning field round-trips and pre-positioning records stay readable", () => {
  withStorage(() => {
    writeStudentNoteBuffer({
      encounterId: "enc-1",
      noteKind: "tongue",
      content: "带定位的草稿",
      savedAt: "2026-09-09T08:00:00Z",
      sessionId: "session-1",
      patientId: "pat-9",
    });
    const found = readStudentNoteBuffer("enc-1", "session-1");
    assert.equal(found.patientId, "pat-9");

    // v2 record written before the field existed still parses (no patientId).
    writeStudentNoteBuffer({
      encounterId: "enc-2",
      noteKind: "pulse",
      content: "旧格式草稿",
      savedAt: "2026-09-01T08:00:00Z",
      sessionId: "session-2",
    });
    const legacy = readStudentNoteBuffer("enc-2", "session-2");
    assert.equal(legacy.content, "旧格式草稿");
    assert.equal(legacy.patientId, undefined);
  });
});
