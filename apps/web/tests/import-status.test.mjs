import assert from "node:assert/strict";
import test from "node:test";

import { IMPORT_TERMINAL_STATUSES, isImportTerminalStatus } from "../lib/import-status.ts";

test("UNRESOLVED is a terminal import state", () => {
  assert.equal(isImportTerminalStatus("UNRESOLVED"), true);
});

test("active import states keep polling while every terminal state stops", () => {
  assert.deepEqual([...IMPORT_TERMINAL_STATUSES], [
    "ACCEPTED",
    "REVIEW_REQUIRED",
    "UNRESOLVED",
    "FAILED",
  ]);
  for (const status of ["UPLOADED", "QUEUED", "OCR_RUNNING", "STRUCTURING", "VALIDATING"]) {
    assert.equal(isImportTerminalStatus(status), false);
  }
});
