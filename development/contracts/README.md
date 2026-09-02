# Life Link 共享数据契约

此目录是 Android 与 PC/Web 之间唯一的接口事实来源（source of truth）。

## 当前状态

- `life-radio-api-v1.yaml`：Android、PC 客户端与中央上下文服务的共享同步协议；当前 v1.15.5。时间线支持 ETag 条件校验，PC 小窗与 WebUI 可共享完整业务日缓存；AI reader 个人模式只保持一个有效连接，理解说明先于事实内容。`GET /v1/calendar-days` 以共享业务日起点返回最多 42 个含首含尾日期的逻辑内容量；它接受独立只读或已注册设备凭据，模块互斥，且不把 SQLite 文件占用当作内容量。
- `fixtures/`：双方必须能够收发的最小联调样本；`ai-reader-pairing-claim-v1.json` 是不含 pairing/access Token 或完整配对文本的 AI reader claim 样本，`health-info-v1.json` 覆盖计步观察与统一健康读取响应，`wish-event-system-v1.json` 覆盖心愿、时间线、触发器、v1.13 调度设置、系统里程碑和报告正文/投送状态语义。
- `central-delivery-v1.md`：新中央单目标架构的身份、队列、确认、幂等与迁移边界。
- `one-line-invitation-v1.md`：首选的一行邀请码在线配对、一次性领取、同设备幂等和秘密处理规则。
- `one-line-invitation-payload-v1.schema.json`：`LR1.` 邀请码解码后的短期秘密 payload schema。
- `enrollment-claim-v1.schema.json`：`POST /v1/enrollments/claim` 的无秘密请求正文 schema；支持严格匹配 `desktop-<UUID>` / `android-install-<UUID>` 与对应平台。
- `client-profile-v1.schema.json`：PC 与 Android 共用的 `life-radio-client-profile-v1` 客户端配置 schema；Token 字段只定义形状，不提供真实或默认值。
- `ai-reader-passive-read-v1.md`：v1.15.3 AI reader 的单有效连接、独立身份、稳定同机进程绑定、一次性长期凭据返回、被动读取、reader/epoch 游标、动态标记、served 审计、全部低优先级排除和无副作用原文预览。
- `ai-reader-pairing-claim-v1.schema.json`：`POST /v1/ai-readers/pairings/claim` 的严格无秘密请求正文 schema。
- `central-server` 的 `POST /v1/events/batches` 是唯一正式上传入口；PC 本地服务只采集本机数据、代理中央读接口并提供 WebUI。

旧 P2P/Tailscale 路径已经退出正式实现。任何新增采集类型、字段或确认语义都必须先进入 v1 契约与 fixture。

## 职责边界

| 范围 | 负责端 | 约束 |
| --- | --- | --- |
| Android 采集、Room 队列、重试、权限与界面 | Android 模块 | 按此契约序列化、上传并处理逐事件确认 |
| 中央 HTTP、鉴权、持久化、去重与共享视图 | 中央服务模块 | 按此契约接收、存储并返回确认或读取资源 |
| PC 采集、outbox、本地代理与 WebUI | PC 模块 | 只上传本机事实，通过本地代理访问中央，不保存共享权威副本 |
| `development/contracts/` | 双方共享 | 任何不兼容变更须先更新契约，再改实现 |

## 变更规则

以下任何变动都属于“影响数据的变动”：事件类型或含义、字段、时间单位、ID 生成规则、上传批次、确认条件、端点、鉴权、重试与去重规则。

1. **先改契约，再改代码。** 在同一提交中更新 `life-radio-api-v1.yaml` 和受影响的 fixture。
2. **Android 强制维护。** Android 侧每次做上述变动，必须同步更新本目录；如果只是尚未实现的协议调整，也要先在此写明状态。
3. **优先向后兼容。** 只新增可选字段，不改已有字段的含义、类型或单位。无法兼容时新建 `life-radio-api-v2.yaml`，不要覆盖 v1。
4. **以确认结果为准。** v1.1 客户端仅将 `confirmed_event_ids` 中的事件标记为已送达；未确认或被拒绝的事件保留在本地队列。`accepted_event_ids` 只用于兼容 v1.0。
5. **双端联调。** 修改后，发送 `fixtures/sync-batch-v1.json`，服务端应返回符合 `fixtures/sync-ack-v1.json` 的响应；PC/Web 与 Android 都要记录验证结果。
6. **保护隐私。** 未经明确新增并记录，不上传屏幕正文、键盘输入、通知正文、联系人、精确位置等敏感原始内容。Token 不得写入契约、fixture、源码默认值或提交记录。

## v1.13 事件与调度边界

- `GET /v1/event-background?business_date=YYYY-MM-DD` 是中央动态只读投影，返回可直接展示的心愿、设备与应用、黑名单、位置与活动四个规范文字分段，以及稳定 AI 理解说明。设备与应用只列 15 分钟内仍在线的设备，并以“设备→正在使用的应用”相邻排列；离线设备及其旧应用不显示。位置可展示带更新时间的上次状态；位置与活动分段还会列出与生成时刻前 60 分钟有交集的全部活动区间，跨过一小时前边界的区间保留原始完整起止和时长，不按一小时窗口裁切。只有 `real_time_items` 中 `include_in_ai=true` 的新鲜实时项进入当前状态；背景中的过去一小时活动区间是明确标时的历史事实。该读取不会创建事件，也不包含 AI 凭据、提供方、模型、重试队列或消费游标。
- 系统里程碑由中央按固定规则追加到时间线：设备合计用量每 60 分钟、全平台黑名单每 30 分钟、稳定地点停留每 60 分钟、步行/跑步/交通工具连续状态每 30 分钟、睡觉时间后在线检查每 30 分钟。它们不是用户可创建的触发器，也不复制高频原始采集事件。
- 心愿可选择的新增预设触发器为 `scheduled_reminder`，参数为 `reminder_local_time`（`HH:mm`）。中心仅在槽位附近生成；错过超过 15 分钟不补发。标题以 `心愿提醒·` 开始，前缀不写入详情。
- 共享设置包含睡觉时间、AI 显示名称、早报、晚报和定时总结。旧库仍保留 `ai_display_name=AI` 兼容列，但 v1.15 起响应名称由当前有效 reader 身份投影且不可手工修改；三类报告默认禁用。
- 报告事件可附带 `delivery` 展示状态；v1.13 只会生成 `not_configured`，其余状态只预留未来展示语义。历史事件保存调度时的 AI 显示名称快照，后续改名不回写历史。

## v1.15.3 AI reader 被动只读边界

- 短期 pairing token 仅用于 `POST /v1/ai-readers/pairings/claim`；成功只返回一次长期 `access_token`、`reader_id`、`expires_at` 和 `context_url`。AI reader 凭据独立于上传 Token、旧全局只读 Token 和注册设备管理权限。

- `GET /v1/read/ai/context` 仅使用 `aiReaderAuth`。每次返回复用 `EventBackgroundResponse` 的完整背景、复用 `TimelineEvent` 的游标增量事件、带 `version/unchanged/content` 的理解说明、`next_cursor` 和本页三档重要程度计数。报告正文仍为 `evidence.body`。
- `GET /v1/read/ai/updates` 仅使用 `aiReaderAuth`，可提交当前不透明游标，只返回 `update_mcp`。当前业务日中存在该游标之后产生的 `importance=high`、`wish_id` 非空或 `trigger_id` 非空的非审计事件时为 `true`；检查不返回正文、不签发或推进游标，也不写访问事件。其他普通事件仍会在下一次 context 正式读取时一并返回。
- 游标是中央签发并绑定 reader、epoch 与业务日的不透明值。非法、篡改或跨 reader 游标返回 `400 invalid_cursor`；重新配对后的旧 epoch 返回 `409 cursor_superseded`。跨过共享业务日边界时自动切换到当前业务日，不提供上一业务日未读事件；无效、过期或撤销 reader Token 返回 `401`。
- AI reader 配对文本生成、列表、访问记录、无副作用原文预览、清理读取进度和撤销只接受 `registeredDeviceAuth`。清理只增加当前 reader epoch、清空当前标记并让 AI 自动重读当前业务日；不提供跳过积压。访问记录中的 `served` 只说明中央成功提供响应。
- 时间线的 `ai_reader` 是按当前 reader 当前 epoch 访问记录计算的只读字段。所有 `low` 事件对用户可见但状态为 `not_applicable`，永不返回给 AI；连接和访问审计事件同样遵守此规则。
- 原文预览按当前 reader 最新已签发游标展示下一次 compact JSON，但不会签发可用游标、写访问日志、推进标记或创建访问事件。
- Token、Authorization、完整配对文本和原始 GPS 不得写入 fixture 或日志。配对创建响应仅为用户复制短暂返回。首版不定义主动信号、Webhook、AI 写入或已读 ACK。

## 推荐协作流程

1. 总控先批准跨模块语义，再由契约任务更新本目录和 fixture。
2. 中央、PC、Android 分别按同一契约实现，不允许任一客户端先创造长期字段含义。
3. 各模块完成专项测试后，用同一 fixture 做集成验证；出现差异时先回到本目录修正约定，不靠各端自行猜测。

## Android 采集实现状态

- Android v2.1 起，主采集器使用系统 `UsageStatsManager` 的前台/后台事件流，不再依赖 ActivityWatch。首次采集回看当天零点至当前时刻，后续通过持久化的未结算前台会话覆盖 App 未运行期间的间隔。
- 当前 Room 过渡格式为 `source=android_usage_events`、`source_type=android`、`data_type=app_usage`；`data` 含 `package`、`app`、`classname`，时长单位为秒。
- 事件 ID 是由包名、Activity 和开始时间生成的稳定 UUID，重复扫描不会产生新事件；上传时映射为 v1 `app.foreground`。
- ActivityWatch 代码仍保留，但只有用户在 Android 设置中显式开启“备用采集”后才会运行。
- Android 的原生位置采集必须由用户主动启用。v2.9 起，每次成功且精度合格的定位都形成独立 `location.observation`；v3.0 起以 Android 实际定位时间排序，并对系统重复回放的同一定位复用 event ID。
- Android 不再按 150 米生成新位置段。PC 保存完整观察序列，并可在查询或展示层派生 `location.sample` / `location.stay` 语义视图；旧客户端的位置段更新规则继续兼容。详细规则见 [`location-semantic-events-v1.md`](location-semantic-events-v1.md)。
- 位置 payload 可包含坐标、地址/地名和常去地点标签；个人模式下完整地址只能经鉴权 HTTPS 上传到用户的中央服务。采集默认关闭，必须由用户在 Android 端主动启用。

## Android v3.0 位置观察状态（2026-07-30）

- Android Room 的 `location_samples` 与待同步 `data_events` 使用同一个观察 ID 成对写入，避免本机看见数据但同步队列缺失。
- 每条 `location.observation` 包含时间、坐标、精度、来源、可用地址，以及上一观察间隔内的加速度传感器统计。
- 加速度统计只用于诊断：记录传感器可用性、采样数、阈值上穿次数、阈值与峰值；它不参与位置筛选或采样间隔调整。
- 事件时间改为 Android `location_time`；`observed_at` 继续表示应用收到并落库的时间。PC 可用两者之差识别系统延迟回放。
- 五分钟请求改为高精度且关闭主动批量延迟；十分钟以上旧缓存定位会被拒绝，同一秒同坐标的重复回放复用同一事件 ID。
- PC 的 `/api/locations` 同时返回不可变 `observations` 与按 150 米派生的 `segments`。完整字段和 fixture 见 `location-semantic-events-v1.md` 与 `fixtures/sync-batch-v1.json`。
