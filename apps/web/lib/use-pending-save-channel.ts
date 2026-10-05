"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  applyPendingCheckOutcome,
  markChecksVerifying,
  runPendingSaveVerificationRound,
  type PendingSaveCheck,
} from "@/lib/pending-save-checks";
import {
  clearPendingSave,
  getPendingSave,
  listPendingSaves,
  type PendingSave,
  type PendingSaveKind,
} from "@/lib/save-request";

export type PendingSaveChannelOptions = {
  /** Which pending records this editor owns. */
  kind: PendingSaveKind;
  noteId?: string;
  encounterId?: string;
  /** False pauses listing entirely (e.g. no encounter selected yet). */
  enabled?: boolean;
  /**
   * Look one save up by its request id. Resolve a short display label when
   * the save had landed (e.g. "v3"); reject — including 404 — when not.
   * The channel classifies 404 as 暂未查到 and anything else as 核对失败.
   */
  lookup: (entry: PendingSave) => Promise<string>;
  /**
   * Called after the MOUNT verification round confirmed landings. Recheck
   * rounds return their labels to the caller instead, so each editor keeps
   * its own notice wording per phase.
   */
  onLanded?: (labels: string[]) => void | Promise<void>;
  /** Confirm text for dropping a record; editors keep their own copy. */
  dropConfirmMessage: string;
};

/**
 * The 编辑与保存 module's pending-save channel as a React hook: ONE state
 * machine shared by the work editor and the quick student-note form.
 *
 * Every unresolved save in scope becomes visible IMMEDIATELY (核对中) when
 * the editor mounts or the scope changes, then is looked up by request id
 * one by one and marked 暂未查到 / 核对失败 / removed (已入库) per entry — a
 * failed lookup round never hides records. Each round is ATTRIBUTED: it
 * freezes the scope and callbacks it started with, so a slow answer for
 * encounter A can never announce through encounter B's callbacks after the
 * user switched (A→B→A announces again — it is the right object on screen
 * again), and only the newest round owns the `checking` flag. The channel
 * also surfaces a save that just failed with unknown outcome
 * (surfaceUnresolved) and answers explicit 再次核对 rounds (recheck).
 * Replay (retry) stays with the editors because each path incorporates the
 * result differently.
 */
export function usePendingSaveChannel(options: PendingSaveChannelOptions) {
  const [checks, setChecks] = useState<PendingSaveCheck[]>([]);
  const [checking, setChecking] = useState(false);
  const { kind, noteId, encounterId, enabled } = options;
  const optionsRef = useRef(options);
  const checksRef = useRef<PendingSaveCheck[]>([]);
  const roundSeqRef = useRef(0);
  // Latest-value mirrors, written after each commit so callbacks and effects
  // never read options/checks through a stale render closure.
  useEffect(() => {
    optionsRef.current = options;
  });
  useEffect(() => {
    checksRef.current = checks;
  }, [checks]);

  // Sequential verification: mark each entry as its own answer arrives. A
  // resolved lookup means the save had landed; a 404 means 暂未查到 (the
  // record STAYS); any other failure marks this and every still-unverified
  // entry 核对失败 and stops hammering. Records always stay visible and
  // retryable. The round captures THIS scope's options at start — the shared
  // runner enforces that only same-scope completions announce and only the
  // newest round clears `checking`.
  const runVerification = useCallback(async (entries: PendingSave[], announce: boolean): Promise<string[]> => {
    const round = ++roundSeqRef.current;
    const startedFrom = optionsRef.current;
    return runPendingSaveVerificationRound({
      scope: {
        kind: startedFrom.kind,
        noteId: startedFrom.noteId,
        encounterId: startedFrom.encounterId,
      },
      entries,
      announce,
      lookup: (entry) => startedFrom.lookup(entry),
      onLanded: (labels) => startedFrom.onLanded?.(labels),
      currentScope: () => {
        const current = optionsRef.current;
        return {
          kind: current.kind,
          noteId: current.noteId,
          encounterId: current.encounterId,
        };
      },
      isNewestRound: () => roundSeqRef.current === round,
      applyOutcome: (requestId, outcome) => {
        setChecks((current) => applyPendingCheckOutcome(current, requestId, outcome));
      },
      clearEntry: (requestId) => clearPendingSave(requestId),
      setChecking,
    });
  }, []);

  // Mount / scope-change listing: records become visible (核对中) BEFORE the
  // first lookup leaves, then are verified one by one.
  const scopeKey = encounterId ?? noteId ?? "";
  useEffect(() => {
    if (enabled === false) return;
    void (async () => {
      const pending = listPendingSaves({ kind, noteId, encounterId });
      setChecks(pending.map((entry) => ({ entry, status: "verifying" as const })));
      if (!pending.length) return;
      await runVerification(pending, true);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, scopeKey, enabled, runVerification]);

  /** Drop stale checks at once when the editor switches scope itself. */
  const resetScope = useCallback(() => setChecks([]), []);

  /** Remove a confirmed entry after the editor's own replay succeeded. */
  const resolveLanded = useCallback((requestId: string) => {
    setChecks((current) => current.filter((check) => check.entry.requestId !== requestId));
  }, []);

  /** Surface a just-failed save (unknown outcome) as 暂未查到 immediately. */
  const surfaceUnresolved = useCallback((requestId: string) => {
    const entry = getPendingSave(requestId);
    if (!entry) return;
    setChecks((current) =>
      current.some((check) => check.entry.requestId === requestId)
        ? current
        : [...current, { entry, status: "unresolved" as const }],
    );
  }, []);

  /** Entries answerable by another verification round. */
  const recheckableCount = checks.filter((check) => check.status !== "verifying").length;

  const recheck = useCallback(async (): Promise<string[]> => {
    const retryable = checksRef.current.filter((check) => check.status !== "verifying");
    if (!retryable.length) return [];
    setChecks(markChecksVerifying);
    return runVerification(retryable.map((check) => check.entry), false);
  }, [runVerification]);

  const drop = useCallback((entry: PendingSave) => {
    if (!window.confirm(optionsRef.current.dropConfirmMessage)) return;
    clearPendingSave(entry.requestId);
    setChecks((current) => current.filter((check) => check.entry.requestId !== entry.requestId));
  }, []);

  return { checks, checking, recheckableCount, recheck, drop, surfaceUnresolved, resolveLanded, resetScope };
}
