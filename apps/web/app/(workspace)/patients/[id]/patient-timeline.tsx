"use client";

import {
  BookOpenText,
  BrainCircuit,
  CalendarDays,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ClipboardPlus,
  ExternalLink,
  FileImage,
  FileText,
  GitCompareArrows,
  Plus,
  ScrollText,
  Stamp,
  Stethoscope,
  X,
} from "lucide-react";
import Link from "next/link";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { RequestGeneration } from "@/lib/request-generation";
import { ProvenanceBadge } from "@/components/provenance-badge";
import { apiContentUrl, apiFetch } from "@/lib/api";
import {
  SaveRequest,
  beginSaveSubmission,
  clearPendingSave,
  isDefinitiveSaveRejection,
  leavesCurrentPage,
  type PendingSave,
} from "@/lib/save-request";
import {
  PENDING_STATUS_VIEW,
  describePendingSave,
  pendingCheckSummary,
} from "@/lib/pending-save-checks";
import { usePendingSaveChannel } from "@/lib/use-pending-save-channel";
import {
  clearStudentNoteBuffer,
  listStudentNoteBuffers,
  readStudentNoteBuffer,
  writeStudentNoteBuffer,
  type StudentNoteBuffer,
} from "@/lib/student-note-buffer";

import { DiffRequest, PrescriptionDiffPanel } from "./prescription-diff-panel";
import { LongitudinalAnalysisPanel } from "./longitudinal-analysis-panel";
import { CaseRecordPanel } from "./case-record-panel";
import { TeacherNotePanel } from "./teacher-note-panel";
import { listPatientNotes, versionTypeLabels, type NoteListItem } from "./note-editor";

type PrescriptionItem = {
  id: string;
  original_text: string;
  is_basic_drug: boolean;
  raw_drug_name: string;
  canonical_drug_name: string | null;
  processing_method: string | null;
  explicit_package_dose_g: string | null;
  package_count: string | null;
  original_package_unit: string | null;
  calculated_dose_g: string | null;
  display_dose: string;
  dose_source: string;
  dose_scope: string;
  review_status: string;
  source: string;
};

/** Light row for the date index; full content is fetched per encounter. */
type EncounterSummary = {
  id: string;
  encounter_at: string;
  hospital: string;
  department: string | null;
  physician: string | null;
  chief_complaint_raw: string | null;
  source: string;
  prescription_item_count: number;
  student_note_count: number;
  ai_analysis_count: number;
};

type PatientSummary = {
  id: string;
  internal_patient_code: string;
  display_name: string | null;
  sex: string;
  estimated_birth_year: number | null;
  encounter_count: number;
  encounters: EncounterSummary[];
  view: "summary";
};

type Encounter = {
  id: string;
  encounter_at: string;
  hospital: string;
  department: string | null;
  physician: string | null;
  age_at_encounter: number | null;
  chief_complaint_raw: string | null;
  present_illness_raw: string | null;
  past_history_raw: string | null;
  allergy_raw: string | null;
  source: string;
  document_id: string;
  document_url: string;
  symptoms: Array<{ id: string; raw: string; normalized: string | null; status: string; trend: string | null; source: string }>;
  diagnoses: Array<{ id: string; raw: string; normalized: string | null; diagnosis_type: string; is_primary: boolean; source: string }>;
  prescriptions: Array<{ id: string; total_decoctions: number | null; raw_instruction_text: string | null; source: string; items: PrescriptionItem[] }>;
  student_notes: Array<{ id: string; note_kind: string; content: string; source: string; version: number }>;
  ai_analyses: Array<{ id: string; task_type: string; provider: string; model: string; output: string; source: string }>;
};

const trendLabels: Record<string, string> = {
  improved: "改善 ↑",
  worsened: "加重 ↓",
  resolved: "已缓解",
  new: "新出现",
  persistent: "持续",
};

const noteTypeLabels: Record<string, string> = {
  teacher_note: "跟师笔记",
  case_record: "医案整理",
};

function formatDay(value: string) {
  return new Date(value).toLocaleDateString("zh-CN");
}

export function PatientTimeline({
  patientId,
  initialEncounterId,
  initialResumeSessionId,
}: {
  patientId: string;
  initialEncounterId?: string;
  /** 工作台“继续工作”指派的补录会话：自动展开表单并高亮该草稿，恢复仍需显式确认。 */
  initialResumeSessionId?: string;
}) {
  const [patient, setPatient] = useState<PatientSummary | null>(null);
  const [error, setError] = useState("");
  // Full encounter content, fetched on demand and cached per encounter.
  const [details, setDetails] = useState<Record<string, Encounter>>({});
  const [detailStates, setDetailStates] = useState<Record<string, { loading: boolean; error: string }>>({});
  const pendingDetails = useRef(new Map<string, Promise<void>>());
  const [selectedId, setSelectedId] = useState("");
  const [sourceOpen, setSourceOpen] = useState(false);
  const [noteOpen, setNoteOpen] = useState(false);
  const [teacherNoteOpen, setTeacherNoteOpen] = useState(false);
  const [busy, setBusy] = useState("");
  const [diffRequest, setDiffRequest] = useState<DiffRequest | null>(null);
  const [works, setWorks] = useState<NoteListItem[] | null>(null);
  const [worksTotal, setWorksTotal] = useState(0);
  const [worksOffset, setWorksOffset] = useState(0);
  const [worksRetryOffset, setWorksRetryOffset] = useState(0);
  const [worksLoading, setWorksLoading] = useState(false);
  const [worksError, setWorksError] = useState("");
  // Mirror of the quick student-note textarea. The field itself stays
  // uncontrolled; this mirror only drives the leave/refresh guards so an
  // unsaved sentence can never disappear without a confirm.
  const [noteDraft, setNoteDraft] = useState("");
  const [noteFormError, setNoteFormError] = useState("");
  // Persistent local drafts for the quick form, keyed per encounter + editing
  // session (same isolation as the note editor); offered back as a list,
  // never auto-filled.
  const [noteBufferOffers, setNoteBufferOffers] = useState<StudentNoteBuffer[]>([]);
  const [noteBufferHealthy, setNoteBufferHealthy] = useState(false);
  // True when the LAST student-note save could not be registered locally:
  // the save proceeds, but the UI must stop claiming cross-refresh
  // verification is in place.
  const [noteProtectionUnavailable, setNoteProtectionUnavailable] = useState(false);
  const [studentNotice, setStudentNotice] = useState("");
  // The quick form no longer keeps its own pending-check machinery; see the
  // shared 编辑与保存 channel below (created after loadDetail).
  const noteSave = useRef(new SaveRequest());
  const noteSaving = useRef(false);
  const noteKindRef = useRef<HTMLSelectElement | null>(null);
  // Identifies THIS mounted timeline, so two tabs taking notes on the same
  // encounter keep separate recovery records.
  const timelineSessionRef = useRef("");

  function noteEditingSessionId() {
    if (!timelineSessionRef.current) timelineSessionRef.current = crypto.randomUUID();
    return timelineSessionRef.current;
  }
  // Unsaved state of the open teacher-note editor. Collapsing the panel or
  // switching encounters unmounts that editor, so both buttons must confirm
  // before discarding it.
  const teacherEditorDirtyRef = useRef(false);
  const handleTeacherDirtyChange = useCallback((dirty: boolean) => {
    teacherEditorDirtyRef.current = dirty;
  }, []);
  const quickNoteDirty = noteDraft.trim().length > 0;

  const loadSummary = useCallback(async () => {
    const data = await apiFetch<PatientSummary>(
      `/api/v1/patients/${patientId}?view=summary`,
    );
    setPatient(data);
    return data;
  }, [patientId]);

  const loadDetail = useCallback(
    (encounterId: string) => {
      const pending = pendingDetails.current.get(encounterId);
      if (pending) return pending;
      setDetailStates((current) => ({ ...current, [encounterId]: { loading: true, error: "" } }));
      const request = apiFetch<Encounter>(
        `/api/v1/patients/${patientId}/encounters/${encounterId}`,
      ).then((detail) => {
        setDetails((current) => ({ ...current, [encounterId]: detail }));
        setDetailStates((current) => ({ ...current, [encounterId]: { loading: false, error: "" } }));
      }).catch((reason) => {
        setDetailStates((current) => ({ ...current, [encounterId]: {
          loading: false,
          error: reason instanceof Error ? reason.message : "读取就诊内容失败",
        } }));
      }).finally(() => {
        pendingDetails.current.delete(encounterId);
      });
      pendingDetails.current.set(encounterId, request);
      return request;
    },
    [patientId],
  );

  useEffect(() => {
    let active = true;
    apiFetch<PatientSummary>(`/api/v1/patients/${patientId}?view=summary`)
      .then((data) => {
        if (!active) return;
        setPatient(data);
        setError("");
        // A ?encounter= deep link (from the writing library or a note's
        // evidence list) names the visit to open; otherwise default to the
        // latest encounter and keep whatever the user already selected.
        setSelectedId((current) => {
          if (initialEncounterId && data.encounters.some((item) => item.id === initialEncounterId)) {
            return initialEncounterId;
          }
          return current || data.encounters[0]?.id || "";
        });
      })
      .catch((reason: Error) => {
        if (active) setError(reason.message);
      });
    return () => {
      active = false;
    };
  }, [patientId, initialEncounterId]);

  useEffect(() => {
    if (!selectedId || details[selectedId]) return;
    const timer = window.setTimeout(() => void loadDetail(selectedId), 0);
    return () => window.clearTimeout(timer);
  }, [selectedId, details, loadDetail]);

  // 作品列表按最近更新排序分页加载（数据库侧 total/limit/offset）：新建或
  // 刚更新的作品出现在第一页顶部，超过一页的作品通过翻页全部可达，不自动
  // 拉取全量。请求代序保护：迟到/乱序的旧页响应绝不覆盖当前页；加载期间
  // 翻页按钮禁用（快速双击只前进一页），失败保持当前页并可重试。
  const WORKS_PAGE_SIZE = 20;
  const worksRequests = useRef(new RequestGeneration());
  const loadWorks = useCallback(
    async (offset = 0) => {
      const generation = worksRequests.current.next();
      setWorksLoading(true);
      try {
        const payload = await listPatientNotes(patientId, {
          sort: "latest",
          limit: WORKS_PAGE_SIZE,
          offset,
        });
        if (!worksRequests.current.accepts(generation)) return;
        setWorks(payload.items);
        setWorksTotal(payload.total);
        setWorksOffset(offset);
        setWorksError("");
      } catch (reason) {
        if (!worksRequests.current.accepts(generation)) return;
        // 失败必须可见（保持当前页），重试按钮可再次加载同一页。
        setWorksRetryOffset(offset);
        setWorksError(reason instanceof Error ? reason.message : "作品列表读取失败");
      } finally {
        if (worksRequests.current.accepts(generation)) setWorksLoading(false);
      }
    },
    [patientId],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => void loadWorks(), 0);
    return () => window.clearTimeout(timer);
  }, [loadWorks]);

  // The teacher-note editor registers its own beforeunload (with a recovery
  // buffer behind it); the quick form also has a local draft buffer, but a
  // refresh/close still gets this explicit guard first.
  useEffect(() => {
    if (!quickNoteDirty) return;
    const confirmLeave = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = true;
    };
    const confirmNavigation = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!anchor || event.defaultPrevented || !leavesCurrentPage(
        event, anchor.getAttribute("href")!, window.location.href,
        anchor.getAttribute("target"), anchor.hasAttribute("download"),
      )) return;
      if (!window.confirm(
        noteBufferHealthy
          ? "学生补录表单有未保存内容；本机缓冲已留存草稿，下次打开该就诊时可恢复。确定继续吗？"
          : "学生补录表单有未保存内容。本地缓冲不可用，离开后内容会丢失，确定继续吗？",
      )) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    };
    window.addEventListener("beforeunload", confirmLeave);
    document.addEventListener("click", confirmNavigation, true);
    return () => {
      window.removeEventListener("beforeunload", confirmLeave);
      document.removeEventListener("click", confirmNavigation, true);
    };
  }, [quickNoteDirty, noteBufferHealthy]);

  // Debounced local draft for the quick form. If storage is unavailable the
  // confirm guards above stay authoritative.
  useEffect(() => {
    if (!noteOpen || !selectedId || !quickNoteDirty) return;
    const timer = window.setTimeout(() => {
      const kind = noteKindRef.current?.value ?? "tongue";
      const persisted = writeStudentNoteBuffer({
        encounterId: selectedId,
        noteKind: kind,
        content: noteDraft,
        savedAt: new Date().toISOString(),
        sessionId: noteEditingSessionId(),
        // 定位信息：让工作台能直达 /patients/{id}?encounter=… 接续这份草稿。
        patientId,
      });
      setNoteBufferHealthy(persisted);
    }, 600);
    return () => window.clearTimeout(timer);
  }, [noteOpen, selectedId, quickNoteDirty, noteDraft, patientId]);

  // The 编辑与保存 module's pending-save channel: student-note saves whose
  // outcome was never confirmed become visible IMMEDIATELY (核对中) when an
  // encounter is selected, then are marked per entry as their lookups return;
  // a failed verification round leaves them visible — never silently hidden.
  const lookupStudentPendingSave = useCallback(async (entry: PendingSave) => {
    await apiFetch(`/api/v1/encounters/${entry.encounterId}/student-notes/${entry.requestId}`);
    return "已入库";
  }, []);
  const handleStudentPendingLanded = useCallback((labels: string[]) => {
    void labels;
    setStudentNotice("上次未确认的补录保存已核对入库，没有产生重复。");
    // 补录影响当前就诊正文和索引计数；核对命中后刷新正文。
    if (selectedId) void loadDetail(selectedId).catch(() => {
      // Detail refresh failure keeps the notice; content is on the server.
    });
  }, [loadDetail, selectedId]);
  const studentPendingChannel = usePendingSaveChannel({
    kind: "student_note",
    encounterId: selectedId || undefined,
    enabled: Boolean(selectedId),
    lookup: lookupStudentPendingSave,
    onLanded: handleStudentPendingLanded,
    dropConfirmMessage: "确定丢弃这条待重试的补录保存记录吗？其中尚未入库的内容将无法再找回。",
  });
  const studentPendingChecks = studentPendingChannel.checks;

  // 工作台“继续工作”深链（?encounter=…&resume=<sessionId>）：落到该就诊后
  // 展开补录表单并把指派的草稿置顶高亮——绝不自动恢复内容，恢复仍由用户
  // 点击“恢复草稿”显式完成。仅执行一次；记录已不存在时安静跳过。
  const resumeAppliedRef = useRef(false);
  useEffect(() => {
    if (!initialResumeSessionId || !selectedId || selectedId !== initialEncounterId) return;
    const timer = window.setTimeout(() => {
      if (resumeAppliedRef.current) return;
      if (!readStudentNoteBuffer(selectedId, initialResumeSessionId)) return;
      resumeAppliedRef.current = true;
      setNoteFormError("");
      setStudentNotice("");
      setNoteBufferOffers(
        listStudentNoteBuffers(selectedId).filter((item) => item.content !== noteDraft),
      );
      setNoteOpen(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [selectedId, initialEncounterId, initialResumeSessionId, noteDraft]);

  function confirmTeacherEditorLeave() {
    return (
      !teacherEditorDirtyRef.current ||
      window.confirm("跟师笔记有未保存修改。切换或收起后修改会丢失，确定继续吗？")
    );
  }

  // The quick student-note form keeps a local draft buffer, but the guards
  // still ask before discarding an in-progress sentence, whether the form
  // unmounts via the toggle or via an encounter switch.
  function confirmQuickNoteLeave() {
    if (noteSaving.current) return false;
    if (!quickNoteDirty) return true;
    return window.confirm(
      noteBufferHealthy
        ? "学生补录表单有未保存内容；本机缓冲已留存草稿，下次打开该就诊时可恢复。确定继续吗？"
        : "学生补录表单有未保存内容。本地缓冲不可用，离开后内容会丢失，确定继续吗？",
    );
  }

  function toggleQuickNote() {
    if (noteOpen) {
      if (!confirmQuickNoteLeave()) return;
      setNoteOpen(false);
      setNoteDraft("");
      setNoteFormError("");
      setNoteBufferOffers([]);
      return;
    }
    setNoteFormError("");
    setStudentNotice("");
    // Offer (never auto-fill) this encounter's local drafts from every
    // editing session, excluding one identical to the current field text.
    const offers = listStudentNoteBuffers(selectedId).filter(
      (record) => record.content !== noteDraft,
    );
    setNoteBufferOffers(offers);
    setNoteOpen(true);
  }

  function restoreNoteBuffer(record: StudentNoteBuffer) {
    // Write THIS session's copy BEFORE clearing the original: between
    // restore and the first debounced autosave there is otherwise no
    // persisted copy, and a refresh in that window would lose the text.
    const replacement: StudentNoteBuffer = {
      ...record,
      sessionId: noteEditingSessionId(),
      savedAt: new Date().toISOString(),
      patientId,
    };
    const persisted = writeStudentNoteBuffer(replacement);
    if (persisted && record.sessionId !== noteEditingSessionId()) {
      clearStudentNoteBuffer(record.encounterId, record.sessionId);
    }
    setNoteBufferHealthy(persisted);
    setNoteDraft(record.content);
    const options = noteKindRef.current?.options;
    if (noteKindRef.current && options) {
      for (let index = 0; index < options.length; index += 1) {
        if (options[index].value === record.noteKind) {
          noteKindRef.current.value = record.noteKind;
          break;
        }
      }
    }
    setNoteBufferOffers(
      listStudentNoteBuffers(record.encounterId).filter((item) => item.content !== record.content),
    );
    setStudentNotice(
      `已恢复 ${new Date(record.savedAt).toLocaleString("zh-CN")} 的补录草稿。` +
        (persisted ? "" : "注意：本地无法写入新的恢复副本，原缓冲保留，请尽快保存。"),
    );
  }

  function discardNoteBuffer(record: StudentNoteBuffer) {
    clearStudentNoteBuffer(record.encounterId, record.sessionId);
    setNoteBufferOffers((current) =>
      current.filter((item) => item.sessionId !== record.sessionId),
    );
  }

  function syncEncounterUrl(id: string) {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.set("encounter", id);
    window.history.replaceState(null, "", url.toString());
  }

  function selectEncounter(id: string) {
    if (!id || id === selectedId) return;
    if (!confirmTeacherEditorLeave()) return;
    if (!confirmQuickNoteLeave()) return;
    setSourceOpen(false);
    setNoteOpen(false);
    setNoteDraft("");
    setNoteFormError("");
    setNoteBufferOffers([]);
    setStudentNotice("");
    studentPendingChannel.resetScope();
    setTeacherNoteOpen(false);
    setDiffRequest(null);
    setSelectedId(id);
    syncEncounterUrl(id);
  }

  async function addNote(event: FormEvent<HTMLFormElement>, encounterId: string) {
    event.preventDefault();
    if (noteSaving.current) return;
    noteSaving.current = true;
    setBusy(`note-${encounterId}`);
    setNoteFormError("");
    const form = new FormData(event.currentTarget);
    const payload = {
      note_kind: String(form.get("note_kind") ?? ""),
      content: String(form.get("content") ?? ""),
    };
    // The 编辑与保存 module's ONE submission path: continue the unresolved
    // pending request when the SAME revision is re-saved after a refresh
    // (restored draft, unchanged content and kind), otherwise mint a fresh
    // request id — and always register the pending record BEFORE the request
    // leaves the browser.
    const submission = beginSaveSubmission({
      kind: "student_note",
      filter: { encounterId, patientId },
      payload,
      inMemory: noteSave.current,
      identityPayload: JSON.stringify({ encounterId, ...payload }),
      // 定位信息：让工作台能把这条待核对保存与本编辑会话的草稿归为一个工作条目。
      sessionId: noteEditingSessionId(),
    });
    setNoteProtectionUnavailable(!submission.recorded);
    try {
      await apiFetch(`/api/v1/encounters/${encounterId}/student-notes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: submission.body,
      });
      clearPendingSave(submission.requestId);
      setNoteProtectionUnavailable(false);
      noteSave.current.complete();
      clearStudentNoteBuffer(encounterId, noteEditingSessionId());
      setNoteDraft("");
      setNoteOpen(false);
      setNoteBufferOffers([]);
      // 补录影响当前就诊正文和索引计数，两处都要刷新。
      await loadDetail(encounterId);
      try {
        await loadSummary();
      } catch {
        // 索引刷新失败不阻塞主流程；正文已更新。
      }
    } catch (reason) {
      // 保存失败必须就地可见；全局错误在此页面不会渲染。
      if (isDefinitiveSaveRejection(reason)) {
        clearPendingSave(submission.requestId);
        setNoteProtectionUnavailable(false);
      } else {
        // The request may have landed or may still be in flight: surface the
        // pending record NOW (暂未查到) instead of waiting for the next
        // encounter selection to notice it.
        studentPendingChannel.surfaceUnresolved(submission.requestId);
      }
      setNoteFormError(reason instanceof Error ? reason.message : "保存学习笔记失败，已填内容仍保留在表单中");
    } finally {
      noteSaving.current = false;
      setBusy("");
    }
  }

  async function retryStudentPendingSave(entry: PendingSave) {
    if (!entry.encounterId || noteSaving.current) return;
    noteSaving.current = true;
    setBusy(`note-${entry.encounterId}`);
    setNoteFormError("");
    setStudentNotice("");
    try {
      // Exact replay: the recorded body (request id included) is re-sent
      // byte-for-byte, so a save that DID land can never be duplicated.
      await apiFetch(`/api/v1/encounters/${entry.encounterId}/student-notes`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: entry.body,
      });
      clearPendingSave(entry.requestId);
      studentPendingChannel.resolveLanded(entry.requestId);
      setStudentNotice("已补交上次未完成的补录保存；请求标识保持一致，不会产生重复。表单中未保存的内容保持不变。");
      await loadDetail(entry.encounterId);
      try {
        await loadSummary();
      } catch {
        // 索引刷新失败不阻塞主流程。
      }
    } catch (reason) {
      // A rejected REPLAY proves nothing about the original attempt — the
      // record stays until it verifies as landed or the user drops it.
      setNoteFormError(
        reason instanceof Error
          ? `补交未完成的保存失败：${reason.message}（待核对记录已保留，可稍后再次核对或重试）`
          : "补交未完成的保存失败（待核对记录已保留）",
      );
    } finally {
      noteSaving.current = false;
      setBusy("");
    }
  }

  async function recheckStudentPendingSaves() {
    if (noteSaving.current || studentPendingChannel.checking || !studentPendingChannel.recheckableCount) return;
    noteSaving.current = true;
    setBusy("student-pending-check");
    setNoteFormError("");
    // Failures surface per entry (核对失败) instead of as a global error, so
    // the records themselves stay the source of truth.
    const landed = await studentPendingChannel.recheck();
    if (landed.length) {
      setStudentNotice("再次核对完成：部分保存已核对入库，没有产生重复。");
      try {
        await loadDetail(selectedId);
      } catch {
        // 正文刷新失败不影响核对结论。
      }
    } else {
      setStudentNotice("再次核对完成：仍未查到这些保存的入库结果。");
    }
    noteSaving.current = false;
    setBusy("");
  }

  function dropStudentPendingSaveEntry(entry: PendingSave) {
    studentPendingChannel.drop(entry);
  }

  function compareWithPrevious(currentId: string, previousId: string) {
    setDiffRequest({ currentId, previousId });
  }

  function openTool(detailsId: string) {
    const target = document.getElementById(detailsId) as HTMLDetailsElement | null;
    if (target) target.open = true;
  }

  function toggleTeacherNote() {
    if (teacherNoteOpen) {
      if (!confirmTeacherEditorLeave()) return;
      setTeacherNoteOpen(false);
      return;
    }
    setTeacherNoteOpen(true);
  }

  if (error && !patient) return <EmptyState title="患者工作区暂时无法读取" detail={error} />;
  if (!patient) return <div className="h-80 animate-pulse rounded-2xl bg-white/60" />;

  const summaries = patient.encounters;
  const selectedSummary = summaries.find((item) => item.id === selectedId) ?? summaries[0];
  const selectedIndex = selectedSummary
    ? summaries.findIndex((item) => item.id === selectedSummary.id)
    : -1;
  const olderSummary = selectedIndex >= 0 ? summaries[selectedIndex + 1] : undefined;
  const newerSummary = selectedIndex > 0 ? summaries[selectedIndex - 1] : undefined;
  const selectedDetail = selectedSummary ? details[selectedSummary.id] : undefined;
  const detailLoading = selectedSummary ? detailStates[selectedSummary.id]?.loading : false;
  const detailError = selectedSummary ? detailStates[selectedSummary.id]?.error : "";

  const encounterLabel = (encounter: EncounterSummary) =>
    `${formatDay(encounter.encounter_at)}${encounter.department ? ` · ${encounter.department}` : ""}`;

  const pendingSummary = pendingCheckSummary(studentPendingChecks);

  const dateIndex = (
    <div className="surface rounded-2xl p-3">
      <p className="flex items-center gap-2 px-2 pb-2 pt-1 text-xs font-bold tracking-wide text-stone-500">
        <CalendarDays aria-hidden="true" size={14} className="text-[#1f6857]" />
        就诊索引（{summaries.length}）
      </p>
      <div className="flex gap-2 overflow-x-auto lg:max-h-[60vh] lg:flex-col lg:overflow-y-auto">
        {summaries.map((encounter) => {
          const active = encounter.id === selectedSummary?.id;
          return (
            <button
              key={encounter.id}
              type="button"
              onClick={() => selectEncounter(encounter.id)}
              aria-current={active ? "true" : undefined}
              className={`focus-ring shrink-0 rounded-xl border px-3 py-2 text-left text-sm transition-colors lg:w-full lg:shrink ${
                active
                  ? "border-[#1f6857] bg-[#eef7f3] font-semibold text-[#175849]"
                  : "border-stone-200 bg-white text-stone-600 hover:border-[#9bc5b8]"
              }`}
            >
              <span className="block whitespace-nowrap font-medium lg:whitespace-normal">{formatDay(encounter.encounter_at)}</span>
              <span className="mt-0.5 hidden max-w-[15rem] truncate text-xs text-stone-400 lg:block">
                {encounter.chief_complaint_raw || "主诉未记录"}
              </span>
              <span className="mt-0.5 hidden text-[11px] text-stone-400 lg:block">
                {encounter.prescription_item_count} 味药
                {encounter.student_note_count ? ` · 补录 ${encounter.student_note_count}` : ""}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );

  return (
    <div>
      <header className="surface rounded-2xl p-5 sm:p-6">
        <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-start">
          <div>
            <p className="text-sm font-medium text-[#1f6857]">患者工作区</p>
            <div className="mt-1 flex flex-wrap items-baseline gap-3">
              <h1 className="text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl">{patient.internal_patient_code}</h1>
              {patient.display_name ? <span className="text-base text-stone-500">{patient.display_name}</span> : null}
            </div>
            <p className="mt-2 text-sm text-stone-500">
              {patient.sex === "male" ? "男" : patient.sex === "female" ? "女" : "性别未记录"}
              {patient.estimated_birth_year ? ` · 约 ${patient.estimated_birth_year} 年出生` : ""}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {summaries.length >= 2 ? (
              <button type="button" onClick={() => openTool("longitudinal-analysis-tool")} className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#9bc5b8] bg-[#eef7f3] px-4 py-3 text-sm font-semibold text-[#175849]"><BrainCircuit aria-hidden="true" size={17} />病程分析</button>
            ) : null}
            {summaries.length >= 1 ? (
              <button type="button" onClick={() => openTool("case-record-tool")} className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#9bc5b8] bg-[#eef7f3] px-4 py-3 text-sm font-semibold text-[#175849]"><ScrollText aria-hidden="true" size={17} />医案整理</button>
            ) : null}
            <div className="rounded-xl bg-[#f0ede4] px-4 py-3 text-sm text-stone-600">
              共 <strong className="text-stone-900">{summaries.length}</strong> 次就诊
            </div>
          </div>
        </div>
      </header>

      {!selectedSummary ? (
        <div className="mt-5">
          <EmptyState
            title="该患者暂无就诊记录"
            detail="成功导入并匹配到该患者的病历会按日期出现在这里。"
          />
          <div className="mt-3 text-center">
            <Link href="/imports" className="focus-ring inline-flex rounded-xl bg-[#1f6857] px-4 py-2 text-sm text-white">去导入病历</Link>
          </div>
        </div>
      ) : (
        <div className="mt-5 lg:grid lg:grid-cols-[280px_minmax(0,1fr)] lg:items-start lg:gap-5">
          <div className="lg:sticky lg:top-20">
            {/* 手机：下拉选择就诊；桌面：左侧日期索引。 */}
            <div className="lg:hidden">
              <label className="surface flex items-center gap-3 rounded-2xl px-4 py-3 text-sm">
                <CalendarDays aria-hidden="true" size={16} className="shrink-0 text-[#1f6857]" />
                <span className="sr-only">选择就诊</span>
                <select
                  value={selectedSummary.id}
                  onChange={(event) => selectEncounter(event.target.value)}
                  className="focus-ring w-full bg-transparent text-sm font-medium text-stone-800"
                >
                  {summaries.map((encounter) => (
                    <option key={encounter.id} value={encounter.id}>
                      {encounterLabel(encounter)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="mt-3 hidden lg:mt-0 lg:block">{dateIndex}</div>
          </div>

          <div className="mt-4 lg:mt-0">
            <article className="surface overflow-hidden rounded-2xl">
              <div className="border-b border-stone-200 bg-[#faf8f2] px-5 py-4 sm:px-6">
                <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
                  <div className="flex items-center gap-3">
                    <CalendarDays aria-hidden="true" className="text-[#1f6857]" size={18} />
                    <div>
                      <h2 className="font-bold text-stone-900">{formatDay(selectedSummary.encounter_at)}</h2>
                      <p className="mt-0.5 text-xs text-stone-500">{selectedSummary.department ?? selectedSummary.hospital}{selectedSummary.physician ? ` · ${selectedSummary.physician}` : ""}</p>
                    </div>
                  </div>
                  <ProvenanceBadge source={selectedSummary.source} />
                </div>
              </div>

              <div className="space-y-7 p-5 sm:p-6">
                {detailError && !detailLoading ? (
                  <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
                    <p>{detailError}</p>
                    <button type="button" onClick={() => void loadDetail(selectedSummary.id)} className="focus-ring mt-2 rounded-xl border border-red-300 bg-white px-3 py-1.5 text-xs">重试加载</button>
                  </div>
                ) : null}
                {!selectedDetail && !detailError ? (
                  <div className="h-64 animate-pulse rounded-xl bg-stone-100" aria-busy="true" />
                ) : null}
                {selectedDetail ? (
                  <>
                <section>
                  <h3 className="text-xs font-bold tracking-wide text-stone-500">病历摘要</h3>
                  <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2">
                    <div><dt className="text-xs text-stone-400">主诉</dt><dd className="mt-1 leading-6 text-stone-800">{selectedDetail.chief_complaint_raw || "未记录"}</dd></div>
                    <div><dt className="text-xs text-stone-400">现病史</dt><dd className="mt-1 leading-6 text-stone-800">{selectedDetail.present_illness_raw || "未记录"}</dd></div>
                    <div><dt className="text-xs text-stone-400">既往史</dt><dd className="mt-1 leading-6 text-stone-800">{selectedDetail.past_history_raw || "未记录"}</dd></div>
                    <div><dt className="text-xs text-stone-400">过敏史</dt><dd className="mt-1 leading-6 text-stone-800">{selectedDetail.allergy_raw || "未记录"}</dd></div>
                  </dl>
                </section>

                <section>
                  <h3 className="text-xs font-bold tracking-wide text-stone-500">症状</h3>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {selectedDetail.symptoms.length ? selectedDetail.symptoms.map((symptom) => (
                      <span key={symptom.id} className="inline-flex items-center gap-2 rounded-xl border border-stone-200 bg-white px-3 py-2 text-sm text-stone-700">
                        {symptom.normalized || symptom.raw}
                        {symptom.trend ? <span className="text-xs font-semibold text-[#1f6857]">{trendLabels[symptom.trend] ?? symptom.trend}</span> : null}
                      </span>
                    )) : <span className="text-sm text-stone-500">未记录</span>}
                  </div>
                </section>

                <section>
                  <h3 className="text-xs font-bold tracking-wide text-stone-500">诊断</h3>
                  <div className="mt-3 space-y-2">
                    {selectedDetail.diagnoses.length ? selectedDetail.diagnoses.map((diagnosis) => (
                      <div key={diagnosis.id} className="flex flex-wrap items-center gap-2 text-sm">
                        <Stethoscope aria-hidden="true" className="text-stone-400" size={16} />
                        <span className="font-medium text-stone-800">{diagnosis.normalized || diagnosis.raw}</span>
                        {diagnosis.is_primary ? <span className="rounded bg-stone-100 px-1.5 py-0.5 text-[11px] text-stone-500">主诊断</span> : null}
                        <ProvenanceBadge source={diagnosis.source} />
                      </div>
                    )) : <span className="text-sm text-stone-500">未记录</span>}
                  </div>
                </section>

                {selectedDetail.prescriptions.map((prescription) => (
                  <section key={prescription.id}>
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <h3 className="text-xs font-bold tracking-wide text-stone-500">处方 · {prescription.total_decoctions ? `共 ${prescription.total_decoctions} 剂` : "剂数未记录"}</h3>
                      <ProvenanceBadge source={prescription.source} />
                    </div>
                    {/* 桌面：表格；手机：条目卡片，避免横向滚动。 */}
                    <div className="mt-3 hidden overflow-x-auto rounded-xl border border-stone-200 sm:block">
                      <table className="w-full min-w-[640px] border-collapse text-left text-sm">
                        <thead className="bg-[#f4f1e9] text-xs text-stone-500"><tr><th className="px-4 py-3 font-semibold">原文</th><th className="px-4 py-3 font-semibold">标准药名</th><th className="px-4 py-3 font-semibold">包装</th><th className="px-4 py-3 font-semibold">计算剂量</th><th className="px-4 py-3 font-semibold">作用域</th></tr></thead>
                        <tbody className="divide-y divide-stone-200 bg-white">
                          {prescription.items.map((item) => (
                            <tr key={item.id}>
                              <td className="px-4 py-3 text-stone-700">{item.original_text}</td>
                              <td className="px-4 py-3"><span className="font-medium text-stone-800">{item.canonical_drug_name || "待确认"}</span>{item.processing_method ? <span className="ml-1 text-xs text-stone-500">· {item.processing_method}</span> : null}</td>
                              <td className="px-4 py-3 tabular-nums text-stone-600">{item.package_count ? `${item.package_count}${item.original_package_unit ?? ""}` : "未记录"}</td>
                              <td className="px-4 py-3 font-medium tabular-nums text-stone-800">{item.display_dose}</td>
                              <td className="px-4 py-3 text-xs text-stone-500">
                                {item.dose_scope === "per_decoction"
                                  ? "每剂"
                                  : item.dose_scope === "total_prescription"
                                    ? "全疗程"
                                    : "尚未确认"}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <ul className="mt-3 space-y-2 sm:hidden">
                      {prescription.items.map((item) => (
                        <li key={item.id} className="rounded-xl border border-stone-200 bg-white p-3">
                          <div className="flex items-start justify-between gap-3">
                            <p className="min-w-0 font-medium text-stone-800">
                              {item.canonical_drug_name || "待确认"}
                              {item.processing_method ? <span className="ml-1 text-xs font-normal text-stone-500">· {item.processing_method}</span> : null}
                            </p>
                            <p className="shrink-0 text-right text-sm font-semibold tabular-nums text-stone-800">{item.display_dose}</p>
                          </div>
                          <p className="mt-1 text-xs text-stone-500">
                            {item.dose_scope === "per_decoction" ? "每剂剂量" : item.dose_scope === "total_prescription" ? "全疗程总量" : "剂量作用域尚未确认"}
                            {item.package_count ? ` · 包装 ${item.package_count}${item.original_package_unit ?? ""}` : " · 包装未记录"}
                          </p>
                          <p className="mt-1 truncate text-xs text-stone-400">原文：{item.original_text}</p>
                        </li>
                      ))}
                    </ul>
                    {prescription.raw_instruction_text ? <p className="mt-3 whitespace-pre-wrap rounded-xl bg-[#f7f4ec] px-4 py-3 text-xs leading-5 text-stone-600">{prescription.raw_instruction_text}</p> : null}
                  </section>
                ))}

                {studentNotice ? (
                  <p role="status" className="mb-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{studentNotice}</p>
                ) : null}

                {noteProtectionUnavailable ? (
                  <p role="alert" className="mb-3 rounded-xl border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800">
                    本次补录保存未能写入本地“待核对”记录：如果保存结果因刷新或断网丢失，将无法自动核对或补交。请检查浏览器存储（隐私模式/配额）后重试；现在仍会尝试提交本次保存。
                  </p>
                ) : null}

                {studentPendingChecks.length ? (
                  <div role="alert" className="mb-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                    <p className="font-semibold">
                      该就诊有 {pendingSummary.total} 次补录保存的入库结果尚未确认：{pendingSummary.parts.join("；")}。可稍后再次核对；用原内容重试也不会产生重复（同一标识由服务端幂等收敛）。
                    </p>
                    <p className="mt-1">
                      <button type="button" disabled={busy !== "" || !studentPendingChannel.recheckableCount} onClick={() => void recheckStudentPendingSaves()} className="focus-ring rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-semibold text-amber-800 disabled:opacity-60">再次核对</button>
                    </p>
                    <ul className="mt-2 space-y-2">
                      {studentPendingChecks.map((check) => (
                        <li key={check.entry.requestId} className="flex flex-wrap items-center justify-between gap-2">
                          <span className="min-w-0 flex-1 truncate">
                            {describePendingSave(check.entry)}
                            <span className={`ml-2 rounded px-1.5 py-0.5 text-[10px] font-semibold ${PENDING_STATUS_VIEW[check.status].className}`}>
                              {PENDING_STATUS_VIEW[check.status].label}
                            </span>
                          </span>
                          <span className="flex shrink-0 gap-2">
                            {check.status !== "verifying" ? (
                              <>
                                <button type="button" disabled={busy !== ""} onClick={() => void retryStudentPendingSave(check.entry)} className="focus-ring rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-60">用原内容重试</button>
                                <button type="button" disabled={busy !== ""} onClick={() => dropStudentPendingSaveEntry(check.entry)} className="focus-ring rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs text-amber-800 disabled:opacity-60">丢弃记录</button>
                              </>
                            ) : null}
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {selectedDetail.student_notes.length ? (
                  <section>
                    <h3 className="text-xs font-bold tracking-wide text-stone-500">学生补录</h3>
                    <div className="mt-3 space-y-2">
                      {selectedDetail.student_notes.map((note) => <div key={note.id} className="rounded-xl border border-blue-200 bg-blue-50 p-4"><div className="mb-2"><ProvenanceBadge source={note.source} /></div><p className="whitespace-pre-wrap text-sm leading-6 text-blue-950">{note.content}</p></div>)}
                    </div>
                  </section>
                ) : null}

                {selectedDetail.ai_analyses.length ? (
                  <section>
                    <h3 className="text-xs font-bold tracking-wide text-stone-500">AI 解读</h3>
                    <div className="mt-3 space-y-2">
                      {selectedDetail.ai_analyses.map((analysis) => <div key={analysis.id} className="rounded-xl border border-amber-200 bg-amber-50 p-4"><div className="mb-2 flex items-center gap-2"><ProvenanceBadge source={analysis.source} /><span className="text-[11px] text-amber-700">{analysis.provider} / {analysis.model}</span></div><p className="text-sm leading-6 text-amber-950">{analysis.output}</p></div>)}
                    </div>
                  </section>
                ) : null}

                <section className="flex flex-wrap gap-2 border-t border-stone-200 pt-5">
                  {olderSummary ? (
                    <button type="button" onClick={() => compareWithPrevious(selectedDetail.id, olderSummary.id)} className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#9bc5b8] bg-[#eef7f3] px-3 py-2 text-sm font-medium text-[#175849]"><GitCompareArrows aria-hidden="true" size={16} />与上次就诊比较</button>
                  ) : null}
                  <button type="button" onClick={() => setSourceOpen(!sourceOpen)} className="focus-ring inline-flex items-center gap-2 rounded-xl border border-stone-300 bg-white px-3 py-2 text-sm text-stone-700"><FileImage aria-hidden="true" size={16} />原始资料<ChevronDown aria-hidden="true" size={15} /></button>
                  <button type="button" onClick={toggleQuickNote} className="focus-ring inline-flex items-center gap-2 rounded-xl border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-800"><ClipboardPlus aria-hidden="true" size={16} />学生补录</button>
                  <button type="button" onClick={toggleTeacherNote} className="focus-ring inline-flex items-center gap-2 rounded-xl border border-[#9bc5b8] bg-[#eef7f3] px-3 py-2 text-sm font-medium text-[#175849]"><BookOpenText aria-hidden="true" size={16} />跟师笔记</button>
                </section>

                {teacherNoteOpen ? (
                  <TeacherNotePanel
                    patientId={patientId}
                    encounterId={selectedDetail.id}
                    onDirtyChange={handleTeacherDirtyChange}
                  />
                ) : null}

                {sourceOpen ? (
                  <div className="overflow-hidden rounded-xl border border-stone-300 bg-stone-100">
                    <div className="flex items-center justify-between border-b border-stone-300 px-4 py-2 text-xs text-stone-600"><span>受保护原始资料</span><a href={apiContentUrl(selectedDetail.document_url)} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium text-[#1f6857]">新窗口打开<ExternalLink size={13} /></a></div>
                    <p className="border-b border-stone-200 bg-white px-4 py-2 text-xs leading-5 text-stone-500">
                      当前入口可核对：原始页面图像。OCR 全文查看与字段级证据定位暂未提供；处方表中的文字即来自该图像的识别结果。
                    </p>
                    <iframe title={`${patient.internal_patient_code} 原始病历`} src={apiContentUrl(selectedDetail.document_url)} className="h-[520px] w-full bg-white" />
                  </div>
                ) : null}

                {noteOpen ? (
                  <form onSubmit={(event) => addNote(event, selectedDetail.id)} className="rounded-xl border border-blue-200 bg-blue-50 p-4">
                    {noteBufferOffers.length ? (
                      <div role="status" className="mb-3 rounded-xl border border-blue-300 bg-white px-3 py-2 text-xs text-blue-900">
                        <p className="font-semibold">检测到 {noteBufferOffers.length} 份本机留存的补录草稿（每个标签页独立保留，互不覆盖，不会自动删除）：</p>
                        <ul className="mt-2 space-y-2">
                          {noteBufferOffers.map((record) => {
                            const isResumeTarget = Boolean(
                              noteOpen &&
                                initialResumeSessionId &&
                                record.sessionId === initialResumeSessionId,
                            );
                            return (
                              <li
                                key={record.sessionId}
                                className={`flex flex-wrap items-center justify-between gap-2 rounded-lg px-2 py-1 ${isResumeTarget ? "ring-2 ring-blue-500 bg-blue-100/70" : ""}`}
                              >
                                <span className="min-w-0 flex-1 truncate">
                                  {isResumeTarget ? (
                                    <span className="mr-2 inline-block rounded bg-blue-700 px-1.5 py-0.5 text-[11px] font-semibold text-white">继续目标</span>
                                  ) : null}
                                  「{record.content.length > 16 ? `${record.content.slice(0, 16)}…` : record.content}」 · {new Date(record.savedAt).toLocaleString("zh-CN")} · {record.content.length} 字
                                </span>
                                <span className="flex shrink-0 gap-2">
                                  <button type="button" onClick={() => restoreNoteBuffer(record)} className="focus-ring rounded-lg bg-blue-700 px-2.5 py-1 text-[11px] font-semibold text-white">恢复草稿</button>
                                  <button type="button" onClick={() => discardNoteBuffer(record)} className="focus-ring rounded-lg border border-blue-300 bg-white px-2.5 py-1 text-[11px] text-blue-800">丢弃</button>
                                </span>
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    ) : null}
                    <div className="grid gap-3 sm:grid-cols-[180px_1fr]">
                      <select ref={noteKindRef} name="note_kind" aria-label="补录类型" disabled={busy !== ""} className="focus-ring rounded-xl border border-blue-200 bg-white px-3 py-2 text-sm"><option value="tongue">舌象</option><option value="pulse">脉象</option><option value="teacher_oral">老师口头信息</option><option value="student_observation">自己观察</option><option value="student_interpretation">个人理解</option></select>
                      <textarea
                        name="content"
                        aria-label="学生补录内容"
                        disabled={busy !== ""}
                        required
                        placeholder="补录内容会明确标记为学生补录，不与医生病历混合。"
                        rows={3}
                        value={noteDraft}
                        onChange={(event) => setNoteDraft(event.target.value)}
                        className="focus-ring rounded-xl border border-blue-200 bg-white p-3 text-sm"
                      />
                    </div>
                    {noteFormError ? (
                      <p role="alert" className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{noteFormError}</p>
                    ) : null}
                    <button type="submit" disabled={busy === `note-${selectedDetail.id}`} className="focus-ring mt-3 inline-flex items-center gap-2 rounded-xl bg-blue-700 px-3 py-2 text-sm font-semibold text-white"><Plus size={15} />保存新版本</button>
                  </form>
                ) : null}

                {diffRequest ? (
                  <div className="rounded-2xl border border-[#bcd8ce] bg-[#f4faf7] p-1">
                    <div className="flex items-center justify-between px-4 pt-3">
                      <p className="text-xs font-bold tracking-wide text-[#175849]">与上次就诊对比结果</p>
                      <button type="button" onClick={() => setDiffRequest(null)} className="focus-ring inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-stone-500 hover:bg-white"><X aria-hidden="true" size={13} />关闭对比</button>
                    </div>
                    <PrescriptionDiffPanel
                      encounters={summaries}
                      requestedPair={diffRequest}
                    />
                  </div>
                ) : null}
                  </>
                ) : null}

                <nav aria-label="就诊切换" className="flex items-center justify-between gap-3 border-t border-stone-200 pt-4 text-sm">
                  {olderSummary ? (
                    <button type="button" onClick={() => selectEncounter(olderSummary.id)} className="focus-ring inline-flex items-center gap-1 rounded-xl border border-stone-300 bg-white px-3 py-2 text-stone-700"><ChevronLeft aria-hidden="true" size={15} />上一次 {formatDay(olderSummary.encounter_at)}</button>
                  ) : <span className="text-xs text-stone-400">这是最早一次就诊</span>}
                  {newerSummary ? (
                    <button type="button" onClick={() => selectEncounter(newerSummary.id)} className="focus-ring inline-flex items-center gap-1 rounded-xl border border-stone-300 bg-white px-3 py-2 text-stone-700">{formatDay(newerSummary.encounter_at)} 下一次<ChevronRight aria-hidden="true" size={15} /></button>
                  ) : <span className="text-xs text-stone-400">这是最近一次就诊</span>}
                </nav>
              </div>
            </article>

            <section aria-labelledby="clinical-tools-title" className="mt-8">
              <h2 id="clinical-tools-title" className="text-xl font-bold text-stone-900">病程分析与医案整理</h2>
              <p className="mt-2 max-w-3xl text-sm leading-6 text-stone-500">
                患者级工具读取已记录事实，沿用原有 Phase 7、8、9 逻辑；跨就诊写作也可在「写作」库中继续。
              </p>

              {summaries.length >= 2 ? (
                <details id="longitudinal-analysis-tool" className="group mt-4">
                  <summary className="surface focus-ring flex cursor-pointer list-none items-center justify-between rounded-2xl px-5 py-4 font-semibold text-stone-800">
                    <span className="flex items-center gap-2"><BrainCircuit aria-hidden="true" size={18} className="text-[#1f6857]" />Longitudinal Analysis · 病程分析</span>
                    <ChevronDown aria-hidden="true" size={17} className="transition-transform group-open:rotate-180" />
                  </summary>
                  <LongitudinalAnalysisPanel patientId={patientId} encounters={summaries} />
                </details>
              ) : null}

              <details id="case-record-tool" className="group mt-4">
                <summary className="surface focus-ring flex cursor-pointer list-none items-center justify-between rounded-2xl px-5 py-4 font-semibold text-stone-800">
                  <span className="flex items-center gap-2"><ScrollText aria-hidden="true" size={18} className="text-[#1f6857]" />Case Record · 医案整理</span>
                  <ChevronDown aria-hidden="true" size={17} className="transition-transform group-open:rotate-180" />
                </summary>
                <p className="mt-4 rounded-xl border border-stone-200 bg-[#faf8f2] px-4 py-3 text-xs leading-5 text-stone-600">
                  先选择就诊范围，再生成跨就诊医案草稿；单次就诊的跟师笔记入口在上方就诊卡片内。
                </p>
                <CaseRecordPanel patientId={patientId} encounters={summaries} />
              </details>
            </section>

            <section aria-labelledby="works-title" className="mt-8">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 id="works-title" className="text-xl font-bold text-stone-900">该患者的师承作品</h2>
                <Link href="/notes" className="focus-ring inline-flex items-center gap-1 text-sm text-[#1f6857]">打开写作库<ExternalLink aria-hidden="true" size={13} /></Link>
              </div>
              {worksError && works === null ? (
                <div role="alert" className="mt-3 rounded-2xl border border-red-200 bg-red-50 px-5 py-4 text-sm text-red-700">
                  <p>作品列表读取失败：{worksError}</p>
                  <button type="button" onClick={() => void loadWorks(worksRetryOffset)} className="focus-ring mt-2 rounded-xl border border-red-300 bg-white px-3 py-1.5 text-xs">重试加载</button>
                </div>
              ) : works === null ? (
                <div className="mt-3 h-16 animate-pulse rounded-2xl bg-white/60" />
              ) : works.length ? (
                <ul className="surface mt-3 divide-y divide-stone-200 rounded-2xl">
                  {works.map((work) => (
                    <li key={work.note_id}>
                      <Link href={`/notes/${work.note_id}`} className="focus-ring flex items-center gap-3 px-4 py-3.5 hover:bg-[#faf8f2] sm:px-5">
                        <FileText aria-hidden="true" className="shrink-0 text-[#1f6857]" size={17} />
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-semibold text-stone-800">
                            {work.latest.title || noteTypeLabels[work.note_type] || work.note_type}
                            <span className="ml-2 rounded bg-[#eef7f3] px-1.5 py-0.5 text-[11px] font-medium text-[#175849]">{noteTypeLabels[work.note_type] ?? work.note_type}</span>
                          </p>
                          <p className="mt-0.5 text-xs text-stone-500">
                            {work.version_count} 个版本 · 最近：{versionTypeLabels[work.latest.version_type] ?? work.latest.version_type} · {new Date(work.latest.created_at).toLocaleString("zh-CN", { dateStyle: "short", timeStyle: "short" })}
                            {work.latest_final ? " · 已有定稿" : " · 尚未定稿"}
                          </p>
                          {work.evidence_window && work.evidence_window.later_encounter_count > 0 ? (
                            <p className="mt-1 text-xs text-amber-700">
                              此后有 {work.evidence_window.later_encounter_count} 次新就诊未包含在该作品中。
                            </p>
                          ) : null}
                        </div>
                        <ChevronRight aria-hidden="true" className="shrink-0 text-stone-400" size={16} />
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-3 rounded-2xl border border-dashed border-stone-300 px-5 py-6 text-center text-sm text-stone-500">
                  还没有该患者的跟师笔记或医案；在上方就诊卡片点「跟师笔记」，或在医案整理中选择就诊范围。
                </p>
              )}
              {worksError && works !== null ? (
                <div role="alert" className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-xs text-red-700">
                  <span>作品列表翻页失败（已保持在当前页）：{worksError}</span>
                  <button
                    type="button"
                    disabled={worksLoading}
                    onClick={() => void loadWorks(worksRetryOffset)}
                    className="focus-ring rounded-lg border border-red-300 bg-white px-2.5 py-1 text-red-700 disabled:opacity-40"
                  >
                    重试本页
                  </button>
                </div>
              ) : null}
              {works !== null && works.length && worksTotal > works.length ? (
                <nav
                  aria-label="作品列表分页"
                  aria-busy={worksLoading}
                  className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-stone-500"
                >
                  <span>共 {worksTotal} 件作品 · 按最近更新排序</span>
                  <span className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={worksLoading || worksOffset === 0}
                      onClick={() => void loadWorks(worksOffset - WORKS_PAGE_SIZE)}
                      className="focus-ring rounded-lg border border-stone-300 bg-white px-2.5 py-1 disabled:opacity-40"
                    >
                      上一页
                    </button>
                    <span>
                      第 {worksOffset / WORKS_PAGE_SIZE + 1} / {Math.ceil(worksTotal / WORKS_PAGE_SIZE)} 页
                    </span>
                    <button
                      type="button"
                      disabled={worksLoading || worksOffset + works.length >= worksTotal}
                      onClick={() => void loadWorks(worksOffset + WORKS_PAGE_SIZE)}
                      className="focus-ring rounded-lg border border-stone-300 bg-white px-2.5 py-1 disabled:opacity-40"
                    >
                      {worksLoading ? "加载中…" : "下一页"}
                    </button>
                  </span>
                </nav>
              ) : null}
              <p className="mt-2 flex items-center gap-1 text-xs text-stone-400">
                <Stamp aria-hidden="true" size={12} />
                作品在写作库统一管理，历史版本永久保留。
              </p>
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
