# 服务适配与只读集成

## 已有适配器

| 功能 | 实际接口/实现 | 失败边界 |
| --- | --- | --- |
| OCR | `app/services/ocr/base.py` 的 OCRProvider / OCRResult；mock、MinerU | 保存 text 与完整 raw_result；mock 需明确文本，MinerU 需原图许可 |
| 文件 | `app/services/storage.py` 的 LocalStorageAdapter | 内容寻址、类型/大小校验；不是公共文件目录 |
| AI | `app/services/ai/gateway.py`、`live_provider.py` | OpenAI-compatible chat completions；严格 schema、证据校验；未配置/失败关闭 |
| 固定抽取 | `app/services/ai/extractor.py` | 按 OCR hash 精确查注册表；未知输入拒绝 |
| 队列 | `app/workers/celery_app.py` | Redis 协议；任务幂等，结果过期；公开部署使用 Valkey |

设置服务插槽包括 `mineru_ocr`、`clinical_extraction`、`vision_verification`、`longitudinal_analysis`、`apprenticeship_writing`。运行时配置保存在本实例数据库，密钥加密；已配置插槽优先于 bootstrap 环境回退。选择服务不改变临床校验。原图和含身份 OCR 的许可见[隐私边界](privacy-boundaries.md)。扩展一个适配器需明确输入、输出、超时、重试、错误码、schema 和许可，并用完全合成反例验证；本版不新增通用插件框架。

## 只读 API 示例

正式 API 默认 localhost:8100，契约为该实例 `/openapi.json`。`examples/read_only_api.py` 用交互密码登录并分页列出患者的稳定 ID，不打印姓名、病历号、Key 或 token。示例不保存凭证，先确认目标为自己的本机实例。

```sh
python examples/read_only_api.py --base-url http://localhost:8100
```

认证使用 `/api/v1/auth/token`；示例消费 `/api/v1/patients` 的 items/total/limit/offset。401/403 需重新核对权限，不绕过认证；未配置服务返回既有错误，不能据此补造临床数据。集成端需要保留来源类型、NULL、UNKNOWN、就诊 ID、版本和证据关联；不能把 AI 草稿写回医生诊断。其余接口从 OpenAPI 生成客户端，不发明批量导出接口。
