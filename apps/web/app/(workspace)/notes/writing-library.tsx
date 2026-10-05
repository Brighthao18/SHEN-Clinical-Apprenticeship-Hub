"use client";

import {
  ArrowRight,
  BookOpenText,
  FileText,
  ScrollText,
  Stamp,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { EmptyState } from "@/components/empty-state";
import { apiFetch } from "@/lib/api";

import { versionTypeLabels, type NoteVersionSummary } from "../patients/[id]/note-editor";

type LibraryPatient = {
  id: string;
  internal_patient_code: string;
  display_name: string | null;
};

type LibraryNoteItem = {
  note_id: string;
  patient_id: string;
  patient: LibraryPatient | null;
  note_type: string;
  version_count: number;
  first_created_at: string;
  latest: NoteVersionSummary;
  latest_final: NoteVersionSummary | null;
  evidence_window?: {
    latest_evidence_encounter_at: string | null;
    later_encounter_count: number;
  };
};

type PatientOption = {
  id: string;
  internal_patient_code: string;
  display_name: string | null;
};

const noteTypeLabels: Record<string, string> = {
  teacher_note: "跟师笔记",
  case_record: "医案整理",
};

// A note is "进行中" while its newest version is not the user's final save.
// A previously saved final stays visible through `latest_final` and never
// disappears just because a new draft was appended after it.
function isFinalized(item: LibraryNoteItem) {
  return item.latest.version_type === "user_final";
}

function noteTitle(item: LibraryNoteItem) {
  const heading = item.latest.title || noteTypeLabels[item.note_type] || item.note_type;
  const evidenceCount = item.latest.evidence_encounters.length;
  return evidenceCount
    ? `${heading} · 依据 ${evidenceCount} 次就诊`
    : heading;
}

function formatTime(value: string) {
  return new Date(value).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" });
}

function encounterLink(item: LibraryNoteItem) {
  const first = item.latest.evidence_encounters[0];
  return first ? `/patients/${item.patient_id}?encounter=${first}` : `/patients/${item.patient_id}`;
}

function NoteCard({ item }: { item: LibraryNoteItem }) {
  return (
    <article className="rounded-xl border border-stone-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 rounded-full bg-[#eef7f3] px-2 py-1 text-[11px] font-semibold text-[#175849]">
              {item.note_type === "teacher_note" ? <BookOpenText aria-hidden="true" size={12} /> : <ScrollText aria-hidden="true" size={12} />}
              {noteTypeLabels[item.note_type] ?? item.note_type}
            </span>
            {isFinalized(item) ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-1 text-[11px] font-semibold text-emerald-700">
                <Stamp aria-hidden="true" size={12} /> 已定稿
              </span>
            ) : (
              <span className="rounded-full bg-amber-50 px-2 py-1 text-[11px] font-semibold text-amber-800">进行中</span>
            )}
          </div>
          <h3 className="mt-2 truncate font-bold text-stone-900">{noteTitle(item)}</h3>
          {item.patient ? (
            <Link href={encounterLink(item)} className="focus-ring mt-1 inline-flex flex-wrap items-center gap-1 text-xs text-stone-500 hover:text-[#1f6857]">
              患者 {item.patient.internal_patient_code}
              {item.patient.display_name ? ` · ${item.patient.display_name}` : ""}
              <ArrowRight aria-hidden="true" size={12} />
            </Link>
          ) : null}
          {item.evidence_window && item.evidence_window.later_encounter_count > 0 ? (
            <p className="mt-1 text-xs text-amber-700">
              适用范围：基于 {item.evidence_window.latest_evidence_encounter_at ? new Date(item.evidence_window.latest_evidence_encounter_at).toLocaleDateString("zh-CN") : "记录"} 之前的就诊；此后该患者还有 {item.evidence_window.later_encounter_count} 次就诊未包含，需要时可在患者页重新生成（追加新版本，不覆盖本稿）。
            </p>
          ) : null}
        </div>
        <Link
          href={`/notes/${item.note_id}`}
          className="focus-ring inline-flex shrink-0 items-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white"
        >
          <FileText aria-hidden="true" size={15} />
          打开编辑
        </Link>
      </div>
      <dl className="mt-3 grid gap-x-6 gap-y-1 text-xs text-stone-500 sm:grid-cols-3">
        <div>
          <dt className="inline">最近编辑：</dt>
          <dd className="inline font-medium text-stone-700">
            {versionTypeLabels[item.latest.version_type] ?? item.latest.version_type} · {formatTime(item.latest.created_at)}
          </dd>
        </div>
        <div>
          <dt className="inline">最近定稿：</dt>
          <dd className="inline font-medium text-stone-700">
            {item.latest_final ? formatTime(item.latest_final.created_at) : "尚未定稿"}
          </dd>
        </div>
        <div>
          <dt className="inline">版本数：</dt>
          <dd className="inline font-medium text-stone-700">{item.version_count}（全部永久保留）</dd>
        </div>
      </dl>
    </article>
  );
}

const PAGE_SIZE = 20;

export function WritingLibrary() {
  const router = useRouter();
  const [notes, setNotes] = useState<LibraryNoteItem[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [patients, setPatients] = useState<PatientOption[]>([]);
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [patientFilter, setPatientFilter] = useState("");
  const [selectedPatient, setSelectedPatient] = useState("");
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);

  function setItemsForPage(
    library: { items: LibraryNoteItem[]; total: number },
    offset: number,
    append: boolean,
  ) {
    setTotal(library.total);
    setNotes((current) => {
      if (!append || current === null) return library.items;
      // 追加去重：翻页窗口之间列表发生变化时也不会显示重复卡片。
      const known = new Set(current.map((item) => item.note_id));
      return [...current, ...library.items.filter((item) => !known.has(item.note_id))];
    });
  }

  const load = useCallback(
    async (offset: number, append: boolean) => {
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (typeFilter) params.set("note_type", typeFilter);
      if (statusFilter === "finalized") params.set("finalized", "true");
      if (statusFilter === "in_progress") params.set("finalized", "false");
      if (patientFilter) params.set("patient_id", patientFilter);
      try {
        const [library, directory] = await Promise.all([
          apiFetch<{ items: LibraryNoteItem[]; total: number }>(`/api/v1/notes?${params.toString()}`, {
            signal: controller.signal,
          }),
          apiFetch<{ items: PatientOption[] }>("/api/v1/patients?limit=100", {
            signal: controller.signal,
          }),
        ]);
        if (!controller.signal.aborted) {
          setItemsForPage(library, offset, append);
          setPatients(directory.items);
          setError("");
        }
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "读取写作库失败");
        }
      }
    },
    [typeFilter, statusFilter, patientFilter],
  );

  useEffect(() => {
    // 筛选或首载：从第一页开始。
    const timer = window.setTimeout(() => void load(0, false), 0);
    return () => {
      window.clearTimeout(timer);
      request.current?.abort();
    };
  }, [load]);

  async function loadMore() {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      await load(notes?.length ?? 0, true);
    } finally {
      setLoadingMore(false);
    }
  }

  const notes_ = notes ?? [];
  const continueEditing = notes_.filter((item) => !isFinalized(item)).slice(0, 3);
  const hasMore = notes !== null && notes.length < total;

  return (
    <div>
      <p className="text-sm font-medium text-[#1f6857]">用户原稿 · AI 草稿 · 用户定稿</p>
      <h1 className="mt-1 text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl">师承写作</h1>
      <p className="mt-2 max-w-3xl text-sm text-stone-500">
        跟师笔记与医案整理的全部版本永久保留；AI 只产出草稿，定稿权在你。最近编辑与最近定稿分别显示，定稿后继续修改不会盖掉已定稿版本。
      </p>

      {error ? (
        <div role="alert" className="mt-5 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          <p>{error}</p>
          <button type="button" onClick={() => void load(0, false)} className="focus-ring mt-2 rounded-xl border border-red-300 bg-white px-3 py-1.5 text-sm">重试</button>
        </div>
      ) : null}

      <section aria-labelledby="start-writing-title" className="surface mt-6 rounded-2xl p-5 sm:p-6">
        <h2 id="start-writing-title" className="text-base font-bold text-stone-900">发起新写作</h2>
        <p className="mt-1 text-sm text-stone-500">
          写作从患者资料出发：选择患者后进入其工作区，在具体就诊旁生成跟师笔记，或选定就诊范围整理医案。
        </p>
        <form
          className="mt-3 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (selectedPatient) router.push(`/patients/${selectedPatient}`);
          }}
        >
          <label className="sr-only" htmlFor="start-writing-patient">选择患者</label>
          <select
            id="start-writing-patient"
            value={selectedPatient}
            onChange={(event) => setSelectedPatient(event.target.value)}
            className="focus-ring min-w-0 flex-1 rounded-xl border border-stone-300 bg-white px-3 py-2.5 text-sm sm:max-w-md"
          >
            <option value="">选择患者…</option>
            {patients.map((patient) => (
              <option key={patient.id} value={patient.id}>
                {patient.internal_patient_code}{patient.display_name ? ` · ${patient.display_name}` : ""}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={!selectedPatient}
            className="focus-ring inline-flex items-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
          >
            进入患者工作区 <ArrowRight aria-hidden="true" size={15} />
          </button>
        </form>
      </section>

      {notes === null && !error ? (
        <div className="mt-6 h-40 animate-pulse rounded-2xl bg-white/60" aria-busy="true" />
      ) : null}

      {notes !== null && continueEditing.length ? (
        <section aria-labelledby="continue-title" className="mt-6">
          <h2 id="continue-title" className="text-base font-bold text-stone-900">继续编辑</h2>
          <p className="mt-1 text-xs text-stone-500">最近有草稿未定稿的作品。</p>
          <div className="mt-3 space-y-3">
            {continueEditing.map((item) => <NoteCard key={item.note_id} item={item} />)}
          </div>
        </section>
      ) : null}

      {notes !== null ? (
        <section aria-labelledby="library-title" className="mt-8">
          <h2 id="library-title" className="text-base font-bold text-stone-900">全部作品</h2>
          <div className="mt-3 flex flex-wrap gap-2">
            <label className="text-xs font-semibold text-stone-500">
              类型
              <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} className="focus-ring ml-2 rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal">
                <option value="">全部</option>
                <option value="teacher_note">跟师笔记</option>
                <option value="case_record">医案整理</option>
              </select>
            </label>
            <label className="text-xs font-semibold text-stone-500">
              状态
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="focus-ring ml-2 rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal">
                <option value="">全部</option>
                <option value="in_progress">进行中</option>
                <option value="finalized">已定稿</option>
              </select>
            </label>
            <label className="text-xs font-semibold text-stone-500">
              患者
              <select value={patientFilter} onChange={(event) => setPatientFilter(event.target.value)} className="focus-ring ml-2 max-w-[16rem] rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal">
                <option value="">全部</option>
                {patients.map((patient) => (
                  <option key={patient.id} value={patient.id}>
                    {patient.internal_patient_code}{patient.display_name ? ` · ${patient.display_name}` : ""}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {notes.length ? (
            <div className="mt-4 space-y-3" aria-live="polite">
              {notes.map((item) => <NoteCard key={item.note_id} item={item} />)}
            </div>
          ) : (
            <div className="mt-4">
              <EmptyState
                title="还没有符合条件的作品"
                detail="在上方选择患者进入工作区，或从患者时间线直接发起跟师笔记与医案整理。"
              />
            </div>
          )}
          {hasMore ? (
            <div className="mt-4 text-center">
              <button
                type="button"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                className="focus-ring inline-flex items-center gap-2 rounded-xl border border-stone-300 bg-white px-4 py-2 text-sm font-medium text-stone-700 disabled:opacity-60"
              >
                {loadingMore ? "正在加载…" : `加载更多（已显示 ${notes.length}/${total} 篇）`}
              </button>
            </div>
          ) : null}
        </section>
      ) : null}

      {notes !== null && notes.length ? (
        <p className="mt-6 text-xs text-stone-400">共 {total} 篇作品（当前显示 {notes.length} 篇）；所有历史版本可在编辑页查看。</p>
      ) : null}
    </div>
  );
}
