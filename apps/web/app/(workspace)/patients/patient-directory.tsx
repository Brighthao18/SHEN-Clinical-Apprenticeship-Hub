"use client";

import { ArrowRight, Search, UserRound } from "lucide-react";
import Link from "next/link";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { apiFetch } from "@/lib/api";

type Patient = {
  id: string;
  internal_patient_code: string;
  display_name: string | null;
  sex: string;
  estimated_birth_year: number | null;
  last_encounter_at: string | null;
  encounter_count: number;
};

const PAGE_SIZE = 50;

export function PatientDirectory() {
  const [patients, setPatients] = useState<Patient[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(async (search = "", nextPage = 0) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    setSubmittedQuery(search.trim());
    try {
      const data = await apiFetch<{ items: Patient[]; total: number }>(
        `/api/v1/patients?query=${encodeURIComponent(search.trim())}&limit=${PAGE_SIZE}&offset=${nextPage * PAGE_SIZE}`,
        { signal: controller.signal },
      );
      if (!controller.signal.aborted) {
        setPatients(data.items);
        setTotal(data.total);
        setPage(nextPage);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "读取失败");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => {
      window.clearTimeout(timer);
      request.current?.abort();
    };
  }, [load]);

  function search(event: FormEvent) {
    event.preventDefault();
    load(query);
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const showingRange = total
    ? `第 ${page * PAGE_SIZE + 1}–${Math.min((page + 1) * PAGE_SIZE, total)} 位，共 ${total} 位患者`
    : "当前显示 0 位患者";

  return (
    <div>
      <p className="text-sm font-medium text-[#1f6857]">纵向临床身份</p>
      <h1 className="mt-1 text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl">患者</h1>
      <p className="mt-2 text-sm text-stone-500">以稳定患者代码和就诊时间线为中心，不把病例退化成文件夹。</p>

      <form onSubmit={search} className="mt-6 flex max-w-xl gap-2">
        <label htmlFor="patient-query" className="sr-only">按患者代码或姓名精确查找</label>
        <div className="relative flex-1">
          <Search aria-hidden="true" className="absolute left-3 top-3 text-stone-400" size={18} />
          <input
            id="patient-query"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="患者代码，或完整姓名精确查找"
            className="focus-ring w-full rounded-xl border border-stone-300 bg-white py-2.5 pl-10 pr-3 text-sm"
          />
        </div>
        <button type="submit" className="focus-ring rounded-xl bg-[#1f6857] px-4 text-sm font-semibold text-white">查找</button>
      </form>

      {submittedQuery ? <button type="button" onClick={() => { setQuery(""); void load(); }} className="focus-ring mt-3 rounded-lg px-2 py-1 text-sm text-[#1f6857]">清除搜索，查看全部患者</button> : null}
      <div className="mt-6" aria-busy={loading}>
        <p role="status" className="mb-3 text-sm text-stone-500">
          {loading ? "正在查找患者…" : error ? "查询未完成，请重试。" : `${showingRange}${submittedQuery ? "（搜索结果）" : ""}`}
        </p>
        {loading ? <div aria-hidden="true" className="h-56 animate-pulse rounded-2xl bg-white/60" /> : null}
        {error ? <div role="alert"><EmptyState title="患者列表暂时无法读取" detail={error} /><button type="button" onClick={() => void load(submittedQuery, page)} className="focus-ring mt-3 rounded-xl border border-stone-300 px-4 py-2 text-sm">重试查询</button></div> : null}
        {!loading && !error && !patients.length ? <EmptyState title={submittedQuery ? "没有匹配患者" : "尚无患者记录"} detail={submittedQuery ? "请核对完整姓名或患者代码，也可以清除搜索查看全部患者。" : "成功导入病历后，患者及就诊时间线会出现在这里。"} /> : null}
        {!loading && !error && !patients.length && !submittedQuery ? <Link href="/imports" className="focus-ring mt-3 inline-flex rounded-xl bg-[#1f6857] px-4 py-2 text-sm text-white">导入第一份病历</Link> : null}
        {!loading && !error && patients.length ? (
          <>
            <div className="surface divide-y divide-stone-200 rounded-2xl">
              {patients.map((patient) => (
                <Link key={patient.id} href={`/patients/${patient.id}`} className="focus-ring flex items-center gap-4 px-4 py-4 hover:bg-[#faf8f2] sm:px-5">
                  <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-[#dcebe5] text-[#1f6857]"><UserRound aria-hidden="true" size={19} /></span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-semibold text-stone-800">{patient.internal_patient_code}</p>
                      {patient.display_name ? <span className="text-sm text-stone-500">{patient.display_name}</span> : null}
                    </div>
                    <p className="mt-1 text-xs text-stone-500">
                      {patient.sex === "male" ? "男" : patient.sex === "female" ? "女" : "性别未记录"} · {patient.encounter_count} 次就诊
                      {patient.last_encounter_at ? ` · 最近 ${new Date(patient.last_encounter_at).toLocaleDateString("zh-CN")}` : ""}
                    </p>
                  </div>
                  <ArrowRight aria-hidden="true" className="shrink-0 text-stone-400" size={17} />
                </Link>
              ))}
            </div>
            {totalPages > 1 ? (
              <nav aria-label="患者分页" className="mt-4 flex items-center justify-between gap-3">
                <button
                  type="button"
                  disabled={page === 0 || loading}
                  onClick={() => void load(submittedQuery, page - 1)}
                  className="focus-ring rounded-xl border border-stone-300 bg-white px-4 py-2 text-sm text-stone-700 disabled:opacity-50"
                >
                  上一页
                </button>
                <p className="text-sm text-stone-500">第 {page + 1} / {totalPages} 页</p>
                <button
                  type="button"
                  disabled={page + 1 >= totalPages || loading}
                  onClick={() => void load(submittedQuery, page + 1)}
                  className="focus-ring rounded-xl border border-stone-300 bg-white px-4 py-2 text-sm text-stone-700 disabled:opacity-50"
                >
                  下一页
                </button>
              </nav>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}
