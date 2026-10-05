import { listAllNoteEditBuffers } from "./note-edit-buffer.ts";
import { listAllStudentNoteBuffers } from "./student-note-buffer.ts";
import { listPendingSaves } from "./save-request.ts";

/**
 * 工作台「继续工作」的本机聚合：把散在各编辑器里的本地状态（未保存编辑
 * 缓冲、结果未核对的保存）汇总成可点击的工作条目。只做聚合与排序，绝不
 * 自动恢复、自动补交或删除——处理动作仍在对应编辑页里。
 *
 * 定位规则（2026-09-09 起）：
 * - 同一编辑会话（sessionId）在同一对象上的缓冲与待核对保存归并为一个
 *   工作条目；不同会话、不同内容绝不合并——本模块从不丢弃或改写任何记录。
 * - 补录记录自带 patientId 时直接构造患者页深链；旧记录只带 encounterId，
 *   标记 needsResolution，由工作台经受保护的 encounter-owner 查询解析归属，
 *   解析失败时如实提示，绝不猜测。
 * - 恢复链接携带 resume=<sessionId>，编辑页只据此高亮对应记录，恢复动作
 *   仍由用户在编辑页显式完成。
 */

export type LocalContinueItem = {
  /** Stable list key. */
  key: string;
  kind: "editing" | "verifying";
  /** Which editor owns the resume action. */
  family: "note" | "student_note";
  noteId?: string;
  encounterId?: string;
  patientId?: string;
  resumeSessionId?: string;
  /** Deep link when constructible locally; null while patientId is unknown. */
  href: string | null;
  /** True when the owner must be resolved via the encounter-owner lookup. */
  needsResolution: boolean;
  /** 面向用户的描述，不暴露技术细节。 */
  detail: string;
  savedAt: string;
};

function preview(text: string): string {
  return text.length > 16 ? `${text.slice(0, 16)}…` : text;
}

type Group = {
  key: string;
  family: "note" | "student_note";
  noteId?: string;
  encounterId?: string;
  patientId?: string;
  sessionId?: string;
  parts: string[];
  hasPending: boolean;
  savedAt: string;
};

function materialize(group: Group): LocalContinueItem {
  let href: string | null = null;
  let needsResolution = false;
  if (group.family === "note" && group.noteId) {
    href = group.sessionId
      ? `/notes/${group.noteId}?resume=${encodeURIComponent(group.sessionId)}`
      : `/notes/${group.noteId}`;
  } else if (group.family === "student_note" && group.encounterId) {
    if (group.patientId) {
      href =
        `/patients/${group.patientId}?encounter=${encodeURIComponent(group.encounterId)}` +
        (group.sessionId ? `&resume=${encodeURIComponent(group.sessionId)}` : "");
    } else {
      // 旧记录只带 encounterId：工作台先解析归属，再决定能否直达。
      needsResolution = true;
    }
  }
  return {
    key: group.key,
    kind: group.hasPending ? "verifying" : "editing",
    family: group.family,
    noteId: group.noteId,
    encounterId: group.encounterId,
    patientId: group.patientId,
    resumeSessionId: group.sessionId,
    href,
    needsResolution,
    detail: group.parts.join("；"),
    savedAt: group.savedAt,
  };
}

export function collectLocalContinueWork(): LocalContinueItem[] {
  const groups = new Map<string, Group>();

  function groupFor(
    key: string,
    family: "note" | "student_note",
    ids: { noteId?: string; encounterId?: string; patientId?: string; sessionId?: string },
  ): Group {
    const existing = groups.get(key);
    if (existing) {
      if (!existing.patientId && ids.patientId) existing.patientId = ids.patientId;
      return existing;
    }
    const created: Group = {
      key,
      family,
      noteId: ids.noteId,
      encounterId: ids.encounterId,
      patientId: ids.patientId,
      sessionId: ids.sessionId,
      parts: [],
      hasPending: false,
      savedAt: "",
    };
    groups.set(key, created);
    return created;
  }

  function touch(group: Group, savedAt: string) {
    if (!group.savedAt || group.savedAt < savedAt) group.savedAt = savedAt;
  }

  for (const buffer of listAllNoteEditBuffers()) {
    const group = groupFor(`note:${buffer.noteId}:${buffer.sessionId}`, "note", {
      noteId: buffer.noteId,
      sessionId: buffer.sessionId,
    });
    group.parts.push(`未保存编辑 · 基于 v${buffer.version} · ${buffer.text.length} 字`);
    touch(group, buffer.savedAt);
  }

  for (const buffer of listAllStudentNoteBuffers()) {
    const group = groupFor(`student:${buffer.encounterId}:${buffer.sessionId}`, "student_note", {
      encounterId: buffer.encounterId,
      patientId: buffer.patientId,
      sessionId: buffer.sessionId,
    });
    group.parts.push(`未保存补录「${preview(buffer.content)}」 · ${buffer.content.length} 字`);
    touch(group, buffer.savedAt);
  }

  for (const entry of listPendingSaves()) {
    if (entry.kind === "note_revision" && entry.noteId) {
      const group = entry.sessionId
        ? groupFor(`note:${entry.noteId}:${entry.sessionId}`, "note", {
            noteId: entry.noteId,
            sessionId: entry.sessionId,
          })
        : groupFor(`pending:${entry.requestId}`, "note", { noteId: entry.noteId });
      let detail = "保存结果待核对";
      try {
        const parsed = JSON.parse(entry.body) as { content?: unknown; final?: unknown };
        const text = typeof parsed.content === "string" ? parsed.content : "";
        detail = `保存结果待核对 · 「${preview(text)}」 · ${parsed.final ? "最终版" : "草稿"}`;
      } catch {
        // Malformed body keeps the generic detail.
      }
      group.parts.push(detail);
      group.hasPending = true;
      touch(group, entry.savedAt);
    } else if (entry.kind === "student_note" && entry.encounterId) {
      const group = entry.sessionId
        ? groupFor(`student:${entry.encounterId}:${entry.sessionId}`, "student_note", {
            encounterId: entry.encounterId,
            patientId: entry.patientId,
            sessionId: entry.sessionId,
          })
        : groupFor(`pending:${entry.requestId}`, "student_note", {
            encounterId: entry.encounterId,
            patientId: entry.patientId,
          });
      let detail = "保存结果待核对";
      try {
        const parsed = JSON.parse(entry.body) as { content?: unknown };
        if (typeof parsed.content === "string") {
          detail = `保存结果待核对 · 「${preview(parsed.content)}」`;
        }
      } catch {
        // Malformed body keeps the generic detail.
      }
      group.parts.push(detail);
      group.hasPending = true;
      touch(group, entry.savedAt);
    }
  }

  return [...groups.values()]
    .map(materialize)
    .sort((a, b) => (a.savedAt < b.savedAt ? 1 : a.savedAt > b.savedAt ? -1 : 0));
}
