# 开发与验证

使用 Python 3.12、Node.js 24 和 package.json 声明的 pnpm 版本。全部命令从根目录执行。

```sh
python scripts/shen.py setup
corepack enable
corepack pnpm install --frozen-lockfile
python scripts/shen.py test-api
corepack pnpm lint:web
corepack pnpm test:web
corepack pnpm build:web
```

`setup` 创建本项目 work/venv，按 requirements-dev.lock 安装并装入当前 API；前端只使用 pnpm-lock.yaml。后端测试默认隔离内存 SQLite，TEST_DATABASE_URL 只允许明确 test 命名的独立库，绝不继承业务 DATABASE_URL。PostgreSQL、迁移和容器另测，SQLite 不是替代。

Windows 静态检查：`work/venv/Scripts/ruff.exe check apps/api/app apps/api/tests apps/api/alembic`；Linux 用 `work/venv/bin/ruff`。继承外层忽略规则的工作区可加 `--no-respect-gitignore`，使用各文件所属的现有 Ruff 配置。新包装工具用 `python -m compileall -q scripts examples` 验证语法。时间显示保留用户本地时区，相关测试显式覆盖 UTC 与 Asia/Shanghai，不依赖执行机器的时区。

完整浏览器测试：

```sh
corepack pnpm --filter @shen/web exec playwright install chromium
corepack pnpm --filter @shen/web test:e2e
```

Linux 缺系统库时安装测试浏览器使用 `playwright install --with-deps chromium`。设置 E2E_BROWSER_CHANNEL=chrome 可使用已安装 Chrome。E2E_PYTHON 可指定隔离 Python 路径；默认为当前 checkout 的 work/venv（Windows Scripts、Linux bin）。只操作 shen-e2e 项目、数据库 shen_e2e、端口5433/8002/3002，不使用开发或生产库。日志仅用合成数据，位于被忽略的 e2e/.logs。

运行器拒绝任何已有锁（含死进程、损坏/未知 owner），不会自动清理。端口占用或无法判断也拒绝；核对进程后人工处理，不按端口自动结束进程。锁测试纳入前端单元验证。

依赖变更时通过 uv 的 universal compile 生成 Windows/Linux marker 与哈希，保持根目录相对路径：

```sh
uv pip compile apps/api/pyproject.toml apps/api/requirements-build.in --universal --python-version 3.12 --generate-hashes -o apps/api/requirements.lock
uv pip compile apps/api/pyproject.toml apps/api/requirements-build.in --extra dev --universal --python-version 3.12 --generate-hashes -o apps/api/requirements-dev.lock
```

uv 是维护工具，不是用户启动依赖；编译器版本在锁文件头记录。见 [uv 官方说明](https://docs.astral.sh/uv/pip/compile/)。更新后在 Windows 与 Linux容器各验证实际安装，重建 `licenses/dependency-inventory.json` / SBOM 并审查许可与漏洞。镜像摘要更新也需重测。

许可维护工具是 `scripts/update_notices.py`，用本项目隔离 Python 运行；它从锁文件对应的官方包注册表下载并校验发布包哈希，把缓存放在 `work/license-cache`。工具写入 inventory、SBOM 和许可文件；需人工复核未提供许可原文的包、许可解释、生成日期及 THIRD_PARTY_NOTICES。新增公开文件经审阅后更新 `release-allowlist.txt`，再运行公开树检查；不能把下载完成视为许可审阅完成。

`python scripts/check_public_tree.py` 只检查 Git 跟踪的公开清单，不扫描私有目录。CI 使用合成资料、只读权限、固定 Action SHA；Fork PR 不得到真实数据或发布密钥。发布包由 Git 当前树生成，不归档工作区缓存、私有数据或旧历史。
