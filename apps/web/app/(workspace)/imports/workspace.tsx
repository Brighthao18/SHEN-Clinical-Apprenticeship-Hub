"use client";

import {
  AlertTriangle,
  CheckCircle2,
  CircleX,
  FileImage,
  LoaderCircle,
  RotateCcw,
  ShieldAlert,
  Upload,
} from "lucide-react";
import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";

import { IngestionReasonDetails } from "@/components/ingestion-reason-details";
import { StatusPill } from "@/components/status-pill";
import { apiFetch } from "@/lib/api";
import { canRetryImportItem, isImportTerminalStatus } from "@/lib/import-status";
import { RequestGeneration } from "@/lib/request-generation";

type ImportItem = {
  id: string;
  original_filename: string;
  document_id: string;
  status: string;
  error_code: string | null;
  patient_id: string | null;
  encounter_id: string | null;
};

type ImportBatch = {
  id: string;
  label: string | null;
  status: string;
  total_items: number;
  accepted_items: number;
  review_items: number;
  failed_items: number;
  unresolved_items: number;
  items: ImportItem[];
};

type ImportBatchSummary = Omit<ImportBatch, "items"> & { created_at: string };

type Health = { ocr_provider: string };

const sampleText = `暨南大学附属第一医院
广州华侨医院
门（急）诊病历
姓名：张三
性别：男
年龄：70岁
日期：2026-08-14
病历号：JNU-000128
科室：中医科门诊
主诉：腹痛，夜尿多，睡眠有改善，头晕
现病史：
既往史：胰十二指肠切除术
诊断：胰腺术后（主诊断）
处方：
（基）醋鳖甲（10g）2包
白花蛇舌草（15g）2包
（基）枳壳（麸炒）（10g）1包
红景天2袋
共7剂
每日1剂
医师：沈老师`;

export function ImportWorkspace() {
  const inputRef = useRef<HTMLInputElement>(null);
  const requests = useRef(new RequestGeneration());
  const selectedBatch = useRef<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [provider, setProvider] = useState("mock");
  const [ocrText, setOcrText] = useState(sampleText);
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [retryingId, setRetryingId] = useState("");
  const [retryNotice, setRetryNotice] = useState<{
    id: string;
    ok: boolean;
    text: string;
    refreshable?: boolean;
  } | null>(null);
  // Items whose retry POST already succeeded this session: the button stays
  // off for them so a stale FAILED row can never trigger a double submission.
  const [retrySubmitted, setRetrySubmitted] = useState<Set<string>>(new Set());
  const [recentBatches, setRecentBatches] = useState<ImportBatchSummary[]>([]);
  const batchId = batch?.id;
  const batchStatus = batch?.status;

  // The workspace keeps only the current batch in memory; the address bar
  // (?batch=) plus the recent-batches list make a refresh or re-entry
  // recoverable.
  function updateBatchUrl(id: string | null) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("batch", id);
    else url.searchParams.delete("batch");
    window.history.replaceState(window.history.state, "", url);
  }

  async function loadBatch(id: string) {
    const generation = requests.current.next();
    selectedBatch.current = id;
    try {
      const next = await apiFetch<ImportBatch>(`/api/v1/imports/${id}`);
      if (!requests.current.accepts(generation)) return null;
      setBatch(next);
      setError("");
      updateBatchUrl(id);
      return next;
    } catch (reason) {
      if (!requests.current.accepts(generation)) return null;
      throw reason;
    }
  }

  async function refreshBatch() {
    if (!batchId) return false;
    try {
      const next = await loadBatch(batchId);
      if (!next) return false;
      // This is a fresh read after the successful POST. A second failure is
      // retryable again; only an unread/stale result keeps the button locked.
      setRetrySubmitted((current) => {
        const remaining = new Set(current);
        next.items.forEach((item) => remaining.delete(item.id));
        return remaining;
      });
      return true;
    } catch {
      return false;
    }
  }

  useEffect(() => {
    const requestGeneration = requests.current;
    apiFetch<Health>("/health").then((health) => setProvider(health.ocr_provider)).catch(() => undefined);
    const fromUrl = new URLSearchParams(window.location.search).get("batch");
    if (fromUrl) {
      const generation = requests.current.next();
      selectedBatch.current = fromUrl;
      apiFetch<ImportBatch>(`/api/v1/imports/${fromUrl}`)
        .then((next) => {
          if (!requests.current.accepts(generation)) return;
          setBatch(next);
          updateBatchUrl(fromUrl);
        })
        .catch(() => {
          if (requests.current.accepts(generation)) {
            setError("无法恢复地址中的导入批次，可从“近期批次”重新选择。");
          }
        });
    }
    apiFetch<ImportBatchSummary[]>("/api/v1/imports?limit=5")
      .then((items) => setRecentBatches(items))
      .catch(() => undefined);
    return () => { requestGeneration.next(); };
  }, []);

  useEffect(() => {
    if (!batchId || !batchStatus || isImportTerminalStatus(batchStatus)) return;
    let consecutiveFailures = 0;
    let active = true;
    let pending = false;
    const timer = window.setInterval(() => {
      if (pending || selectedBatch.current !== batchId) return;
      pending = true;
      const generation = requests.current.current();
      apiFetch<ImportBatch>(`/api/v1/imports/${batchId}`)
        .then((next) => {
          if (!active || !requests.current.accepts(generation)) return;
          consecutiveFailures = 0;
          setBatch(next);
        })
        .catch(() => {
          if (!active || !requests.current.accepts(generation)) return;
          // Transient poll failures stay silent; a persistent failure must
          // not freeze the board with a stale "processing" state.
          consecutiveFailures += 1;
          if (consecutiveFailures >= 5) {
            window.clearInterval(timer);
            setError("处理进度查询失败，请刷新页面查看最新状态。");
          }
        })
        .finally(() => { pending = false; });
    }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [batchId, batchStatus]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!files.length) {
      setError("请先选择至少一张病历图片");
      return;
    }
    if (provider === "mock" && files.length !== 1) {
      setError("Mock OCR 一次只接收一份文件；切换 MinerU 后可批量上传。");
      return;
    }
    setError("");
    setSubmitting(true);
    const generation = requests.current.next();
    selectedBatch.current = null;
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    form.set("label", "本周导入");
    form.set("document_type", "outpatient_record");
    if (provider === "mock") form.set("mock_ocr_text", ocrText);
    try {
      const result = await apiFetch<ImportBatch>("/api/v1/imports", { method: "POST", body: form });
      if (requests.current.accepts(generation)) {
        selectedBatch.current = result.id;
        setBatch(result);
        updateBatchUrl(result.id);
      }
      apiFetch<ImportBatchSummary[]>("/api/v1/imports?limit=5")
        .then((items) => setRecentBatches(items))
        .catch(() => undefined);
    } catch (reason) {
      if (requests.current.accepts(generation)) {
        selectedBatch.current = batchId ?? null;
        setError(reason instanceof Error ? reason.message : "导入失败");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function retryItem(item: ImportItem) {
    if (retryingId || selectedBatch.current !== batchId) return;
    const generation = requests.current.next();
    selectedBatch.current = null;
    setRetryingId(item.id);
    setRetryNotice(null);
    try {
      await apiFetch<{ document_id: string; status: string }>(
        `/api/v1/documents/${item.document_id}/retry`,
        { method: "POST" },
      );
      if (!requests.current.accepts(generation)) return;
      selectedBatch.current = batchId ?? null;
      // The retry itself succeeded; only the progress refresh below may fail.
      // These outcomes are reported separately so a refresh failure never
      // reads as "retry failed" and lures a duplicate submission.
      setRetrySubmitted((current) => new Set(current).add(item.id));
      const refreshed = await refreshBatch();
      setRetryNotice(
        refreshed
          ? null
          : { id: item.id, ok: true, text: "已提交重试，进度暂不可读。", refreshable: true },
      );
    } catch (reason) {
      if (!requests.current.accepts(generation)) return;
      selectedBatch.current = batchId ?? null;
      setRetryNotice({
        id: item.id,
        ok: false,
        text: reason instanceof Error ? reason.message : "重试失败，请稍后再试。",
      });
    } finally {
      setRetryingId("");
    }
  }

  async function refreshAfterRetry() {
    const refreshed = await refreshBatch();
    if (refreshed) {
      setRetryNotice(null);
    } else {
      setError("进度查询失败，请稍后点击“重新查询”。");
    }
  }

  return (
    <div>
      <div>
        <p className="text-sm font-medium text-[#1f6857]">批量入口 · 异常才审核</p>
        <h1 className="mt-1 text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl">本周导入</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-stone-500">
          原图按内容哈希保存；OCR、解析与标准化不会覆盖原始证据。完全相同的文件再次上传不会创建重复就诊。
        </p>
      </div>

      <div className="mt-7 grid gap-6 xl:grid-cols-[.9fr_1.1fr]">
        <form onSubmit={submit} className="surface rounded-2xl p-5 sm:p-6">
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="focus-ring flex min-h-52 w-full flex-col items-center justify-center rounded-2xl border-2 border-dashed border-stone-300 bg-[#faf8f2] px-6 text-center hover:border-[#6d9b8d]"
          >
            <span className="grid size-12 place-items-center rounded-2xl bg-[#dcebe5] text-[#1f6857]">
              <Upload aria-hidden="true" size={23} />
            </span>
            <span className="mt-4 text-sm font-semibold text-stone-800">选择扫描图片或 PDF</span>
            <span className="mt-1 text-xs leading-5 text-stone-500">
              {provider === "mock" ? "Mock 模式：一次一份" : "MinerU 模式：可选择多份"}
            </span>
          </button>
          <input
            ref={inputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/bmp,application/pdf"
            multiple={provider === "mineru"}
            className="sr-only"
            onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
          />

          {files.length ? (
            <div className="mt-4 space-y-2">
              {files.map((file) => (
                <div key={`${file.name}-${file.size}`} className="flex items-center gap-3 rounded-xl border border-stone-200 px-3 py-2.5">
                  <FileImage aria-hidden="true" className="text-stone-500" size={18} />
                  <span className="min-w-0 flex-1 truncate text-sm text-stone-700">{file.name}</span>
                  <span className="text-xs tabular-nums text-stone-400">{(file.size / 1024 / 1024).toFixed(1)} MB</span>
                </div>
              ))}
            </div>
          ) : null}

          {provider === "mock" ? (
            <div className="mt-5">
              <label htmlFor="mock-text" className="text-sm font-semibold text-stone-700">Mock OCR 明文</label>
              <p className="mt-1 text-xs leading-5 text-stone-500">开发/回归测试专用。系统只使用你明确提供的文字，不读取图片内容。</p>
              {ocrText === sampleText ? (
                <p role="alert" className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">
                  注意：当前仍是内置示例文本（张三）。此时上传的图片不会参与解析——入库的是这份示例病历。正式使用请切换 MinerU 或清空改写此文本。
                </p>
              ) : null}
              <textarea
                id="mock-text"
                value={ocrText}
                onChange={(event) => setOcrText(event.target.value)}
                rows={12}
                className="focus-ring mt-2 w-full rounded-xl border border-stone-300 bg-white p-3 font-mono text-xs leading-5"
              />
            </div>
          ) : (
            <div className="mt-5 flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
              <ShieldAlert aria-hidden="true" className="mt-0.5 shrink-0" size={18} />
              <p className="leading-6">MinerU 将收到未经本系统预脱敏的原始图片。请先确认权限、账号区域和保留条款。</p>
            </div>
          )}

          {error ? <p role="alert" className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p> : null}
          <button
            type="submit"
            disabled={submitting}
            className="focus-ring mt-5 inline-flex w-full items-center justify-center gap-2 rounded-xl bg-[#1f6857] px-4 py-3 text-sm font-semibold text-white disabled:opacity-60"
          >
            {submitting ? <LoaderCircle aria-hidden="true" className="animate-spin" size={17} /> : <Upload aria-hidden="true" size={17} />}
            {submitting ? "正在建立导入批次…" : "开始自动处理"}
          </button>
        </form>

        <section aria-labelledby="batch-title">
          <div className="mb-3 flex items-center justify-between">
            <h2 id="batch-title" className="text-base font-bold text-stone-900">处理结果</h2>
            {batch ? <StatusPill status={batch.status} /> : null}
          </div>
          {recentBatches.length ? (
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-stone-500">近期批次：</span>
              {recentBatches.map((recentBatch) => (
                <button
                  key={recentBatch.id}
                  type="button"
                  onClick={() => loadBatch(recentBatch.id).catch(() => setError("无法读取该批次，请稍后再试。"))}
                  className={`focus-ring inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium ${batch?.id === recentBatch.id ? "border-[#1f6857] bg-[#eef7f3] text-[#175849]" : "border-stone-200 bg-white text-stone-600 hover:border-[#9bc5b8]"}`}
                >
                  {recentBatch.label || new Date(recentBatch.created_at).toLocaleString("zh-CN")}
                  <StatusPill status={recentBatch.status} />
                </button>
              ))}
            </div>
          ) : null}
          {!batch ? (
            <div className="surface flex min-h-80 flex-col items-center justify-center rounded-2xl px-6 text-center">
              <FileImage aria-hidden="true" className="text-stone-300" size={32} />
              <p className="mt-3 text-sm font-semibold text-stone-700">等待本周第一批病历</p>
              <p className="mt-1 max-w-sm text-xs leading-5 text-stone-500">完成后这里只显示状态与异常，不要求逐字段确认每一张病历。</p>
            </div>
          ) : (
            <div className="surface overflow-hidden rounded-2xl">
              <div className="grid grid-cols-2 gap-3 border-b border-stone-200 bg-[#faf8f2] px-5 py-4 text-center sm:grid-cols-4">
                <div><p className="text-xl font-bold">{batch.total_items}</p><p className="text-xs text-stone-500">病历</p></div>
                <div><p className="text-xl font-bold text-emerald-700">{batch.accepted_items}</p><p className="text-xs text-stone-500">已导入</p></div>
                <div><p className="text-xl font-bold text-slate-700">{batch.unresolved_items}</p><p className="text-xs text-stone-500">未解决</p></div>
                <div><p className="text-xl font-bold text-red-700">{batch.failed_items}</p><p className="text-xs text-stone-500">失败</p></div>
              </div>
              {isImportTerminalStatus(batch.status) ? (
                <p className="border-b border-stone-200 bg-white px-5 py-3 text-xs leading-5 text-stone-600">
                  本批处理已结束。已导入病例已写入临床数据库；未解决或失败病例没有进入临床数据库，也不会继续后台轮询。
                </p>
              ) : null}
              {batch.review_items ? (
                <p className="border-b border-amber-200 bg-amber-50 px-5 py-3 text-xs leading-5 text-amber-900">
                  另有 {batch.review_items} 项来自旧版兼容审查队列；它们不属于当前 UNRESOLVED 流程。
                </p>
              ) : null}
              <div className="divide-y divide-stone-200">
                {batch.items.map((item) => (
                  <article key={item.id} className="px-5 py-4">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-semibold text-stone-800">{item.original_filename}</p>
                        <div className="mt-2"><StatusPill status={item.status} /></div>
                      </div>
                      {item.status === "ACCEPTED" ? <CheckCircle2 aria-label="处理完成" className="text-emerald-600" size={20} /> : null}
                      {item.status === "REVIEW_REQUIRED" ? <AlertTriangle aria-label="需要确认" className="text-amber-600" size={20} /> : null}
                      {item.status === "UNRESOLVED" ? <ShieldAlert aria-label="未解决" className="text-slate-600" size={20} /> : null}
                      {item.status === "FAILED" ? <CircleX aria-label="处理失败" className="text-red-600" size={20} /> : null}
                    </div>
                    {item.status === "UNRESOLVED" || item.status === "FAILED" ? (
                      <IngestionReasonDetails code={item.error_code} status={item.status} />
                    ) : null}
                    <div className="mt-3 flex flex-wrap items-center gap-3 text-xs font-medium">
                      {item.patient_id ? <Link href={`/patients/${item.patient_id}`} className="text-[#1f6857] hover:underline">查看患者时间线</Link> : null}
                      {item.status === "REVIEW_REQUIRED" ? <Link href="/reviews" className="text-amber-700 hover:underline">处理异常字段</Link> : null}
                      {canRetryImportItem(item.status) && item.document_id ? (
                        <button
                          type="button"
                          onClick={() => retryItem(item)}
                          disabled={retryingId !== "" || retrySubmitted.has(item.id)}
                          className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-red-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-red-700 disabled:opacity-60"
                        >
                          {retryingId === item.id ? <LoaderCircle aria-hidden="true" className="animate-spin" size={13} /> : <RotateCcw aria-hidden="true" size={13} />}
                          {retrySubmitted.has(item.id) ? "已提交重试" : "重试处理"}
                        </button>
                      ) : null}
                      {retryNotice?.id === item.id ? (
                        <>
                          <span role="status" className={retryNotice.ok ? "text-emerald-700" : "text-red-700"}>
                            {retryNotice.text}
                          </span>
                          {retryNotice.refreshable ? (
                            <button
                              type="button"
                              onClick={refreshAfterRetry}
                              className="focus-ring rounded-lg border border-stone-300 bg-white px-2.5 py-1 text-xs font-semibold text-stone-700"
                            >
                              重新查询
                            </button>
                          ) : null}
                        </>
                      ) : null}
                    </div>
                  </article>
                ))}
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
