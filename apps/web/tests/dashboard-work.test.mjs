import assert from "node:assert/strict";
import test from "node:test";

import { inProgressNotesQuery, recentEncounterHref } from "../lib/dashboard-work.ts";

test("in-progress notes query uses the database-side finalized filter", () => {
  const query = new URL(inProgressNotesQuery, "http://localhost");
  assert.equal(query.pathname, "/api/v1/notes");
  assert.equal(query.searchParams.get("finalized"), "false");
  assert.equal(query.searchParams.get("limit"), "3");
});

test("recent encounter links land on the displayed encounter, not the default one", () => {
  assert.equal(recentEncounterHref("pat-1", "enc-2"), "/patients/pat-1?encounter=enc-2");
  assert.equal(recentEncounterHref("a b", "e/1"), "/patients/a%20b?encounter=e%2F1");
});
