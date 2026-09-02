# Central delivery semantics v1

本文件定义 Life Link 新架构的唯一送达语义。中央上下文服务是事件的唯一长期存储；PC、Android 等客户端只采集本机数据并向中央服务上传，AI 和 Web UI 只从中央服务读取。

## 拓扑与角色

- 每个用户只有一个逻辑 `central` 目标。域名、IP、机房或隧道变化不得创建新的送达目标。
- PC 与 Android 客户端不互相发现、不互相推送，也不接收其他客户端的长期数据副本。
- 中央服务收到事件后不得把它重新转发给另一台 PC 或手机。
- 客户端只需通过 HTTPS 访问中央服务；客户端间不建立网络连接。
- AI 使用只读接口和独立凭据读取中央长期库，不扫描客户端本地文件。

## 稳定设备身份

- `device_id` 在应用首次安装时生成并持久保存，恢复显示名、切换网络、修改 hostname 或更换中央服务地址时保持不变。
- `device_id` 不得由 Tailscale 节点 ID、IP、hostname、ActivityWatch bucket、手机号、IMEI 或 MAC 地址派生。
- 客户端上报的 `display_name` 只记录最近原始名称；中央可保存独立 `custom_name`。所有中央读取视图优先使用 `custom_name`，它不参与去重或鉴权，也不会被客户端后续上传覆盖。
- 设备删除采用 `retired_at` 逻辑停用：撤销该设备凭据并从当前管理列表隐藏，但不得删除原始事件、批次或稳定 `device_id`。明确绑定该设备的设备用量触发器同时停用；重新接入必须重新领取邀请和新凭据。
- 上传凭据必须在服务端绑定一个 `device_id`。令牌对应的设备与请求 `device.device_id` 不一致时，整批拒绝且不得写入。
- ActivityWatch、UsageStats 和 fused location 是事件的 `source.collector`，不是设备身份。

## 客户端注册与凭据

- PC 和 Android 的首选流程都是 [`one-line-invitation-v1.md`](one-line-invitation-v1.md) 定义的单行邀请码在线配对。邀请码默认 24 小时有效且只能绑定一个稳定 `device_id`；同设备响应丢失后的 claim 可以幂等重试，不同设备复用必须拒绝。
- 在线 claim 只接受平台匹配的 installation ID：PC 为 `desktop-<UUID>`，Android 为 `android-install-<UUID>`。显示名、Android 包名和 Activity 名均不得充当或改变 `device_id`。
- 在恢复路径中，远端 PC 先导出不含秘密的 `life-radio-enrollment-request-v1` 注册请求，再由管理员签发包含设备专用上传 Token 的 `life-radio-client-profile-v1` 配置包。
- 每台 PC 必须使用独立上传 Token；配置包中的 `device.device_id` 必须与目标 PC 本机 installation ID 完全一致。导入不匹配的配置包时必须整体拒绝，不得覆盖本机身份或写入部分秘密。
- 签发工具必须先把变更后的完整 Token 集合原子写入中央 SQLite，并让运行中的服务热加载成功，再导出配置包。任一步失败都不得导出凭据包；新设备鉴权不得依赖服务重启。
- 远端中央地址只接受 HTTPS。可选 `read_token` 表示读取该用户全部中央可读数据的权限，不是单设备读取权限；只负责采集的 PC 应省略它。
- PC 与 Android 统一通过一行邀请码在线领取设备专用配置；失败时重新签发短期邀请码，不人工传递配置文件。

## 本地队列与批次

- 客户端先把事件持久化到本地 outbox，再尝试上传；网络失败、超时、限流或服务端错误不得删除事件。
- 一个上传批次最多 500 条事件，`Idempotency-Key` 必须等于 `batch_id`。
- 对同一设备、同一 `batch_id`、同一请求内容的重试，中央服务必须返回第一次持久化的相同确认结果。
- 同一设备复用 `batch_id` 但请求内容不同，中央服务必须返回 `409 idempotency_conflict`，不得部分写入。
- 客户端可以在重启后重新打包尚未确认的事件。中央服务按稳定 `event_id` 去重，因此新 batch 中的已存在事件仍可安全确认。

## 逐事件确认

- `confirmed_event_ids` 是 v1.1 唯一的队列清理依据。
- `stored`、`duplicate`、`updated` 都表示中央服务已对该事件提供持久化保证，因此必须进入 `confirmed_event_ids`。
- `rejected` 不得进入 `confirmed_event_ids`；客户端应保留事件并记录 `code` 和 `message` 供诊断或修正。
- `accepted_event_ids` 与 `rejected_events` 为 v1.0 兼容字段。新客户端不得只依据 `accepted_event_ids` 推断送达完成。
- 客户端收到格式不完整、缺少 `confirmed_event_ids` 或无法对应请求事件的响应时，不推进队列。

## 事件幂等与修订

- `event_id` 在一个用户的中央库中全局唯一。相同 ID、相同设备、相同 revision 和相同规范化内容再次到达时返回 `duplicate`。
- `revision` 省略时视为 0。事件默认不可变；同 revision 内容不同必须返回 `event_conflict`。
- `location.observation`、`custom.event` 和其他瞬时事实始终不可变。
- ActivityWatch 的持续 `app.foreground` 事件以及兼容旧客户端的 `location.sample` / `location.stay` 可以使用更高 revision 更新；只允许时长和契约声明的活动 payload 单调前进。
- 活动位置段用同一个 `event_id` 和递增 `revision` 延长 `duration_seconds`、`observed_until` / `latest_observed_at`；定稿时可从 `location.sample` 升级为 `location.stay` 并把 `is_active` 设为 `false`。定稿后的段不得重新激活或更新，段起点坐标不得借 revision 改写。
- 更低 revision 不得覆盖更高 revision；另一设备不得更新已存在事件，即使 event ID 相同。

## 中央只读 MVP

中央只读接口与设备上传接口严格分权：

- `POST /v1/events/batches` 使用绑定单个 `device_id` 的设备上传 Token。
- `GET /v1/read/devices` 与 `GET /v1/read/usage` 只接受独立 read token。
- 设备上传 Token 不具备任何中央读取权限；携带上传 Token 调用只读接口时必须拒绝，不能因为它是有效 Bearer Token 而放行。
- read token 不得用于上传事件，也不得在浏览器脚本、查询参数、日志或响应正文中暴露。PC WebUI 应由持有 read token 的本机服务端代理读取。

两个 MVP 查询都使用以下参数：

- `from`：必填、包含式窗口起点，必须是 UTC ISO 8601 `Z` 时间。
- `to`：必填、排他式窗口终点，必须是 UTC ISO 8601 `Z` 时间，且严格晚于 `from`。
- `local_device_id`：可选，仅把匹配稳定 ID 的设备标记为 `is_local=true`。它不扩大读取权限，也不从结果中过滤其他设备；未知 ID 使所有设备的 `is_local=false`。
- 缺少参数、不是 UTC `Z` 时间、`from >= to` 或窗口超过服务端限制时返回 `400`。
- 瞬时事件仅在 `occurred_at >= from && occurred_at < to` 时属于窗口。持续事件只要与 `[from,to)` 相交就可参与统计，但累计时长必须裁剪到窗口边界；服务端可以读取窗口前的必要 URL 标签等归属上下文，但不得把窗口外时长计入结果。

### `GET /v1/read/devices`

- 返回中央库中已登记的设备身份、`last_seen_at`、窗口内去重事件数、相关批次数和按 `event_type` 分类的数量。相关批次指其 ACK `event_results` 至少引用一个窗口事件的不同上传批次。
- `device_key` 必须等于稳定 `device_id`，供现有 WebUI 持久化设备选择；不得再按显示名、hostname 或网络地址生成或合并设备。
- `last_seen_at` 是中央服务最后一次成功处理该设备上传批次的时间。它只表示最后同步时间，不证明设备当前联网、应用正在运行或服务可访问。
- 为兼容现有设备卡片而返回的 `connected` / `disconnected` 也只是依据 `last_seen_at` 与配置的新鲜度窗口计算的同步状态，不是网络在线探测结果。
- `window` 是规范字段；响应可同时提供内容相同的 `today` 和 `last_received_at` 兼容别名，使 PC 服务无需重新扫描本地 JSON 即可代理现有 `/api/devices`。只有当调用方传入完整业务日的 UTC 边界时，`today` 才具有“今日”的含义。

### `GET /v1/read/usage`

- 只聚合中央库中 `event_type=app.foreground` 的去重后当前 revision；位置、日历、自定义事件等带时长的事实不得计入应用用量。
- 响应包含每台设备及 `all` 总集的 `apps`、`hourly`、`hourly_apps`、`sites`、`hourly_sites`、事件计数和 AFK 秒数，单位统一为秒，足以代理现有 PC `/api/usage`；`hourly_online` 仅作为旧客户端兼容别名，值与 `hourly` 相同。
- `hourly*` 使用响应声明的用户时区，以本地整数小时 `0` 至 `23` 为键；查询的事实边界仍完全由 `from` 与排他 `to` 决定。
- 统一对外指标为“设备使用时长”：PC 从 ActivityWatch 前台 window 区间中剪去明确 `status=afk` 的重叠部分，再按区间并集统计；缺少 AFK 事实时不把用量归零。Android/iOS 保持应用前台区间口径，不应用 PC 的 AFK 裁剪。单设备单小时的设备使用时长不得超过 3600 秒。
- 桌面 Chrome 用量继续根据原始 window 事件和 browser URL 标记在查询时推导网站区间；网站标记不是独立可累加的真实用时，不得把 URL 事件自身的碎片时长直接当作网站总时长。
- `local_device_id` 只影响每台设备的 `is_local`，不得改变任何设备或 `all` 的统计值。

MVP 只覆盖设备和用量读取。位置、自定义事件、批次明细与 AI Markdown 仍属于后续只读契约，不能通过未声明的临时端点复用上传 Token 暴露。

## 时间与业务日

- `occurred_at` 与 `sent_at` 使用 UTC ISO 8601 `Z`；持续时长统一为整数秒。
- `received_at` 由中央服务生成，不能由客户端覆盖。
- 用户时区和跨日时间点只影响查询、统计和 AI 摘要的业务日划分，不改变原始事件时间，也不决定事件是否接收。

## 新路径禁止项

新代码不得依赖或生成以下 P2P 概念：

- peer 列表、远端 PC 发现或向全部 PC 广播；
- 每个 peer 一份 delivery ledger；
- `origin_device`、`relay:` 设备别名或收到后再次转发；
- Tailscale device ID / IP 作为持久身份；
- `/push` 作为新客户端上传端点；
- provider / receiver 模式或强制同步到全部远端 PC。

旧 `/push`、Tailscale 发现和多节点账本不再属于当前实现。
