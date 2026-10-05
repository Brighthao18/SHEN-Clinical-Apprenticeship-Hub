import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../lib/api.ts";
import {
  SaveRequest,
  beginSaveSubmission,
  clearPendingSave,
  findContinuablePendingSave,
  getPendingSave,
  isDefinitiveSaveRejection,
  leavesCurrentPage,
  listPendingSaves,
  recordPendingSave,
} from "../lib/save-request.ts";

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

test("uncertain save retries reuse their identity; changed or completed saves do not", () => {
  const save = new SaveRequest();
  const first = save.forPayload('patient-1:version-2:草稿');
  assert.equal(save.forPayload('patient-1:version-2:草稿'), first);
  assert.notEqual(save.forPayload('patient-1:version-2:定稿'), first);
  const second = save.forPayload('patient-1:version-2:定稿');
  save.complete();
  assert.notEqual(save.forPayload('patient-1:version-2:定稿'), second);
});

test("a fresh editor adopts the pending request id of the revision it continues", () => {
  // Cross-refresh: the in-memory SaveRequest is gone; saving the same
  // revision (restored buffer) must adopt the OLD request id so the server's
  // idempotency keeps covering the save.
  const fresh = new SaveRequest();
  const adopted = fresh.forPayload('{"content":"同一份文字","final":false}', "old-request-id");
  assert.equal(adopted, "old-request-id");
  // The adopted identity then behaves like any in-mount identity: same
  // payload keeps it, changed payload mints a new one.
  assert.equal(fresh.forPayload('{"content":"同一份文字","final":false}'), "old-request-id");
  assert.notEqual(fresh.forPayload('{"content":"新的一句","final":false}'), "old-request-id");
  // Without a pending record there is nothing to adopt: a new id is minted.
  const fresh2 = new SaveRequest();
  assert.match(fresh2.forPayload('{"content":"同一份文字","final":false}'), /^[0-9a-f-]{36}$/);
});

test("navigation guard covers internal links without blocking source tabs and page anchors", () => {
  const click = { button: 0, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false };
  const current = 'http://localhost/patients/one?encounter=a';
  assert.equal(leavesCurrentPage(click, '/notes', current, null, false), true);
  assert.equal(leavesCurrentPage(click, '?encounter=b', current, null, false), true);
  assert.equal(leavesCurrentPage(click, '#main-content', current, null, false), false);
  assert.equal(leavesCurrentPage(click, '/source', current, '_blank', false), false);
  assert.equal(leavesCurrentPage(click, '/export', current, null, true), false);
  assert.equal(leavesCurrentPage({ ...click, ctrlKey: true }, '/notes', current, null, false), false);
});

test("pending saves persist, filter, and clear", () => {
  withStorage(() => {
    assert.deepEqual(listPendingSaves(), []);
    const noteEntry = {
      requestId: "11111111-1111-4111-8111-111111111111",
      kind: "note_revision",
      noteId: "note-1",
      body: JSON.stringify({ content: "未确认草稿", final: false, request_id: "11111111-1111-4111-8111-111111111111" }),
      savedAt: "2026-09-08T09:00:00Z",
    };
    const noteEntry2 = {
      requestId: "22222222-2222-4222-8222-222222222222",
      kind: "note_revision",
      noteId: "note-2",
      body: "{}",
      savedAt: "2026-09-08T09:01:00Z",
    };
    const studentEntry = {
      requestId: "33333333-3333-4333-8333-333333333333",
      kind: "student_note",
      encounterId: "enc-1",
      body: JSON.stringify({ content: "补录", request_id: "33333333-3333-4333-8333-333333333333" }),
      savedAt: "2026-09-08T09:02:00Z",
    };
    assert.equal(recordPendingSave(noteEntry), true);
    assert.equal(recordPendingSave(noteEntry2), true);
    assert.equal(recordPendingSave(studentEntry), true);

    assert.deepEqual(listPendingSaves().map((entry) => entry.requestId), [
      studentEntry.requestId,
      noteEntry2.requestId,
      noteEntry.requestId,
    ]); // newest first
    assert.equal(listPendingSaves({ kind: "note_revision", noteId: "note-1" }).length, 1);
    assert.equal(listPendingSaves({ kind: "student_note", encounterId: "enc-1" }).length, 1);
    assert.equal(listPendingSaves({ kind: "note_revision", noteId: "note-9" }).length, 0);

    clearPendingSave(noteEntry.requestId);
    assert.equal(listPendingSaves({ noteId: "note-1" }).length, 0);
    assert.equal(listPendingSaves().length, 2);
  });
});

test("malformed pending-save entries are ignored, never thrown", () => {
  withStorage((store) => {
    store.set("shen.pending-save.v1.bad-json", "{oops");
    store.set(
      "shen.pending-save.v1.bad-shape",
      JSON.stringify({ requestId: "x", kind: "something-else", body: "{}", savedAt: "t" }),
    );
    store.set(
      "shen.pending-save.v1.note-without-id",
      JSON.stringify({ requestId: "y", kind: "note_revision", body: "{}", savedAt: "t" }),
    );
    store.set(
      "shen.pending-save.v1.student-without-encounter",
      JSON.stringify({ requestId: "z", kind: "student_note", body: "{}", savedAt: "t" }),
    );
    assert.deepEqual(listPendingSaves(), []);
  });
});

test("continuation matches the recorded revision exactly, ignoring its request id", () => {
  withStorage(() => {
    const notePayload = {
      requestId: "44444444-4444-4444-8444-444444444444",
      kind: "note_revision",
      noteId: "note-1",
      body: JSON.stringify({
        content: "恢复后的同一份文字",
        final: false,
        based_on_version_id: "version-9",
        request_id: "44444444-4444-4444-8444-444444444444",
      }),
      savedAt: "2026-09-08T09:00:00Z",
    };
    const studentPayload = {
      requestId: "55555555-5555-5555-8555-555555555555",
      kind: "student_note",
      encounterId: "enc-1",
      body: JSON.stringify({
        note_kind: "teacher_oral",
        content: "老师强调随证加减",
        request_id: "55555555-5555-5555-8555-555555555555",
      }),
      savedAt: "2026-09-08T09:01:00Z",
    };
    assert.equal(recordPendingSave(notePayload), true);
    assert.equal(recordPendingSave(studentPayload), true);

    // Exact same revision continues the original request.
    assert.equal(
      findContinuablePendingSave(
        { kind: "note_revision", noteId: "note-1" },
        { content: "恢复后的同一份文字", final: false, based_on_version_id: "version-9" },
      )?.requestId,
      notePayload.requestId,
    );
    assert.equal(
      findContinuablePendingSave(
        { kind: "student_note", encounterId: "enc-1" },
        { note_kind: "teacher_oral", content: "老师强调随证加减" },
      )?.requestId,
      studentPayload.requestId,
    );

    // Any content change is a NEW revision — never a continuation.
    assert.equal(
      findContinuablePendingSave(
        { kind: "note_revision", noteId: "note-1" },
        { content: "改过一个字的文字", final: false, based_on_version_id: "version-9" },
      ),
      null,
    );
    // final flag and base version are part of the revision identity.
    assert.equal(
      findContinuablePendingSave(
        { kind: "note_revision", noteId: "note-1" },
        { content: "恢复后的同一份文字", final: true, based_on_version_id: "version-9" },
      ),
      null,
    );
    assert.equal(
      findContinuablePendingSave(
        { kind: "note_revision", noteId: "note-1" },
        { content: "恢复后的同一份文字", final: false, based_on_version_id: "version-8" },
      ),
      null,
    );
    // Scoping: another note/encounter/kind never continues someone else's
    // request, even with an identical payload.
    assert.equal(
      findContinuablePendingSave(
        { kind: "note_revision", noteId: "note-2" },
        { content: "恢复后的同一份文字", final: false, based_on_version_id: "version-9" },
      ),
      null,
    );
    assert.equal(
      findContinuablePendingSave(
        { kind: "student_note", encounterId: "enc-2" },
        { note_kind: "teacher_oral", content: "老师强调随证加减" },
      ),
      null,
    );
    // With nothing pending at all there is nothing to continue.
    clearPendingSave(notePayload.requestId);
    clearPendingSave(studentPayload.requestId);
    assert.equal(
      findContinuablePendingSave(
        { kind: "note_revision", noteId: "note-1" },
        { content: "恢复后的同一份文字", final: false, based_on_version_id: "version-9" },
      ),
      null,
    );
  });
});

test("continuation picks the newest match and skips malformed bodies", () => {
  withStorage((store) => {
    store.set(
      "shen.pending-save.v1.broken",
      JSON.stringify({
        requestId: "66666666-6666-6666-8666-666666666666",
        kind: "note_revision",
        noteId: "note-1",
        body: "{oops",
        savedAt: "2026-09-08T10:00:00Z",
      }),
    );
    const older = {
      requestId: "77777777-7777-7777-8777-777777777777",
      kind: "note_revision",
      noteId: "note-1",
      body: JSON.stringify({ content: "同文", final: false, based_on_version_id: "v1", request_id: "77777777-7777-7777-8777-777777777777" }),
      savedAt: "2026-09-08T09:00:00Z",
    };
    const newer = {
      requestId: "88888888-8888-8888-8888-888888888888",
      kind: "note_revision",
      noteId: "note-1",
      body: JSON.stringify({ content: "同文", final: false, based_on_version_id: "v1", request_id: "88888888-8888-8888-8888-888888888888" }),
      savedAt: "2026-09-08T09:30:00Z",
    };
    recordPendingSave(older);
    recordPendingSave(newer);
    assert.equal(
      findContinuablePendingSave(
        { kind: "note_revision", noteId: "note-1" },
        { content: "同文", final: false, based_on_version_id: "v1" },
      )?.requestId,
      newer.requestId,
    );
  });
});

test("a fresh submission mints an id and registers the pending record before sending", () => {
  withStorage(() => {
    const inMemory = new SaveRequest();
    const submission = beginSaveSubmission({
      kind: "note_revision",
      filter: { noteId: "note-1" },
      payload: { content: "草稿正文", final: false, based_on_version_id: "v1" },
      inMemory,
    });
    assert.match(submission.requestId, /^[0-9a-f-]{36}$/);
    assert.equal(submission.continued, false);
    assert.equal(submission.recorded, true);
    const body = JSON.parse(submission.body);
    assert.equal(body.request_id, submission.requestId);
    assert.equal(body.content, "草稿正文");
    // The registered record carries the exact same body and id.
    const stored = listPendingSaves({ kind: "note_revision", noteId: "note-1" });
    assert.equal(stored.length, 1);
    assert.equal(stored[0].requestId, submission.requestId);
    assert.equal(stored[0].body, submission.body);
    // The in-memory identity now covers this payload: re-saving unchanged
    // keeps the id, and the record is adopted rather than duplicated.
    const again = beginSaveSubmission({
      kind: "note_revision",
      filter: { noteId: "note-1" },
      payload: { content: "草稿正文", final: false, based_on_version_id: "v1" },
      inMemory,
    });
    assert.equal(again.requestId, submission.requestId);
    assert.equal(again.continued, true);
    assert.equal(listPendingSaves({ kind: "note_revision", noteId: "note-1" }).length, 1);
  });
});

test("a submission after refresh adopts the pending request and keeps its record untouched", () => {
  withStorage(() => {
    const original = {
      requestId: "99999999-9999-9999-8999-999999999999",
      kind: "note_revision",
      noteId: "note-1",
      body: JSON.stringify({ content: "恢复的同一修订", final: false, based_on_version_id: "v1", request_id: "99999999-9999-9999-8999-999999999999" }),
      savedAt: "2026-09-08T09:00:00Z",
    };
    recordPendingSave(original);
    // A FRESH editor: its in-memory identity knows nothing about the record.
    const submission = beginSaveSubmission({
      kind: "note_revision",
      filter: { noteId: "note-1" },
      payload: { content: "恢复的同一修订", final: false, based_on_version_id: "v1" },
      inMemory: new SaveRequest(),
    });
    assert.equal(submission.requestId, original.requestId);
    assert.equal(submission.continued, true);
    assert.equal(submission.recorded, true);
    // The original record's savedAt survives: continuation never re-registers.
    const stored = listPendingSaves({ kind: "note_revision", noteId: "note-1" })[0];
    assert.equal(stored.savedAt, original.savedAt);
    assert.equal(stored.body, original.body);
    // A genuinely NEW revision mints a fresh id instead.
    const changed = beginSaveSubmission({
      kind: "note_revision",
      filter: { noteId: "note-1" },
      payload: { content: "新的一句话", final: false, based_on_version_id: "v1" },
      inMemory: new SaveRequest(),
    });
    assert.notEqual(changed.requestId, original.requestId);
    assert.equal(changed.continued, false);
  });
});

test("the timeline's distinct in-memory identity string still continues the same revision", () => {
  withStorage(() => {
    recordPendingSave({
      requestId: "aaaaaaa1-0000-0000-8000-000000000001",
      kind: "student_note",
      encounterId: "enc-1",
      body: JSON.stringify({ note_kind: "pulse", content: "脉滑", request_id: "aaaaaaa1-0000-0000-8000-000000000001" }),
      savedAt: "2026-09-08T09:00:00Z",
    });
    const submission = beginSaveSubmission({
      kind: "student_note",
      filter: { encounterId: "enc-1" },
      payload: { note_kind: "pulse", content: "脉滑" },
      inMemory: new SaveRequest(),
      identityPayload: JSON.stringify({ encounterId: "enc-1", note_kind: "pulse", content: "脉滑" }),
    });
    assert.equal(submission.requestId, "aaaaaaa1-0000-0000-8000-000000000001");
    assert.equal(submission.continued, true);
    assert.equal(JSON.parse(submission.body).request_id, submission.requestId);
  });
});

test("a submission whose record cannot be stored still proceeds, flagged unrecorded", () => {
  const store = new Map();
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (name) => (store.has(name) ? store.get(name) : null),
      setItem: () => {
        throw new Error("quota exceeded");
      },
      removeItem: (name) => void store.delete(name),
      key: (index) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
  });
  try {
    const submission = beginSaveSubmission({
      kind: "student_note",
      filter: { encounterId: "enc-9" },
      payload: { note_kind: "tongue", content: "苔白" },
      inMemory: new SaveRequest(),
    });
    assert.equal(submission.recorded, false);
    assert.equal(submission.continued, false);
    assert.match(submission.requestId, /^[0-9a-f-]{36}$/);
    assert.equal(listPendingSaves().length, 0);
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous);
    else delete globalThis.localStorage;
  }
});

test("only content-level 4xx rejections clear the pending path", () => {
  // Definitive: the server processed THIS exact request and refused the
  // payload (validation / payload too large / the note itself is gone).
  assert.equal(isDefinitiveSaveRejection(new ApiError("校验失败", 400)), true);
  assert.equal(isDefinitiveSaveRejection(new ApiError("笔记不存在", 404)), true);
  assert.equal(isDefinitiveSaveRejection(new ApiError("内容过大", 413)), true);
  assert.equal(isDefinitiveSaveRejection(new ApiError("校验失败", 422)), true);
  // NOT definitive — the record must survive:
  assert.equal(isDefinitiveSaveRejection(new ApiError("登录已过期", 401)), false);
  assert.equal(isDefinitiveSaveRejection(new ApiError("请求超时", 408)), false);
  assert.equal(isDefinitiveSaveRejection(new ApiError("请求过快", 429)), false);
  assert.equal(isDefinitiveSaveRejection(new ApiError("无法连接", 0)), false);
  assert.equal(isDefinitiveSaveRejection(new ApiError("服务错误", 502)), false);
  assert.equal(isDefinitiveSaveRejection(new ApiError("响应不可读", 200)), false);
  assert.equal(isDefinitiveSaveRejection(new Error("其他")), false);
  assert.equal(isDefinitiveSaveRejection(null), false);
});

test("409 splits: replay conflict stays pending; fresh-request conflicts clear", () => {
  // 重放冲突：同请求标识已承载其他内容——恰恰证明该标识的版本已存在，
  // 原保存可能已入库，记录必须保留给“先核对”流程。
  assert.equal(
    isDefinitiveSaveRejection(
      new ApiError("保存标识已用于其他内容，请核对后重新提交", 409),
    ),
    false,
  );
  // 新请求的 409（预览凭证失效 / 依据范围歧义）都在落库前拒绝：
  // 什么都没写入，确定性未入库，记录应当即清除。
  assert.equal(
    isDefinitiveSaveRejection(
      new ApiError(
        "所选资料在预览后已发生变化（或预览与本次采用不匹配）：请重新预览并确认后再采用；原依据保持不变",
        409,
      ),
    ),
    true,
  );
  assert.equal(
    isDefinitiveSaveRejection(
      new ApiError(
        "PRESCRIPTION_DIFF_AMBIGUOUS：所选就诊范围存在无法唯一配对的处方，采用新依据已停止，原依据保持不变",
        409,
      ),
    ),
    true,
  );
});

test("positioning fields (patientId, sessionId) round-trip; legacy records without them stay valid", () => {
  withStorage((store) => {
    const submission = beginSaveSubmission({
      kind: "student_note",
      filter: { encounterId: "enc-1", patientId: "pat-9" },
      payload: { content: "补录", note_kind: "tongue" },
      inMemory: new SaveRequest(),
      sessionId: "session-x",
    });
    const entry = getPendingSave(submission.requestId);
    assert.equal(entry.patientId, "pat-9");
    assert.equal(entry.sessionId, "session-x");

    // Pre-positioning record (no patientId/sessionId) still parses.
    store.set(
      "shen.pending-save.v1.legacy-req",
      JSON.stringify({
        requestId: "legacy-req",
        kind: "student_note",
        encounterId: "enc-2",
        body: "{}",
        savedAt: "2026-09-01T00:00:00Z",
      }),
    );
    const legacy = getPendingSave("legacy-req");
    assert.equal(legacy.encounterId, "enc-2");
    assert.equal(legacy.patientId, undefined);
    assert.equal(legacy.sessionId, undefined);
  });
});
