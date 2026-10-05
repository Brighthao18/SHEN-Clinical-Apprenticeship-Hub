"use client";

import { AlertTriangle, Check, ExternalLink, LoaderCircle, Plus } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { IngestionReasonDetails } from "@/components/ingestion-reason-details";
import { StatusPill } from "@/components/status-pill";
import { apiContentUrl, apiFetch } from "@/lib/api";
import { getIngestionReasonExplanation } from "@/lib/ingestion-reasons";

type Review = {
  id: string;
  document_id: string;
  encounter_id: string | null;
  prescription_item_id: string | null;
  issue_code: string;
  field_path: string;
  severity: string;
  reason_category: string | null;
  criticality: string | null;
  blocking: boolean;
  observed_value: Record<string, unknown>;
  status: string;
  resolution_note: string | null;
  row_version: number;
  created_at: string;
  hospital: string | null;
  encounter_date: string | null;
};

type DrugOption = {
  id: string;
  canonical_name: string;
  processing_method: string | null;
};

function SpecSupplementForm({ review }: { review: Review }) {
  const observedDrug =
    typeof review.observed_value.drug === "string" ? review.observed_value.drug : "";
  const observedUnit =
    typeof review.observed_value.original_package_unit === "string"
      ? review.observed_value.original_package_unit
      : "";
  const [query, setQuery] = useState(observedDrug);
  const [options, setOptions] = useState<DrugOption[]>([]);
  const [selected, setSelected] = useState<DrugOption | null>(null);
  const [hospital, setHospital] = useState(review.hospital ?? "");
  const [dose, setDose] = useState("");
  const [unit, setUnit] = useState(observedUnit === "袋" ? "袋" : "包");
  const [processing, setProcessing] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState(review.encounter_date ?? "");
  const [sourceNote, setSourceNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [searchError, setSearchError] = useState("");

  useEffect(() => {
    const handle = setTimeout(() => {
      apiFetch<DrugOption[]>(
        `/api/v1/drug-dictionary?status=verified&q=${encodeURIComponent(query.trim())}`,
      )
        .then((items) => {
          setOptions(items);
          setSearchError("");
        })
        .catch((reason: Error) => {
          // 搜索失败不能伪装成"无结果"——那会被当成字典里没有该药。
          setOptions([]);
          setSearchError(reason.message);
        });
    }, 250);
    return () => clearTimeout(handle);
  }, [query]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    setSaving(true);
    setError("");
    try {
      await apiFetch("/api/v1/drug-dictionary/specifications", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          drug_dictionary_id: selected.id,
          hospital,
          package_dose_g: dose,
          original_package_unit: unit,
          processing_method: processing.trim() || null,
          effective_from: effectiveFrom || null,
          source_note: sourceNote.trim() || null,
        }),
      });
      setSaved(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存规格失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-4 rounded-xl border border-[#cfe3da] bg-[#eef6f2] p-4">
      <p className="text-sm font-semibold text-[#1f6857]">补充医院药物规格</p>
      <p className="mt-1 text-xs leading-5 text-stone-500">
        规格属于人工核对的一手参考数据（AI 不会代填）：请按药房标签/处方单填写每包克数。保存后立即以
        manual_verified 入库，后续导入与重新处理即可据此计算剂量；本条记录仍需在下方人工标记已核对。
      </p>

      {saved ? (
        <p className="mt-3 rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          规格已入库。确认无误后请在下方填写核对说明并「标记为已核对」。
        </p>
      ) : (
        <form onSubmit={submit} className="mt-3 space-y-3">
          <div>
            <label htmlFor={`drug-${review.id}`} className="text-xs font-semibold text-stone-600">
              药物（从已核验字典中选择）
            </label>
            <input
              id={`drug-${review.id}`}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setSelected(null);
              }}
              placeholder="输入药名搜索…"
              className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm"
            />
            {selected ? (
              <p className="mt-1 text-xs text-emerald-700">
                已选择：{selected.canonical_name}
                {selected.processing_method ? `（${selected.processing_method}）` : ""}
              </p>
            ) : null}
            {searchError ? (
              <p role="alert" className="mt-1 text-xs text-red-700">字典搜索失败：{searchError}</p>
            ) : null}
            {!selected && options.length ? (
              <div className="mt-1 max-h-40 space-y-1 overflow-y-auto rounded-xl border border-stone-200 bg-white p-1">
                {options.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    onClick={() => {
                      setSelected(option);
                      setQuery(option.canonical_name);
                    }}
                    className="focus-ring block w-full rounded-lg px-3 py-1.5 text-left text-sm text-stone-700 hover:bg-[#eef6f2]"
                  >
                    {option.canonical_name}
                    {option.processing_method ? `（${option.processing_method}）` : ""}
                  </button>
                ))}
              </div>
            ) : null}
            {!selected && !options.length && query.trim() ? (
              <p className="mt-1 text-xs text-stone-400">没有匹配的已核验字典条目。</p>
            ) : null}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs font-semibold text-stone-600">
              医院
              <input
                value={hospital}
                onChange={(event) => setHospital(event.target.value)}
                required
                className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
              />
            </label>
            <label className="text-xs font-semibold text-stone-600">
              每包克数（g）
              <input
                type="number"
                value={dose}
                onChange={(event) => setDose(event.target.value)}
                required
                min="0.001"
                step="0.1"
                placeholder="例如 10"
                className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
              />
            </label>
            <label className="text-xs font-semibold text-stone-600">
              包装单位
              <select
                value={unit}
                onChange={(event) => setUnit(event.target.value)}
                className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
              >
                <option value="包">包</option>
                <option value="袋">袋</option>
              </select>
            </label>
            <label className="text-xs font-semibold text-stone-600">
              炮制（可选）
              <input
                value={processing}
                onChange={(event) => setProcessing(event.target.value)}
                placeholder="例如 醋制；留空表示不区分"
                className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
              />
            </label>
            <label className="text-xs font-semibold text-stone-600">
              生效起始日（可选，默认就诊日）
              <input
                type="date"
                value={effectiveFrom}
                onChange={(event) => setEffectiveFrom(event.target.value)}
                className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
              />
            </label>
            <label className="text-xs font-semibold text-stone-600">
              来源备注（可选）
              <input
                value={sourceNote}
                onChange={(event) => setSourceNote(event.target.value)}
                placeholder="例如 药房标签 / 处方单打印件"
                className="focus-ring mt-1 w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
              />
            </label>
          </div>

          {error ? <p role="alert" className="rounded-xl bg-red-50 px-4 py-2.5 text-sm text-red-700">{error}</p> : null}

          <button
            type="submit"
            disabled={saving || !selected}
            className="focus-ring inline-flex items-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
          >
            {saving ? <LoaderCircle className="animate-spin" size={14} /> : <Plus aria-hidden="true" size={14} />}
            保存规格到药物字典
          </button>
        </form>
      )}
    </div>
  );
}

export function ReviewQueue() {
  const [reviews, setReviews] = useState<Review[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setReviews(await apiFetch<Review[]>("/api/v1/reviews?status=open"));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    apiFetch<Review[]>("/api/v1/reviews?status=open")
      .then((items) => {
        if (active) setReviews(items);
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function resolve(event: FormEvent<HTMLFormElement>, review: Review) {
    event.preventDefault();
    setBusy(review.id);
    const form = new FormData(event.currentTarget);
    try {
      await apiFetch(`/api/v1/reviews/${review.id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          note: form.get("note"),
          expected_row_version: review.row_version,
        }),
      });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存审核结果失败");
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <p className="text-sm font-medium text-[#1f6857]">旧 ReviewItem 兼容界面</p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl">兼容审查记录</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-stone-500">
            这里只呈现仍在沿用的字段记录，不包含当前导入流程的 UNRESOLVED 病例。未解决病例不会通过这里手工修改后导入；DrugDictionary PENDING 也属于独立的药名标准化流程。医院规格缺失（HOSPITAL_SPECIFICATION_UNKNOWN）的记录可直接在卡片内补充药物规格——规格由你按药房标签人工填写，以 manual_verified 入库，AI 不会代填。
          </p>
        </div>
        <span className="rounded-xl bg-[#fff1d7] px-4 py-2 text-sm font-semibold text-[#8a5318]">{reviews.length} 项兼容记录</span>
      </div>

      <div className="mt-7">
        {loading ? <div className="h-64 animate-pulse rounded-2xl bg-white/60" /> : null}
        {error ? <p role="alert" className="mb-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p> : null}
        {!loading && !reviews.length ? <EmptyState title="当前没有兼容审查记录" detail="UNRESOLVED 会在导入结果中显示并保持未入库，不会进入这里等待人工补写。" /> : null}
        <div className="space-y-4">
          {reviews.map((review) => (
            <article key={review.id} className="surface rounded-2xl p-5 sm:p-6">
              <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
                <div className="flex items-start gap-3">
                  <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-amber-50 text-amber-700"><AlertTriangle aria-hidden="true" size={18} /></span>
                  <div>
                    <h2 className="font-semibold text-stone-900">
                      {getIngestionReasonExplanation(
                        review.issue_code,
                        review.blocking ? "UNRESOLVED" : "ACCEPTED",
                      ).title}
                    </h2>
                    <p className="mt-1 font-mono text-xs text-stone-400">{review.field_path}</p>
                  </div>
                </div>
                <StatusPill status={review.status} />
              </div>

              <IngestionReasonDetails
                code={review.issue_code}
                status={review.blocking ? "UNRESOLVED" : "ACCEPTED"}
              />

              <div className="mt-4 rounded-xl border border-stone-200 bg-[#faf8f2] p-4">
                <p className="text-xs font-semibold text-stone-500">观察到的值</p>
                <dl className="mt-2 grid gap-2 sm:grid-cols-3">
                  {Object.entries(review.observed_value).map(([key, value]) => (
                    <div key={key}><dt className="text-[11px] text-stone-400">{key}</dt><dd className="mt-0.5 break-words text-sm text-stone-700">{String(value ?? "未记录")}</dd></div>
                  ))}
                </dl>
              </div>

              {review.issue_code === "HOSPITAL_SPECIFICATION_UNKNOWN" ? (
                <SpecSupplementForm review={review} />
              ) : null}

              <form onSubmit={(event) => resolve(event, review)} className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end">
                <div className="flex-1">
                  <label htmlFor={`note-${review.id}`} className="text-xs font-semibold text-stone-600">兼容核对说明</label>
                  <input id={`note-${review.id}`} name="note" required placeholder="例如：已核对，当前医院规格尚无可信来源，剂量继续保持空值" className="focus-ring mt-1.5 w-full rounded-xl border border-stone-300 bg-white px-3 py-2.5 text-sm" />
                </div>
                <button type="submit" disabled={busy === review.id} className="focus-ring inline-flex shrink-0 items-center justify-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60">
                  {busy === review.id ? <LoaderCircle className="animate-spin" size={16} /> : <Check aria-hidden="true" size={16} />}标记为已核对
                </button>
              </form>
              <a href={apiContentUrl(`/api/v1/documents/${review.document_id}/content`)} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-[#1f6857] hover:underline">查看原始资料<ExternalLink size={12} /></a>
            </article>
          ))}
        </div>
      </div>
    </div>
  );
}
