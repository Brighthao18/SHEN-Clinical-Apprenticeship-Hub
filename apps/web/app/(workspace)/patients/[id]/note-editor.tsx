"use client";

import {
  BookOpenText,
  ChevronDown,
  Download,
  FileSearch,
  Landmark,
  LoaderCircle,
  PencilLine,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Stamp,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import {
  SaveRequest,
  beginSaveSubmission,
  clearPendingSave,
  isDefinitiveSaveRejection,
  type PendingSave,
} from "@/lib/save-request";
import {
  PENDING_STATUS_VIEW,
  describePendingSave,
  pendingCheckSummary,
} from "@/lib/pending-save-checks";
import { usePendingSaveChannel } from "@/lib/use-pending-save-channel";
import {
  isNewestVersion,
  mergeNoteVersion,
} from "@/lib/note-version-merge";
import {
  canSaveNoteVersion,
  hasAiDraftForSourceContext,
  isNoteContentDirty,
} from "@/lib/note-interaction-guards";
import {
  clearNoteEditBuffer,
  listNoteEditBuffers,
  NOTE_EDIT_BUFFER_SOFT_LIMIT,
  writeNoteEditBuffer,
  type NoteEditBuffer,
} from "@/lib/note-edit-buffer";
import {
  describeMaterialsBreakdown,
  isPreviewForSelection,
  type EvidencePreviewData,
  type NoteEvidenceSnapshotSummary,
} from "@/lib/note-evidence-preview";
import { findOutOfScopeReferences } from "@/lib/note-evidence-scope";
import {
  diffLines,
  diffRegions,
  type DiffLine,
} from "@/lib/line-diff";

export type { EvidencePreviewData, NoteEvidenceSnapshotSummary };

export type NoteVersionData = {
  id: string;
  note_id: string;
  patient_id: string;
  note_type: string;
  version: number;
  version_type: string;
  title: string | null;
  content: string;
  structured_output: {
    evidence_refs?: Array<{
      evidence_ref_id: string;
      claim: string;
      evidence_encounter_ids: string[];
      evidence_type: string;
    }>;
  } | null;
  evidence_encounters: string[];
  source_context_hash: string;
  /** 该版本实际采用的依据摘要；NULL = 历史版本未保存快照（不按当前资料补建）。 */
  evidence_snapshot: NoteEvidenceSnapshotSummary | null;
  provider: string | null;
  model: string | null;
  prompt_version: number | null;
  based_on_version_id: string | null;
  created_at: string;
  source: string;
};

/** 列表接口只返回摘要：正文与 AI 结构化输出在编辑页按需读取。 */
export type NoteVersionSummary = Omit<NoteVersionData, "content" | "structured_output">;

export type NoteListItem = {
  note_id: string;
  patient_id: string;
  note_type: string;
  version_count: number;
  first_created_at: string;
  latest: NoteVersionSummary;
  /** Newest user_final version; null until the note was ever finalized. */
  latest_final: NoteVersionSummary | null;
  evidence_window?: {
    latest_evidence_encounter_at: string | null;
    later_encounter_count: number;
  };
};

export type NoteHistory = {
  note_id: string;
  patient_id: string;
  patient: {
    id: string;
    internal_patient_code: string;
    display_name: string | null;
  } | null;
  note_type: string;
  evidence_window?: {
    latest_evidence_encounter_at: string | null;
    later_encounter_count: number;
  };
  /** 已采用的资料与当前数据的一致性：提示重新核对可用，绝不判作品过期。 */
  evidence_status?: {
    status: "ok" | "ambiguous" | "unresolvable";
    matches_current_data?: boolean;
  } | null;
  versions: NoteVersionData[];
};

/** POST /notes/{id}/evidence-preview 的只读响应类型在 lib/note-evidence-preview。 */

export type PatientNotesQuery = {
  limit?: number;
  offset?: number;
  /** latest：最新版本创建时间降序（新建/刚更新的作品排最前）。 */
  sort?: "first_created" | "latest";
  note_type?: "teacher_note" | "case_record";
  /** 按最新版本的依据就诊过滤（跟师笔记面板按当前就诊取作品）。 */
  encounter_id?: string;
};

export type PatientNotesPage = {
  items: NoteListItem[];
  total: number;
  limit: number;
  offset: number;
};

export function listPatientNotes(
  patientId: string,
  query: PatientNotesQuery = {},
): Promise<PatientNotesPage> {
  const params = new URLSearchParams();
  if (query.limit !== undefined) params.set("limit", String(query.limit));
  if (query.offset !== undefined) params.set("offset", String(query.offset));
  if (query.sort) params.set("sort", query.sort);
  if (query.note_type) params.set("note_type", query.note_type);
  if (query.encounter_id) params.set("encounter_id", query.encounter_id);
  const search = params.toString();
  return apiFetch<PatientNotesPage>(`/api/v1/patients/${patientId}/notes${search ? `?${search}` : ""}`);
}

export const versionTypeLabels: Record<string, string> = {
  user_original: "用户原稿",
  ai_draft: "AI 草稿",
  user_draft: "用户草稿",
  user_final: "用户定稿",
};

const noteTypeLabels: Record<string, string> = {
  teacher_note: "跟师笔记",
  case_record: "医案整理",
};

const evidenceTypeLabels: Record<string, string> = {
  physician_record: "医生记录",
  student_note: "学生补录",
  ai_extracted: "AI抽取",
  deterministic_prescription_diff: "确定性处方Diff",
  record_absence: "未见记录",
  mixed: "多来源",
};

function versionLabel(version: NoteVersionData) {
  return `v${version.version} · ${versionTypeLabels[version.version_type] ?? version.version_type} · ${new Date(
    version.created_at,
  ).toLocaleString("zh-CN")}`;
}

function buildExportMarkdown(
  version: NoteVersionData,
  patient: NoteHistory["patient"],
): string {
  const typeLabel = noteTypeLabels[version.note_type] ?? version.note_type;
  const lines = [
    `# ${version.title || typeLabel}`,
    "",
    `- 作品类型：${typeLabel}`,
    `- 患者：${patient?.internal_patient_code ?? version.patient_id}${patient?.display_name ? `（${patient.display_name}）` : ""}`,
    `- 版本：v${version.version} · ${versionTypeLabels[version.version_type] ?? version.version_type} · ${new Date(version.created_at).toLocaleString("zh-CN")}`,
  ];
  if (version.provider) {
    lines.push(`- 生成信息：${version.provider} / ${version.model} · 提示词 v${version.prompt_version}（AI 草稿，供修改定稿）`);
  }
  lines.push(`- 来源就诊数：${version.evidence_encounters.length}（见文末内容依据）`);
  lines.push("", "---", "", version.content);
  return lines.join("\n");
}

/** AI 证据结构化引用里出现的全部就诊 ID（无结构化输出则为空）。 */
function structuredRefIdsOf(version: NoteVersionData): string[] {
  return (
    version.structured_output?.evidence_refs?.flatMap((ref) => ref.evidence_encounter_ids) ?? []
  );
}

function exportVersion(version: NoteVersionData, patient: NoteHistory["patient"]) {
  const markdown = buildExportMarkdown(version, patient);
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const typeLabel = noteTypeLabels[version.note_type] ?? "笔记";
  const patientCode = patient?.internal_patient_code ?? version.patient_id.slice(0, 8);
  const day = new Date(version.created_at).toISOString().slice(0, 10);
  anchor.href = url;
  anchor.download = `${typeLabel}_${patientCode}_v${version.version}_${day}.md`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function DiffRegionView({ region, index }: { region: ReturnType<typeof diffRegions>[number]; index: number }) {
  if (region.type === "gap") {
    return (
      <li className="py-1 text-center text-xs text-stone-400" aria-label={`${region.count} 行未变`}>
        …（{region.count} 行未变）…
      </li>
    );
  }
  return (
    <li>
      <ul className="space-y-0.5 font-mono text-xs leading-5">
        {region.lines.map((line: DiffLine, lineIndex) => (
          <li
            key={`${index}-${lineIndex}`}
            className={
              line.type === "added"
                ? "rounded bg-emerald-50 px-2 py-0.5 text-emerald-800"
                : line.type === "removed"
                  ? "rounded bg-rose-50 px-2 py-0.5 text-rose-800"
                  : "px-2 py-0.5 text-stone-400"
            }
          >
            {line.type === "added" ? "+ " : line.type === "removed" ? "− " : "  "}
            {line.text || "（空行）"}
          </li>
        ))}
      </ul>
    </li>
  );
}

export function NoteWorkspace({
  noteId,
  onChanged,
  onDirtyChange,
  resumeSessionId,
}: {
  noteId: string;
  onChanged?: () => void | Promise<void>;
  onDirtyChange?: (dirty: boolean) => void;
  /** 工作台“继续工作”指派的编辑会话：只用于高亮对应缓冲，恢复仍需显式确认。 */
  resumeSessionId?: string;
}) {
  const [history, setHistory] = useState<NoteHistory | null>(null);
  const [selectedId, setSelectedId] = useState("");
  // Unsaved edits are keyed to the version they started from; switching
  // versions or reloading shows that version's stored content again.
  const [edit, setEdit] = useState<{ versionId: string; text: string } | null>(null);
  // Recovery records for this note across ALL editing sessions (tabs, prior
  // visits). Offered back as a list when the editor is clean; each record is
  // cleared only when its own edit is saved, restored, or discarded.
  const [buffers, setBuffers] = useState<NoteEditBuffer[]>([]);
  // Whether the last buffer write actually persisted. When storage is
  // unavailable the dirty-confirm guards stay authoritative.
  const [bufferHealthy, setBufferHealthy] = useState(true);
  // The 编辑与保存 module's pending-save channel is wired right after the
  // notice state below (it writes mount notices through setNotice).
  // True when the LAST save could not be registered locally: the save still
  // proceeds, but cross-refresh verification/replay is NOT in place and the
  // UI must say so instead of claiming protection.
  const [saveProtectionUnavailable, setSaveProtectionUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const saving = useRef(false);
  const saveRequest = useRef(new SaveRequest());
  // Identifies THIS mounted editor, so two tabs (or two visits) writing
  // recovery records for the same note never overwrite each other.
  const sessionRef = useRef("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // The 编辑与保存 module's pending-save channel: every save whose outcome
  // was never confirmed (lost response, refresh, crash) is shown IMMEDIATELY
  // as 核对中 and marked per entry as its lookup returns; a failed
  // verification round leaves records visible — never silently hidden.
  const lookupPendingSave = useCallback(async (entry: PendingSave) => {
    const version = await apiFetch<NoteVersionData>(`/api/v1/notes/versions/${entry.requestId}`);
    return `v${version.version}`;
  }, []);
  const handlePendingLanded = useCallback((labels: string[]) => {
    setNotice(`上次未确认的保存已核对入库（${labels.join("、")}），没有产生重复版本。`);
  }, []);
  const pendingChannel = usePendingSaveChannel({
    kind: "note_revision",
    noteId,
    lookup: lookupPendingSave,
    onLanded: handlePendingLanded,
    dropConfirmMessage: "确定丢弃这条待重试保存记录吗？其中尚未入库的正文将无法再找回。",
  });
  const pendingChecks = pendingChannel.checks;

  function editingSessionId() {
    if (!sessionRef.current) sessionRef.current = crypto.randomUUID();
    return sessionRef.current;
  }

  const refreshBuffers = useCallback(() => {
    setBuffers(listNoteEditBuffers(noteId));
  }, [noteId]);

  // Parents render this workspace with key={noteId}: every instance serves
  // exactly one note, so history, unsaved edits and notices can never leak
  // across notes, and a response that outlives a note switch lands on an
  // unmounted instance where its state updates are no-ops.
  useEffect(() => {
    let active = true;
    apiFetch<NoteHistory>(`/api/v1/notes/${noteId}`)
      .then((data) => {
        if (!active) return;
        setHistory(data);
        setError("");
        setBuffers(listNoteEditBuffers(noteId));
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
  }, [noteId]);

  const selected =
    history?.versions.find((item) => item.id === selectedId) ?? history?.versions.at(-1);
  const displayedContent =
    edit && selected && edit.versionId === selected.id ? edit.text : selected?.content ?? "";
  const dirty = Boolean(
    selected && isNoteContentDirty(selected.content, displayedContent),
  );
  // Records still worth offering: their text differs from what the base
  // version already stores (a missing base version stays offerable so the
  // user decides, never a silent drop).
  const recoverableBuffers = history
    ? buffers.filter((record) => {
        const base = history.versions.find((item) => item.id === record.versionId);
        return !base || base.content !== record.text;
      })
    : [];

  // Debounced autosave of the recovery buffer while editing. If storage is
  // unavailable the dirty-confirm guards stay authoritative.
  useEffect(() => {
    if (!edit || !dirty) return;
    const version = history?.versions.find((item) => item.id === edit.versionId);
    if (!version) return;
    const timer = window.setTimeout(() => {
      const next: NoteEditBuffer = {
        noteId,
        versionId: version.id,
        version: version.version,
        text: edit.text,
        savedAt: new Date().toISOString(),
        sessionId: editingSessionId(),
      };
      const persisted = writeNoteEditBuffer(next);
      setBufferHealthy(persisted);
      if (persisted) refreshBuffers();
    }, 800);
    return () => window.clearTimeout(timer);
  }, [dirty, edit, history, noteId, refreshBuffers]);

  // Surface unsaved changes so a parent that switches notes can ask first;
  // the unmount cleanup clears the flag when this editor goes away.
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  useEffect(() => {
    if (!dirty) return;

    const confirmLeave = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = true;
    };
    const confirmLinkNavigation = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const anchor = target.closest("a[href]");
      if (!anchor || anchor.getAttribute("target") === "_blank" || anchor.hasAttribute("download")) {
        return;
      }
      if (!window.confirm("当前笔记有未保存修改。离开后这些修改会丢失，确定离开吗？")) {
        event.preventDefault();
        event.stopPropagation();
      }
    };

    window.addEventListener("beforeunload", confirmLeave);
    document.addEventListener("click", confirmLinkNavigation, true);
    return () => {
      window.removeEventListener("beforeunload", confirmLeave);
      document.removeEventListener("click", confirmLinkNavigation, true);
    };
  }, [dirty]);

  async function run(action: string, task: () => Promise<NoteVersionData>, message: string) {
    if (saving.current) return;
    saving.current = true;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      const version = await task();
      if (action !== "regen") saveRequest.current.complete();
      // Merge by version id and order by version number: a late/idempotent
      // response must never duplicate a row or depend on arrival order.
      setHistory((current) => (current ? { ...current, ...mergeNoteVersion(current, version) } : current));
      // Move to the saved version only when it is actually the newest one; a
      // replayed older version stays in the dropdown without yanking the
      // selection backwards.
      if (history && isNewestVersion(mergeNoteVersion(history, version).versions, version)) {
        setSelectedId(version.id);
      }
      setEdit(null);
      // AI generation must not erase user edits from the recovery buffer.
      // A completed save clears only THIS session's record for the version
      // it was based on; other tabs' records stay untouched.
      if (action !== "regen" && selected) {
        clearNoteEditBuffer(noteId, selected.id, editingSessionId());
        refreshBuffers();
      }
      // 采用后的确定性正文检查：依据换了但正文没动时，“正文待核对”必须
      // 立刻说出来，不能让替换依据哈希看起来像核对已完成。
      let finalMessage = message;
      if (action === "adopt") {
        const pending = findOutOfScopeReferences(version.content, version.evidence_encounters, {
          knownEncounterIds: knownEncounterIds(),
          structuredRefIds: structuredRefIdsOf(version),
        });
        if (pending.length) {
          finalMessage =
            `${message} 注意：已更新依据，正文待核对——正文/证据引用仍指向范围外标识（` +
            `${pending.map((finding) => finding.identifier.slice(0, 8)).join("、")}…），请核对后再定稿。`;
        }
      }
      setNotice(finalMessage);
      try {
        await onChanged?.();
      } catch {
        setNotice(`${message} 笔记列表刷新失败，已保存内容仍然保留，请稍后刷新列表。`);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      saving.current = false;
      setBusy("");
    }
  }

  // 已知就诊全集：本作品全部版本引用过的就诊 + 选择器加载过的患者就诊
  // 列表。只有出现在这里的标识才能被断言为“范围外就诊”；其余 UUID 一律
  // 按“未识别标识”提示（可能是作品/文档等其他 ID）。
  function knownEncounterIds(): string[] {
    const ids = new Set<string>();
    for (const version of history?.versions ?? []) {
      for (const id of version.evidence_encounters) ids.add(id.toLowerCase());
    }
    for (const row of evidencePreview?.patient_encounters ?? []) ids.add(row.id.toLowerCase());
    return [...ids];
  }

  function saveRevision(
    final: boolean,
    adoptEvidence = false,
    evidenceOverride?: string[],
    previewToken?: string,
  ) {
    if (busy || !selected) return;
    // 修改文字沿用常规保存门槛；采用新资料只要求有内容——即使文字未改，
    // 依据更新本身也是有意义的新版本。
    if (!adoptEvidence && !canSaveNoteVersion(selected.version_type, selected.content, displayedContent, final)) return;
    if (adoptEvidence && (displayedContent.trim().length === 0 || !previewToken)) return;
    // 保存最终版确认的是“正文与所选依据的一致性”：正文仍引用范围外标识
    // 时显式让用户确认，而不是悄悄把待核对状态定稿。
    if (final) {
      const pending = findOutOfScopeReferences(displayedContent, selected.evidence_encounters, {
        knownEncounterIds: knownEncounterIds(),
        structuredRefIds: structuredRefIdsOf(selected),
      });
      if (
        pending.length &&
        !window.confirm(
          `正文仍引用依据范围之外的标识（${pending.length} 个）。\n\n` +
            "保存最终版表示你确认正文与所选依据一致。如需保留这些引用，请先取消，再调整依据范围或修改正文。\n\n仍要保存最终版吗？",
        )
      ) {
        return;
      }
    }
    const payload: Record<string, unknown> = { content: displayedContent, final, based_on_version_id: selected.id };
    if (adoptEvidence) {
      // 采用新资料整理：显式的新依据集合 + 预览凭证。服务端按当前数据库
      // 重算并比对凭证：预览后材料已变化即拒绝，用户必须重新预览确认。
      payload.evidence_encounter_ids = evidenceOverride ?? selected.evidence_encounters;
      payload.preview_token = previewToken;
    }
    // The 编辑与保存 module's ONE submission path: continue the unresolved
    // pending request when the SAME revision is re-saved after a refresh
    // (so server idempotency keeps covering it), otherwise mint a fresh
    // request id — and always register the pending record BEFORE the request
    // leaves the browser.
    const submission = beginSaveSubmission({
      kind: "note_revision",
      filter: { noteId },
      payload,
      inMemory: saveRequest.current,
      // 定位信息：让工作台能把这条待核对保存与本编辑会话的缓冲归为一个工作条目。
      sessionId: editingSessionId(),
    });
    setSaveProtectionUnavailable(!submission.recorded);
    const action = final ? "final" : adoptEvidence ? "adopt" : "draft";
    const message = final
      ? "已保存最终版本。"
      : adoptEvidence
        ? "已按当前资料整理并保存新版本（依据已更新）。"
        : "已保存草稿版本。";
    return run(action, async () => {
      try {
        const version = await apiFetch<NoteVersionData>(`/api/v1/notes/${noteId}/revisions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: submission.body,
        });
        clearPendingSave(submission.requestId);
        setSaveProtectionUnavailable(false);
        return version;
      } catch (reason) {
        if (isDefinitiveSaveRejection(reason)) {
          clearPendingSave(submission.requestId);
          setSaveProtectionUnavailable(false);
        } else {
          // The request may have landed or may still be in flight: surface
          // the pending record NOW (暂未查到) instead of waiting for the
          // next mount to notice it.
          pendingChannel.surfaceUnresolved(submission.requestId);
        }
        throw reason;
      }
    }, message);
  }

  // “更新依据”唯一入口（医案与跟师笔记共用）：打开时默认选中当前版本的
  // 就诊集合，先预览“拟新增/拟移除/材料哪里变了”，用户明确采用后才生成
  // 新版本。跟师笔记保持固定一次就诊的范围约束；预览命中无法唯一配对的
  // 处方对比时与采用路径同样 fail closed。
  const [evidencePanelOpen, setEvidencePanelOpen] = useState(false);
  const [evidenceDraft, setEvidenceDraft] = useState<string[]>([]);
  const [evidencePreview, setEvidencePreview] = useState<EvidencePreviewData | null>(null);
  const [evidencePreviewState, setEvidencePreviewState] = useState({ loading: false, error: "" });
  // 响应归属保护：每次预览请求递增序号，只有“最后一次发出”的请求才允许
  // 写入界面。快速连续勾选时，先发后至的旧响应在这里被丢弃，确认框的
  // 增减数量永远对应最后一次选择。
  const evidencePreviewSeq = useRef(0);
  const isTeacherNote = history?.note_type === "teacher_note";

  function loadEvidencePreview(ids: string[], basedOnVersionId: string) {
    const requestSeq = ++evidencePreviewSeq.current;
    setEvidencePreviewState({ loading: true, error: "" });
    apiFetch<EvidencePreviewData>(`/api/v1/notes/${noteId}/evidence-preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        based_on_version_id: basedOnVersionId,
        evidence_encounter_ids: ids,
      }),
    })
      .then((data) => {
        if (requestSeq !== evidencePreviewSeq.current) return;
        setEvidencePreview(data);
        setEvidencePreviewState({ loading: false, error: "" });
      })
      .catch((reason: Error) => {
        if (requestSeq !== evidencePreviewSeq.current) return;
        setEvidencePreview(null);
        setEvidencePreviewState({ loading: false, error: reason.message });
      });
  }

  function openEvidencePanel() {
    if (!selected || busy) return;
    if (selected.evidence_encounters.length === 0) {
      setError("当前版本没有来源就诊记录，无法更新依据。");
      return;
    }
    if (isTeacherNote && selected.evidence_encounters.length !== 1) {
      setError("跟师笔记必须且只能关联一次就诊，无法在此更新依据。");
      return;
    }
    // 默认选中当前版本的就诊集合（跟师笔记即其固定的那次就诊）。
    const draft = [...selected.evidence_encounters];
    setEvidenceDraft(draft);
    setEvidencePanelOpen(true);
    loadEvidencePreview(draft, selected.id);
  }

  function closeEvidencePanel() {
    evidencePreviewSeq.current += 1;
    setEvidencePanelOpen(false);
    setEvidenceDraft([]);
    setEvidencePreview(null);
    setEvidencePreviewState({ loading: false, error: "" });
  }

  function toggleEvidenceEncounter(id: string) {
    const picked = evidenceDraft.includes(id)
      ? evidenceDraft.filter((item) => item !== id)
      : [...evidenceDraft, id];
    // 保持时间线顺序：按服务端返回的候选列表排序（首次加载前保持原序）。
    const ordered = evidencePreview
      ? evidencePreview.patient_encounters
          .map((row) => row.id)
          .filter((value) => picked.includes(value))
      : picked;
    setEvidenceDraft(ordered);
    if (ordered.length && selected) loadEvidencePreview(ordered, selected.id);
    else {
      // 空范围不允许采用：作废在途响应并清空预览。
      evidencePreviewSeq.current += 1;
      setEvidenceDraft([]);
      setEvidencePreview(null);
      setEvidencePreviewState({ loading: false, error: "" });
    }
  }

  // 只有“回显版本 = 所选版本、回显集合 = 当前勾选”的预览才可用于展示与
  // 确认（服务端回显 + isPreviewForSelection 双保险，乱序响应永不参与）。
  const activePreview =
    evidencePreview && selected && isPreviewForSelection(evidencePreview, selected.id, evidenceDraft)
      ? evidencePreview
      : null;

  function adoptSelectedEvidence() {
    if (busy || !selected || !activePreview) return;
    if (!evidenceDraft.length || displayedContent.trim().length === 0) return;
    const { added, removed } = activePreview.proposal;
    const scopeLine = isTeacherNote
      ? `将基于 v${selected.version} 按当前数据库资料重算该作品的依据（跟师笔记范围固定为这一诊），`
      : `将把 v${selected.version} 的依据调整为所选 ${evidenceDraft.length} 次就诊（拟新增 ${added.length} 次、拟移除 ${removed.length} 次），按当前数据库资料重算依据，`;
    const message =
      scopeLine +
      "并把正在编辑的文字保存为新草稿。\n\n" +
      "历史版本与其依据不会被改动；AI 草稿不会被自动重新生成；正文也不会被自动改写。若采用前资料再次变化，服务端会拒绝并要求重新预览。确定继续吗？";
    if (!window.confirm(message)) return;
    closeEvidencePanel();
    return saveRevision(false, true, [...evidenceDraft], activePreview.proposal.preview_token);
  }

  function regenerate() {
    if (saving.current) return;
    const latest = history?.versions.at(-1);
    const sameContext = Boolean(
      latest &&
        hasAiDraftForSourceContext(
          history?.versions ?? [],
          latest.source_context_hash,
        ),
    );
    const warnings = [
      dirty ? "当前未保存修改不会进入新草稿。" : "",
      sameContext
        ? "当前来源上下文与已有 AI 草稿相同；继续会再次调用 Provider，并追加一个新版本。"
        : "",
    ].filter(Boolean);
    if (warnings.length && !window.confirm(`${warnings.join("\n")}\n\n仍要重新生成吗？`)) {
      return;
    }
    if (dirty && selected) {
      const recovery: NoteEditBuffer = {
        noteId, versionId: selected.id, version: selected.version,
        text: displayedContent, savedAt: new Date().toISOString(),
        sessionId: editingSessionId(),
      };
      if (!writeNoteEditBuffer(recovery)) {
        setBufferHealthy(false);
        setError("未保存编辑暂时无法写入恢复缓冲。请先保存草稿，再重新生成。");
        return;
      }
      setBufferHealthy(true);
      refreshBuffers();
    }
    return run("regen", () => apiFetch<NoteVersionData>(`/api/v1/notes/${noteId}/regenerate`, { method: "POST" }), "已生成新的 AI 草稿版本，旧版本保留。");
  }

  function selectVersion(nextId: string) {
    if (nextId === selected?.id) return;
    const nextVersion = history?.versions.find((item) => item.id === nextId);
    if (!nextVersion) return;
    // With a healthy local buffer the unsaved edits stay recoverable, so no
    // confirm is needed; flush the buffer synchronously first so keystrokes
    // from inside the debounce window are not lost, and switching back later
    // offers 恢复编辑.
    if (dirty && edit && selected) {
      const flushed: NoteEditBuffer = {
        noteId,
        versionId: selected.id,
        version: selected.version,
        text: edit.text,
        savedAt: new Date().toISOString(),
        sessionId: editingSessionId(),
      };
      const persisted = writeNoteEditBuffer(flushed);
      setBufferHealthy(persisted);
      if (persisted) refreshBuffers();
      if (!persisted &&
        !window.confirm("当前版本有未保存修改且本地编辑缓冲不可用，切换后修改会丢失，确定切换吗？")
      ) {
        return;
      }
    }
    setSelectedId(nextId);
    setEdit(null);
    setNotice("");
    // 依据范围面板绑定“打开时所查看的版本”；切换版本即整体重置，避免
    // 旧版本的预览/勾选被带到新版本上。
    closeEvidencePanel();
  }

  function restoreBuffer(record: NoteEditBuffer) {
    const version = history?.versions.find((item) => item.id === record.versionId);
    if (!version) {
      // Versions are append-only, so a missing base version is unexpected;
      // recovery failed, and recovery failure must never drop text quietly.
      if (
        !window.confirm(
          "该编辑缓冲基于的版本已不存在，无法自动恢复。确定丢弃这份未保存内容吗？",
        )
      ) {
        return;
      }
      clearNoteEditBuffer(noteId, record.versionId, record.sessionId);
      refreshBuffers();
      return;
    }
    // Write THIS session's copy BEFORE clearing the original: between
    // restore and the first debounced autosave there is otherwise no
    // persisted copy, and a refresh in that window would lose the text.
    const replacement: NoteEditBuffer = {
      ...record,
      sessionId: editingSessionId(),
      savedAt: new Date().toISOString(),
    };
    const persisted = writeNoteEditBuffer(replacement);
    if (persisted && record.sessionId !== editingSessionId()) {
      // A reliable copy now exists under this session; the original may go.
      // (Same-session records share the record's key — the write above
      // already replaced it in place, and clearing here would delete the
      // fresh copy.)
      clearNoteEditBuffer(noteId, record.versionId, record.sessionId);
    }
    setBufferHealthy(persisted);
    setSelectedId(version.id);
    setEdit({ versionId: version.id, text: record.text });
    refreshBuffers();
    setNotice(
      `已恢复 ${new Date(record.savedAt).toLocaleString("zh-CN")} 的未保存编辑（基于 v${record.version}）。` +
        (persisted ? "" : "注意：本地无法写入新的恢复副本，原缓冲保留，请尽快保存草稿。"),
    );
  }

  function discardBuffer(record: NoteEditBuffer) {
    clearNoteEditBuffer(noteId, record.versionId, record.sessionId);
    refreshBuffers();
    setNotice("");
  }

  function describePendingSaveEntry(entry: PendingSave) {
    return describePendingSave(entry, { finalTag: true });
  }

  async function retryPendingSave(entry: PendingSave) {
    if (saving.current) return;
    saving.current = true;
    setBusy("pending-retry");
    setError("");
    try {
      // Exact replay: the recorded body (request id included) is re-sent
      // byte-for-byte, so a save that DID land can never be duplicated.
      const version = await apiFetch<NoteVersionData>(`/api/v1/notes/${noteId}/revisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: entry.body,
      });
      clearPendingSave(entry.requestId);
      pendingChannel.resolveLanded(entry.requestId);
      // Merge by version id / version number: the replay may return a version
      // this tab already knows (the save HAD landed) or an older one — never
      // duplicate a row or let arrival order decide what "latest" is.
      setHistory((current) => (current ? { ...current, ...mergeNoteVersion(current, version) } : current));
      // The replay must never clobber work in progress: switch the editor to
      // the replayed version only when there is no unsaved edit AND it is the
      // newest version; otherwise the user keeps their current position and
      // picks the version up in the history dropdown.
      if (!dirty && history && isNewestVersion(mergeNoteVersion(history, version).versions, version)) {
        setSelectedId(version.id);
      }
      refreshBuffers();
      setNotice(
        `已补交上次未完成的保存（v${version.version}）；请求标识保持一致，不会产生重复版本。` +
          (dirty ? "你当前的编辑保持不变。" : ""),
      );
      try {
        await onChanged?.();
      } catch {
        setNotice("已补交保存，但笔记列表刷新失败，请稍后刷新列表。");
      }
    } catch (reason) {
      // A rejected REPLAY proves nothing about the original attempt — the
      // record stays until it verifies as landed or the user drops it.
      setError(
        reason instanceof Error
          ? `补交未完成的保存失败：${reason.message}（待核对记录已保留，可稍后再次核对或重试）`
          : "补交未完成的保存失败（待核对记录已保留）",
      );
    } finally {
      saving.current = false;
      setBusy("");
    }
  }

  async function recheckPendingSaves() {
    if (saving.current || pendingChannel.checking || !pendingChannel.recheckableCount) return;
    saving.current = true;
    setBusy("pending-check");
    setError("");
    // Failures surface per entry (核对失败) instead of as a global error, so
    // the records themselves stay the source of truth.
    const landed = await pendingChannel.recheck();
    if (landed.length) {
      setNotice(`再次核对完成：${landed.join("、")} 已入库，没有产生重复版本。`);
      try {
        await onChanged?.();
      } catch {
        // 列表刷新失败不阻塞核对结论。
      }
    } else {
      setNotice("再次核对完成：仍未查到这些保存的入库结果。");
    }
    saving.current = false;
    setBusy("");
  }

  const selectedVersions = history?.versions ?? [];
  const selectedVersionIndex = selected
    ? selectedVersions.findIndex((item) => item.id === selected.id)
    : -1;
  const previousVersion = selectedVersionIndex > 0 ? selectedVersions[selectedVersionIndex - 1] : undefined;

  const pendingSummary = pendingCheckSummary(pendingChecks);

  if (loading) return <div className="mt-4 h-40 animate-pulse rounded-xl bg-stone-100" />;

  const evidenceRefs = selected?.structured_output?.evidence_refs ?? [];
  // 正文范围的确定性检查（随编辑实时更新）：呈现文本的系统附录只含范围内
  // ID，因此命中必来自正文本身或 AI 证据引用。
  const scopeFindings = selected
    ? findOutOfScopeReferences(displayedContent, selected.evidence_encounters, {
        knownEncounterIds: knownEncounterIds(),
        structuredRefIds: structuredRefIdsOf(selected),
      })
    : [];

  return (
    <div className="mt-4">
      {error ? (
        <p role="alert" className="mb-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>
      ) : null}
      {notice ? (
        <p role="status" className="mb-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</p>
      ) : null}

      {saveProtectionUnavailable ? (
        <p role="alert" className="mb-3 rounded-xl border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800">
          本次保存未能写入本地“待核对”记录：如果保存结果因刷新或断网丢失，将无法自动核对或补交。请检查浏览器存储（隐私模式/配额）后重试；现在仍会尝试提交本次保存。
        </p>
      ) : null}

      {pendingChecks.length ? (
        <div role="alert" className="mb-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <p className="font-semibold">
            有 {pendingSummary.total} 次保存的入库结果尚未确认：{pendingSummary.parts.join("；")}。可稍后再次核对；用原内容重试也不会产生重复版本（同一标识由服务端幂等收敛）。
          </p>
          <p className="mt-1">
            <button type="button" disabled={busy !== "" || !pendingChannel.recheckableCount} onClick={() => void recheckPendingSaves()} className="focus-ring rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-semibold text-amber-800 disabled:opacity-60"><Search aria-hidden="true" size={13} />再次核对</button>
          </p>
          <ul className="mt-2 space-y-2">
            {pendingChecks.map((check) => (
              <li key={check.entry.requestId} className="flex flex-wrap items-center justify-between gap-2">
                <span className="min-w-0 flex-1 truncate">
                  {describePendingSaveEntry(check.entry)}
                  <span className={`ml-2 rounded px-1.5 py-0.5 text-[10px] font-semibold ${PENDING_STATUS_VIEW[check.status].className}`}>
                    {PENDING_STATUS_VIEW[check.status].label}
                  </span>
                </span>
                <span className="flex shrink-0 gap-2">
                  {check.status !== "verifying" ? (
                    <>
                      <button type="button" disabled={busy !== ""} onClick={() => void retryPendingSave(check.entry)} className="focus-ring inline-flex items-center gap-1 rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-60"><RefreshCw aria-hidden="true" size={13} />用原内容重试</button>
                      <button type="button" disabled={busy !== ""} onClick={() => pendingChannel.drop(check.entry)} className="focus-ring inline-flex items-center gap-1 rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs text-amber-800 disabled:opacity-60"><Trash2 aria-hidden="true" size={13} />丢弃记录</button>
                    </>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {!dirty && recoverableBuffers.length ? (
        <div role="status" className="mb-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <p className="font-semibold">
            检测到 {recoverableBuffers.length} 份未保存的编辑缓冲（每个标签页独立保留，互不覆盖，不会自动删除）：
            {recoverableBuffers.length > NOTE_EDIT_BUFFER_SOFT_LIMIT
              ? `数量较多（超过 ${NOTE_EDIT_BUFFER_SOFT_LIMIT} 份），建议逐条恢复保存，或丢弃不需要的内容。`
              : ""}
          </p>
          <ul className="mt-2 space-y-2">
            {recoverableBuffers.map((record) => {
              const isResumeTarget = Boolean(resumeSessionId && record.sessionId === resumeSessionId);
              return (
                <li
                  key={`${record.versionId}.${record.sessionId}`}
                  className={`flex flex-wrap items-center justify-between gap-2 rounded-lg px-2 py-1 ${isResumeTarget ? "ring-2 ring-amber-500 bg-amber-100/70" : ""}`}
                >
                  <span className="min-w-0 flex-1 truncate">
                    {isResumeTarget ? (
                      <span className="mr-2 inline-block rounded bg-amber-600 px-1.5 py-0.5 text-[11px] font-semibold text-white">继续目标</span>
                    ) : null}
                    基于 v{record.version} · {new Date(record.savedAt).toLocaleString("zh-CN")} · {record.text.length} 字
                  </span>
                  <span className="flex shrink-0 gap-2">
                    <button type="button" onClick={() => restoreBuffer(record)} className="focus-ring inline-flex items-center gap-1 rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white"><RotateCcw aria-hidden="true" size={13} />恢复编辑</button>
                    <button type="button" onClick={() => discardBuffer(record)} className="focus-ring inline-flex items-center gap-1 rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs text-amber-800"><Trash2 aria-hidden="true" size={13} />丢弃</button>
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      <div className="flex flex-wrap items-end justify-between gap-3">
        <label className="text-xs font-semibold text-stone-600">
          版本历史（所有版本永久保留）
          <select
            value={selected?.id ?? ""}
            disabled={busy !== "" || loading}
            onChange={(event) => selectVersion(event.target.value)}
            className="focus-ring mt-1 block rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm font-normal"
          >
            {history?.versions.map((version) => (
              <option key={version.id} value={version.id}>
                {versionLabel(version)}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={regenerate}
          disabled={busy !== "" || loading}
          className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#9bc5b8] bg-[#eef7f3] px-3 py-2 text-sm font-medium text-[#175849] disabled:opacity-60"
        >
          {busy === "regen" ? <LoaderCircle aria-hidden="true" className="animate-spin" size={15} /> : <RefreshCw aria-hidden="true" size={15} />}
          重新生成（不覆盖旧草稿）
        </button>
      </div>

      {selected ? (
        <div className="mt-3 rounded-xl border border-stone-200 bg-white p-3 text-[11px] text-stone-500">
          <span className={`mr-2 rounded px-1.5 py-0.5 font-semibold ${selected.version_type === "ai_draft" ? "bg-amber-100 text-amber-800" : "bg-blue-100 text-blue-800"}`}>
            {versionTypeLabels[selected.version_type] ?? selected.version_type}
          </span>
          <span>{selected.source === "ai_inferred" ? "AI 生成，供修改定稿" : "用户本人文字"}</span>
          {selected.provider ? (
            <span className="ml-2">· {selected.provider} / {selected.model} · 提示词 v{selected.prompt_version}</span>
          ) : null}
          <span className="ml-2">· 内容依据上下文 {selected.source_context_hash.slice(0, 10)}…</span>
          {selected.evidence_snapshot ? (
            <span className="ml-2">
              · 依据快照 v{selected.evidence_snapshot.set_version}：{selected.evidence_snapshot.encounter_count} 次就诊 · 学生记录 {selected.evidence_snapshot.student_note_count} 条 · 处方条目 {selected.evidence_snapshot.prescription_item_count} 条{selected.evidence_snapshot.has_prescription_diff ? " · 含处方对比" : ""}
            </span>
          ) : (
            <span className="ml-2 text-amber-700">· 历史版本未保存依据快照（依据以就诊 ID 与哈希为准，不按当前资料补建）</span>
          )}
          <button
            type="button"
            onClick={() => exportVersion(selected, history?.patient ?? null)}
            className="focus-ring ml-2 inline-flex items-center gap-1 rounded-lg border border-stone-300 bg-[#faf8f2] px-2 py-1 text-[11px] font-medium text-stone-600"
          >
            <Download aria-hidden="true" size={12} />
            导出当前版本 (.md)
          </button>
        </div>
      ) : null}

      {/* 两类提示之二：已采用的资料发生变化 → 提醒可以重新核对，绝不把
          历史作品判成错误或过期，也绝不自动改写本稿。 */}
      {history?.evidence_status?.status === "ok" && history.evidence_status.matches_current_data === false ? (
        <p role="status" className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs leading-5 text-amber-900">
          该版本采用的资料与当前数据已有差异（如就诊内容被更新，或整理规范版本变化）。需要时可用「更新依据」按现在的资料生成新版本核对；本稿与历史版本不会被自动改写。
        </p>
      ) : null}
      {history?.evidence_status?.status === "ambiguous" ? (
        <p role="status" className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs leading-5 text-amber-900">
          该版本依据涉及无法唯一配对的处方对比，暂时无法自动比较资料变化；如需更新请在患者工作区处理，原依据保持不变。
        </p>
      ) : null}

      {/* 正文范围的确定性检查：定位提醒，绝不自动改写；只有能明确识别为
          就诊的才断言“范围外就诊”，其余标识只提示“未识别”；普通叙述是否
          仍成立由用户核对，“保存最终版”即确认正文与所选依据一致。 */}
      {scopeFindings.length ? (
        <div role="status" className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs leading-5 text-amber-900">
          <p className="font-semibold">
            正文待核对：发现依据范围之外的显式标识引用（{scopeFindings.length} 个）。
          </p>
          <ul className="mt-1 space-y-1">
            {scopeFindings.map((finding) => (
              <li key={finding.identifier} className="break-all">
                <span className="mr-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold text-white" style={{ backgroundColor: finding.kind === "out_of_scope_encounter" ? "#b45309" : "#78716c" }}>
                  {finding.kind === "out_of_scope_encounter" ? "范围外就诊" : "未识别标识"}
                </span>
                <span className="font-mono text-[10px]">{finding.identifier}</span>
                {finding.locations.length ? (
                  <ul className="mt-0.5 space-y-0.5 font-sans text-stone-600">
                    {finding.locations.map((location) => (
                      <li key={location.line} className="truncate">
                        行 {location.line}：{location.snippet}
                      </li>
                    ))}
                  </ul>
                ) : finding.inStructuredRefs ? (
                  <span className="ml-2 font-sans text-stone-600">· AI 证据引用</span>
                ) : null}
              </li>
            ))}
          </ul>
          <p className="mt-1">
            普通叙述（如“第三诊…”）是否仍成立由你核对，系统不会自动改写正文；保存最终版即表示确认正文与所选依据一致。
          </p>
        </div>
      ) : null}

      {previousVersion && selected ? (
        <details className="mt-3 rounded-xl border border-stone-200 bg-[#faf8f2] p-4">
          <summary className="focus-ring flex cursor-pointer list-none items-center gap-2 text-sm font-semibold text-stone-700">
            <BookOpenText aria-hidden="true" size={15} className="text-[#1f6857]" />
            与上一版（v{previousVersion.version}）对比，看这次改了什么
            <ChevronDown aria-hidden="true" size={15} />
          </summary>
          <VersionDiff previousContent={previousVersion.content} currentContent={selected.content} />
        </details>
      ) : null}

      <textarea
        aria-label="笔记内容"
        disabled={busy !== "" || !selected}
        value={displayedContent}
        onChange={(event) =>
          setEdit({ versionId: selected?.id ?? "", text: event.target.value })
        }
        rows={18}
        className="focus-ring mt-3 w-full rounded-xl border border-stone-300 bg-white p-4 font-mono text-[13px] leading-6 text-stone-800"
      />

      <p
        role="status"
        className={`mt-2 text-xs font-medium ${dirty ? "text-amber-700" : "text-stone-400"}`}
      >
        {dirty
          ? bufferHealthy
            ? "有未保存修改（已自动保留在本地编辑缓冲，切换版本后可恢复）"
            : "有未保存修改（本地编辑缓冲不可用，请尽快保存）"
          : "内容与当前已保存版本一致"}
      </p>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => saveRevision(false)}
          disabled={busy !== "" || !selected || !canSaveNoteVersion(selected.version_type, selected.content, displayedContent, false)}
          className="focus-ring inline-flex items-center gap-2 rounded-xl border border-stone-300 bg-white px-4 py-2.5 text-sm font-medium text-stone-700 disabled:opacity-60"
        >
          {busy === "draft" ? <LoaderCircle aria-hidden="true" className="animate-spin" size={15} /> : <PencilLine aria-hidden="true" size={15} />}
          保存为草稿
        </button>
        <button
          type="button"
          onClick={() => saveRevision(true)}
          disabled={busy !== "" || !selected || !canSaveNoteVersion(selected.version_type, selected.content, displayedContent, true)}
          className="focus-ring inline-flex items-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
        >
          {busy === "final" ? <LoaderCircle aria-hidden="true" className="animate-spin" size={15} /> : <Stamp aria-hidden="true" size={15} />}
          保存最终版
        </button>
        <button
          type="button"
          onClick={openEvidencePanel}
          disabled={busy !== "" || !selected || displayedContent.trim().length === 0}
          className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#9bc5b8] bg-[#eef7f3] px-4 py-2.5 text-sm font-medium text-[#175849] disabled:opacity-60"
        >
          <FileSearch aria-hidden="true" size={15} />
          更新依据…
        </button>
      </div>
      <p className="mt-2 text-xs font-medium text-stone-500">
        保存最终版表示你已人工核对正文与所选依据；系统仅检查显式标识引用。
      </p>
      <p className="mt-1 text-xs text-stone-400">
        每次保存都会产生新版本；原始 AI 草稿与你的原稿永远不会被覆盖。「更新依据」先按你正在查看的版本预览变化（医案可调整就诊集合，跟师笔记范围固定），确认后携带预览凭证保存新草稿；若采用前资料再次变化，服务端会要求重新预览。需要新的 AI 草稿时使用「重新生成」，两者相互独立。
      </p>

      {evidencePanelOpen ? (
        <div className="mt-3 rounded-xl border border-stone-200 bg-white p-4">
          <p className="text-xs font-semibold text-stone-700">
            {isTeacherNote
              ? "更新依据（跟师笔记固定关联一次就诊，不调整范围）"
              : "更新依据（医案整理可选择多次就诊）"}
          </p>
          <p className="mt-1 text-xs text-stone-500">
            {isTeacherNote
              ? "按当前数据库资料重算这一诊的写作材料；只有点击“采用”才会生成新草稿。"
              : "勾选后自动预览将要发生的变化；只有点击“采用”才会生成新草稿。"}{" "}
            预览与采用都基于你正在查看的
            {selected ? ` v${selected.version}` : ""}，不随最新版本移动。
          </p>
          {evidencePreview ? (
            <ul className="mt-3 space-y-1.5">
              {evidencePreview.patient_encounters
                .filter((row) => !isTeacherNote || evidenceDraft.includes(row.id))
                .map((row) => (
                  <li key={row.id}>
                    <label className="flex items-center gap-2 text-xs text-stone-700">
                      {isTeacherNote ? (
                        <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] font-semibold text-stone-500">
                          固定依据
                        </span>
                      ) : (
                        <input
                          type="checkbox"
                          checked={evidenceDraft.includes(row.id)}
                          onChange={() => toggleEvidenceEncounter(row.id)}
                          disabled={evidencePreviewState.loading}
                          className="accent-[#1f6857]"
                        />
                      )}
                      <span>
                        {new Date(row.encounter_at).toLocaleDateString("zh-CN")} · {row.chief_complaint_raw || "（未记录主诉）"}
                      </span>
                    </label>
                  </li>
                ))}
            </ul>
          ) : (
            <p className="mt-3 text-xs text-stone-400" aria-busy="true">
              {evidencePreviewState.loading ? "正在读取就诊列表…" : "暂无可选就诊。"}
            </p>
          )}
          {evidencePreviewState.loading && evidencePreview ? (
            <p className="mt-2 text-xs text-stone-400" aria-busy="true">正在预览所选范围…</p>
          ) : null}
          {evidencePreviewState.error ? (
            <p role="alert" className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">{evidencePreviewState.error}</p>
          ) : null}
          {activePreview && !evidencePreviewState.loading && !evidencePreviewState.error ? (
            <div className="mt-3 rounded-lg bg-[#faf8f2] p-3 text-xs leading-5 text-stone-600">
              <p>
                基于 v{activePreview.based_on_version.version} 的依据：{activePreview.current.evidence_encounters.length} 次就诊 → 所选：
                {activePreview.proposal.evidence_encounters.length} 次就诊；拟新增 {activePreview.proposal.added.length} 次、拟移除{" "}
                {activePreview.proposal.removed.length} 次。
              </p>
              {activePreview.latest_version.id !== activePreview.based_on_version.id ? (
                <p className="mt-1 text-amber-800">
                  注意：最新版本已是 v{activePreview.latest_version.version}；本次预览与采用始终基于你正在查看的
                  v{activePreview.based_on_version.version}。
                </p>
              ) : null}
              <p className={activePreview.materials_changed ? "mt-1 font-medium text-amber-800" : "mt-1"}>
                {activePreview.materials_changed
                  ? "按所选范围重算后，写作材料将发生变化（新版本会记录新的依据快照）。"
                  : "按所选范围重算后，写作材料与当前版本一致。"}
              </p>
              {describeMaterialsBreakdown(activePreview.materials_breakdown).map((line) => (
                <p key={line} className="mt-1">{line}</p>
              ))}
            </div>
          ) : null}
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={adoptSelectedEvidence}
              disabled={
                busy !== "" ||
                !selected ||
                !activePreview ||
                evidencePreviewState.loading ||
                evidenceDraft.length === 0 ||
                displayedContent.trim().length === 0
              }
              className="focus-ring inline-flex items-center gap-2 rounded-xl bg-[#1f6857] px-4 py-2 text-sm font-semibold text-white disabled:opacity-60"
            >
              <Stamp aria-hidden="true" size={14} />
              采用所选资料（保存新草稿）
            </button>
            <button
              type="button"
              onClick={closeEvidencePanel}
              className="focus-ring rounded-xl border border-stone-300 bg-white px-4 py-2 text-sm text-stone-600"
            >
              收起
            </button>
          </div>
        </div>
      ) : null}

      <details className="mt-4 rounded-xl border border-stone-200 bg-[#faf8f2] p-4">
        <summary className="focus-ring flex cursor-pointer list-none items-center gap-2 text-sm font-semibold text-stone-700">
          <BookOpenText aria-hidden="true" size={16} className="text-[#1f6857]" />
          内容依据（Encounter 与证据）
          <ChevronDown aria-hidden="true" size={15} />
        </summary>
        <div className="mt-3 space-y-2 text-xs text-stone-600">
          <p className="flex flex-wrap items-center gap-1.5">
            <Landmark aria-hidden="true" size={13} />
            来源就诊：
            {selected?.evidence_encounters.map((id) => (
              <Link
                key={id}
                href={`/patients/${selected.patient_id}?encounter=${id}`}
                className="focus-ring rounded bg-white px-1.5 py-0.5 font-mono text-[10px] text-[#1f6857] underline decoration-dotted"
              >
                {id}
              </Link>
            ))}
          </p>
          {evidenceRefs.length ? (
            <ul className="space-y-1.5">
              {evidenceRefs.map((item) => (
                <li key={item.evidence_ref_id} className="rounded-lg bg-white px-3 py-2">
                  <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[10px] text-stone-500">
                    {evidenceTypeLabels[item.evidence_type] ?? item.evidence_type}
                  </span>
                  <span className="ml-2">{item.claim}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p>用户版本不携带 AI 证据结构；来源就诊仍如上保留。</p>
          )}
        </div>
      </details>

      <p className="mt-3 flex items-center gap-1.5 text-xs text-stone-400">
        <Save aria-hidden="true" size={13} />
        系统只负责把真实病例资料组织成可靠草稿；师承材料以你的定稿为准。
      </p>
    </div>
  );
}

function VersionDiff({ previousContent, currentContent }: { previousContent: string; currentContent: string }) {
  const lines = diffLines(previousContent, currentContent);
  if (!lines) {
    return (
      <p className="mt-3 rounded-lg bg-white px-3 py-2 text-xs text-stone-500">
        版本内容过长，未生成行级对比；请直接阅读上下两版正文。
      </p>
    );
  }
  const regions = diffRegions(lines);
  const changedCount = lines.filter((line) => line.type !== "same").length;
  if (!changedCount) {
    return (
      <p className="mt-3 rounded-lg bg-white px-3 py-2 text-xs text-stone-500">两版正文完全一致（可能是仅保存动作）。</p>
    );
  }
  return (
    <div className="mt-3">
      <p className="text-xs text-stone-500">共 {changedCount} 处行级变化（绿为新增，红为删除）。</p>
      <ul className="mt-2 space-y-2">
        {regions.map((region, index) => (
          <DiffRegionView key={index} region={region} index={index} />
        ))}
      </ul>
    </div>
  );
}
