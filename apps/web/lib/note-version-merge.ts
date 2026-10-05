/**
 * Merge one server-confirmed version into the editor's history WITHOUT
 * trusting response arrival order. A late replay may return a version that
 * already exists (idempotent replay of a save that had landed) or one older
 * than versions already in the list; either way the result is merged by
 * version id and ordered by version number, so "latest" is always the max
 * version — never the last element appended.
 */
export function mergeNoteVersion<T extends { id: string; version: number }>(
  current: { versions: T[] },
  version: T,
): { versions: T[] } {
  const known = current.versions.some((item) => item.id === version.id);
  const versions = known
    ? current.versions.map((item) => (item.id === version.id ? version : item))
    : [...current.versions, version];
  versions.sort((a, b) => a.version - b.version);
  return { versions };
}

/** True when the version is the newest one of the merged history, i.e. the
 * only case where the editor may move the selection to it. A replayed older
 * version must never yank the selection backwards. */
export function isNewestVersion<T extends { id: string; version: number }>(
  versions: T[],
  version: T,
): boolean {
  return versions.at(-1)?.id === version.id;
}
