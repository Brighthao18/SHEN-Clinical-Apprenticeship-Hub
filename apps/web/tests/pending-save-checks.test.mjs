import assert from "node:assert/strict";
import test from "node:test";
import {
  PENDING_STATUS_VIEW,
  applyPendingCheckOutcome,
  describePendingSave,
  markChecksVerifying,
  pendingCheckSummary,
} from "../lib/pending-save-checks.ts";

function check(requestId, status) {
  return { entry: { requestId, kind: "note_revision", body: "{}", savedAt: "2026-09-09T00:00:00Z" }, status };
}

test("a landed lookup removes exactly its own entry", () => {
  const checks = [check("a", "verifying"), check("b", "verifying")];
  const next = applyPendingCheckOutcome(checks, "a", "landed");
  assert.deepEqual(next.map((item) => item.entry.requestId), ["b"]);
  // The previous list is never mutated.
  assert.equal(checks.length, 2);
});

test("an unresolved lookup marks only the answered entry", () => {
  const checks = [check("a", "verifying"), check("b", "verifying")];
  const next = applyPendingCheckOutcome(checks, "b", "unresolved");
  assert.equal(next[0].status, "verifying");
  assert.equal(next[1].status, "unresolved");
});

test("a failed lookup marks the answered entry AND every still-unverified one after it", () => {
  const checks = [
    check("a", "unresolved"),
    check("b", "verifying"),
    check("c", "verifying"),
    check("d", "failed"),
  ];
  const next = applyPendingCheckOutcome(checks, "b", "failed");
  // Earlier resolved entries and later already-failed entries keep theirs.
  assert.equal(next[0].status, "unresolved");
  assert.equal(next[1].status, "failed");
  assert.equal(next[2].status, "failed");
  assert.equal(next[3].status, "failed");
});

test("answers for unknown entries are a no-op", () => {
  const checks = [check("a", "verifying")];
  const next = applyPendingCheckOutcome(checks, "missing", "landed");
  assert.equal(next, checks);
});

test("recheck re-arms only answerable entries; verifying ones are left alone", () => {
  const checks = [check("a", "unresolved"), check("b", "verifying"), check("c", "failed")];
  const next = markChecksVerifying(checks);
  assert.deepEqual(next.map((item) => item.status), ["verifying", "verifying", "verifying"]);
});

test("the summary counts each status and phrases the header fragments", () => {
  const summary = pendingCheckSummary([
    check("a", "unresolved"),
    check("b", "unresolved"),
    check("c", "failed"),
    check("d", "verifying"),
  ]);
  assert.equal(summary.total, 4);
  assert.equal(summary.unresolved, 2);
  assert.equal(summary.failed, 1);
  assert.equal(summary.verifying, 1);
  assert.deepEqual(summary.parts, [
    "暂未查到入库结果 2 次（原请求可能仍在处理）",
    "暂时无法核对 1 次",
    "正在核对 1 次",
  ]);
  const empty = pendingCheckSummary([]);
  assert.deepEqual(empty.parts, []);
});

test("pending saves retain draft/final tags and use the local timezone", () => {
  const entry = {
    requestId: "id",
    kind: "note_revision",
    body: JSON.stringify({ content: "ABCDEFGHIJKLMNOPQ", final: true, request_id: "id" }),
    savedAt: "2026-09-09T00:00:00Z",
  };
  const short = {
    requestId: "id",
    kind: "student_note",
    body: JSON.stringify({ content: "苔白腻", request_id: "id" }),
    savedAt: "2026-09-09T00:00:00Z",
  };
  const malformed = { requestId: "id", kind: "student_note", body: "{oops", savedAt: "2026-09-09T00:00:00Z" };
  const previousTimezone = process.env.TZ;
  try {
    for (const [timezone, timestamp] of [
      ["UTC", "2026/9/9 00:00:00"],
      ["Asia/Shanghai", "2026/9/9 08:00:00"],
    ]) {
      process.env.TZ = timezone;
      assert.equal(
        describePendingSave(entry, { finalTag: true }),
        `「ABCDEFGHIJKLMNOP…」（最终版） · ${timestamp}`,
      );
      assert.equal(describePendingSave(entry), `「ABCDEFGHIJKLMNOP…」 · ${timestamp}`);
      assert.equal(describePendingSave(short), `「苔白腻」 · ${timestamp}`);
      assert.equal(describePendingSave(malformed), timestamp);
    }
  } finally {
    if (previousTimezone === undefined) delete process.env.TZ;
    else process.env.TZ = previousTimezone;
  }
});

test("status view keeps a visible label for every status", () => {
  for (const status of ["verifying", "unresolved", "failed"]) {
    assert.ok(PENDING_STATUS_VIEW[status].label.length > 0);
  }
});
