import type { Metadata } from "next";

import { ReviewQueue } from "./review-queue";

export const metadata: Metadata = { title: "兼容审查记录" };

export default function ReviewsPage() {
  return (
    <div>
      <p className="mb-4 text-xs leading-5 text-stone-500">
        这里的兼容字段记录是旧 ReviewItem 记录，与导入终态「未解决」不是同一队列；中药词典 PENDING 仅表示已导入药名尚待标准化。本页属于维护入口，日常导入无需处理；导入结果里的「处理异常字段」会直接跳转到这里。
      </p>
      <ReviewQueue />
    </div>
  );
}
