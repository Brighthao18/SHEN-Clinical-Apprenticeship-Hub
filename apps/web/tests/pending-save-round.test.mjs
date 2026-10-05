import assert from "node:assert/strict";
import test from "node:test";
import {
  runPendingSaveVerificationRound,
  samePendingSaveScope,
} from "../lib/pending-save-checks.ts";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function entry(requestId, scope) {
  return {
    requestId,
    kind: scope.kind,
    body: "{}",
    savedAt: "2026-09-09T00:00:00Z",
    ...(scope.encounterId ? { encounterId: scope.encounterId } : {}),
    ...(scope.noteId ? { noteId: scope.noteId } : {}),
  };
}

/**
 * One round against a controllable world: `world.current` is the editor's
 * live scope (what a scope switch mutates), `world.newest` tracks the newest
 * in-flight round the way the hook's sequence counter does.
 */
function makeRound({ scope, entries, lookup }) {
  const world = { current: { ...scope }, newest: 0 };
  const calls = { announced: [], checking: [], cleared: [], outcomes: [] };
  const seq = ++world.newest;
  const round = runPendingSaveVerificationRound({
    scope: { ...scope },
    entries,
    announce: true,
    lookup,
    onLanded: async (labels) => {
      calls.announced.push(labels);
    },
    currentScope: () => ({ ...world.current }),
    isNewestRound: () => world.newest === seq,
    applyOutcome: (requestId, outcome) => {
      calls.outcomes.push([requestId, outcome]);
    },
    clearEntry: (requestId) => {
      calls.cleared.push(requestId);
    },
    setChecking: (checking) => {
      calls.checking.push(checking);
    },
  });
  return { round, world, calls };
}

test("a slow answer for A never announces through B after the editor switched", async () => {
  const gate = deferred();
  const { round, world, calls } = makeRound({
    scope: { kind: "student_note", encounterId: "A" },
    entries: [entry("r1", { kind: "student_note", encounterId: "A" })],
    lookup: () => gate.promise.then(() => "已入库"),
  });
  // 编辑器切到就诊 B：作用域变了，但 A 的核对还在途。
  world.current = { kind: "student_note", encounterId: "B" };
  gate.resolve();
  await round;
  // 入库确认（全局清理）照常发生，但成功提示绝不发给 B。
  assert.deepEqual(calls.cleared, ["r1"]);
  assert.deepEqual(calls.outcomes, [["r1", "landed"]]);
  assert.deepEqual(calls.announced, []);
});

test("A→B→A announces again — the right object is on screen again", async () => {
  const gate = deferred();
  const { round, world, calls } = makeRound({
    scope: { kind: "student_note", encounterId: "A" },
    entries: [entry("r1", { kind: "student_note", encounterId: "A" })],
    lookup: () => gate.promise.then(() => "已入库"),
  });
  world.current = { kind: "student_note", encounterId: "B" };
  world.current = { kind: "student_note", encounterId: "A" };
  gate.resolve();
  await round;
  assert.deepEqual(calls.announced, [["已入库"]]);
});

test("an old round finishing never ends a newer round's checking state", async () => {
  const oldGate = deferred();
  const old = makeRound({
    scope: { kind: "student_note", encounterId: "A" },
    entries: [entry("r1", { kind: "student_note", encounterId: "A" })],
    lookup: () => oldGate.promise.then(() => "已入库"),
  });
  // 新一轮启动后执行编号前移：旧轮不再是最新轮。新轮自己的查询仍在途。
  const newerCalls = { checking: [] };
  const newerGate = deferred();
  const newer = runPendingSaveVerificationRound({
    scope: { kind: "student_note", encounterId: "B" },
    entries: [entry("r2", { kind: "student_note", encounterId: "B" })],
    announce: true,
    lookup: () => newerGate.promise.then(() => "已入库"),
    currentScope: () => ({ kind: "student_note", encounterId: "B" }),
    isNewestRound: () => true,
    applyOutcome: () => {},
    clearEntry: () => {},
    setChecking: (checking) => newerCalls.checking.push(checking),
  });
  old.world.newest = 2;
  oldGate.resolve();
  await old.round;
  // 旧轮结束不得清掉新轮的核对加载状态。
  assert.deepEqual(newerCalls.checking, [true]);
  newerGate.resolve();
  await newer;
  assert.deepEqual(newerCalls.checking, [true, false]);
});

test("a failed lookup marks the entry 核对失败 and stops asking", async () => {
  const asked = [];
  const { round, calls } = makeRound({
    scope: { kind: "student_note", encounterId: "A" },
    entries: [
      entry("r1", { kind: "student_note", encounterId: "A" }),
      entry("r2", { kind: "student_note", encounterId: "A" }),
    ],
    lookup: (item) => {
      asked.push(item.requestId);
      return Promise.reject(new Error("服务不可达"));
    },
  });
  await round;
  assert.deepEqual(asked, ["r1"]);
  assert.deepEqual(calls.outcomes, [["r1", "failed"]]);
  assert.deepEqual(calls.cleared, []);
});

test("a 404 lookup counts as 暂未查到: the record stays and the round continues", async () => {
  const notFound = Object.assign(new Error("not found"), { status: 404 });
  const { round, calls } = makeRound({
    scope: { kind: "student_note", encounterId: "A" },
    entries: [
      entry("r1", { kind: "student_note", encounterId: "A" }),
      entry("r2", { kind: "student_note", encounterId: "A" }),
    ],
    lookup: (item) => (item.requestId === "r1" ? Promise.reject(notFound) : Promise.resolve("v3")),
  });
  await round;
  assert.deepEqual(calls.outcomes, [
    ["r1", "unresolved"],
    ["r2", "landed"],
  ]);
  assert.deepEqual(calls.cleared, ["r2"]);
});

test("a landing outside the round's scope object never clears another scope's announcement", async () => {
  // 同一编辑器内的另一部作品（noteId 不同）：kind 相同但对象不同，不算同一作用域。
  assert.equal(
    samePendingSaveScope(
      { kind: "note_revision", noteId: "n1" },
      { kind: "note_revision", noteId: "n2" },
    ),
    false,
  );
  assert.equal(
    samePendingSaveScope(
      { kind: "note_revision", noteId: "n1" },
      { kind: "note_revision", noteId: "n1" },
    ),
    true,
  );
  assert.equal(
    samePendingSaveScope(
      { kind: "student_note", encounterId: "e1" },
      { kind: "note_revision", noteId: "n1" },
    ),
    false,
  );
});
