import type { PendingSave, PendingSaveKind } from "@/lib/save-request";

/**
 * The 编辑与保存 module's shared pending-check model: every save whose
 * outcome was never confirmed is listed IMMEDIATELY as `verifying`, then
 * marked per entry as its own lookup returns — `unresolved` (the server
 * answered "not found yet"; the original request may still be in flight) or
 * `failed` (the server cannot be asked now). A verified save leaves the list
 * entirely. Records are never hidden while their outcome is unknown, whatever
 * the network does. Both editing surfaces (作品编辑器 and 患者补录) render
 * and drive this ONE state machine instead of keeping private copies.
 */
export type PendingSaveCheckStatus = "verifying" | "unresolved" | "failed";

export type PendingSaveCheck = {
  entry: PendingSave;
  status: PendingSaveCheckStatus;
};

export const PENDING_STATUS_VIEW: Record<PendingSaveCheckStatus, { label: string; className: string }> = {
  verifying: { label: "核对中…", className: "bg-amber-100 text-amber-700" },
  unresolved: { label: "暂未查到", className: "bg-amber-200 text-amber-900" },
  failed: { label: "核对失败", className: "bg-red-100 text-red-700" },
};

/**
 * Fold one verification answer into the check list. `landed` removes the
 * entry (record already cleared by the caller). `unresolved` marks only the
 * answered entry. `failed` marks the answered entry AND every still-
 * verifying entry after it — verification is sequential, so those were never
 * asked and must read as 核对失败, not as 核对中 forever. Entries that already
 * have a final status are never overwritten.
 */
export function applyPendingCheckOutcome(
  checks: PendingSaveCheck[],
  requestId: string,
  outcome: "landed" | "unresolved" | "failed",
): PendingSaveCheck[] {
  const hit = checks.findIndex((check) => check.entry.requestId === requestId);
  if (hit === -1) return checks;
  if (outcome === "landed") {
    return checks.filter((check) => check.entry.requestId !== requestId);
  }
  return checks.map((check, index) =>
    check.status === "verifying" && (index === hit || (outcome === "failed" && index > hit))
      ? { ...check, status: outcome }
      : check,
  );
}

/** Re-arm every answerable entry for another verification round. */
export function markChecksVerifying(checks: PendingSaveCheck[]): PendingSaveCheck[] {
  return checks.map((check) =>
    check.status !== "verifying" ? { ...check, status: "verifying" as const } : check,
  );
}

/** The objects one verification round was started FOR — frozen at round start. */
export type PendingSaveScope = {
  kind: PendingSaveKind;
  noteId?: string;
  encounterId?: string;
};

export function samePendingSaveScope(a: PendingSaveScope, b: PendingSaveScope): boolean {
  return a.kind === b.kind && a.noteId === b.noteId && a.encounterId === b.encounterId;
}

export type PendingCheckOutcome = "landed" | "unresolved" | "failed";

/**
 * Structural stand-in for "the server answered 404" (an Error carrying a
 * numeric `status`, as ApiError does): keeps this module dependency-free.
 */
function isNotFoundLookupError(reason: unknown): boolean {
  if (!(reason instanceof Error)) return false;
  return (reason as { status?: unknown }).status === 404;
}

export type PendingSaveVerificationPorts = {
  /** This round's scope, frozen when the round started. */
  scope: PendingSaveScope;
  entries: PendingSave[];
  /** Mount/lookup rounds announce landings; explicit recheck rounds do not. */
  announce: boolean;
  lookup: (entry: PendingSave) => Promise<string>;
  onLanded?: (labels: string[]) => void | Promise<void>;
  /** The editor's CURRENT scope, re-read when the round is ready to announce. */
  currentScope: () => PendingSaveScope;
  /** True while this round is still the newest one in flight. */
  isNewestRound: () => boolean;
  /** Fold one answer into the visible check list. */
  applyOutcome: (requestId: string, outcome: PendingCheckOutcome) => void;
  /** Remove the local pending record once its landing is confirmed. */
  clearEntry: (requestId: string) => void;
  setChecking: (checking: boolean) => void;
};

/**
 * ONE verification round, isolated from every other round.
 *
 * The round binds its lookup/onLanded and its scope at START, so a slow
 * answer can never call back into whatever object the editor has since
 * switched to. Entries are marked as their own answers arrive; a failed
 * lookup stops the round and leaves every record visible — never hidden. A
 * landing clears the local pending record (that part is global and always
 * safe) BEFORE the outcome is folded into the list. The success announcement
 * fires only when the editor's current scope STILL IS the round's scope:
 * A→B must not toast B for A's save, while A→B→A may announce again (it is
 * the right object on screen again). `checking` is owned by the newest round
 * alone, so an old round finishing can never end a newer round's loading
 * state early.
 */
export async function runPendingSaveVerificationRound(
  ports: PendingSaveVerificationPorts,
): Promise<string[]> {
  ports.setChecking(true);
  try {
    const landed: string[] = [];
    for (const entry of ports.entries) {
      let label = "";
      let outcome: PendingCheckOutcome = "unresolved";
      try {
        label = await ports.lookup(entry);
        outcome = "landed";
      } catch (reason) {
        outcome = isNotFoundLookupError(reason) ? "unresolved" : "failed";
      }
      if (outcome === "landed") {
        ports.clearEntry(entry.requestId);
        landed.push(label);
      }
      ports.applyOutcome(entry.requestId, outcome);
      if (outcome === "failed") break;
    }
    if (ports.announce && landed.length && samePendingSaveScope(ports.scope, ports.currentScope())) {
      await ports.onLanded?.(landed);
    }
    return landed;
  } finally {
    if (ports.isNewestRound()) ports.setChecking(false);
  }
}

export function pendingCheckSummary(checks: PendingSaveCheck[]): {
  total: number;
  unresolved: number;
  failed: number;
  verifying: number;
  /** Per-status sentence fragments for the header line, in display order. */
  parts: string[];
} {
  const unresolved = checks.filter((check) => check.status === "unresolved").length;
  const failed = checks.filter((check) => check.status === "failed").length;
  const verifying = checks.length - unresolved - failed;
  const parts = [
    unresolved ? `暂未查到入库结果 ${unresolved} 次（原请求可能仍在处理）` : "",
    failed ? `暂时无法核对 ${failed} 次` : "",
    verifying ? `正在核对 ${verifying} 次` : "",
  ].filter(Boolean);
  return { total: checks.length, unresolved, failed, verifying, parts };
}

/**
 * One line describing a pending save from its recorded body. Both editors'
 * bodies carry `content`; the note editor additionally tags drafts vs finals.
 * Malformed bodies degrade to the saved-at timestamp.
 */
export function describePendingSave(entry: PendingSave, options: { finalTag?: boolean } = {}): string {
  try {
    const parsed = JSON.parse(entry.body) as { content?: unknown; final?: unknown };
    const text = typeof parsed.content === "string" ? parsed.content : "";
    const preview = text.length > 16 ? `${text.slice(0, 16)}…` : text;
    const tag =
      options.finalTag && "final" in parsed ? (parsed.final ? "（最终版）" : "（草稿）") : "";
    return `「${preview}」${tag} · ${new Date(entry.savedAt).toLocaleString("zh-CN")}`;
  } catch {
    return new Date(entry.savedAt).toLocaleString("zh-CN");
  }
}
