"use client";

import { ArrowLeft, BookOpenText, ChevronLeft, ScrollText, UserRound } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { apiFetch } from "@/lib/api";

import { NoteWorkspace, type NoteHistory } from "../../patients/[id]/note-editor";

const noteTypeLabels: Record<string, string> = {
  teacher_note: "跟师笔记",
  case_record: "医案整理",
};

export function WritingWorkspace({
  noteId,
  resumeSessionId,
}: {
  noteId: string;
  /** 工作台“继续工作”指派的编辑会话：透传给编辑器用于高亮对应缓冲。 */
  resumeSessionId?: string;
}) {
  const [history, setHistory] = useState<NoteHistory | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setHistory(await apiFetch<NoteHistory>(`/api/v1/notes/${noteId}`));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取作品失败");
    }
  }, [noteId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  if (error) {
    return (
      <div>
        <Link href="/notes" className="focus-ring inline-flex items-center gap-1 text-sm text-[#1f6857]">
          <ArrowLeft aria-hidden="true" size={15} /> 返回写作库
        </Link>
        <div className="mt-4">
          <EmptyState title="作品暂时无法读取" detail={error} />
          <button type="button" onClick={() => void load()} className="focus-ring mt-3 rounded-xl border border-stone-300 px-4 py-2 text-sm">重试</button>
        </div>
      </div>
    );
  }

  if (!history) {
    return <div className="h-80 animate-pulse rounded-2xl bg-white/60" aria-busy="true" />;
  }

  const latest = history.versions.at(-1);
  const typeLabel = noteTypeLabels[history.note_type] ?? history.note_type;
  // The encounter named in the URL is the first evidence encounter of the
  // currently edited chain, so “查看患者” lands on the exact visit.
  const encounterAnchor = latest?.evidence_encounters[0];
  const patientHref = encounterAnchor
    ? `/patients/${history.patient_id}?encounter=${encounterAnchor}`
    : `/patients/${history.patient_id}`;

  return (
    <div className="mx-auto max-w-4xl">
      <Link href="/notes" className="focus-ring inline-flex items-center gap-1 text-sm text-[#1f6857]">
        <ArrowLeft aria-hidden="true" size={15} /> 返回写作库
      </Link>

      <header className="surface mt-3 rounded-2xl p-5 sm:p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="flex items-center gap-2 text-sm font-medium text-[#1f6857]">
              {history.note_type === "teacher_note" ? <BookOpenText aria-hidden="true" size={15} /> : <ScrollText aria-hidden="true" size={15} />}
              师承写作 · {typeLabel}
            </p>
            <h1 className="mt-1 text-xl font-bold tracking-tight text-stone-900 sm:text-2xl">
              {latest?.title || typeLabel}
            </h1>
          </div>
          {history.patient ? (
            <Link
              href={patientHref}
              className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#9bc5b8] bg-[#eef7f3] px-4 py-2.5 text-sm font-semibold text-[#175849]"
            >
              <UserRound aria-hidden="true" size={15} />
              {history.patient.internal_patient_code}
              {history.patient.display_name ? ` · ${history.patient.display_name}` : ""}
              <ChevronLeft aria-hidden="true" size={14} className="rotate-180" />
            </Link>
          ) : null}
        </div>
        <p className="mt-2 text-xs leading-5 text-stone-500">
          编辑与定稿都在这里完成；内容依据保留来源就诊，可随时回到患者时间线核对。
        </p>
        {history.evidence_window && history.evidence_window.later_encounter_count > 0 ? (
          <p role="status" className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-900">
            适用范围提示：本作品基于 {history.evidence_window.latest_evidence_encounter_at ? new Date(history.evidence_window.latest_evidence_encounter_at).toLocaleDateString("zh-CN") : "所选"} 之前的就诊整理；此后该患者还有 {history.evidence_window.later_encounter_count} 次就诊未包含。如需纳入，请在患者页重新生成——会追加新版本，不会覆盖本稿。
          </p>
        ) : null}
      </header>

      <NoteWorkspace
        key={noteId}
        noteId={noteId}
        resumeSessionId={resumeSessionId}
        onChanged={() => void load()}
      />
    </div>
  );
}
