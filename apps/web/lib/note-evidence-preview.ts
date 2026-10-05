/** 版本依据快照的只读摘要；NULL = 历史版本未保存快照（不按当前资料补建）。 */
export type NoteEvidenceSnapshotSummary = {
  set_version: number;
  context_version: string;
  prescription_diff_algorithm_version: string | null;
  context_hash: string;
  encounter_count: number;
  symptom_count: number;
  diagnosis_count: number;
  prescription_item_count: number;
  student_note_count: number;
  has_prescription_diff: boolean;
};

/** “材料变化”的三类拆解：范围增减之外的两组原因，以及不可比较的降级说明。 */
export type MaterialsBreakdown = {
  /** false 时 not_comparable_reason 说明为什么无法比较（绝不回填历史快照）。 */
  comparable: boolean;
  not_comparable_reason:
    | "legacy_no_snapshot"
    | "base_set_unavailable"
    | "base_ambiguous"
    | null;
  /** 按当前规则重算“原依据范围”，与该版本存储快照不一致 = 已有资料变化。 */
  kept_materials_changed: boolean;
  /** 存储快照的各层版本号与当前规则版本的差异；非空时哈希差异不可直接归因。 */
  rule_versions_changed: Record<string, { stored: unknown; current: unknown }>;
};

const RULE_VERSION_LABELS: Record<string, string> = {
  set_version: "依据快照格式",
  context_version: "写作上下文",
  prescription_diff_algorithm_version: "处方对比算法",
  deidentification_version: "脱敏规则",
};

/**
 * 把拆解结果变成面板里的解释行；空数组 = 没有需要单独说明的变化。
 * 范围增减（拟新增/拟移除）由 proposal.added/removed 呈现，不在这里重复。
 */
export function describeMaterialsBreakdown(
  breakdown: MaterialsBreakdown,
): string[] {
  if (!breakdown.comparable) {
    switch (breakdown.not_comparable_reason) {
      case "legacy_no_snapshot":
        return ["该版本未保存依据快照（历史版本），无法自动比较已有资料变化；就诊增减仍然有效。"];
      case "base_set_unavailable":
        return ["该版本的原依据就诊无法解析（就诊不存在或范围不完整），无法比较已有资料变化。"];
      case "base_ambiguous":
        return ["该版本的原依据范围存在无法唯一配对的处方对比，无法比较已有资料变化。"];
      default:
        return ["暂时无法自动比较已有资料变化。"];
    }
  }
  const lines: string[] = [];
  if (breakdown.kept_materials_changed) {
    lines.push("该版本已采用的资料与当前数据相比已有变化（如就诊内容更新、新增补录）。");
  }
  const ruleKeys = Object.keys(breakdown.rule_versions_changed);
  if (ruleKeys.length) {
    const labels = ruleKeys
      .map((key) => RULE_VERSION_LABELS[key] ?? key)
      .join("、");
    lines.push(
      `整理规则版本已更新（${labels}）；材料哈希差异可能来自规则或资料，以下对比按当前规则计算。`,
    );
  }
  return lines;
}

/** POST /notes/{id}/evidence-preview 的只读响应：先看清楚，再决定是否采用。 */
export type EvidencePreviewData = {
  note_id: string;
  note_type: string;
  /** 预览绑定（并回显）所基于的版本：增减数量相对它计算，采用必须同版本。 */
  based_on_version: {
    id: string;
    version: number;
    version_type: string;
  };
  latest_version: {
    id: string;
    version: number;
  };
  current: {
    evidence_encounters: string[];
    source_context_hash: string;
    evidence_snapshot: NoteEvidenceSnapshotSummary | null;
  };
  patient_encounters: Array<{
    id: string;
    encounter_at: string;
    chief_complaint_raw: string | null;
  }>;
  proposal: {
    evidence_encounters: string[];
    added: string[];
    removed: string[];
    source_context_hash: string;
    evidence_snapshot: NoteEvidenceSnapshotSummary | null;
    /** 预览凭证：采用请求必须原样带回；服务端重算比对，材料已变即拒绝。 */
    preview_token: string;
  };
  set_changed: boolean;
  materials_changed: boolean;
  /** 材料变化的三类拆解（范围增减之外：已有资料变化 / 规则版本变化）。 */
  materials_breakdown: MaterialsBreakdown;
};

/** 集合相等（顺序无关）：勾选按时间线排序，服务端按 ID 排序，二者同集。 */
export function sameEncounterSet(
  a: readonly string[],
  b: readonly string[],
): boolean {
  if (a.length !== b.length) return false;
  const pool = new Set(a);
  return b.every((id) => pool.has(id));
}

/**
 * 响应归属保护：只有“回显版本 = 当前所选版本，且回显集合 = 当前勾选集合”
 * 的预览才允许显示或用于确认。乱序返回的旧响应（先发后至、或勾选已再变）
 * 在这里被拒绝，界面展示的增减数量永远描述用户此刻的选择。
 */
export function isPreviewForSelection(
  preview: EvidencePreviewData,
  selectedVersionId: string,
  draftIds: readonly string[],
): boolean {
  return (
    preview.based_on_version.id === selectedVersionId &&
    sameEncounterSet(preview.proposal.evidence_encounters, draftIds)
  );
}
