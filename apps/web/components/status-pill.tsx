const labels: Record<string, string> = {
  UPLOADED: "已上传",
  QUEUED: "排队中",
  OCR_RUNNING: "OCR 处理中",
  OCR_COMPLETED: "OCR 完成",
  TEMPLATE_DETECTED: "模板已识别",
  PARSING: "解析中",
  STRUCTURING: "结构化中",
  PATIENT_MATCHING: "匹配患者",
  NORMALIZING: "标准化中",
  VALIDATING: "校验中",
  REVIEW_REQUIRED: "待确认",
  ACCEPTED: "自动完成",
  // 未解决的导入留待解析器升级后重放，无需人工处理。
  UNRESOLVED: "未解决（待重放）",
  FAILED: "处理失败",
  open: "待确认",
  resolved: "已解决",
  dismissed: "已忽略",
};

const styles: Record<string, string> = {
  ACCEPTED: "border-emerald-200 bg-emerald-50 text-emerald-800",
  REVIEW_REQUIRED: "border-amber-200 bg-amber-50 text-amber-800",
  UNRESOLVED: "border-slate-300 bg-slate-100 text-slate-700",
  FAILED: "border-red-200 bg-red-50 text-red-800",
  open: "border-amber-200 bg-amber-50 text-amber-800",
  resolved: "border-emerald-200 bg-emerald-50 text-emerald-800",
};

export function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-semibold ${
        styles[status] ?? "border-slate-200 bg-slate-50 text-slate-700"
      }`}
    >
      {labels[status] ?? status}
    </span>
  );
}
