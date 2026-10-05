import assert from "node:assert/strict";
import test from "node:test";
import { isNewestVersion, mergeNoteVersion } from "../lib/note-version-merge.ts";

function version(id, number) {
  return { id, version: number };
}

test("a new version is inserted by version number, not appended blindly", () => {
  const history = { versions: [version("v1", 1), version("v3", 3)] };
  const merged = mergeNoteVersion(history, version("v2", 2));
  assert.deepEqual(merged.versions.map((item) => item.id), ["v1", "v2", "v3"]);
  // Latest is the max version, never the last response that happened to
  // arrive.
  assert.equal(merged.versions.at(-1).id, "v3");
});

test("an idempotent replay of a landed version merges instead of duplicating", () => {
  const history = { versions: [version("v1", 1), version("v2", 2)] };
  const replayed = { ...version("v2", 2), content: "服务器返回的既有版本" };
  const merged = mergeNoteVersion(history, replayed);
  assert.equal(merged.versions.length, 2);
  assert.deepEqual(merged.versions.map((item) => item.id), ["v1", "v2"]);
  assert.equal(merged.versions.at(-1).content, "服务器返回的既有版本");
});

test("a late OLD version lands in its chronological place, never at the end", () => {
  const history = { versions: [version("v1", 1), version("v5", 5)] };
  const late = mergeNoteVersion(history, version("v3", 3));
  assert.deepEqual(late.versions.map((item) => item.id), ["v1", "v3", "v5"]);
  assert.equal(late.versions.at(-1).id, "v5");
  assert.equal(isNewestVersion(late.versions, version("v3", 3)), false);
});

test("selection may only follow a version that is actually the newest", () => {
  const merged = mergeNoteVersion({ versions: [version("v1", 1)] }, version("v2", 2));
  assert.equal(isNewestVersion(merged.versions, version("v2", 2)), true);
  assert.equal(isNewestVersion(merged.versions, version("v1", 1)), false);
  assert.equal(isNewestVersion([], version("v9", 9)), false);
});

test("merging never mutates the previous history array", () => {
  const history = { versions: [version("v1", 1)] };
  mergeNoteVersion(history, version("v2", 2));
  assert.deepEqual(history.versions.map((item) => item.id), ["v1"]);
});
