# Life Link 同步数据的 AI 读取说明

状态：AI reader v1.15.3 为现役入口；Markdown 路径仅供旧自动化兼容。

本文面向外部 AI/Agent 和兼容性自动化阅读程序。旧版 `/api/received` 和
`received_data_*.json` 已退出运行路径，不得再作为统计或排障来源。

面向 AI 伴侣的统一授权、事件增量、背景摘要和访问审计以 [`../contracts/ai-reader-passive-read-v1.md`](../contracts/ai-reader-passive-read-v1.md) 为准。用量与位置 Markdown 入口继续作为旧自动化兼容接口，不等于新的 AI reader 协议；设计形成过程可查阅 [`已归档/AI接入方案-评估.md`](已归档/AI接入方案-评估.md)。

## AI reader 统一入口

默认中央与 AI 同机：用户在 PC WebUI 首页的 AI 卡片中选择“生成 AI 配对文本”，AI 使用其中的一次性配对凭据领取独立长期只读 Token，随后调用：

`GET http://127.0.0.1:8091/v1/read/ai/context`

首次无游标读取当前业务日事件；后续携带中央返回的 `next_cursor` 获取增量事件。背景每次完整返回，理解说明通过 `understanding_version` 避免重复。长期 Token 只允许这个读取接口，不能上传、管理设备或修改设置。具体字段、状态码和管理边界以 [`../contracts/ai-reader-passive-read-v1.md`](../contracts/ai-reader-passive-read-v1.md) 为准。

默认读取即为 compact：事件只包含 `at`、`importance` 和正文，时间为明确的 `Asia/Shanghai` 本地时间；需要完整结构时显式附加 `?view=full`。

## MCP 连接包

PC WebUI 首页的“生成 AI 配对包”会生成一个由用户主动交给目标 AI 的 ZIP。包内包含 Windows `life-link-mcp.exe`、现役中文 Skill、一次性配对材料、通用 Reader 身份和 MCP 配置样例；Life Link 不识别目标 AI，也不修改任何特定应用的配置。将压缩包发送给目标 AI 后，由它解压并自行登记 stdio MCP，也可按实际身份补充同机进程绑定。

`life-link-mcp.exe` 提供 `lifelink_connection_status` 与 `lifelink_read_context` 两个只读工具。首次读取自动领取 Reader Token，后续保存并提交中央签发的游标；Token、游标和理解版本在 Windows 上使用当前用户 DPAPI 加密。MCP 只是现役 Reader HTTP 协议的本地适配层，不增加上传、管理或写入权限。

当前没有 `lifelink_check_updates`：中央协议尚未提供无副作用的轻量更新检查，不能让一次完整上下文读取冒充轮询标志，否则会错误推进读取标记。该能力需要以后先增加独立中央契约。

当前代码和自动化测试已经完成，用户也已使用真实外部 AI/Agent 完成配对与读取验收。个人模式只维持一个有效 AI 连接；新 AI 身份成功配对时，中央会撤销原有效 Token，同时保留历史 reader 与访问日志用于审计。活动状态已由中央执行步数与定位双来源证据门槛；任一来源不足的分钟不会进入活动状态或 AI 背景。

## Markdown 兼容入口

同机自动化优先使用 PC 客户端的回环代理。它负责按本地业务日计算查询范围，不会向浏览器或阅读程序暴露中央凭据；不要通过 Tailscale、花生壳或其他公网入口暴露 8090：

| 目的 | 接口 |
| --- | --- |
| 发现可读上下文 | `GET http://127.0.0.1:8090/api/ai-context/index.json` |
| 应用使用摘要 | `GET http://127.0.0.1:8090/api/ai-context/usage.md` |
| 位置轨迹摘要 | `GET http://127.0.0.1:8090/api/ai-context/location.md` |
| 口径与排障 | `GET http://127.0.0.1:8090/api/ai-context/README.md` |

可附加 `?date=YYYY-MM-DD` 指定 Life Link 本地业务日；省略时为当前业务日。
先请求 `index.json`，再读取其中列出的两个 Markdown 摘要。事实来自中央长期库；PC 代理会获取中央动态生成的结果并落盘当天快照，因此读取端不必自行进行事件去重或统计。

不与 PC 客户端同机的 AI 可以直接访问中央 HTTPS 入口：

| 目的 | 中央接口 |
| --- | --- |
| 应用使用摘要 | `GET /v1/read/ai/usage.md?from=<UTC>&to=<UTC>` |
| 位置轨迹摘要 | `GET /v1/read/ai/location.md?from=<UTC>&to=<UTC>` |

远程读取必须在 `Authorization: Bearer <read token>` 请求头中使用独立只读凭据；不得把 Token 放进 URL、文档、日志或提示词。`from` 与 `to` 是本地业务日边界换算后的 UTC 时间，远程程序如果不想自行换算，应继续通过某台已注册 PC 的回环代理读取。

## 读数边界

- 对外统一使用“设备使用时长”：PC 前台应用区间剪去明确 AFK 重叠；手机保持前台应用事件口径，不再单独展示在线时长。
- 黑名单是用户希望避免过度沉迷的应用或网站。Chrome 网站时长由 Chrome 前台跨度与 URL 标签推断，不能把网页观察记录自身的时长直接相加。
- 新版位置数据是手机逐条同步的不可变观察，PC 再派生位置段；旧版活动段仍按兼容规则更新。地址可能为空，不得猜测。
- 摘要只反映已确认写入中央长期库的数据。手机或远端 PC 未连接、服务未运行、同步尚未完成时，摘要不会凭空补齐数据。

完整的字段含义、异常判断和排障步骤以 PC 服务的
[`ai_context/README.md`](../../pc-dashboard/ai_context/README.md) 为准。

## 自动化建议

在本地整数点后 15 分钟读取一次两个摘要，只汇报相较前次有意义的变化：
持续黑名单使用、设备长期不报告、位置切换或长时间停留。读取失败时先用中央 `GET /v1/health` 检查服务是否存活；同机兼容自动化再查 PC 的 `GET /health`、`GET /api/sync/central`、`GET /api/devices`、`GET /api/usage` 和 `GET /api/locations`。不应自行删除数据、重置 outbox 或修改同步设置。个人健康参考另由受鉴权的 `/v1/health-info` 提供，不能与公开存活检查混为一谈。

## 协议与历史兼容

跨端事件契约以 [`contracts/life-radio-api-v1.yaml`](../contracts/life-radio-api-v1.yaml)
为准。旧版 `/push`、`/api/received*`、Tailscale 发现和 PC 间广播已经退出当前运行路径；不能再把 PC 客户端当作同步接收服务器。
