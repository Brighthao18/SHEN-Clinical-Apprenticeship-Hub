import assert from "node:assert/strict";
import test from "node:test";
import {
  describeMaterialsBreakdown,
  isPreviewForSelection,
  sameEncounterSet,
} from "../lib/note-evidence-preview.ts";

function preview(basedOnId, proposalIds) {
  return {
    note_id: "n1",
    note_type: "case_record",
    based_on_version: { id: basedOnId, version: 1, version_type: "user_original" },
    latest_version: { id: "v3", version: 3 },
    current: { evidence_encounters: [], source_context_hash: "h", evidence_snapshot: null },
    patient_encounters: [],
    proposal: {
      evidence_encounters: proposalIds,
      added: [],
      removed: [],
      source_context_hash: "h2",
      evidence_snapshot: null,
      preview_token: "t".repeat(64),
    },
    set_changed: false,
    materials_changed: false,
  };
}

test("sameEncounterSet is order-insensitive and length-sensitive", () => {
  assert.equal(sameEncounterSet(["a", "b"], ["b", "a"]), true);
  assert.equal(sameEncounterSet(["a"], ["a", "a"]), false);
  assert.equal(sameEncounterSet(["a", "b"], ["a", "c"]), false);
  assert.equal(sameEncounterSet([], []), true);
});

test("a preview whose echoed version matches the selection is accepted", () => {
  const p = preview("v1", ["e1", "e2"]);
  assert.equal(isPreviewForSelection(p, "v1", ["e2", "e1"]), true);
});

test("a preview bound to another version is rejected (viewing v1 while latest is v3)", () => {
  const p = preview("v1", ["e1"]);
  assert.equal(isPreviewForSelection(p, "v2", ["e1"]), false);
});

test("a preview whose set no longer matches the draft is rejected", () => {
  const p = preview("v1", ["e1", "e2"]);
  assert.equal(isPreviewForSelection(p, "v1", ["e1"]), false);
  assert.equal(isPreviewForSelection(p, "v1", ["e1", "e2", "e3"]), false);
});

test("an unchanged and comparable breakdown yields no extra explanation lines", () => {
  const lines = describeMaterialsBreakdown({
    comparable: true,
    not_comparable_reason: null,
    kept_materials_changed: false,
    rule_versions_changed: {},
  });
  assert.deepEqual(lines, []);
});

test("kept-material change and rule-version change are explained separately", () => {
  const onlyData = describeMaterialsBreakdown({
    comparable: true,
    not_comparable_reason: null,
    kept_materials_changed: true,
    rule_versions_changed: {},
  });
  assert.equal(onlyData.length, 1);
  assert.match(onlyData[0], /已有变化/);

  const dataAndRules = describeMaterialsBreakdown({
    comparable: true,
    not_comparable_reason: null,
    kept_materials_changed: true,
    rule_versions_changed: {
      context_version: { stored: "old", current: "new" },
      prescription_diff_algorithm_version: { stored: "1", current: "2" },
    },
  });
  assert.equal(dataAndRules.length, 2);
  assert.match(dataAndRules[1], /规则版本已更新/);
  assert.match(dataAndRules[1], /写作上下文、处方对比算法/);
});

test("each non-comparable reason states why comparison is impossible", () => {
  const legacy = describeMaterialsBreakdown({
    comparable: false,
    not_comparable_reason: "legacy_no_snapshot",
    kept_materials_changed: false,
    rule_versions_changed: {},
  });
  assert.equal(legacy.length, 1);
  assert.match(legacy[0], /未保存依据快照/);
  assert.match(legacy[0], /就诊增减仍然有效/);

  const unavailable = describeMaterialsBreakdown({
    comparable: false,
    not_comparable_reason: "base_set_unavailable",
    kept_materials_changed: false,
    rule_versions_changed: {},
  });
  assert.match(unavailable[0], /无法解析/);

  const ambiguous = describeMaterialsBreakdown({
    comparable: false,
    not_comparable_reason: "base_ambiguous",
    kept_materials_changed: false,
    rule_versions_changed: {},
  });
  assert.match(ambiguous[0], /无法唯一配对/);
});
