export type ContextVersion = {
  version_type: string;
  source_context_hash: string;
};

export function isNoteContentDirty(savedContent: string, editedContent: string): boolean {
  return savedContent !== editedContent;
}

export function canSaveNoteVersion(
  versionType: string,
  savedContent: string,
  editedContent: string,
  final: boolean,
): boolean {
  return editedContent.trim().length > 0 &&
    (isNoteContentDirty(savedContent, editedContent) || (final && versionType !== "user_final"));
}

export function hasAiDraftForSourceContext(
  versions: ContextVersion[],
  sourceContextHash: string,
): boolean {
  return versions.some(
    (version) =>
      version.version_type === "ai_draft" &&
      version.source_context_hash === sourceContextHash,
  );
}
