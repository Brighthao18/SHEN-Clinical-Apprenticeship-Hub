# 本机正式部署与恢复

首版一台设备一个正式实例，localhost 入口；课题组成员分别部署。需要 Docker Engine 28+、Docker Compose v2.24.4+ 和 Python 3.12。安装、初始化、启动命令均从仓库根目录执行：

```sh
python scripts/shen.py doctor
python scripts/shen.py setup
python scripts/shen.py init
python scripts/shen.py start
```

`setup` 仅创建当前目录的 `work/venv`，安装 hash 锁定依赖；不改变全局 Python。`init` 需要本机交互式终端，管理员密码不显示。它生成被忽略的 `.env.production.local`、独立生产资料目录及备份密钥；已有文件拒绝覆盖。文件含密钥，不要提交、粘贴或上传。保存 PII 加密密钥、HMAC 密钥与 restic 密码的可恢复副本。

Web 默认 <http://localhost:3100>，API 仅 <http://localhost:8100>。PostgreSQL 与 Valkey 不发布宿主端口。正式卷替换开发挂载，不加载演示注册表或合成种子；空 mock 注册表使未知资料拒绝。首次启动不具备真实 OCR/AI，先阅读[四条数据边界](privacy-boundaries.md)，在设置页逐服务配置并确认。已有其他部署的授权不会复制。

环境 bootstrap 仅是替代入口。启用 MinerU 需 `OCR_PROVIDER=mineru` 和 `ALLOW_IDENTIFIABLE_MINERU_UPLOAD=true` 及凭证；启用真实抽取需 `AI_EXTRACTION_PROVIDER=live_openai_compatible`、对应配置和 `AI_EXTRACTION_ALLOW_IDENTIFIABLE_INPUT=true`。Vision 功能还需 `AI_VISION_VERIFICATION_ENABLED=true`，原图上传需专用确认/开关。API 与 worker 接收相同开关。设置页已保存的运行时服务配置优先级和持久性见[适配说明](provider-adapters.md)。仅填 Key 不构成授权。

生产状态的 BACKUP_MANIFEST_DIR 可指向本次部署的备份目录，用于备份状态显示；它不会自动创建备份。不要为展示状态填入其他部署路径。修改当前环境后重新启动服务，`start` 先验证当前配置。

## 备份与恢复

备份是独立操作，不随应用启动自动执行。Windows 使用 `work/venv/Scripts/python.exe`，Linux 使用 `work/venv/bin/python`。下面展示 Windows 命令，`<backup-directory>` 替换为 create 返回的本机目录，不保留尖括号。

```powershell
work/venv/Scripts/python.exe scripts/clinical_backup.py create --environment production --tier daily
work/venv/Scripts/python.exe scripts/clinical_backup.py verify --environment production --backup-dir <backup-directory>
work/venv/Scripts/python.exe scripts/clinical_backup.py restore-test --environment production --backup-dir <backup-directory>
```

内层备份包含数据库导出、文档、配置清单、逐文件 hash 和行数；恢复验证使用新建隔离数据库，不覆盖原库。真实备份只能存放在受保护的私有目录。restic 为额外加密仓库，需要本机另装可信 restic；`scripts/restic_backup.py --help` 说明入口。restic 不代替内层完整性与隔离 PostgreSQL 恢复。本候选验证仅使用合成资料，不读取现有真实备份。

停止服务保留数据用 `python scripts/shen.py stop`。不要对正式部署执行删除卷命令。升级前完成可核验备份和隔离恢复；本版本不新增业务迁移，但仍执行现有 Alembic 升级。已有配置和数据库不能由公开包覆盖。应用回退应使用相同旧镜像/依赖；未来若改表，仅退镜像无法撤回数据迁移，应从已验证备份隔离恢复后核对。

首版没有同机多个正式实例的隔离承诺。移动端优先私有网络访问；公网、反向代理、HTTPS 或多人部署需要另做配置验证。
