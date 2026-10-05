export const IMPORT_TERMINAL_STATUSES = [
  "ACCEPTED",
  "REVIEW_REQUIRED",
  "UNRESOLVED",
  "FAILED",
] as const;

const terminalStatuses = new Set<string>(IMPORT_TERMINAL_STATUSES);

export function isImportTerminalStatus(status: string): boolean {
  return terminalStatuses.has(status);
}

export function canRetryImportItem(status: string): boolean {
  // Only FAILED documents offer a same-view retry (POST
  // /documents/{id}/retry re-enqueues them). UNRESOLVED keeps its separate
  // explicit-reprocess rule and is never retried from the UI.
  return status === "FAILED";
}
