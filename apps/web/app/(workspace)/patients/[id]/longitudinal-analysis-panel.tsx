"use client";

import {
  AlertTriangle,
  Bot,
  BrainCircuit,
  ChevronDown,
  History,
  LoaderCircle,
  MessageCircleQuestion,
  Route,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { ProvenanceBadge } from "@/components/provenance-badge";
import { apiFetch } from "@/lib/api";

type EncounterOption = { id: string; encounter_at: string };

type EvidenceRef = {
  evidence_ref_id: string;
  claim: string;
  evidence_encounter_ids: string[];
  evidence_type: string;
  evidence_keys: string[];
};

type EvidenceStatement = { text: string; evidence_ref_ids: string[] };

type TrajectoryPoint = {
  encounter_id: string;
  date: string;
  state: string;
  description: string;
  evidence_ref_ids: string[];
};

type StructuredAnalysis = {
  course_summary: EvidenceStatement;
  symptom_trajectory: Array<{ symptom: string; points: TrajectoryPoint[] }>;
  diagnosis_trajectory: Array<{ diagnosis: string; points: TrajectoryPoint[] }>;
  prescription_strategy_trajectory: Array<{
    previous_encounter_id: string;
    current_encounter_id: string;
    summary: string;
    deterministic_changes: string[];
    evidence_ref_ids: string[];
  }>;
  persistent_medications: Array<{
    medication: string;
    encounter_ids: string[];
    evidence_ref_ids: string[];
  }>;
  key_turning_points: Array<{
    previous_encounter_id: string;
    current_encounter_id: string;
    recorded_changes: string[];
    evidence_ref_ids: string[];
  }>;
  possible_clinical_reasoning: Array<{
    inference: string;
    evidence_ref_ids: string[];
    provenance: "ai_inferred";
    tentative: true;
  }>;
  uncertainty_and_missing_data: EvidenceStatement[];
  discussion_questions: Array<{ question: string; evidence_ref_ids: string[] }>;
  evidence_refs: EvidenceRef[];
};

type AnalysisVersion = {
  id: string;
  provider: string;
  model: string;
  created_at: string;
  source_encounters: string[];
  input_context_hash: string;
  deidentification_version: string;
  structured_output: StructuredAnalysis;
};

const stateLabels: Record<string, string> = {
  new: "新出现",
  persistent: "持续",
  improved: "改善",
  worsened: "加重",
  resolved: "缓解",
  recorded: "已记录",
  not_recorded: "未记录",
  unknown: "无法确定",
};

const sourceLabels: Record<string, string> = {
  physician_record: "医生记录",
  student_note: "学生补录",
  ai_extracted: "AI抽取",
  deterministic_prescription_diff: "确定性处方Diff",
  record_absence: "未见记录",
  mixed: "多来源",
};

function displayDate(value: string) {
  return new Date(value).toLocaleDateString("zh-CN");
}

function EvidenceLinks({
  refIds,
  refs,
  dates,
}: {
  refIds: string[];
  refs: Map<string, EvidenceRef>;
  dates: Map<string, string>;
}) {
  const evidence = refIds.map((id) => refs.get(id)).filter(Boolean) as EvidenceRef[];
  if (!evidence.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {evidence.map((item) => (
        <span
          key={item.evidence_ref_id}
          title={item.claim}
          className="rounded-md border border-stone-200 bg-white px-2 py-1 text-[11px] text-stone-500"
        >
          {sourceLabels[item.evidence_type] ?? item.evidence_type} ·{" "}
          {item.evidence_encounter_ids.map((id) => dates.get(id) ?? id.slice(0, 8)).join(" / ")}
        </span>
      ))}
    </div>
  );
}

export function LongitudinalAnalysisPanel({
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
  const dateById = useMemo(
    () => new Map(ordered.map((encounter) => [encounter.id, displayDate(encounter.encounter_at)])),
    [ordered],
  );
  const [startId, setStartId] = useState(ordered[0]?.id ?? "");
  const [endId, setEndId] = useState(ordered.at(-1)?.id ?? "");
  const [versions, setVersions] = useState<AnalysisVersion[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    apiFetch<{ items: AnalysisVersion[] }>(
      `/api/v1/patients/${patientId}/longitudinal-analyses`,
    )
      .then((payload) => {
        if (!active) return;
        setVersions(payload.items);
        setSelectedId(payload.items[0]?.id || "");
        setError("");
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
  }, [patientId]);

  async function generate() {
    setGenerating(true);
    setError("");
    try {
      const created = await apiFetch<AnalysisVersion>(
        `/api/v1/patients/${patientId}/longitudinal-analyses`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            start_encounter_id: startId,
            end_encounter_id: endId,
          }),
        },
      );
      setVersions((current) => [created, ...current]);
      setSelectedId(created.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成纵向分析失败");
    } finally {
      setGenerating(false);
    }
  }

  const selected = versions.find((version) => version.id === selectedId) ?? versions[0];
  const output = selected?.structured_output;
  const refs = useMemo(
    () => new Map((output?.evidence_refs ?? []).map((item) => [item.evidence_ref_id, item])),
    [output],
  );
  // 适用范围：旧分析不会自动覆盖后来的就诊；这里如实显示缺口。
  const notIncludedCount = selected
    ? ordered.filter((encounter) => !selected.source_encounters.includes(encounter.id)).length
    : 0;

  return (
    <section id="longitudinal-analysis" className="surface mt-7 scroll-mt-5 rounded-2xl p-5 sm:p-6">
      <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-start">
        <div className="max-w-2xl">
          <div className="flex items-center gap-2 text-[#1f6857]">
            <BrainCircuit aria-hidden="true" size={20} />
            <h2 className="font-bold text-stone-900">纵向分析</h2>
          </div>
          <p className="mt-2 text-sm leading-6 text-stone-500">
            数据库与 Phase 7 Diff 回答“发生了什么”；AI 只串联证据，并把推测单独标记。未记录不会被解释为正常或消失。
          </p>
        </div>
        {versions.length ? (
          <label className="text-xs font-medium text-stone-500">
            查看历史版本
            <select
              value={selected?.id ?? ""}
              onChange={(event) => setSelectedId(event.target.value)}
              className="focus-ring mt-1 block rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm text-stone-700"
            >
              {versions.map((version) => (
                <option key={version.id} value={version.id}>
                  {new Date(version.created_at).toLocaleString("zh-CN")} · {version.model}
                </option>
              ))}
            </select>
          </label>
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
          disabled={generating || ordered.length < 2}
          className="focus-ring inline-flex items-center justify-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
        >
          {generating ? <LoaderCircle aria-hidden="true" className="animate-spin" size={17} /> : <Sparkles aria-hidden="true" size={17} />}
          {versions.length ? "重新生成新版本" : "生成纵向分析"}
        </button>
      </div>

      {error ? (
        <p role="alert" className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error}
        </p>
      ) : null}
      {loading ? <div className="mt-5 h-36 animate-pulse rounded-xl bg-stone-100" /> : null}
      {!loading && !output ? (
        <div className="mt-5 rounded-xl border border-dashed border-stone-300 px-5 py-8 text-center">
          <Bot aria-hidden="true" className="mx-auto text-stone-400" size={24} />
          <p className="mt-2 text-sm font-semibold text-stone-700">尚无纵向分析版本</p>
          <p className="mt-1 text-xs text-stone-500">默认分析全部就诊，也可选择连续的开始与结束就诊。</p>
        </div>
      ) : null}

      {output && selected ? (
        <div className="mt-6 space-y-6">
          <div className="flex flex-wrap items-center gap-2 text-[11px] text-stone-500">
            <ProvenanceBadge source="ai_inferred" />
            <span>{selected.provider} / {selected.model}</span>
            <span>·</span>
            <span>{selected.source_encounters.length} 次就诊</span>
            <span>·</span>
            <span>上下文 {selected.input_context_hash.slice(0, 10)}…</span>
          </div>

          {notIncludedCount > 0 ? (
            <p role="status" className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-900">
              适用范围提示：本分析基于所选 {selected.source_encounters.length} 次就诊；该患者还有 {notIncludedCount} 次就诊未包含。旧结果不会自动更新，需要纳入时请调整上方范围重新生成（追加新版本，不覆盖本分析）。
            </p>
          ) : null}

          <section className="rounded-2xl border border-[#bcd8ce] bg-[#f4faf7] p-5">
            <div className="flex items-center gap-2">
              <ShieldCheck aria-hidden="true" className="text-[#1f6857]" size={19} />
              <h3 className="font-bold text-stone-900">病程事实</h3>
            </div>
            <p className="mt-3 text-sm leading-7 text-stone-800">{output.course_summary.text}</p>
            <EvidenceLinks refIds={output.course_summary.evidence_ref_ids} refs={refs} dates={dateById} />

            {output.key_turning_points.length ? (
              <div className="mt-5">
                <h4 className="text-xs font-bold tracking-wide text-stone-500">关键变化</h4>
                <ol className="mt-3 space-y-3">
                  {output.key_turning_points.map((point, index) => (
                    <li key={`${point.previous_encounter_id}-${point.current_encounter_id}`} className="rounded-xl bg-white p-4 text-sm">
                      <p className="font-semibold text-stone-800">
                        {index + 1}. {dateById.get(point.previous_encounter_id)} → {dateById.get(point.current_encounter_id)}
                      </p>
                      <ul className="mt-2 space-y-1 text-stone-700">
                        {point.recorded_changes.map((change) => <li key={change}>· {change}</li>)}
                      </ul>
                      <EvidenceLinks refIds={point.evidence_ref_ids} refs={refs} dates={dateById} />
                    </li>
                  ))}
                </ol>
              </div>
            ) : null}
          </section>

          <div className="grid gap-5 lg:grid-cols-2">
            <section className="rounded-2xl border border-stone-200 p-5">
              <div className="flex items-center gap-2"><Route aria-hidden="true" size={18} className="text-[#1f6857]" /><h3 className="font-bold text-stone-900">症状轨迹</h3></div>
              <div className="mt-4 space-y-4">
                {output.symptom_trajectory.map((trajectory) => (
                  <div key={trajectory.symptom}>
                    <h4 className="text-sm font-semibold text-stone-800">{trajectory.symptom}</h4>
                    <div className="mt-2 space-y-2 border-l border-stone-200 pl-3">
                      {trajectory.points.map((point) => (
                        <div key={`${trajectory.symptom}-${point.encounter_id}`} className="text-sm">
                          <p><span className="font-medium text-stone-700">{displayDate(point.date)}</span><span className="ml-2 rounded bg-stone-100 px-1.5 py-0.5 text-[11px] text-stone-500">{stateLabels[point.state] ?? point.state}</span></p>
                          <p className="mt-1 leading-6 text-stone-600">{point.description}</p>
                          <EvidenceLinks refIds={point.evidence_ref_ids} refs={refs} dates={dateById} />
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
                {!output.symptom_trajectory.length ? <p className="text-sm text-stone-500">现有记录不足以形成症状轨迹。</p> : null}
              </div>
            </section>

            <section className="rounded-2xl border border-stone-200 p-5">
              <div className="flex items-center gap-2"><History aria-hidden="true" size={18} className="text-[#1f6857]" /><h3 className="font-bold text-stone-900">诊断 / 证候轨迹</h3></div>
              <div className="mt-4 space-y-4">
                {output.diagnosis_trajectory.map((trajectory) => (
                  <div key={trajectory.diagnosis}>
                    <h4 className="text-sm font-semibold text-stone-800">{trajectory.diagnosis}</h4>
                    <div className="mt-2 space-y-2 border-l border-stone-200 pl-3">
                      {trajectory.points.map((point) => (
                        <div key={`${trajectory.diagnosis}-${point.encounter_id}`} className="text-sm">
                          <p><span className="font-medium text-stone-700">{displayDate(point.date)}</span><span className="ml-2 rounded bg-stone-100 px-1.5 py-0.5 text-[11px] text-stone-500">{stateLabels[point.state] ?? point.state}</span></p>
                          <p className="mt-1 leading-6 text-stone-600">{point.description}</p>
                          <EvidenceLinks refIds={point.evidence_ref_ids} refs={refs} dates={dateById} />
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
                {!output.diagnosis_trajectory.length ? <p className="text-sm text-stone-500">现有记录不足以形成诊断或证候轨迹。</p> : null}
              </div>
            </section>

            <section className="rounded-2xl border border-stone-200 p-5 lg:col-span-2">
              <div className="flex items-center gap-2"><History aria-hidden="true" size={18} className="text-[#1f6857]" /><h3 className="font-bold text-stone-900">处方调整轨迹</h3></div>
              <div className="mt-4 space-y-3">
                {output.prescription_strategy_trajectory.map((item) => (
                  <article key={`${item.previous_encounter_id}-${item.current_encounter_id}`} className="rounded-xl bg-stone-50 p-4">
                    <p className="text-xs font-semibold text-stone-500">{dateById.get(item.previous_encounter_id)} → {dateById.get(item.current_encounter_id)}</p>
                    <p className="mt-2 text-sm leading-6 text-stone-700">{item.summary}</p>
                    <div className="mt-2 flex flex-wrap gap-1.5">{item.deterministic_changes.map((change) => <span key={change} className="rounded-md bg-white px-2 py-1 text-[11px] text-stone-600">{change}</span>)}</div>
                    <EvidenceLinks refIds={item.evidence_ref_ids} refs={refs} dates={dateById} />
                  </article>
                ))}
              </div>
              {output.persistent_medications.length ? (
                <div className="mt-4 border-t border-stone-200 pt-4">
                  <h4 className="text-xs font-bold text-stone-500">持续保留的药物</h4>
                  <div className="mt-2 flex flex-wrap gap-2">{output.persistent_medications.map((item) => <span key={item.medication} className="rounded-xl border border-[#bcd8ce] bg-[#f4faf7] px-3 py-2 text-sm text-[#175849]">{item.medication}</span>)}</div>
                </div>
              ) : null}
            </section>
          </div>

          <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5">
            <div className="flex flex-wrap items-center gap-2">
              <BrainCircuit aria-hidden="true" className="text-amber-700" size={19} />
              <h3 className="font-bold text-amber-950">AI 对治疗思路的理解</h3>
              <ProvenanceBadge source="ai_inferred" />
            </div>
            <div className="mt-4 space-y-3">
              {output.possible_clinical_reasoning.map((item, index) => (
                <div key={`${index}-${item.inference}`} className="rounded-xl border border-amber-200 bg-white/70 p-4">
                  <p className="text-sm leading-7 text-amber-950">{item.inference}</p>
                  <EvidenceLinks refIds={item.evidence_ref_ids} refs={refs} dates={dateById} />
                </div>
              ))}
              {!output.possible_clinical_reasoning.length ? <p className="text-sm text-amber-800">现有证据不足以提出可靠推测。</p> : null}
            </div>
          </section>

          <section className="rounded-2xl border border-blue-200 bg-blue-50 p-5">
            <div className="flex items-center gap-2"><MessageCircleQuestion aria-hidden="true" className="text-blue-700" size={19} /><h3 className="font-bold text-blue-950">值得跟老师讨论</h3></div>
            <ol className="mt-4 space-y-3">
              {output.discussion_questions.map((item, index) => (
                <li key={`${index}-${item.question}`} className="rounded-xl bg-white p-4 text-sm leading-6 text-blue-950">
                  <p><strong>{index + 1}.</strong> {item.question}</p>
                  <EvidenceLinks refIds={item.evidence_ref_ids} refs={refs} dates={dateById} />
                </li>
              ))}
            </ol>
          </section>

          {output.uncertainty_and_missing_data.length ? (
            <details className="rounded-2xl border border-stone-200 p-5">
              <summary className="focus-ring flex cursor-pointer list-none items-center gap-2 font-semibold text-stone-800">
                <AlertTriangle aria-hidden="true" size={18} className="text-amber-600" />不确定性与缺失记录<ChevronDown aria-hidden="true" size={16} />
              </summary>
              <div className="mt-4 space-y-3">
                {output.uncertainty_and_missing_data.map((item) => (
                  <div key={item.text} className="text-sm leading-6 text-stone-600">
                    <p>{item.text}</p><EvidenceLinks refIds={item.evidence_ref_ids} refs={refs} dates={dateById} />
                  </div>
                ))}
              </div>
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
