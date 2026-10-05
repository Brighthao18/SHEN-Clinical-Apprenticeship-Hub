// 工作台的深链契约。进行中作品列表依赖数据库侧 finalized 过滤：最新版本
// 未定稿即进行中（曾定稿后又追加新草稿的作品仍会列出），本地不再做二次
// 过滤，较早的进行中作品不会被最近的定稿挤出。
export const inProgressNotesQuery = "/api/v1/notes?finalized=false&limit=3";

// 最近病例卡片必须定位到卡片展示的那次就诊；不带深链会落到患者默认就诊，
// 日期与主诉就会对不上。
export function recentEncounterHref(patientId: string, encounterId: string): string {
  return `/patients/${encodeURIComponent(patientId)}?encounter=${encodeURIComponent(encounterId)}`;
}
