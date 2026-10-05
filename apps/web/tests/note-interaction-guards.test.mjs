import assert from "node:assert/strict";
import test from "node:test";

import {
  canSaveNoteVersion,
  hasAiDraftForSourceContext,
  isNoteContentDirty,
} from "../lib/note-interaction-guards.ts";

test("unchanged drafts may be finalized, but unchanged final versions cannot be duplicated", () => {
  assert.equal(canSaveNoteVersion("ai_draft", "内容", "内容", true), true);
  assert.equal(canSaveNoteVersion("user_draft", "内容", "内容", true), true);
  assert.equal(canSaveNoteVersion("user_final", "内容", "内容", true), false);
  assert.equal(canSaveNoteVersion("user_final", "内容", "修改内容", true), true);
  assert.equal(canSaveNoteVersion("ai_draft", "内容", "内容", false), false);
  assert.equal(canSaveNoteVersion("ai_draft", "内容", "  ", true), false);
});

test("unchanged note content cannot create a redundant save", () => {
  assert.equal(isNoteContentDirty("原版本", "原版本"), false);
  assert.equal(isNoteContentDirty("原版本", "原版本。"), true);
});

test("same source context is detected only when an AI draft already exists", () => {
  const versions = [
    { version_type: "user_original", source_context_hash: "context-a" },
    { version_type: "ai_draft", source_context_hash: "context-a" },
    { version_type: "user_draft", source_context_hash: "context-b" },
  ];
  assert.equal(hasAiDraftForSourceContext(versions, "context-a"), true);
  assert.equal(hasAiDraftForSourceContext(versions, "context-b"), false);
});
