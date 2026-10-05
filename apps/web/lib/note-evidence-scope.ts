/**
 * 正文范围的确定性检查：“正文待核对”状态的来源。
 *
 * 只对显式标识引用下结论，并区分两种性质：
 * - ``out_of_scope_encounter``：能明确识别为就诊（出现在已知就诊集合、
 *   但不在本版本依据范围内）——可以断言“范围外就诊”；
 * - ``unidentified``：识别不了类型的 UUID（可能是作品/文档等其他 ID）——
 *   只提示“未识别标识”，绝不断言它是范围外就诊。
 *
 * 扫描呈现文本是安全的：系统附录只含范围内 ID，永远不会命中。只定位
 * 提醒，绝不改写正文；普通叙述（如“第三诊…”）是否仍然成立由用户核对，
 * “保存最终版”即用户确认正文与所选依据一致。
 */

export type TextScopeFinding = {
  identifier: string;
  kind: "out_of_scope_encounter" | "unidentified";
  /** 定位：命中所在行号（1 起）与围绕命中位置的片段（约 80 字）。 */
  locations: Array<{ line: number; snippet: string }>;
  /** 该标识是否出现在 AI 证据结构化引用中（正文未提及时单独列出）。 */
  inStructuredRefs: boolean;
};

const UUID_PATTERN =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;

const MAX_LOCATIONS = 3;
const SNIPPET_WINDOW = 80;
/** 片段在命中点前的保留长度：让命中位置尽量落在片段中部。 */
const SNIPPET_CONTEXT_BEFORE = 24;

function snippetAround(line: string, hitIndex: number): string {
  const start = Math.max(0, hitIndex - SNIPPET_CONTEXT_BEFORE);
  const end = Math.min(line.length, start + SNIPPET_WINDOW);
  const prefix = start > 0 ? "…" : "";
  const suffix = end < line.length ? "…" : "";
  return `${prefix}${line.slice(start, end)}${suffix}`;
}

export function findOutOfScopeReferences(
  text: string,
  evidenceEncounterIds: readonly string[],
  options: {
    /** 已知就诊全集（如本作品全部版本引用过的就诊、患者就诊列表）。 */
    knownEncounterIds?: readonly string[];
    /** AI 证据结构化引用中的就诊 ID；也参与“已知就诊”判定。 */
    structuredRefIds?: readonly string[];
  } = {},
): TextScopeFinding[] {
  const inScope = new Set(evidenceEncounterIds.map((id) => id.toLowerCase()));
  const known = new Set(
    [...(options.knownEncounterIds ?? []), ...(options.structuredRefIds ?? [])].map((id) =>
      id.toLowerCase(),
    ),
  );
  const structured = new Set((options.structuredRefIds ?? []).map((id) => id.toLowerCase()));
  const findings = new Map<string, TextScopeFinding>();
  const locatedLines = new Map<string, Set<number>>();
  text.split("\n").forEach((line, index) => {
    for (const match of line.matchAll(UUID_PATTERN)) {
      const id = match[0].toLowerCase();
      if (inScope.has(id)) continue;
      const finding = findings.get(id) ?? {
        identifier: id,
        kind: known.has(id) ? ("out_of_scope_encounter" as const) : ("unidentified" as const),
        locations: [],
        inStructuredRefs: structured.has(id),
      };
      const seenLines = locatedLines.get(id) ?? new Set<number>();
      if (finding.locations.length < MAX_LOCATIONS && !seenLines.has(index)) {
        seenLines.add(index);
        locatedLines.set(id, seenLines);
        finding.locations.push({ line: index + 1, snippet: snippetAround(line, match.index ?? 0) });
      }
      findings.set(id, finding);
    }
  });
  for (const id of structured) {
    if (!inScope.has(id) && !findings.has(id)) {
      findings.set(id, {
        identifier: id,
        kind: "out_of_scope_encounter",
        locations: [],
        inStructuredRefs: true,
      });
    }
  }
  return [...findings.values()].sort((a, b) => a.identifier.localeCompare(b.identifier));
}
