import assert from "node:assert/strict";
import test from "node:test";
import { findOutOfScopeReferences } from "../lib/note-evidence-scope.ts";

// 含 a–f 十六进制字母的标识：转大写才有实际变化，大小写用例才成立。
const IN_SCOPE_A = "a1b2c3d4-1111-4111-8111-abcdef111111";
const IN_SCOPE_B = "b2c3d4e5-2222-4222-8222-bcdefa222222";
const REMOVED_C = "c3d4e5f6-3333-4333-8333-cdefab333333";
const WORK_ID_D = "d4e5f6a7-4444-4444-8444-defabc444444";

test("a removed encounter referenced in the body is located with line and centered snippet", () => {
  const padding = "很长的叙述。".repeat(20);
  const text = [
    "初诊主诉记录。",
    `${padding}仍引用 ${REMOVED_C} 的处方变化。`,
    "结尾。",
  ].join("\n");
  const findings = findOutOfScopeReferences(text, [IN_SCOPE_A, IN_SCOPE_B], {
    knownEncounterIds: [IN_SCOPE_A, IN_SCOPE_B, REMOVED_C],
  });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].identifier, REMOVED_C);
  assert.equal(findings[0].kind, "out_of_scope_encounter");
  assert.deepEqual(
    findings[0].locations.map((location) => location.line),
    [2],
  );
  // 片段围绕命中位置截取：引用在长段落后半部时也必须出现在片段里。
  assert.ok(findings[0].locations[0].snippet.includes(REMOVED_C));
  assert.ok(findings[0].locations[0].snippet.startsWith("…"));
});

test("identifiable encounters vs unidentified ids get different kinds", () => {
  const text = `范围外的就诊 ${REMOVED_C}；另一个无法识别类型的标识 ${WORK_ID_D}。`;
  const findings = findOutOfScopeReferences(text, [IN_SCOPE_A], {
    knownEncounterIds: [IN_SCOPE_A, REMOVED_C],
  });
  assert.deepEqual(
    findings.map((finding) => [finding.identifier, finding.kind]),
    [
      [REMOVED_C, "out_of_scope_encounter"],
      [WORK_ID_D, "unidentified"],
    ],
  );
});

test("in-scope ids (including the system appendix block) never match", () => {
  const text = [
    "正文干净。",
    "――――",
    "内容依据（Encounter IDs）：",
    `- ${IN_SCOPE_A}`,
    `- ${IN_SCOPE_B}`,
  ].join("\n");
  assert.deepEqual(
    findOutOfScopeReferences(text, [IN_SCOPE_A, IN_SCOPE_B], {
      knownEncounterIds: [IN_SCOPE_A, IN_SCOPE_B],
    }),
    [],
  );
});

test("matching is case-insensitive and findings are reported lowercased", () => {
  const upper = REMOVED_C.toUpperCase();
  assert.notEqual(upper, REMOVED_C);
  const text = `第一处 ${upper}\n第二处 ${REMOVED_C}\n第三处 ${upper}`;
  const findings = findOutOfScopeReferences(text, [], {
    knownEncounterIds: [REMOVED_C],
  });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].identifier, REMOVED_C);
  assert.equal(findings[0].locations.length, 3);
});

test("a structured-ref-only id is flagged as encounter without line locations", () => {
  const findings = findOutOfScopeReferences("正文没有提到任何 ID。", [IN_SCOPE_A], {
    structuredRefIds: [IN_SCOPE_A, REMOVED_C],
  });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].identifier, REMOVED_C);
  assert.equal(findings[0].kind, "out_of_scope_encounter");
  assert.deepEqual(findings[0].locations, []);
  assert.equal(findings[0].inStructuredRefs, true);
});

test("multiple findings are sorted; locations cap at three lines", () => {
  const text = [
    `${WORK_ID_D}`,
    `${REMOVED_C}`,
    `${WORK_ID_D}`,
    `${REMOVED_C}`,
    `${REMOVED_C}`,
    `${REMOVED_C}`,
  ].join("\n");
  const findings = findOutOfScopeReferences(text, [], {
    knownEncounterIds: [REMOVED_C],
  });
  assert.deepEqual(
    findings.map((finding) => finding.identifier),
    [REMOVED_C, WORK_ID_D],
  );
  const removed = findings.find((finding) => finding.identifier === REMOVED_C);
  assert.deepEqual(
    removed.locations.map((location) => location.line),
    [2, 4, 5],
  );
});

test("clean text with no ids yields nothing", () => {
  assert.deepEqual(
    findOutOfScopeReferences("第三诊叙述，但没有引用任何标识。", []),
    [],
  );
});
