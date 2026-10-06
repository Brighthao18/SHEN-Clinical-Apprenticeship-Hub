# 依赖安全审阅

2026-10-05，针对本候选锁文件运行 pip-audit 2.10.1 和 pnpm audit。初始审计发现 PyJWT、Next.js 以及 brace-expansion 的已知问题；分别更新到 PyJWT 2.15.1、Next.js/eslint-config-next 16.3.8，锁定 brace-expansion 1.1.21/5.0.12，并重新运行认证、后端、前端及容器检查。没有修改临床算法。

2026-10-06，pnpm audit 新报告 `source-map-js@1.2.1` 的 [GHSA-68fv-2mgg-jv7q](https://github.com/advisories/GHSA-68fv-2mgg-jv7q)（high，索引 source map 偏移可阻塞事件循环），生产路径为 next -> postcss -> source-map-js。按 brace-expansion 先例以 override 锁定已修复的 1.2.2：锁文件只改该包；用 `scripts/update_notices.py` 的同一生成函数只更新该组件的 inventory、SBOM 与许可文件，下载包经注册表与锁 integrity 双重校验，许可仍为 BSD-3-Clause 且许可原文与 1.2.1 逐字节相同。

更新后的 Python runtime/dev 审计和前端 production 审计均未报告已知问题。它们是特定审计来源和日期的结果，不证明不存在未知漏洞，也不覆盖基础镜像全部系统组件。

完整前端开发依赖仍有 `braces@3.0.3` 的 [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm)：深嵌套 glob 可耗尽栈。注册表尚未发布建议的 3.0.4。本项目锁中的路径为 eslint-config-next -> eslint-plugin-next -> fast-glob -> micromatch -> braces，仅用于开发 lint；生产依赖审计没有该路径。预览版保留精确记录：不把外部输入用作 lint glob，CI 有时间限制，待上游修复版发布后更新。未通过宽泛忽略或降低风险等级制造“全依赖零漏洞”的结论。

固定摘要的 Python/Node/PostgreSQL/Valkey 镜像及最终应用 rootfs 另有版本/SBOM 清单；本轮不宣称完成全基础镜像 CVE 审计。源码包不包含 node_modules、系统镜像或模型权重。公开发布时重新审计并检查其当日修复状态。
