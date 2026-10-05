"use client";

import { BookLock, CheckCheck, CheckCircle2, LoaderCircle, Merge, ShieldOff } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

type Entry = {
  id: string;
  status: string;
  canonical_name: string;
  processing_method: string | null;
  candidate_raw_name: string | null;
  aliases: string[];
  hospital_display_names: string[];
  usage_count: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  representative_evidence: string | null;
  source_documents: number;
  /** 已通过该词条解析的处方条目数（影响停用/合并的范围）。 */
  linked_item_count: number;
  /** 同名待标准化、确认后可回填的处方条目数。 */
  pending_raw_item_count: number;
};

export function DrugDictionaryView() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<string | null>(null);
  const [editCanonical, setEditCanonical] = useState("");
  const [editProcessing, setEditProcessing] = useState("");
  const [mergeSource, setMergeSource] = useState("");
  const [mergeTarget, setMergeTarget] = useState("");

  const refresh = useCallback(async () => {
    setEntries(await apiFetch<Entry[]>("/api/v1/drug-dictionary"));
  }, []);

  useEffect(() => {
    apiFetch<Entry[]>("/api/v1/drug-dictionary")
      .then(setEntries)
      .catch((err) => setError((err as Error).message))
      .finally(() => setLoading(false));
  }, []);

  async function act(action: () => Promise<void>, done: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(done);
      await refresh();
      setSelected(new Set());
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function verify(entry: Entry, canonical: string, processing: string | null) {
    return act(async () => {
      await apiFetch(`/api/v1/drug-dictionary/${entry.id}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canonical_name: canonical, processing_method: processing }),
      });
    }, `已确认 ${canonical}，历史条目已自动回填。`);
  }

  function acceptSuggestion(entry: Entry) {
    return verify(entry, entry.canonical_name, entry.processing_method);
  }

  function batchAccept(pending: Entry[]) {
    const ids = selected.size ? [...selected] : pending.map((e) => e.id);
    return act(async () => {
      await apiFetch("/api/v1/drug-dictionary/batch-verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      });
    }, `已批量确认 ${ids.length} 个词条（按 AI 建议或当前标准名）。`);
  }

  function merge() {
    if (!mergeSource || !mergeTarget) return;
    // Merge rewrites identity mappings and cannot be undone from the UI.
    const confirmed = window.confirm("确认合并词条？来源词条将并入目标词条，此操作无法从界面撤销。");
    if (!confirmed) return;
    return act(async () => {
      await apiFetch(`/api/v1/drug-dictionary/${mergeSource}/merge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_id: mergeTarget }),
      });
    }, "已合并词条。");
  }

  function saveEdit(entry: Entry) {
    return verify(entry, editCanonical, editProcessing || null);
  }

  const pending = entries.filter((e) => e.status === "PENDING");
  const verified = entries.filter((e) => e.status === "VERIFIED");
  const disabled = entries.filter((e) => e.status === "DISABLED");

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="mx-auto w-full max-w-5xl">
      <header>
        <h1 className="text-xl font-bold text-stone-900">中药词典</h1>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-stone-500">
          老师的常用药集合相对稳定：一味新药只需确认一次，之后系统永久复用。确认前条目为
          PENDING，对应处方条目已随病例入库并保留原始药名、canonical 暂空，统计口径可排除。
          PENDING 是药名标准化状态，不是未进入临床数据库的 UNRESOLVED。
        </p>
      </header>

      {notice ? (
        <p className="mt-4 rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>
      ) : null}

      <section aria-labelledby="pending-title" className="surface mt-6 rounded-2xl p-5 sm:p-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="pending-title" className="flex items-center gap-2 text-base font-bold text-stone-900">
            <BookLock aria-hidden="true" size={18} />
            待标准化药名 <span className="text-sm font-normal text-stone-500">{pending.length}</span>
          </h2>
          {pending.length ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => batchAccept(pending)}
              className="focus-ring inline-flex items-center gap-2 rounded-xl bg-[#1f6857] px-3.5 py-2 text-sm font-semibold text-white disabled:opacity-60"
            >
              <CheckCheck aria-hidden="true" size={16} />
              {selected.size ? `批量确认标准名 ${selected.size}` : "采用全部候选标准名"}
            </button>
          ) : null}
        </div>

        {loading ? (
          <p className="mt-4 flex items-center gap-2 text-sm text-stone-500">
            <LoaderCircle aria-hidden="true" className="animate-spin" size={16} /> 加载中…
          </p>
        ) : !pending.length ? (
          <p className="mt-4 text-sm text-stone-500">没有待标准化药名。</p>
        ) : (
          <ul className="mt-4 space-y-3">
            {pending.map((entry) => (
              <li key={entry.id} className="rounded-xl border border-stone-200 p-4">
                <div className="flex flex-wrap items-center gap-3">
                  <input
                    type="checkbox"
                    checked={selected.has(entry.id)}
                    onChange={() => toggle(entry.id)}
                    className="size-4"
                    aria-label={`选择 ${entry.candidate_raw_name ?? ""}`}
                  />
                  <span className="text-sm font-semibold text-stone-900">
                    {entry.candidate_raw_name ?? entry.canonical_name}
                  </span>
                  <span className="text-xs text-stone-500">出现 {entry.usage_count} 次</span>
                  {entry.pending_raw_item_count > 0 ? (
                    <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-800">
                      确认后可回填 {entry.pending_raw_item_count} 条同名记录
                    </span>
                  ) : null}
                  {editing === entry.id ? (
                    <span className="ml-auto flex flex-wrap items-center gap-2">
                      <input
                        value={editCanonical}
                        onChange={(event) => setEditCanonical(event.target.value)}
                        placeholder="标准药名"
                        className="focus-ring rounded-lg border border-stone-300 px-2.5 py-1.5 text-sm"
                      />
                      <input
                        value={editProcessing}
                        onChange={(event) => setEditProcessing(event.target.value)}
                        placeholder="炮制（可空）"
                        className="focus-ring rounded-lg border border-stone-300 px-2.5 py-1.5 text-sm"
                      />
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => saveEdit(entry)}
                        className="focus-ring rounded-lg bg-[#1f6857] px-3 py-1.5 text-xs font-semibold text-white"
                      >
                        保存确认
                      </button>
                    </span>
                  ) : (
                    <span className="ml-auto flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => acceptSuggestion(entry)}
                        className="focus-ring inline-flex items-center gap-1.5 rounded-lg bg-[#1f6857] px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-60"
                      >
                        <CheckCircle2 aria-hidden="true" size={14} />
                        接受建议「{entry.canonical_name}
                        {entry.processing_method ? `/${entry.processing_method}` : ""}」
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setEditing(entry.id);
                          setEditCanonical(entry.canonical_name);
                          setEditProcessing(entry.processing_method ?? "");
                        }}
                        className="focus-ring rounded-lg border border-stone-300 px-3 py-1.5 text-xs font-semibold text-stone-700"
                      >
                        修改后确认
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          act(async () => {
                            await apiFetch(`/api/v1/drug-dictionary/${entry.id}/disable`, { method: "POST" });
                          }, "已停用（疑似误报，不进入词典）。")
                        }
                        className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-stone-300 px-3 py-1.5 text-xs font-semibold text-stone-600"
                      >
                        <ShieldOff aria-hidden="true" size={14} />
                        非药物/误报
                      </button>
                    </span>
                  )}
                </div>
                {entry.representative_evidence ? (
                  <p className="mt-2 truncate text-xs text-stone-500">
                    证据：{entry.representative_evidence}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="verified-title" className="surface mt-6 rounded-2xl p-5 sm:p-6">
        <h2 id="verified-title" className="text-base font-bold text-stone-900">
          已确认词条 <span className="text-sm font-normal text-stone-500">{verified.length}</span>
        </h2>
        {!verified.length ? (
          <p className="mt-4 text-sm text-stone-500">尚无已确认词条。</p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-xs uppercase tracking-wide text-stone-500">
                  <th className="py-2 pr-3">标准药名</th>
                  <th className="py-2 pr-3">炮制</th>
                  <th className="py-2 pr-3">原始名/别名</th>
                  <th className="py-2 pr-3">出现次数</th>
                  <th className="py-2 pr-3">关联处方条目</th>
                  <th className="py-2 pr-3">关联文档</th>
                  <th className="py-2">操作</th>
                </tr>
              </thead>
              <tbody>
                {verified.map((entry) => (
                  <tr key={entry.id} className="border-t border-stone-100">
                    <td className="py-2 pr-3 font-medium text-stone-800">{entry.canonical_name}</td>
                    <td className="py-2 pr-3 text-stone-600">{entry.processing_method ?? "—"}</td>
                    <td className="py-2 pr-3 text-xs text-stone-500">
                      {[entry.candidate_raw_name, ...entry.aliases].filter(Boolean).join("、") || "—"}
                    </td>
                    <td className="py-2 pr-3 tabular-nums text-stone-700">{entry.usage_count}</td>
                    <td className="py-2 pr-3 tabular-nums text-stone-700">
                      {entry.linked_item_count || <span className="text-stone-400">0</span>}
                    </td>
                    <td className="py-2 pr-3 tabular-nums text-stone-700">{entry.source_documents}</td>
                    <td className="py-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          if (!window.confirm(`确认停用「${entry.canonical_name || entry.candidate_raw_name}」？停用后该词条不再参与药物解析（可恢复）。`)) {
                            return;
                          }
                          void act(async () => {
                            await apiFetch(`/api/v1/drug-dictionary/${entry.id}/disable`, { method: "POST" });
                          }, "已停用。");
                        }}
                        className="focus-ring rounded-lg border border-stone-300 px-2.5 py-1 text-xs font-semibold text-stone-600"
                      >
                        停用
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <form
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            merge();
          }}
          className="mt-5 flex flex-wrap items-center gap-2 border-t border-stone-100 pt-4"
        >
          <Merge aria-hidden="true" size={16} className="text-stone-500" />
          <select
            value={mergeSource}
            onChange={(event) => setMergeSource(event.target.value)}
            className="focus-ring rounded-lg border border-stone-300 px-2.5 py-1.5 text-sm"
          >
            <option value="">合并来源词条…</option>
            {entries.map((e) => (
              <option key={e.id} value={e.id}>
                {e.canonical_name}
                {e.processing_method ? `/${e.processing_method}` : ""}（{e.status}）
              </option>
            ))}
          </select>
          <select
            value={mergeTarget}
            onChange={(event) => setMergeTarget(event.target.value)}
            className="focus-ring rounded-lg border border-stone-300 px-2.5 py-1.5 text-sm"
          >
            <option value="">合并到…</option>
            {verified.map((e) => (
              <option key={e.id} value={e.id}>
                {e.canonical_name}
                {e.processing_method ? `/${e.processing_method}` : ""}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={busy || !mergeSource || !mergeTarget}
            className="focus-ring rounded-lg border border-stone-300 px-3 py-1.5 text-xs font-semibold text-stone-700 disabled:opacity-50"
          >
            合并
          </button>
        </form>

        {disabled.length ? (
          <p className="mt-4 text-xs text-stone-500">
            已停用 {disabled.length} 条（可在需要时恢复）。
          </p>
        ) : null}
      </section>
    </div>
  );
}
