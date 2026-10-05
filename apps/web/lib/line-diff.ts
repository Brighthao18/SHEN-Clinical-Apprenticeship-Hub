export type DiffLineType = "same" | "added" | "removed";

export type DiffLine = {
  type: DiffLineType;
  text: string;
  /** 1-based line number in the old text; absent for added lines. */
  oldNumber?: number;
  /** 1-based line number in the new text; absent for removed lines. */
  newNumber?: number;
};

export type DiffRegion = {
  /** "changed" when the region contains at least one added/removed line. */
  type: "same" | "changed";
  lines: DiffLine[];
};

/** Guard so a pathological paste cannot trigger a huge O(n*m) DP table. */
const MAX_DIFF_LINES = 600;

/**
 * Line-level LCS diff of two note versions. Returns null when either side
 * exceeds the size guard; callers show a "内容过长" notice instead.
 */
export function diffLines(oldText: string, newText: string): DiffLine[] | null {
  // An empty string is zero lines, not one empty line.
  const oldLines = oldText === "" ? [] : oldText.split("\n");
  const newLines = newText === "" ? [] : newText.split("\n");
  if (oldLines.length > MAX_DIFF_LINES || newLines.length > MAX_DIFF_LINES) {
    return null;
  }

  const rows = oldLines.length;
  const cols = newLines.length;
  // lcs[i][j] = LCS length of oldLines[i..] vs newLines[j..]
  const lcs: Uint32Array[] = Array.from(
    { length: rows + 1 },
    () => new Uint32Array(cols + 1),
  );
  for (let i = rows - 1; i >= 0; i -= 1) {
    for (let j = cols - 1; j >= 0; j -= 1) {
      lcs[i][j] =
        oldLines[i] === newLines[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }

  const result: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < rows && j < cols) {
    if (oldLines[i] === newLines[j]) {
      result.push({ type: "same", text: oldLines[i], oldNumber: i + 1, newNumber: j + 1 });
      i += 1;
      j += 1;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      result.push({ type: "removed", text: oldLines[i], oldNumber: i + 1 });
      i += 1;
    } else {
      result.push({ type: "added", text: newLines[j], newNumber: j + 1 });
      j += 1;
    }
  }
  while (i < rows) {
    result.push({ type: "removed", text: oldLines[i], oldNumber: i + 1 });
    i += 1;
  }
  while (j < cols) {
    result.push({ type: "added", text: newLines[j], newNumber: j + 1 });
    j += 1;
  }
  return result;
}

const CONTEXT_LINES = 1;

/**
 * Collapse a full diff into changed regions with a little context, so the UI
 * shows “修改了什么” instead of reprinting the whole note. Long unchanged
 * stretches become one marker line.
 */
export function diffRegions(lines: DiffLine[]): Array<DiffRegion | { type: "gap"; count: number }> {
  const changed = lines.map((line) => line.type !== "same");
  const keep = new Array(lines.length).fill(false);
  lines.forEach((_, index) => {
    if (!changed[index]) return;
    for (let offset = Math.max(0, index - CONTEXT_LINES); offset <= Math.min(lines.length - 1, index + CONTEXT_LINES); offset += 1) {
      keep[offset] = true;
    }
  });

  const regions: Array<DiffRegion | { type: "gap"; count: number }> = [];
  let index = 0;
  while (index < lines.length) {
    if (!keep[index]) {
      let end = index;
      while (end < lines.length && !keep[end]) end += 1;
      regions.push({ type: "gap", count: end - index });
      index = end;
      continue;
    }
    const region: DiffRegion = { type: "same", lines: [] };
    while (index < lines.length && keep[index]) {
      region.lines.push(lines[index]);
      if (lines[index].type !== "same") region.type = "changed";
      index += 1;
    }
    regions.push(region);
  }
  return regions;
}
