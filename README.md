# SHEN · 一站式临床师承知识管理

[English](README.en.md) · [入门](docs/getting-started.md) · [正式部署](docs/deployment.md) · [隐私边界](docs/privacy-boundaries.md)

SHEN 是面向中医师承学习、本地优先的一站式临床知识管理工具：保留来源文件与 OCR，整理患者时间线，确定性比较处方，并保存可追溯的跟师笔记和医案版本。当前为 **v0.2.0-rc.1 开源预览版**，Apache-2.0 许可，版权署名 FU HAO。

适合个人独立部署；课题组成员各自保管数据库、资料和密钥。当前仍是单用户应用。现有摄取规则围绕 JNU 门诊和旧版处方场景，其他医院的身份与剂量语义需要单独验证。工程测试不代表临床准确率或机构级生产就绪。

## 无密钥体验

需要 Docker Engine 28 或更新版本（可通过 Docker Desktop 提供）和 Docker Compose v2.24.4 或更新版本。首次构建需要联网下载依赖；演示后端服务隔离外网，网页使用单独的本机入口网络。

从 [GitHub 版本发布页](https://github.com/Brighthao18/SHEN-Clinical-Apprenticeship-Hub/releases)下载源码包并解压，或克隆仓库：

```sh
git clone https://github.com/Brighthao18/SHEN-Clinical-Apprenticeship-Hub.git
cd SHEN-Clinical-Apprenticeship-Hub
```

在仓库根目录运行：

```sh
docker compose --env-file examples/demo/demo.env -f compose.demo.yaml up --build -d --wait --wait-timeout 180
```

打开 <http://localhost:3200>，登录 `admin` / `shen-demo-only`。界面常驻合成数据标识。演示包含两次复诊、一份缺少病历号而无法导入的记录和一份用户作品，支持核对原图、处方变化、编辑、追加版本及导出。

**演示只用于合成资料。** 密码与加密密钥是公开固定值，不能用来保存真实患者数据。它与正式部署使用不同端口和存储。

停止演示并保留数据：

```sh
docker compose --env-file examples/demo/demo.env -f compose.demo.yaml stop
```

## 正式使用

安装 Python 3.12，在根目录依次运行：

```sh
python scripts/shen.py setup
python scripts/shen.py init
python scripts/shen.py start
```

`init` 在本机交互生成新密钥和密码哈希，拒绝覆盖已有配置。正式入口为 <http://localhost:3100>。默认只使用 mock，真实 OCR、抽取和原图核验均不启用；配置服务前阅读[正式部署](docs/deployment.md)和[隐私边界](docs/privacy-boundaries.md)。不能把演示环境升级成真实数据环境。

## 能力与边界

- 原始字节、SHA-256、OCR 与完整服务结果保留，身份信息与临床事实分开存储。
- 校验通过才导入；`UNRESOLVED` 不生成患者、就诊或处方，不要求补造缺失信息。
- 药物、患者身份、剂量算术和处方差异由确定性逻辑约束；未知剂量保持未知。
- AI 病程分析与写作通过专用服务插槽配置；推断与草稿保持单独标识，保存及重新生成追加版本。
- 单用户、本地存储、localhost 部署；没有多人协作、HIS/FHIR、RAG、老师规律分析或通用医院适配。

数据可能外发的四条路径：[原图 OCR、含身份的 OCR 文本抽取、原图核验、脱敏分析/写作](docs/privacy-boundaries.md)。服务由部署者逐项配置与确认；开源代码不包含服务额度、模型权重、真实病例或医院规格库。

## 开发与集成

[开发验证](docs/development.md) · [架构与数据](docs/architecture.md) · [支持范围](docs/supported-scope.md) · [服务适配与 API](docs/provider-adapters.md) · [贡献](CONTRIBUTING.md) · [安全报告](SECURITY.md) · [第三方许可](THIRD_PARTY_NOTICES.md) · [发布检查](docs/release-checklist.md)

Next.js / FastAPI / PostgreSQL / Celery，队列部署使用 Valkey。API 需要认证，原始文件不能作为公开静态目录提供。请仅用从零构造的合成数据提交 Issue、测试和截图。
