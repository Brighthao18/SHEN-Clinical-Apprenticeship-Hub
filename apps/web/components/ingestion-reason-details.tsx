import { getIngestionReasonExplanation, hasKnownIngestionReason } from "@/lib/ingestion-reasons";

export function IngestionReasonDetails({ code, status }: { code: string | null; status: string }) {
  const explanation = getIngestionReasonExplanation(code, status);
  const unknownCode = Boolean(code && !hasKnownIngestionReason(code));

  return (
    <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-700">
      <p className="font-semibold text-slate-900">{explanation.title}</p>
      <dl className="mt-3 space-y-2 leading-6">
        <div>
          <dt className="inline font-semibold">发生了什么：</dt>
          <dd className="inline">{explanation.happened}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">是否已入库：</dt>
          <dd className="inline">{explanation.database}</dd>
        </div>
        <div>
          <dt className="inline font-semibold">是否需要操作：</dt>
          <dd className="inline">{explanation.action}</dd>
        </div>
      </dl>
      {code ? (
        <details className="mt-3 text-xs text-slate-500" open={unknownCode}>
          <summary className="cursor-pointer font-medium">技术排障代码</summary>
          <code className="mt-1 block break-all">{code}</code>
        </details>
      ) : null}
    </div>
  );
}
