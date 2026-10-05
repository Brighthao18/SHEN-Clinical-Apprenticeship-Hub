import assert from "node:assert/strict";
import test from "node:test";
import { collectLocalContinueWork } from "../lib/continue-work.ts";
import { recordPendingSave } from "../lib/save-request.ts";
import { writeNoteEditBuffer } from "../lib/note-edit-buffer.ts";
import { writeStudentNoteBuffer } from "../lib/student-note-buffer.ts";

/** Minimal localStorage stub so the modules think they run in a browser. */
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

test("continue-work aggregates edit buffers and pending saves, newest first", () => {
  withStorage(() => {
    assert.deepEqual(collectLocalContinueWork(), []);
    writeNoteEditBuffer({
      noteId: "note-1",
      versionId: "v1",
      version: 1,
      text: "一份未保存的编辑",
      savedAt: "2026-09-09T08:00:00Z",
      sessionId: "session-a",
    });
    recordPendingSave({
      requestId: "req-1",
      kind: "note_revision",
      noteId: "note-2",
      body: JSON.stringify({ content: "待核对的保存内容", final: false, request_id: "req-1" }),
      savedAt: "2026-09-09T09:00:00Z",
    });
    const items = collectLocalContinueWork();
    assert.equal(items.length, 2);
    // Newest first: the pending save (09:00) before the buffer (08:00).
    assert.equal(items[0].kind, "verifying");
    assert.equal(items[0].href, "/notes/note-2");
    assert.ok(items[0].detail.includes("待核对的保存内容"));
    assert.equal(items[1].kind, "editing");
    assert.equal(items[1].href, "/notes/note-1?resume=session-a");
    assert.ok(items[1].detail.includes("基于 v1"));
  });
});

test("one editing session's buffer and pending save merge into ONE work item", () => {
  withStorage(() => {
    writeNoteEditBuffer({
      noteId: "note-1",
      versionId: "v3",
      version: 3,
      text: "编辑中的文字",
      savedAt: "2026-09-09T08:00:00Z",
      sessionId: "session-a",
    });
    recordPendingSave({
      requestId: "req-a",
      kind: "note_revision",
      noteId: "note-1",
      sessionId: "session-a",
      body: JSON.stringify({ content: "同一会话的保存", final: false, request_id: "req-a" }),
      savedAt: "2026-09-09T08:30:00Z",
    });
    const items = collectLocalContinueWork();
    assert.equal(items.length, 1);
    // 待核对状态优先呈现；两个细节并列；恢复链接指回同一会话。
    assert.equal(items[0].kind, "verifying");
    assert.ok(items[0].detail.includes("未保存编辑"));
    assert.ok(items[0].detail.includes("保存结果待核对"));
    assert.equal(items[0].href, "/notes/note-1?resume=session-a");
    assert.equal(items[0].savedAt, "2026-09-09T08:30:00Z");
  });
});

test("different sessions or contents NEVER merge into one item", () => {
  withStorage(() => {
    writeNoteEditBuffer({
      noteId: "note-1",
      versionId: "v3",
      version: 3,
      text: "会话 A 的编辑",
      savedAt: "2026-09-09T08:00:00Z",
      sessionId: "session-a",
    });
    recordPendingSave({
      requestId: "req-b",
      kind: "note_revision",
      noteId: "note-1",
      sessionId: "session-b",
      body: JSON.stringify({ content: "会话 B 的保存", final: false, request_id: "req-b" }),
      savedAt: "2026-09-09T08:30:00Z",
    });
    const items = collectLocalContinueWork();
    assert.equal(items.length, 2);
    assert.deepEqual(
      items.map((item) => item.kind).sort(),
      ["editing", "verifying"],
    );
  });
});

test("student-note records join the aggregation and deep-link when patientId is known", () => {
  withStorage(() => {
    writeStudentNoteBuffer({
      encounterId: "enc-1",
      noteKind: "tongue",
      content: "一份补录草稿",
      savedAt: "2026-09-09T07:00:00Z",
      sessionId: "session-s",
      patientId: "pat-9",
    });
    recordPendingSave({
      requestId: "req-s",
      kind: "student_note",
      encounterId: "enc-1",
      patientId: "pat-9",
      sessionId: "session-s",
      body: JSON.stringify({ content: "同一会话的补录保存", request_id: "req-s" }),
      savedAt: "2026-09-09T07:30:00Z",
    });
    const items = collectLocalContinueWork();
    assert.equal(items.length, 1);
    assert.equal(items[0].family, "student_note");
    assert.equal(items[0].kind, "verifying");
    assert.equal(items[0].patientId, "pat-9");
    assert.equal(
      items[0].href,
      "/patients/pat-9?encounter=enc-1&resume=session-s",
    );
    assert.ok(items[0].detail.includes("未保存补录"));
    assert.ok(items[0].detail.includes("保存结果待核对"));
  });
});

test("student-note records WITHOUT patientId are listed but need owner resolution", () => {
  withStorage(() => {
    // 旧缓冲：只带 encounterId；旧待核对：无 sessionId（无法归并，保持独立）。
    writeStudentNoteBuffer({
      encounterId: "enc-1",
      noteKind: "pulse",
      content: "旧记录草稿",
      savedAt: "2026-09-09T07:00:00Z",
      sessionId: "session-old",
    });
    recordPendingSave({
      requestId: "req-old",
      kind: "student_note",
      encounterId: "enc-1",
      body: JSON.stringify({ content: "旧记录保存", request_id: "req-old" }),
      savedAt: "2026-09-09T06:00:00Z",
    });
    const items = collectLocalContinueWork();
    assert.equal(items.length, 2);
    for (const item of items) {
      assert.equal(item.family, "student_note");
      assert.equal(item.href, null);
      assert.equal(item.needsResolution, true);
      assert.equal(item.patientId, undefined);
    }
    // 无 sessionId 的待核对保持独立条目，绝不并入不知道的会话。
    assert.ok(items[0].detail.includes("旧记录草稿"));
    assert.ok(items[1].detail.includes("保存结果待核对"));
  });
});

test("malformed pending-save bodies degrade to the generic detail", () => {
  withStorage(() => {
    recordPendingSave({
      requestId: "req-broken",
      kind: "note_revision",
      noteId: "note-3",
      body: "{oops",
      savedAt: "2026-09-09T07:30:00Z",
    });
    const items = collectLocalContinueWork();
    assert.equal(items.length, 1);
    assert.equal(items[0].noteId, "note-3");
    assert.ok(items[0].detail.includes("保存结果待核对"));
  });
});
