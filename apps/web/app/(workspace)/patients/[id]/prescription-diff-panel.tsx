"use client";

import {
  ArrowDown,
  ArrowRight,
  ArrowRightLeft,
  ArrowUp,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  GitCompareArrows,
  LoaderCircle,
  Minus,
  Plus,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type EncounterOption = {
  id: string;
  encounter_at: string;
  department: string | null;
};

export type DiffRequest = {
  previousId: string;
  currentId: string;
};

type DiffSummary = {
  total: number;
  added: number;
  removed: number;
  dose_increased: number;
  dose_decreased: number;
  processing_changed: number;
  unchanged: number;
  normalization_pending: number;
  unknown_dose: number;
};

type DiffItem = {
  primary_change:
    | "ADDED"
    | "REMOVED"
    | "DOSE_INCREASED"
    | "DOSE_DECREASED"
    | "PROCESSING_CHANGED"
    | "UNCHANGED";
  change_types: string[];
  identity_basis: "verified_canonical" | "raw_exact";
  canonical_identity: string | null;
  previous_raw_name: string | null;
  current_raw_name: string | null;
  previous_processing: string | null;
  current_processing: string | null;
  processing_changed: boolean;
  previous_dose_g: string | null;
  current_dose_g: string | null;
  signed_change_g: string | null;
  absolute_change_g: string | null;
  dose_change: "INCREASED" | "DECREASED" | "UNCHANGED" | "UNKNOWN" | "NOT_APPLICABLE";
  previous_dose_scope: string | null;
  current_dose_scope: string | null;
  previous_normalization_status: string | null;
  current_normalization_status: string | null;
  evidence: {
    previous_encounter_id: string | null;
    current_encounter_id: string | null;
    previous_document_id: string | null;
    current_document_id: string | null;
    previous_prescription_item_id: string | null;
    current_prescription_item_id: string | null;
  };
};

type DiffResponse = {
  algorithm_version: string;
  dose_basis: "per_decoction";
  previous_encounter: { id: string; encounter_at: string; document_id: string };
  current_encounter: { id: string; encounter_at: string; document_id: string };
  summary: DiffSummary;
  items: DiffItem[];
};

const groupMeta: Record<
  DiffItem["primary_change"],
  { label: string; tone: string; icon: typeof Plus }
> = {
  ADDED: { label: "新增", tone: "border-emerald-200 bg-emerald-50 text-emerald-800", icon: Plus },
  REMOVED: { label: "停用", tone: "border-rose-200 bg-rose-50 text-rose-800", icon: Minus },
  DOSE_INCREASED: { label: "加量", tone: "border-amber-200 bg-amber-50 text-amber-800", icon: ArrowUp },
  DOSE_DECREASED: { label: "减量", tone: "border-sky-200 bg-sky-50 text-sky-800", icon: ArrowDown },
  PROCESSING_CHANGED: { label: "炮制变化", tone: "border-violet-200 bg-violet-50 text-violet-800", icon: ArrowRightLeft },
  UNCHANGED: { label: "保持不变", tone: "border-stone-200 bg-stone-50 text-stone-700", icon: ArrowRight },
};

const groupOrder: DiffItem["primary_change"][] = [
  "ADDED",
  "REMOVED",
  "DOSE_INCREASED",
  "DOSE_DECREASED",
  "PROCESSING_CHANGED",
];

function formatDate(value: string): string {
  return new Date(value).toLocaleDateString("zh-CN");
}

function doseText(item: DiffItem, side: "previous" | "current"): string {
  const rawName = side === "previous" ? item.previous_raw_name : item.current_raw_name;
  const dose = side === "previous" ? item.previous_dose_g : item.current_dose_g;
  if (rawName === null) return "—";
  return dose === null ? "剂量未知" : `${dose} g`;
}

function DiffItemCard({ item }: { item: DiffItem }) {
  const meta = groupMeta[item.primary_change];
  const Icon = meta.icon;
  const label = item.canonical_identity ?? item.current_raw_name ?? item.previous_raw_name ?? "药名未记录";
  const rawChanged =
    item.previous_raw_name &&
    item.current_raw_name &&
    item.previous_raw_name !== item.current_raw_name;
  const pending = item.change_types.includes("NORMALIZATION_PENDING");

  return (
    <article className="rounded-xl border border-stone-200 bg-white p-4">
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs font-semibold ${meta.tone}`}>
              <Icon aria-hidden="true" size={13} />
              {meta.label}
            </span>
            <h4 className="font-bold text-stone-900">{label}</h4>
            {pending ? (
              <span className="rounded-full border border-orange-200 bg-orange-50 px-2 py-1 text-[11px] font-semibold text-orange-800">
                标准化待确认
              </span>
            ) : null}
            {item.dose_change === "UNKNOWN" ? (
              <span className="rounded-full border border-stone-200 bg-stone-50 px-2 py-1 text-[11px] font-semibold text-stone-600">
                剂量变化未知
              </span>
            ) : null}
          </div>
          {rawChanged ? (
            <p className="mt-2 text-xs text-stone-500">
              原药名：{item.previous_raw_name} <ArrowRight aria-hidden="true" className="inline" size={12} /> {item.current_raw_name}
            </p>
          ) : null}
        </div>
        <div className="grid shrink-0 grid-cols-[1fr_auto_1fr] items-center gap-2 rounded-lg bg-[#f7f4ec] px-3 py-2 text-sm tabular-nums text-stone-700">
          <span className="text-right">{doseText(item, "previous")}</span>
          <ArrowRight aria-label="变为" size={14} />
          <span className="font-semibold text-stone-900">{doseText(item, "current")}</span>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-stone-500">
        {item.signed_change_g && item.signed_change_g !== "0" ? (
          <span>
            每剂变化：<strong className="text-stone-700">{Number(item.signed_change_g) > 0 ? "+" : ""}{item.signed_change_g} g</strong>
          </span>
        ) : null}
        {item.processing_changed ? (
          <span>
            炮制：<strong className="text-stone-700">{item.previous_processing ?? "未注明"} → {item.current_processing ?? "未注明"}</strong>
          </span>
        ) : null}
        <span>匹配依据：{item.identity_basis === "verified_canonical" ? "已核验标准药名" : "原药名精确一致"}</span>
      </div>
    </article>
  );
}

export function PrescriptionDiffPanel({
  encounters,
  requestedPair,
}: {
  encounters: EncounterOption[];
  requestedPair: DiffRequest | null;
}) {
  const [previousId, setPreviousId] = useState(encounters[1]?.id ?? "");
  const [currentId, setCurrentId] = useState(encounters[0]?.id ?? "");
  const [result, setResult] = useState<DiffResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showUnchanged, setShowUnchanged] = useState(false);

  const comparePair = useCallback(async (previous: string, current: string) => {
    if (!previous || !current || previous === current) {
      setError("请选择两次不同的就诊");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const payload = await apiFetch<DiffResponse>(
        `/api/v1/encounters/${current}/prescription-diff?compare_to_encounter_id=${previous}`,
      );
      setResult(payload);
      setPreviousId(payload.previous_encounter.id);
      setCurrentId(payload.current_encounter.id);
      setShowUnchanged(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "处方对比失败");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!requestedPair) return;
    const timer = window.setTimeout(() => {
      void comparePair(requestedPair.previousId, requestedPair.currentId);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [comparePair, requestedPair]);

  const unchangedItems = result?.items.filter((item) => item.primary_change === "UNCHANGED") ?? [];

  return (
    <section id="prescription-diff" className="surface mt-7 scroll-mt-5 rounded-2xl p-5 sm:p-6">
      <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-start">
        <div>
          <div className="flex items-center gap-2 text-[#1f6857]">
            <GitCompareArrows aria-hidden="true" size={19} />
            <p className="text-sm font-semibold">Prescription Diff</p>
          </div>
          <h2 className="mt-1 text-xl font-bold text-stone-900">对比处方</h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-stone-500">
            事实变化由结构化处方确定性计算；默认只比较每剂剂量，不使用整个疗程总量。
          </p>
        </div>

        <div className="grid w-full gap-3 sm:grid-cols-[1fr_auto_1fr_auto] sm:items-end lg:max-w-3xl">
          <label className="text-xs font-semibold text-stone-600">
            前次就诊
            <select
              value={previousId}
              onChange={(event) => setPreviousId(event.target.value)}
              className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal text-stone-800"
            >
              {encounters.map((encounter) => (
                <option key={encounter.id} value={encounter.id}>
                  {formatDate(encounter.encounter_at)}{encounter.department ? ` · ${encounter.department}` : ""}
                </option>
              ))}
            </select>
          </label>
          <ArrowRight aria-hidden="true" className="mb-3 hidden text-stone-400 sm:block" size={18} />
          <label className="text-xs font-semibold text-stone-600">
            本次就诊
            <select
              value={currentId}
              onChange={(event) => setCurrentId(event.target.value)}
              className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal text-stone-800"
            >
              {encounters.map((encounter) => (
                <option key={encounter.id} value={encounter.id}>
                  {formatDate(encounter.encounter_at)}{encounter.department ? ` · ${encounter.department}` : ""}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => void comparePair(previousId, currentId)}
            disabled={busy || !previousId || !currentId || previousId === currentId}
            className="focus-ring inline-flex h-10 items-center justify-center gap-2 rounded-xl bg-[#1f6857] px-4 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? <LoaderCircle aria-hidden="true" className="animate-spin" size={16} /> : <GitCompareArrows aria-hidden="true" size={16} />}
            开始对比
          </button>
        </div>
      </div>

      {error ? (
        <div className="mt-5 flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800">
          <CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={16} />
          <span>{error}</span>
        </div>
      ) : null}

      {result ? (
        <div className="mt-6">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
            {(
              [
                ["added", "新增", "text-emerald-700"],
                ["removed", "停用", "text-rose-700"],
                ["dose_increased", "加量", "text-amber-700"],
                ["dose_decreased", "减量", "text-sky-700"],
                ["processing_changed", "炮制变化", "text-violet-700"],
                ["unchanged", "保持不变", "text-stone-600"],
              ] as const
            ).map(([key, label, tone]) => (
              <div key={key} className="rounded-xl border border-stone-200 bg-[#faf8f2] px-3 py-3">
                <p className="text-xs text-stone-500">{label}</p>
                <p className={`mt-1 text-2xl font-bold tabular-nums ${tone}`}>{result.summary[key]}</p>
              </div>
            ))}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-stone-500">
            <span>{formatDate(result.previous_encounter.encounter_at)} → {formatDate(result.current_encounter.encounter_at)}</span>
            <span>每剂剂量</span>
            <span>算法版本 {result.algorithm_version}</span>
            {result.summary.unknown_dose ? <span>{result.summary.unknown_dose} 味剂量变化未知</span> : null}
          </div>

          {result.summary.normalization_pending ? (
            <div className="mt-4 flex items-start gap-2 rounded-xl border border-orange-200 bg-orange-50 p-3 text-xs leading-5 text-orange-900">
              <CircleAlert aria-hidden="true" className="mt-0.5 shrink-0" size={15} />
              <span>
                {result.summary.normalization_pending} 味药的 canonical 尚待确认；仅在前后原药名精确一致且一对一时进行匹配，未采用 AI 建议名。
              </span>
            </div>
          ) : null}

          <div className="mt-5 space-y-5">
            {groupOrder.map((group) => {
              const items = result.items.filter((item) => item.primary_change === group);
              if (!items.length) return null;
              return (
                <section key={group}>
                  <h3 className="mb-2 text-xs font-bold tracking-wide text-stone-500">{groupMeta[group].label}</h3>
                  <div className="space-y-2">{items.map((item) => <DiffItemCard key={item.evidence.current_prescription_item_id ?? item.evidence.previous_prescription_item_id} item={item} />)}</div>
                </section>
              );
            })}

            {unchangedItems.length ? (
              <section>
                <button
                  type="button"
                  onClick={() => setShowUnchanged((value) => !value)}
                  className="focus-ring flex w-full items-center justify-between rounded-xl border border-stone-200 bg-stone-50 px-4 py-3 text-left text-sm font-semibold text-stone-700"
                >
                  <span>{showUnchanged ? "收起" : "展开"} {unchangedItems.length} 味保持不变项</span>
                  {showUnchanged ? <ChevronUp aria-hidden="true" size={16} /> : <ChevronDown aria-hidden="true" size={16} />}
                </button>
                {showUnchanged ? <div className="mt-2 space-y-2">{unchangedItems.map((item) => <DiffItemCard key={item.evidence.current_prescription_item_id ?? item.evidence.previous_prescription_item_id} item={item} />)}</div> : null}
              </section>
            ) : null}

            {!result.items.length ? (
              <p className="rounded-xl border border-dashed border-stone-300 p-5 text-sm text-stone-500">两次就诊均无结构化处方项目。</p>
            ) : null}
          </div>
        </div>
      ) : (
        <div className="mt-6 rounded-xl border border-dashed border-stone-300 bg-[#faf8f2] p-6 text-center text-sm text-stone-500">
          选择两次就诊后开始对比。
        </div>
      )}
    </section>
  );
}
