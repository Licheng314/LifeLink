# AI reader 被动只读协议 v1

本协议定义 Life Link v1.15.3 的外部 AI/Agent 被动拉取边界。AI reader 是独立只读主体，不是 PC/Android 设备，不复用设备上传 Token、`readBearerAuth` 或注册设备管理权限。个人模式只保持一个有效 AI 连接。

## 配对与凭据

- reader 使用短期 Bearer pairing token 调用 `POST /v1/ai-readers/pairings/claim`。请求正文必须符合 [`ai-reader-pairing-claim-v1.schema.json`](ai-reader-pairing-claim-v1.schema.json)，无秘密样本见 [`fixtures/ai-reader-pairing-claim-v1.json`](fixtures/ai-reader-pairing-claim-v1.json)。
- 中央复制给用户的完整配对文本同时携带简短接入说明和 claim 请求正文模板：读取方先按 `central_instance_id` 判断是否已经接入同一中央，再以自身稳定的 reader 身份替换模板占位值；已有有效连接时不重复领取，首次成功 claim 后立即读取一次 context。
- pairing token 只允许 claim，中央只保存其密码学散列。`pairing_id` 必须与 Token 所属配对一致；无效、未知、撤销或不匹配返回 `401`，过期返回 `410`，已领取返回 `409`，错误响应不得回显秘密。
- 成功响应只包含 `access_token`、`reader_id`、`expires_at` 和固定读取地址 `context_url`。长期 `access_token` 只在首次成功 claim 返回一次；服务端不得通过重试、列表、日志或管理接口再次取回明文。响应丢失时必须撤销该配对并重新配对，首版不定义明文凭据恢复。
- 新 reader 身份成功领取时，中央在同一事务内撤销其他 reader Token；重新配对同一稳定身份则轮换其 Token。历史 reader 和访问日志保留审计用途，但任意时刻最多一个 reader 为 active。
- 同机 Windows 且读取方能准确确认伴侣应用时，claim 的 `reader` 应携带稳定 `process_binding`。独立原生应用使用精确进程文件名；Node、Python、Java 等共享宿主必须同时提供精确宿主进程文件名和某个启动参数内连续、稳定的应用路径段，不能只检测通用宿主。OpenClaw 的现役绑定是 `node.exe` 加 `node_modules/openclaw` 两段，不保存完整绝对路径、PID、启动时间或完整命令行。远程、非 Windows 或无法准确确认时省略；管理端点返回 `process_running=true/false/null` 和可空的 `process_display_name`，WebUI 仅在 `true` 时显示“检测到某进程正在运行”。该状态不参与 Token 有效性判定。重新配对同一稳定 reader 且未提交新绑定时保留已有绑定；旧 `process_identity` 只作兼容，不再出现在新配对模板中。
- 长期 Token 只授予 `GET /v1/read/ai/context`，中央只保存散列，并支持到期和撤销。Token 无效、过期或撤销均返回 `401`。
- HTTP/应用/审计日志不得记录 Authorization、Token、完整配对文本或可恢复的秘密；契约和 fixture 不提供可用 Token 或完整配对文本。

## 上下文读取

`GET /v1/read/ai/context` 使用 `aiReaderAuth`，可选参数为 `business_date`、`cursor`、`understanding_version` 和 `view`。

省略 `view` 或使用 `view=compact` 时返回简短表达：背景和有效实时项压成文字数组，每条事件只保留 `at`、`importance` 和中央正文，事件时间明确转换为 `Asia/Shanghai` 的 `+08:00` ISO 时间；事件 ID、key、内部 evidence、delivery、统计窗口和去重键只在 compact 响应中省略。报告仍完整使用冻结的 `evidence.body`。只有显式使用 `view=full` 才返回完整结构。

- 每次成功响应都返回当前 `background` 全量快照；它复用中央背景字段，但不重复携带已经单独版本化的 `ai_understanding`。
- JSON 表达顺序固定先给出元数据和 `understanding`，再给出 `background`、`current`/`events` 等事实内容，让理解说明成为后续内容的语义基础；调用方仍应按字段名解析。
- `events` 是现有中央 `TimelineEvent` 的增量。首次无游标读取返回目标业务日的现有合格事件，后续只返回该 reader 上次位置之后中央提供的事件。所有 `importance=low` 的时间线事件都不提供给 AI；报告正文保持在 `event.evidence.body`，投送展示状态不是正文。
- `understanding` 始终返回 `version` 和 `unchanged`。调用方版本相同时省略 `content`；首次读取或版本变化时返回完整 `AIUnderstandingGuide`。
- `importance_counts` 是本次返回事件的 `high`、`normal`、`low` 数量；`next_cursor` 是中央签发的不透明值。
- 响应不得包含客户端原始采集事件、原始 GPS、SQLite、设备凭据、AI Token 或共享写权限。实时背景只遵守现有 `include_in_ai` 规则。

## 游标

- 游标由中央签发，绑定 `reader_id` 和该 reader 当前 cursor epoch；调用方不得解析、拼接或从时间戳自行构造。
- 无法识别、格式非法、属于其他 reader 或被篡改的游标返回 `400 invalid_cursor`。
- 游标同时绑定业务日。携带上一业务日游标跨过共享跨日边界后，中央自动切换到当前业务日，从当前业务日开头提供事件并签发新游标；不会提供上一业务日尚未读取的事件。
- 同一业务日内，游标只提供该业务日范围中在游标位置之后新创建的事件。服务恢复后补生成但 `occurred_at` 属于其他业务日的事件不会混入当前列表。
- reader 重新配对或用户执行“清理标记”后 epoch 变化，旧 epoch 的有效游标返回 `409 cursor_superseded`。AI 必须丢弃旧游标并自动无游标重试一次；用户不经手游标。无游标重试只重新获取当前业务日，不提供“跳过积压”模式。

## 管理与访问记录

管理端仅使用 `registeredDeviceAuth`：`POST /v1/ai-readers/pairings`、`GET /v1/ai-readers`、`GET /v1/ai-readers/{reader_id}/process-status`、`GET /v1/ai-readers/{reader_id}/access-logs`、`GET /v1/ai-readers/{reader_id}/context-preview`、`POST /v1/ai-readers/{reader_id}/clear-reading-progress` 和 `DELETE /v1/ai-readers/{reader_id}`。

- 列表展示 reader 身份、状态、配对/到期/最后请求时间、当前 epoch 及请求/提供位置，不返回长期 Token。
- access log 中的 `served` 只表示中央已准备并返回成功响应，不表示 AI 已阅读、理解或向用户展示；有效读取者的游标错误也可以保存安全错误码。记录可包含请求 ID、业务日、事件与重要程度计数、返回的事件 ID、背景生成时间、理解版本、响应 hash、字节数和耗时。
- access log 不保存或返回 Token、Authorization、完整配对文本、完整响应正文、数据库查询或原始 GPS。
- 删除 reader 会撤销长期凭据；后续读取返回 `401`，已存在事实和最小审计记录不被删除。
- 时间线按当前有效 reader 的当前 epoch 访问记录动态返回 `ai_reader.state`，不会给每条事件增加长期送达字段。清理进度会增加 epoch 并清空当前标记，但保留历史访问日志。
- 所有低优先级事件的动态状态均为 `not_applicable`，WebUI 不显示待提供/已提供标记；普通和高优先级事件才参与游标标记。
- 建立连接和成功提供上下文分别生成 `system.ai_reader_connected`、`system.ai_reader_context_served` 低优先级审计事件；它们面向用户显示、默认灰色，并随全部低优先级事件被中央排除在 AI reader 响应之外。
- `context-preview` 是“下一次规范读取”的只读预览：默认按该 reader 当前 epoch 最新已签发游标筛选当前业务日事件，返回 compact JSON，并始终展开完整 `understanding.items` 方便用户核对；它不会签发可用游标、写访问记录、推进标记、更新 reader 状态或创建审计事件。预览中的 `next_cursor` 只能是说明性占位值。

## 首版不做

首版不定义主动更新信号、Webhook、会话注入、AI 写入或回复、AI 修改事实/心愿/设置，也不定义已读 ACK。WebUI 只提供配对、状态、访问日志和读取进度管理，不持有 AI Token。
