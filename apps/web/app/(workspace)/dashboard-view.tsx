"use client";

import {
  ArrowRight,
  BookOpenText,
  FileCheck2,
  FileUp,
  LoaderCircle,
  ScrollText,
  ShieldAlert,
  Stamp,
  UserRoundPlus,
  UsersRound,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { apiFetch } from "@/lib/api";
import { collectLocalContinueWork, type LocalContinueItem } from "@/lib/continue-work";
import { inProgressNotesQuery, recentEncounterHref } from "@/lib/dashboard-work";

type Dashboard = {
  timezone: string;
  week_start: string;
  ocr_provider: string;
  counts: {
    imported: number;
    accepted: number;
    unresolved: number;
    compatibility_reviews: number;
    new_patients: number;
    returning_patients: number;
  };
  recent_patients: Array<{ id: string; code: string; sex: string; last_encounter_at: string }>;
  recent_encounters: Array<{
    id: string;
    patient_id: string;
    encounter_at: string;
    chief_complaint: string | null;
  }>;
};

const countCards = [
  { key: "imported", label: "本周新导入", icon: FileUp },
  { key: "accepted", label: "已导入临床库", icon: FileCheck2 },
  { key: "unresolved", label: "本周未解决", icon: ShieldAlert },
  { key: "new_patients", label: "新患者", icon: UserRoundPlus },
  { key: "returning_patients", label: "复诊患者", icon: UsersRound },
] as const;

type WritingNote = {
  note_id: string;
  note_type: string;
  patient: { id: string; internal_patient_code: string; display_name: string | null } | null;
  version_count: number;
  latest: {
    title: string | null;
    created_at: string;
    version_type: string;
  };
  latest_final: { created_at: string } | null;
};

const writingTypeLabels: Record<string, string> = {
  teacher_note: "跟师笔记",
  case_record: "医案整理",
};

const writingVersionLabels: Record<string, string> = {
  user_original: "用户原稿",
  ai_draft: "AI 草稿",
  user_draft: "用户草稿",
  user_final: "用户定稿",
};

function ContinueWriting() {
  const [notes, setNotes] = useState<WritingNote[] | null>(null);
  const [localItems, setLocalItems] = useState<LocalContinueItem[]>([]);
  const [loadError, setLoadError] = useState("");
  // 旧补录记录只带 encounterId：工作台经受保护的 owner 查询解析归属。
  // 解析失败必须如实可见并提供重试，绝不猜测归属、绝不丢弃记录。
  const [ownership, setOwnership] = useState<Record<string, string>>({});
  const [failedOwnership, setFailedOwnership] = useState<Record<string, boolean>>({});

  const load = useCallback(() => {
    // 进行中筛选在数据库侧完成（finalized=false），本地不再二次过滤；
    // 较早的进行中作品不会被最近的定稿挤出前 20 条。
    setNotes(null);
    setLoadError("");
    apiFetch<{ items: WritingNote[] }>(inProgressNotesQuery)
      .then((payload) => {
        setNotes(payload.items);
        setLoadError("");
      })
      .catch((reason: Error) => {
        // 读取失败必须可见：静默隐藏会让用户把失败当成"没有草稿"。
        setLoadError(reason.message);
      });
  }, []);

  useEffect(() => {
    // 本机聚合只读 localStorage，必须在客户端 effect 里执行。
    const timer = window.setTimeout(() => {
      setLocalItems(collectLocalContinueWork().slice(0, 6));
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    for (const item of localItems) {
      if (!item.needsResolution || !item.encounterId) continue;
      const encounterId = item.encounterId;
      if (ownership[encounterId] || failedOwnership[encounterId]) continue;
      apiFetch<{ patient_id: string }>(`/api/v1/encounters/${encounterId}/owner`)
        .then((payload) => {
          setOwnership((current) => ({ ...current, [encounterId]: payload.patient_id }));
        })
        .catch(() => {
          setFailedOwnership((current) => ({ ...current, [encounterId]: true }));
        });
    }
  }, [localItems, ownership, failedOwnership]);

  function retryOwnership(encounterId: string) {
    setFailedOwnership((current) => {
      const next = { ...current };
      delete next[encounterId];
      return next;
    });
  }

  const titleByNoteId = useMemo(() => {
    const map = new Map<string, string>();
    for (const note of notes ?? []) {
      map.set(note.note_id, note.latest.title || writingTypeLabels[note.note_type] || note.note_type);
    }
    return map;
  }, [notes]);

  // 服务器作品列表的加载/失败只影响“进行中的作品”小节；本机草稿与待核对
  // 记录在其上方独立呈现，永远不被远程请求阻塞。
  return (
    <section aria-labelledby="continue-writing-title" className="surface mt-7 rounded-2xl p-5 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 id="continue-writing-title" className="text-base font-bold text-stone-900">继续工作</h2>
          <p className="mt-1 text-xs text-stone-500">本机未保存的编辑与补录草稿、待核对的保存，以及最新版本尚未定稿的作品；点击直达对应对象，处理动作在编辑页里完成。</p>
        </div>
        <Link href="/notes" className="focus-ring inline-flex items-center gap-1 text-sm font-medium text-[#1f6857] hover:underline">
          打开写作库 <ArrowRight aria-hidden="true" size={14} />
        </Link>
      </div>
      {localItems.length ? (
        <ul className="mt-3 divide-y divide-stone-200 rounded-xl border border-amber-200 bg-amber-50/60">
          {localItems.map((item) => {
            const label =
              item.family === "student_note"
                ? "学生补录"
                : titleByNoteId.get(item.noteId ?? "") ?? "作品";
            const badge = (
              <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] font-semibold ${item.kind === "verifying" ? "bg-amber-200 text-amber-900" : "bg-white text-amber-800 ring-1 ring-inset ring-amber-300"}`}>
                {item.kind === "verifying" ? "待核对" : "编辑中"}
              </span>
            );
            const savedAtCell = (
              <span className="shrink-0 text-xs text-stone-400">
                {new Date(item.savedAt).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" })}
              </span>
            );
            // 旧补录记录需要先解析归属；解析成功前不可点击，失败时如实提示。
            const resolvedPatientId = item.encounterId ? ownership[item.encounterId] : undefined;
            const resolutionFailed = Boolean(item.encounterId && failedOwnership[item.encounterId]);
            if (item.needsResolution && !resolvedPatientId && !resolutionFailed) {
              return (
                <li key={item.key} className="flex items-center gap-3 px-3 py-2.5" aria-busy="true">
                  {badge}
                  <span className="min-w-0 flex-1 truncate text-sm text-stone-800">
                    {label}
                    <span className="ml-2 text-xs text-stone-500">正在确认所属患者…</span>
                  </span>
                </li>
              );
            }
            if (item.needsResolution && resolutionFailed) {
              return (
                <li key={item.key} className="px-3 py-2.5">
                  <div className="flex items-center gap-3">
                    {badge}
                    <span className="min-w-0 flex-1 text-sm text-stone-800">
                      {label}
                      <span className="ml-2 text-xs leading-5 text-stone-600">
                        无法确认这条记录属于哪位患者（就诊可能已不存在，或服务暂不可用）。记录仍保留在本机，不会丢失；也可在患者工作区手动打开对应就诊处理。
                      </span>
                    </span>
                    {savedAtCell}
                    <button
                      type="button"
                      onClick={() => item.encounterId && retryOwnership(item.encounterId)}
                      className="focus-ring shrink-0 rounded-lg border border-amber-300 bg-white px-2.5 py-1 text-xs text-amber-800"
                    >
                      重试确认
                    </button>
                  </div>
                </li>
              );
            }
            const href =
              item.family === "student_note" && item.needsResolution && resolvedPatientId && item.encounterId
                ? `/patients/${resolvedPatientId}?encounter=${encodeURIComponent(item.encounterId)}` +
                  (item.resumeSessionId ? `&resume=${encodeURIComponent(item.resumeSessionId)}` : "")
                : item.href;
            return (
              <li key={item.key}>
                <Link href={href ?? "#"} className="focus-ring flex items-center gap-3 px-3 py-2.5 hover:bg-amber-50">
                  {badge}
                  <span className="min-w-0 flex-1 truncate text-sm text-stone-800">
                    {label}
                    <span className="ml-2 text-xs text-stone-500">{item.detail}</span>
                  </span>
                  {savedAtCell}
                  <ArrowRight aria-hidden="true" className="shrink-0 text-stone-400" size={14} />
                </Link>
              </li>
            );
          })}
        </ul>
      ) : null}
      {loadError ? (
        <div role="alert" className="mt-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <p>进行中作品读取失败：{loadError}</p>
          <p className="mt-1 text-xs text-red-600/80">上方本机草稿与待核对记录不受影响。</p>
          <button type="button" onClick={load} className="focus-ring mt-2 rounded-xl border border-red-300 bg-white px-3 py-1.5 text-xs text-red-700">重试加载</button>
        </div>
      ) : notes === null ? (
        <p className="mt-3 flex items-center gap-2 text-xs text-stone-500" aria-busy="true">
          <LoaderCircle aria-hidden="true" className="animate-spin text-[#1f6857]" size={14} />
          正在读取进行中的作品…
        </p>
      ) : notes.length ? (
        <>
          <p className="mt-3 text-xs font-semibold text-stone-500">进行中的作品</p>
          <ul className="mt-3 divide-y divide-stone-200">
          {notes.map((note) => (
            <li key={note.note_id}>
              <Link href={`/notes/${note.note_id}`} className="focus-ring flex items-center gap-3 py-3 hover:bg-[#faf8f2]">
                {note.note_type === "teacher_note"
                  ? <BookOpenText aria-hidden="true" className="shrink-0 text-[#1f6857]" size={17} />
                  : <ScrollText aria-hidden="true" className="shrink-0 text-[#1f6857]" size={17} />}
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold text-stone-800">
                    {note.latest.title || writingTypeLabels[note.note_type] || note.note_type}
                    <span className="ml-2 rounded bg-[#eef7f3] px-1.5 py-0.5 text-[11px] font-medium text-[#175849]">
                      {writingTypeLabels[note.note_type] ?? note.note_type}
                    </span>
                  </p>
                  <p className="mt-0.5 text-xs text-stone-500">
                    {note.patient ? `${note.patient.internal_patient_code} · ` : ""}
                    {writingVersionLabels[note.latest.version_type] ?? note.latest.version_type} · {new Date(note.latest.created_at).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" })}
                    {note.latest_final ? " · 已有定稿" : ""}
                  </p>
                </div>
                <ArrowRight aria-hidden="true" className="shrink-0 text-stone-400" size={15} />
              </Link>
            </li>
          ))}
          </ul>
        </>
      ) : !localItems.length ? (
        <p className="mt-3 rounded-xl border border-dashed border-stone-300 px-5 py-5 text-center text-sm text-stone-500">
          <Stamp aria-hidden="true" className="mx-auto mb-1 text-stone-400" size={18} />
          没有进行中的工作；从患者工作区发起跟师笔记或医案整理后，会在这里继续。
        </p>
      ) : null}
    </section>
  );
}

export function DashboardView() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");

  // 统计与最近病例和“继续工作”相互独立：dashboard 接口失败只降级本区域并
  // 给出重试，不再让整页提前返回、把本机恢复入口一起藏掉。
  const fetchDashboard = useCallback(
    () =>
      apiFetch<Dashboard>("/api/v1/dashboard")
        .then(setData)
        .catch((reason: Error) => setError(reason.message)),
    [],
  );

  // 挂载时状态本就是空，直接请求；重试按钮才需要先回到加载态。
  useEffect(() => {
    void fetchDashboard();
  }, [fetchDashboard]);

  function retryDashboard() {
    setData(null);
    setError("");
    void fetchDashboard();
  }

  return (
    <div>
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          {data ? <p className="text-sm font-medium text-[#1f6857]">从 {data.week_start} 开始的本周</p> : null}
          <h1 className="mt-1 text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl">我的师承工作台</h1>
          <p className="mt-2 text-sm text-stone-500">查看本周导入终态，再回到患者时间线与师承写作。</p>
        </div>
        <Link
          href="/imports"
          className="focus-ring inline-flex items-center justify-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white"
        >
          导入本周病历 <ArrowRight aria-hidden="true" size={16} />
        </Link>
      </div>

      <ContinueWriting />

      {error ? (
        <div role="alert" className="surface mt-7 rounded-2xl p-5">
          <p className="text-sm font-semibold text-red-700">本周概况与最近病例读取失败</p>
          <p className="mt-1 break-all text-sm text-red-700">{error}</p>
          <p className="mt-1 text-xs text-stone-500">上方“继续工作”与本机记录不受影响。</p>
          <button type="button" onClick={retryDashboard} className="focus-ring mt-2 rounded-xl border border-red-300 bg-white px-3 py-1.5 text-xs text-red-700">重试加载</button>
        </div>
      ) : !data ? (
        <div className="mt-7 h-64 animate-pulse rounded-2xl bg-white/60" aria-label="正在加载工作台" />
      ) : (
        <>
      <section className="surface mt-7 flex flex-wrap items-center gap-x-7 gap-y-2 rounded-2xl px-5 py-4" aria-label="本周概况">
        {countCards.map(({ key, label, icon: Icon }) => (
          <span key={key} className="flex items-center gap-2 text-sm text-stone-500">
            <Icon aria-hidden="true" className="text-[#1f6857]" size={15} />
            <span className={`font-bold tabular-nums ${key === "unresolved" && data.counts.unresolved > 0 ? "text-red-700" : "text-stone-900"}`}>{data.counts[key]}</span>
            {label}
          </span>
        ))}
      </section>

      <div className="mt-7 grid gap-6 xl:grid-cols-[1.05fr_.95fr]">
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-base font-bold text-stone-900">最近病例</h2>
            <Link href="/patients" className="text-sm font-medium text-[#1f6857] hover:underline">查看全部</Link>
          </div>
          {data.recent_encounters.length ? (
            <div className="surface divide-y divide-stone-200 rounded-2xl">
              {data.recent_encounters.map((encounter) => (
                <Link
                  href={recentEncounterHref(encounter.patient_id, encounter.id)}
                  key={encounter.id}
                  className="focus-ring flex items-center justify-between gap-4 px-5 py-4 hover:bg-[#faf8f2]"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-stone-800">
                      {encounter.chief_complaint || "主诉未记录"}
                    </p>
                    <p className="mt-1 text-xs text-stone-500">{new Date(encounter.encounter_at).toLocaleDateString("zh-CN")}</p>
                  </div>
                  <ArrowRight aria-hidden="true" className="shrink-0 text-stone-400" size={16} />
                </Link>
              ))}
            </div>
          ) : (
            <EmptyState title="还没有病例" detail="导入第一份门诊病历后，最近病例会出现在这里。" />
          )}
        </section>

        <section>
          <h2 className="mb-3 text-base font-bold text-stone-900">最近患者</h2>
          {data.recent_patients.length ? (
            <div className="surface mb-6 divide-y divide-stone-200 rounded-2xl">
              {data.recent_patients.map((patient) => (
                <Link
                  href={`/patients/${patient.id}`}
                  key={patient.id}
                  className="focus-ring flex items-center justify-between gap-4 px-5 py-3.5 hover:bg-[#faf8f2]"
                >
                  <p className="truncate text-sm font-semibold text-stone-800">{patient.code}</p>
                  <p className="shrink-0 text-xs text-stone-500">
                    {patient.last_encounter_at ? `最近 ${new Date(patient.last_encounter_at).toLocaleDateString("zh-CN")}` : "暂无就诊"}
                  </p>
                </Link>
              ))}
            </div>
          ) : null}
        </section>
      </div>
        </>
      )}
    </div>
  );
}
