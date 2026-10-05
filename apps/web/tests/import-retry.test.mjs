import assert from "node:assert/strict";
import test from "node:test";

import { canRetryImportItem } from "../lib/import-status.ts";

test("only FAILED import items offer a same-view retry", () => {
  assert.equal(canRetryImportItem("FAILED"), true);
});

test("UNRESOLVED and other states never retry from the UI", () => {
  // UNRESOLVED keeps its explicit reprocess rule (scripts/reprocess_unresolved.py);
  // active and accepted states have nothing to recover from.
  for (const status of [
    "UNRESOLVED",
    "ACCEPTED",
    "REVIEW_REQUIRED",
    "QUEUED",
    "OCR_RUNNING",
    "UPLOADED",
  ]) {
    assert.equal(canRetryImportItem(status), false);
  }
});
