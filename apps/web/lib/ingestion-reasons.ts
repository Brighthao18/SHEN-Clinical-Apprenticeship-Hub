import translations from "./ingestion-reasons.json";

export type IngestionReasonExplanation = {
  title: string;
  happened: string;
  database: string;
  action: string;
};

const reasonTranslations = translations as Record<string, IngestionReasonExplanation>;

export function getIngestionReasonExplanation(
  code: string | null,
  status: string,
): IngestionReasonExplanation {
  if (code && reasonTranslations[code]) return reasonTranslations[code];

  if (status === "FAILED") {
    return {
      title: "自动处理失败",
      happened: "系统遇到尚未翻译的处理错误，已安全停止本次任务。",
      database: "该病例未完成临床导入。",
      action: "请保留下面的技术代码并联系维护者排查；不要手工强制导入。",
    };
  }

  return {
    title: "存在未识别的安全阻断原因",
    happened: "系统遇到尚未翻译的阻断原因，因此按安全规则停止导入。",
    database: "该病例未进入临床数据库。",
    action: "无需手工修改或强制导入；请保留下面的技术代码供维护者排查。",
  };
}

export function hasKnownIngestionReason(code: string | null): boolean {
  return Boolean(code && reasonTranslations[code]);
}
