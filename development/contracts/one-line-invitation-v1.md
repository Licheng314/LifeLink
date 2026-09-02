# One-line online enrollment invitation v1

## PC 与 Android 设备身份

同一条 `LR1.` 在线配对流程同时支持 `platform=desktop` 和 `platform=android`。PC 的稳定 installation ID 必须是 `desktop-<canonical UUID>`；Android 必须复用 `SettingsStore` 已持久化的 `android-install-<canonical UUID>`，不得在配对时生成或覆盖身份。平台与 ID 前缀必须匹配；显示名、Android package / Activity、hostname 和网络地址都不是设备身份。

Android 与 PC 遵守相同的默认 24 小时有效期、单设备一次领取、有效期内同 `device_id` 幂等重试、过期 `410`、不同设备复用 `409`、邀请 Token 仅保存 hash，以及 `upload` / `dashboard` scope 边界。`upload` 只返回设备上传能力；`dashboard` 才可按服务端策略附带全局只读 Token。旧 Tailscale 和多端发现不属于 claim 或中央上传协议。

当前产品入口不再按 Android 与 PC 区分邀请码：`central-server/maintenance/create_invitation.bat` 对两类设备统一签发 `dashboard` scope。`upload` scope 仅为已有邀请和协议兼容保留，不再提供独立的“手机邀请”入口。

本文定义 Life Link PC 与 Android 共用的唯一在线配对流程。用户只需在客户端粘贴一行邀请码；客户端据此向中央服务申领绑定本机 installation ID 的长期配置。

## 邀请码格式

邀请码必须是单行文本：

```text
LR1.<base64url-without-padding-of-compact-utf8-json>
```

- 前缀固定为 `LR1.`。
- 后半部分是 UTF-8 紧凑 JSON（无无意义空白）的 RFC 4648 URL-safe Base64 编码，并移除末尾 `=` padding。
- Base64url 只是传输编码，不是加密或签名；任何拿到该行文本的人都能解码并在有效期内尝试领取。
- 解码后的对象必须符合 [`one-line-invitation-payload-v1.schema.json`](one-line-invitation-payload-v1.schema.json)，字段固定为 `v`、`invitation_id`、`central_base_url`、`invitation_token`、`scope`、`expires_at`。
- `scope` 只允许 `upload` 或 `dashboard`。`upload` 只能申领上传能力；`dashboard` 允许服务端在长期配置中附加全量 `read_token`。
- 新建邀请的默认有效期是 24 小时。服务端可允许管理员选择更短有效期，但不得由客户端延长 `expires_at`。
- 客户端必须在联网前完成前缀、Base64、JSON、schema、HTTPS 地址和过期时间校验，并对输入设置合理长度上限。解析 URL 时还必须拒绝用户名、密码、查询参数和 fragment。

邀请码包含短期秘密。契约与 fixture 不提供可用邀请码或真实 Token；测试应在内存中生成一次性假值。

## Claim 请求

客户端从 `central_base_url` 固定拼接 `POST /v1/enrollments/claim`，不得从邀请码读取或接受其他 claim 路径。请求使用：

```http
Authorization: Bearer <invitation_token>
Content-Type: application/json
```

请求正文必须符合 [`enrollment-claim-v1.schema.json`](enrollment-claim-v1.schema.json)，无秘密样本见 [`fixtures/enrollment-claim-v1.json`](fixtures/enrollment-claim-v1.json)：

```json
{
  "schema_version": "life-radio-enrollment-claim-v1",
  "invitation_id": "22222222-2222-4222-8222-222222222222",
  "device": {
    "device_id": "desktop-11111111-1111-4111-8111-111111111111",
    "platform": "desktop",
    "display_name": "Remote PC"
  }
}
```

正文 `invitation_id` 必须与 Bearer Token 所属邀请完全一致。`device.device_id` 必须来自当前 PC 已持久化的 installation ID；配对不得生成或覆盖另一个本机身份。

## 成功响应与幂等

- 首次有效 claim 会把邀请原子绑定到请求中的稳定 `device_id`，创建永久设备专用 `upload_token`，并返回符合 [`client-profile-v1.schema.json`](client-profile-v1.schema.json) 的 `life-radio-client-profile-v1`。
- `scope=upload` 的成功响应必须省略 `read_token`。`scope=dashboard` 的成功响应可以按服务端策略包含 `read_token`；一旦包含，它就拥有用户全部中央可读数据的权限。
- 邀请默认只能被一个设备领取一次。成功绑定后，不同 `device_id` 再使用同一邀请必须返回 `409 invitation_already_claimed`，不得签发任何凭据。
- 若首次成功响应在网络中丢失，同一 `invitation_id`、同一 Bearer Token、同一稳定 `device_id` 的重试必须返回首次签发的同一份长期配置，不得轮换或重复创建 Token。`display_name` 的变化不改变设备身份，幂等重试仍返回首次保存的设备描述与配置。
- 为支持上述重试，服务端必须在发出首次 `200` 前原子保存邀请绑定与可恢复的首次响应材料。实现可以加密保存响应材料或使用等价的安全可恢复机制，但不得在普通日志中保存明文 Token。
- `expires_at` 限制所有 claim。服务端必须先检查有效期；一旦过期，无论是首次领取、已领取同设备的幂等重试，还是不同设备复用，都返回 `410 invitation_expired`，不得再次返回长期配置。未知、撤销、Token 不匹配或正文 `invitation_id` 不匹配时不得透露邀请状态，返回 `401`。
- 服务端无法持久化绑定、长期凭据或热更新运行时鉴权集合时返回 `503`，且不得返回部分配置。调用方可以在不改变本机身份的前提下重试。

## 服务端安全要求

- `invitation_token` 只以不可逆密码学散列保存；比较时使用恒定时间比较。数据库不得保存其明文。
- HTTP 访问日志、应用日志、异常、追踪、指标标签和审计事件都不得记录 `Authorization` 内容、邀请码原文、`invitation_token`、`upload_token` 或 `read_token`。
- claim 端点必须限速，并限制请求体大小；失败响应不得回显邀请码或 Token。
- 成功 claim 必须让运行中的中央服务立即接受新 `upload_token`，不能依赖重启。持久化与运行时热更新未全部成功前不得返回配置。
- 邀请撤销、过期、首次绑定和幂等重试状态以中央持久库为准，不能只放在进程内存中。

## 客户端安全要求

- 邀请码只能进入专用输入框或受控导入流程；不得放入 URL 路径、查询参数、fragment、浏览器历史或 HTTP Referer。
- 不得把邀请码、`invitation_token`、`upload_token` 或 `read_token` 写入 `localStorage`、`sessionStorage`、网页缓存、诊断日志、剪贴板历史备份或未加密配置文件。
- WebUI 不直接持有任何 Token。PC 本地后端完成 claim，并把长期凭据保存到操作系统秘密存储；页面只接收成功、失败和安全化错误码。
- claim 成功并验证配置可用后，客户端应清空输入框、剪贴板中的邀请码和临时内存副本。失败时只能显示不含秘密的诊断信息。

## 恢复

邀请码失效、遗失或 claim 失败时，由中央服务重新签发一条短期邀请码；不再保留离线双文件签发路径。已有客户端配置继续有效，不需要重复领取。
