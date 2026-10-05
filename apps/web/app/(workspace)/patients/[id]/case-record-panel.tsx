"use client";

import { BookOpenText, LoaderCircle, ScrollText, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";

import { NoteWorkspace, listPatientNotes, type NoteListItem } from "./note-editor";

type EncounterOption = { id: string; encounter_at: string };

function displayDate(value: string) {
  return new Date(value).toLocaleDateString("zh-CN");
}

export function CaseRecordPanel({
  patientId,
  encounters,
}: {
  patientId: string;
  encounters: EncounterOption[];
}) {
  const ordered = useMemo(
    () => [...encounters].sort((a, b) => a.encounter_at.localeCompare(b.encounter_at)),
    [encounters],
  );
  const [startId, setStartId] = useState(ordered[0]?.id ?? "");
  const [endId, setEndId] = useState(ordered.at(-1)?.id ?? "");
  const [notes, setNotes] = useState<NoteListItem[] | null>(null);
  const [notesTotal, setNotesTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [editorDirty, setEditorDirty] = useState(false);

  // 医案由数据库侧过滤（note_type=case_record，最近更新优先）分页加载；
  // 更多作品按用户显式点击追加，不自动拉全量。
  const PAGE_SIZE = 200;
  const caseNotes = notes ?? [];
  const activeId = selectedId || caseNotes[0]?.note_id || "";

  // Switching away (or generating a new draft that switches) discards
  // unsaved edits in the editor, so ask before leaving.
  function confirmLeaveEditor() {
    return (
      !editorDirty ||
      window.confirm("当前笔记有未保存修改。切换后这些修改会丢失，确定切换吗？")
    );
  }

  useEffect(() => {
    let active = true;
    listPatientNotes(patientId, { note_type: "case_record", sort: "latest", limit: PAGE_SIZE })
      .then((payload) => {
        if (!active) return;
        setNotes(payload.items);
        setNotesTotal(payload.total);
        // 默认选中最近更新的医案（sort=latest 的第一条）。
        setSelectedId(payload.items[0]?.note_id ?? "");
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
  }, [patientId]);

  async function loadMore() {
    // 防重入：加载中再次点击直接忽略（按钮同时禁用）。
    if (loadingMore) return;
    setLoadingMore(true);
    setLoadMoreError("");
    try {
      // offset 按已确认页面推进（已加载条数）；追加时按 note_id 去重，
      // 即使响应乱序/重放也不会出现重复条目。
      const offset = caseNotes.length;
      const payload = await listPatientNotes(patientId, {
        note_type: "case_record",
        sort: "latest",
        limit: PAGE_SIZE,
        offset,
      });
      setNotes((current) => {
        const seen = new Set((current ?? []).map((note) => note.note_id));
        const fresh = payload.items.filter((note) => !seen.has(note.note_id));
        return [...(current ?? []), ...fresh];
      });
      setNotesTotal(payload.total);
      if (payload.items.length === 0) {
        setLoadMoreError("没有更多医案了。");
      }
    } catch (reason) {
      // 失败保留已加载页面，按钮保持可点 = 重试。
      setLoadMoreError(
        reason instanceof Error ? `更多医案加载失败：${reason.message}（可重试）` : "更多医案加载失败（可重试）",
      );
    } finally {
      setLoadingMore(false);
    }
  }

  async function generate() {
    if (
      editorDirty &&
      !window.confirm("当前笔记有未保存修改。整理新医案会切换到新草稿，未保存修改会丢失，确定继续吗？")
    ) {
      return;
    }
    setGenerating(true);
    setError("");
    try {
      const created = await apiFetch<{ note_id: string }>(
        `/api/v1/patients/${patientId}/case-record-drafts`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            start_encounter_id: startId || null,
            end_encounter_id: endId || null,
          }),
        },
      );
      const payload = await listPatientNotes(patientId, {
        note_type: "case_record",
        sort: "latest",
        limit: PAGE_SIZE,
      });
      setNotes(payload.items);
      setNotesTotal(payload.total);
      // 新草稿是最近更新的一条，回到第一页即可定位。
      setSelectedId(created.note_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "整理医案失败");
    } finally {
      setGenerating(false);
    }
  }

  return (
    <section id="case-record" className="surface mt-7 scroll-mt-5 rounded-2xl p-5 sm:p-6">
      <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-start">
        <div className="max-w-2xl">
          <div className="flex items-center gap-2 text-[#1f6857]">
            <ScrollText aria-hidden="true" size={20} />
            <h2 className="font-bold text-stone-900">医案整理</h2>
          </div>
          <p className="mt-2 text-sm leading-6 text-stone-500">
            按就诊时间顺序整理一段真实病程：初诊、二诊……方药变化直接引用 Phase 7 确定性 Diff，
            按语单独标注为 AI 辅助理解。AI 只产出草稿，最终医案由你定稿。
          </p>
        </div>
        {caseNotes.length ? (
          <label className="text-xs font-medium text-stone-500">
            查看已有医案（共 {notesTotal} 篇，按最近更新排序）
            <select
              value={activeId}
              onChange={(event) => {
                if (!confirmLeaveEditor()) return;
                setSelectedId(event.target.value);
              }}
              className="focus-ring mt-1 block rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm text-stone-700"
            >
              {caseNotes.map((note) => (
                <option key={note.note_id} value={note.note_id}>
                  {new Date(note.first_created_at).toLocaleString("zh-CN")} · {note.version_count} 个版本
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {notes !== null && (loadMoreError || notesTotal > caseNotes.length) ? (
          <div className="mt-2">
            {loadMoreError ? (
              <p role="alert" className="mb-1 text-xs text-red-700">
                {loadMoreError}
              </p>
            ) : null}
            {notesTotal > caseNotes.length ? (
              <button
                type="button"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                className="focus-ring rounded-lg border border-stone-300 bg-white px-2.5 py-1.5 text-xs text-stone-600 disabled:opacity-50"
              >
                {loadingMore ? "正在加载更多医案…" : `显示更多医案（已加载 ${caseNotes.length} / ${notesTotal}）`}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="mt-5 grid gap-3 rounded-xl border border-stone-200 bg-[#faf8f2] p-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <label className="text-xs font-semibold text-stone-600">
          开始就诊
          <select
            value={startId}
            onChange={(event) => setStartId(event.target.value)}
            className="focus-ring mt-1.5 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 text-sm font-normal"
          >
            {ordered.map((encounter) => (
              <option key={encounter.id} value={encounter.id}>{displayDate(encounter.encounter_at)}</option>
            ))}
          </select>
        </label>
        <label className="text-xs font-semibold text-stone-600">
          结束就诊
          <select
            value={endId}
            onChange={(event) => setEndId(event.target.value)}
            className="focus-ring mt-1.5 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 text-sm font-normal"
          >
            {ordered.map((encounter) => (
              <option key={encounter.id} value={encounter.id}>{displayDate(encounter.encounter_at)}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={generate}
          disabled={generating || !ordered.length}
          className="focus-ring inline-flex items-center justify-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
        >
          {generating ? <LoaderCircle aria-hidden="true" className="animate-spin" size={17} /> : <BookOpenText aria-hidden="true" size={17} />}
          整理医案（新草稿）
        </button>
      </div>

      {error ? (
        <p role="alert" className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </p>
      ) : null}
      {!notes ? <div className="mt-5 h-32 animate-pulse rounded-xl bg-stone-100" /> : null}
      {notes && !activeId ? (
        <div className="mt-5 rounded-xl border border-dashed border-stone-300 px-5 py-8 text-center">
          <Sparkles aria-hidden="true" className="mx-auto text-stone-400" size={22} />
          <p className="mt-2 text-sm font-semibold text-stone-700">尚无医案</p>
          <p className="mt-1 text-xs text-stone-500">选择就诊范围后点击“整理医案”，生成可编辑的草稿。</p>
        </div>
      ) : null}

      {activeId ? (
        <NoteWorkspace key={activeId} noteId={activeId} onDirtyChange={setEditorDirty} />
      ) : null}
    </section>
  );
}
