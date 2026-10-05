"use client";

import { BookOpenText, LoaderCircle, Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

import { NoteWorkspace, listPatientNotes, type NoteListItem } from "./note-editor";

export function TeacherNotePanel({
  patientId,
  encounterId,
  onDirtyChange,
}: {
  patientId: string;
  encounterId: string;
  /** Reported upward so the timeline can confirm before unmounting the editor. */
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [notes, setNotes] = useState<NoteListItem[] | null>(null);
  const [activeNoteId, setActiveNoteId] = useState("");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [editorDirty, setEditorDirty] = useState(false);

  const matching = (notes ?? []).filter(
    (note) =>
      note.note_type === "teacher_note" &&
      note.latest.evidence_encounters.includes(encounterId),
  );
  const activeId = activeNoteId || matching.at(-1)?.note_id || "";

  const handleEditorDirty = useCallback(
    (dirty: boolean) => {
      setEditorDirty(dirty);
      onDirtyChange?.(dirty);
    },
    [onDirtyChange],
  );

  // Switching away (or generating a new draft that switches) discards
  // unsaved edits in the editor, so ask before leaving.
  function confirmLeaveEditor() {
    return (
      !editorDirty ||
      window.confirm("当前笔记有未保存修改。切换后这些修改会丢失，确定切换吗？")
    );
  }

  const reload = useCallback(async () => {
    // 该就诊的跟师笔记由数据库侧过滤（note_type + 依据就诊），候选天然是小集合。
    const payload = await listPatientNotes(patientId, {
      note_type: "teacher_note",
      encounter_id: encounterId,
      sort: "latest",
      limit: 200,
    });
    setNotes(payload.items);
  }, [patientId, encounterId]);

  useEffect(() => {
    let active = true;
    listPatientNotes(patientId, {
      note_type: "teacher_note",
      encounter_id: encounterId,
      sort: "latest",
      limit: 200,
    })
      .then((payload) => {
        if (!active) return;
        setNotes(payload.items);
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      })
      .finally(() => {
        if (active) setNotes((current) => current ?? []);
      });
    return () => {
      active = false;
    };
  }, [patientId, encounterId]);

  async function generate() {
    if (
      editorDirty &&
      !window.confirm("当前笔记有未保存修改。生成新草稿会切换笔记，未保存修改会丢失，确定继续吗？")
    ) {
      return;
    }
    setGenerating(true);
    setError("");
    try {
      const created = await apiFetch<{ note_id: string }>(
        `/api/v1/encounters/${encounterId}/teacher-note-drafts`,
        { method: "POST" },
      );
      setActiveNoteId(created.note_id);
      await reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成跟师笔记失败");
    } finally {
      setGenerating(false);
    }
  }

  if (!notes) {
    return (
      <div className="rounded-xl border border-stone-200 bg-[#faf8f2] p-4">
        <div className="h-24 animate-pulse rounded-lg bg-stone-100" />
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-[#bcd8ce] bg-[#f4faf7] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="flex items-center gap-2 text-sm font-bold text-stone-900">
            <BookOpenText aria-hidden="true" size={17} className="text-[#1f6857]" />
            跟师笔记（师承写作 · Phase 9）
          </p>
          <p className="mt-1 text-xs leading-5 text-stone-500">
            围绕本次就诊，把结构化事实、学生补录与确定性处方变化整理成七段式草稿；
            “老师观点”只能来自你记录的口头补录，其余为标注过的学习理解。
          </p>
        </div>
        {!activeId ? (
          <button
            type="button"
            onClick={generate}
            disabled={generating}
            className="focus-ring inline-flex items-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
          >
            {generating ? <LoaderCircle aria-hidden="true" className="animate-spin" size={16} /> : <Sparkles aria-hidden="true" size={16} />}
            生成跟师笔记草稿
          </button>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="mt-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>
      ) : null}

      {activeId ? (
        <>
          {matching.length > 1 ? (
            <label className="mt-3 block text-xs font-semibold text-stone-600">
              选择笔记
              <select
                value={activeId}
                onChange={(event) => {
                  if (!confirmLeaveEditor()) return;
                  setActiveNoteId(event.target.value);
                }}
                className="focus-ring mt-1 block rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
              >
                {matching.map((note) => (
                  <option key={note.note_id} value={note.note_id}>
                    {new Date(note.first_created_at).toLocaleString("zh-CN")} · {note.version_count} 个版本
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <NoteWorkspace
            key={activeId}
            noteId={activeId}
            onChanged={reload}
            onDirtyChange={handleEditorDirty}
          />
        </>
      ) : null}
    </div>
  );
}
