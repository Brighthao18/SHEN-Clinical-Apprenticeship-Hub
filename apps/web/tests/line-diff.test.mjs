import assert from "node:assert/strict";
import test from "node:test";

import { diffLines, diffRegions } from "../lib/line-diff.ts";

test("diffLines marks added and removed lines with positions", () => {
  const diff = diffLines("一诊记录\n白术 10g\n陈皮 6g", "一诊记录\n白术 15g\n陈皮 6g\n砂仁 3g");
  assert.ok(diff);
  assert.deepEqual(
    diff.filter((line) => line.type !== "same"),
    [
      { type: "removed", text: "白术 10g", oldNumber: 2 },
      { type: "added", text: "白术 15g", newNumber: 2 },
      { type: "added", text: "砂仁 3g", newNumber: 4 },
    ],
  );
});

test("diffLines handles empty sides and pure additions", () => {
  const fromEmpty = diffLines("", "第一行\n第二行");
  assert.ok(fromEmpty);
  assert.deepEqual(fromEmpty, [
    { type: "added", text: "第一行", newNumber: 1 },
    { type: "added", text: "第二行", newNumber: 2 },
  ]);
  const toEmpty = diffLines("只有一行", "");
  assert.ok(toEmpty);
  assert.deepEqual(toEmpty, [{ type: "removed", text: "只有一行", oldNumber: 1 }]);
});

test("identical texts produce only same lines", () => {
  const diff = diffLines("甲\n乙", "甲\n乙");
  assert.ok(diff);
  assert.ok(diff.every((line) => line.type === "same"));
});

test("oversized input refuses the DP instead of hanging", () => {
  const big = Array.from({ length: 601 }, (_, i) => `行${i}`).join("\n");
  assert.equal(diffLines(big, "小"), null);
});

test("diffRegions keeps one line of context and counts the gaps", () => {
  const oldText = ["a1", "a2", "a3", "a4", "a5", "a6", "a7"].join("\n");
  const newText = ["a1", "a2", "a3", "改动", "a5", "a6", "a7"].join("\n");
  const diff = diffLines(oldText, newText);
  assert.ok(diff);
  const regions = diffRegions(diff);
  const gaps = regions.filter((region) => region.type === "gap");
  // One changed line with one context line on each side leaves two gaps of
  // two unchanged lines each (a1-a2 and a6-a7).
  assert.deepEqual(gaps, [
    { type: "gap", count: 2 },
    { type: "gap", count: 2 },
  ]);
  const changed = regions.find((region) => region.type === "changed");
  assert.ok(changed && "lines" in changed);
  assert.deepEqual(
    changed.lines.map((line) => `${line.type}:${line.text}`),
    ["same:a3", "removed:a4", "added:改动", "same:a5"],
  );
});
