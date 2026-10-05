# 候选验证记录

版本 v0.2.0-rc.1，2026-10-05。以下记录公开发布前完成的本地工程验收；GitHub 实际运行结果以[仓库 Actions](https://github.com/Brighthao18/SHEN-Clinical-Apprenticeship-Hub/actions)为准，本地检查不能替代线上运行。

| 检查 | 实际范围与结果 |
| --- | --- |
| Windows x64 干净克隆 | 本项目隔离安装成功；后端 SQLite 344通过，PostgreSQL 343通过/1项按设计跳过 |
| Linux amd64 容器 | 从 Git 源码安装 Python 3.12 锁定依赖；后端 SQLite 344通过，PostgreSQL 343通过/1项按设计跳过；Ruff 通过 |
| 前端 | Windows 和 Linux 各90项单元/运行锁检查通过；类型、lint、生产构建通过；时间显示测试覆盖 UTC 与 Asia/Shanghai |
| 浏览器 | Windows Chrome 两阶段既有 E2E 共12项通过；干净副本演示的登录、工作台、患者时间线通过，无页面异常，截图仅含合成资料 |
| 迁移 | 隔离 PostgreSQL 空库 Alembic 升级通过，metadata check 无待生成操作；本次无业务 schema 变更 |
| 容器与入口 | Docker Engine 29.8.1 的 Linux amd64 容器健康；宿主实际浏览器可访问 localhost 演示；仅 Web 发布本机端口，后端只接 internal 网络，API/worker 的外部 socket 连接受阻 |
| 新正式配置 | 使用新建合成密钥与空库的独立测试项目，初始化、验证、认证和容器健康通过；真实服务默认关闭，无演示挂载/注册表，DB/队列不发布端口 |
| 公开内容 | 991项公开文件，允许清单和许可文件 hash 校验通过；旧 private Git 历史未复制；构建上下文和镜像所有层通过禁止路径及合成哨兵检查 |

PostgreSQL 跳过项是孤儿 unresolved ledger 的防漂移分支：真实外键不允许来源文档凭空消失，故该分支仅用 SQLite 测试。不是临床路径被忽略。

合成演示经认证接口核对1患者、2就诊、2处方、6药物项、3来源文档、1 unresolved；未解决记录不绑定临床行，三份来源的返回字节及 SHA-256 与种子一致，未登录访问临床接口返回401。种子按明确编写的四个映射走既有确认接口，茯苓保留 pending/未知剂量，不录入医院规格。确定性对比核对加入1、移除1、增量1、炮制变化1、未知剂量1；白术10g到炒白术20g仍按每剂解释。追加原稿、草稿、最终版后原稿及依据 hash 保留；重复种子不增加临床行、不覆盖作品。

Valkey 验证真实 Celery broker/result backend：待处理任务经 AOF 队列重启后完成，结果 TTL 为604800秒；独立合成探针验证失败重试共3次和结果实际过期。合成资料完成既有备份、21文件 hash 清单检查、28表隔离 PostgreSQL 恢复，临时恢复库已移除。restic 加密仓库的实际演练未执行。

最终 API/Web 镜像各11层经检查，没有禁止私有路径、合成哨兵或存储文件，均使用非 root 运行用户。最终 rootfs 的 CycloneDX SBOM 在本地交付的验收附件中；源码依赖清单覆盖497个精确版本组件及其许可材料。镜像内容检查和 SBOM 不等于全基础镜像 CVE 审计。

跨平台验收单独发现并修复了存储键反斜杠/Windows drive 在 POSIX 上被当作字面文件名的边界问题，新增4个边界反例。现有82个其余后端与迁移文件经 LF 统一后与改造前源码一致；配置文件仅更新版本，临床抽取、校验、匹配、剂量、对比、分析与写作算法保持。后端、前端检查不构成临床准确率结论。

这些操作只使用从零构造的资料与独立库，没有访问原真实生产环境、病例、备份或云端临床模型。公开树检查只针对允许清单、已知密钥模式和链接，不能证明任意敏感文本都不存在。依赖审计范围和1项遗留开发依赖问题见 [dependency-security.md](dependency-security.md)。macOS/ARM/NAS、真实 OCR/AI 和 restic 实测未验证。本记录不包含 GitHub 实际 CI 和 Linux 完整浏览器 E2E；线上执行结果另见仓库 Actions 与版本发布说明。
