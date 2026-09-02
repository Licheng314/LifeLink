# Life Link 中央服务：本地运行与诊断

中央服务是新架构的唯一长期事件库。本入口提供 v1 上传、SQLite 存储、设备/用量只读查询和本地诊断；不会启动 ActivityWatch 采集、Tailscale 发现、旧 `/push` 或 PC 间转发。

## 1. 初始化第一个设备

在 `central-server` 目录运行：

```powershell
& "$env:LocalAppData\Programs\Python\Python313\python.exe" central_server.py init --device-id "desktop-install-your-stable-id"
```

默认写入：

```text
%USERPROFILE%\LifeLink\central\config.json
%USERPROFILE%\LifeLink\central\life_radio.sqlite3
```

服务端的运行设置、设备凭据、公开入口、数据库、日志、媒体和运行身份统一保存在 `%USERPROFILE%\LifeLink\central`。源码版和任意发行目录共享这一份中央数据，不再把可变数据放进项目或程序目录。`LIFE_LINK_DATA_ROOT` 可为高级部署整体改写数据根目录。

`init` 使用系统安全随机数生成器创建不少于 32 字符的设备令牌，并在配置尚无 `read_token` 时自动生成一枚独立只读令牌。两类令牌只写入上述外部配置，不打印到终端、不写入服务日志，也不会写进仓库；重复执行 `init` 会保留已有 `read_token`。需要配置客户端时，由用户本人从该外部文件中复制与 `device_id` 对应的上传令牌，并单独保管 `read_token`。

每个 PC 或手机都必须使用自己的稳定 `device_id` 和独立令牌。增加另一台设备时，再运行一次 `init --device-id ...`；命令会追加凭据，不会静默替换已有设备的令牌。

如需指定外部位置：

```powershell
& "$env:LocalAppData\Programs\Python\Python313\python.exe" central_server.py init `
  --config "D:\LifeRadioPrivate\central.json" `
  --database "D:\LifeRadioPrivate\life_radio.sqlite3" `
  --device-id "android-install-your-stable-id"
```

中央服务的默认监听地址、端口和数据库位置定义在程序中。首次通过托盘启动时会自动生成上述唯一配置文件，同时生成所需凭据；命令行部署也可先执行 `central_server.py init`。仓库不再携带需要复制的配置模板，初始化生成的真实配置不得提交。

## 2. 启动

### 独立启动中央服务端

双击 `start_server.bat`。它只启动中央长期库和“Life Link 中央服务端”托盘图标，不启动 PC 采集、WebUI 或用时窗口。首次运行会在 `%USERPROFILE%\LifeLink\central` 创建必要配置与运行数据，并从默认 `127.0.0.1:8091` 开始选择可用端口；端口一旦写入配置，托盘、诊断和 Tailscale 都读取同一值。源码首次启动还会检查 Python 3.13（含 Tkinter）；缺失时询问用户是否通过 Windows `winget` 安装。若本模块的正式启动器尚不存在，会在 `%USERPROFILE%\LifeLink\tools\build-python` 安装仅用于构建的 PyInstaller，并生成 `central-server/LifeLink Central Service.exe`；启动器生成或确认存在后立即登记为当前用户登录后自动启动。MCP EXE 不在此时构建。每次手动运行 BAT 都会在中央服务就绪后显示远程连接检查菜单；已有地址默认保留，没有地址时必须配置 Tailscale 或 HTTPS。直接运行 EXE 和登录后自动启动保持静默。完整流程见根目录 [`README.md`](../README.md)。

托盘使用 Windows 默认“信息”图标，与 PC 客户端的默认应用图标区分。左键打开本地健康状态；右键提供打开服务状态、生成设备配对码、生成 AI 配对包、重启和退出中央服务。“生成 AI 配对包”调用本机 PC 服务的现役打包入口，由它统一签发一次性 AI 配对、装入 MCP EXE 与 Skill、替换旧导出包并打开其中的最新 ZIP；将该压缩包发送给目标 AI 即可继续连接。PC 客户端未运行时会明确失败，不在中央托盘中复制裸 AI 配对文本。AI 读取状态仍只在 PC WebUI 管理。退出中央托盘不会关闭 `pc-dashboard` 客户端。

中央服务的启动项直接指向带图标的 `LifeLink Central Service.exe`，不以“终端”、Python 或脚本宿主名义出现。源码检出使用本模块根目录 `central-server/LifeLink Central Service.exe`：它只是中央服务自己的小型入口，按自身位置启动同项目的 `central_server_app.py`，不打包中央服务本体。使用 `development/tools/build_source_launchers.ps1` 构建；项目移动后手动启动一次中央源码即可刷新启动项路径。发行包则直接指向其自身的正式 EXE。

登记启动项后会反查 `.lnk` 和 EXE 目标。Windows 或管理软件明确禁用时显示“已拦截”；偏好仍为开启但快捷方式被移除时显示“启动项缺失”。

### 接入另一台 PC（首选：一行邀请）

远端 PC 不再复制中央主机配置，也不与其他 PC 互相发现。首选流程是：

1. 中央主机右键“Life Link 中央服务端”托盘图标，选择“生成设备配对码”；它会生成并自动复制一段以 `LR1.` 开头、24 小时过期且只能领取一次的统一设备邀请文本。`maintenance/create_invitation.bat` 保留为命令行恢复入口，PC 与手机使用同一配对码、同一权限口径。
2. 远端 PC 双击 `start_central_client.bat`；尚未配置时会打开本机设置页，将整段邀请粘贴进去并确认。
3. 远端 PC 直接通过邀请中的 HTTPS 地址向中央领取自己的稳定 profile，随后安全写入 `%USERPROFILE%\LifeLink\client\config.json` 并启动客户端。

统一设备邀请包含上传权限和中央只读权限；PC 与 Android 领取后使用相同的设备权限口径。邀请本身在过期前属于短期秘密，不要写入文档、日志或 Git。

一行邀请码是 PC 与 Android 共用的唯一注册入口。领取失败时重新签发邀请码，不再使用“请求文件—签发 profile—传回导入”的双文件流程。

### 手动启动中央服务

使用默认配置：

```powershell
& "$env:LocalAppData\Programs\Python\Python313\python.exe" central_server.py
```

使用显式配置：

```powershell
& "$env:LocalAppData\Programs\Python\Python313\python.exe" central_server.py run `
  --config "D:\LifeRadioPrivate\central.json"
```

没有任何设备令牌时，生产启动会明确失败，并提示先执行 `init`。代码测试若确实需要空凭据服务，必须显式构造 `CentralConfig(..., allow_empty_tokens=True)`；外部配置和命令行不能打开此绕过项。

只监听 `127.0.0.1:<配置端口>`，首次默认尝试 `8091`。这适合本机验收，不代表已完成公网 HTTPS、云部署或反向代理配置。

## 3. PC WebUI 与 AI 读取中央数据

领取统一 `dashboard` 邀请后，PC 客户端 profile 会保存设备凭据和中央只读凭据；`LIFE_RADIO_CENTRAL_READ_TOKEN` 仍可作为部署时覆盖项。浏览器只访问本机同源的 `/api/devices`、`/api/usage`、`/api/locations` 和 `/api/ai-context/*`，由 PC 服务代理中央结果，不会把令牌放进页面、查询参数或响应。

中央读取已经覆盖设备、应用用量、位置视图以及用量/位置 AI Markdown 摘要。`/v1/read/locations` 还会按同一业务日窗口动态返回 `activity_state`，融合主手机步数/位置和全部设备 AFK 裁剪后的真实使用，并为每个区间提供可空的代表地址和经纬度。中央服务也提供受只读 Bearer Token 保护的 `/v1/read/*` 与 `/v1/read/ai/*.md`。`GET /v1/settings/shared` 返回中央权威的跨日设置和可空的主健康 Android 设备；更新默认使用 POST，并保留同语义 PATCH。写入只接受已注册设备凭据。

`GET /v1/calendar-days?from=YYYY-MM-DD&to=YYYY-MM-DD` 接受独立只读或任一已注册设备凭据，按共享 `Asia/Shanghai` 与 `day_start_hour` 返回含首含尾、最多 42 个业务日的可用状态和逻辑内容量。原始事件只按规范 `event_json` 的 UTF-8 字节计一次，时间线按其稳定用户可读持久字段和 JSON 字段计量；`usage`、`location`、`health`、`timeline` 与 `other` 五类互斥，合计不等同 SQLite 物理文件大小。该读取不会写入或迁移数据。

PC 新事件使用来源无关的 `app.foreground`、`device.input_state` 和 `web.foreground`：应用只记录进程身份，输入状态只记录 `active/afk/locked` 区间，网站只记录域名。中央继续读取既有 ActivityWatch 历史载荷，但不要求中央或 PC 启动 AW 主程序。

### AI reader 被动只读接入（v1.15.3）

中央服务托盘通过本机 PC 服务生成完整 MCP 连接包；WebUI 保留原“生成 AI 配对文本”的兼容实现，但隐藏其按钮，正常用户入口统一为 MCP 连接包。包内一次性配对信息有效 24 小时且只能领取一次。AI/Agent 在 loopback 调用 `POST /v1/ai-readers/pairings/claim` 后只会收到一次 90 天 AI 专用只读 Token，随后通过 `GET /v1/read/ai/context` 获取完整背景、增量时间线和版本化理解说明。该 Token 不具备上传、普通中央读取或管理权限，SQLite 只保存其 SHA-256。

默认 `GET /v1/read/ai/context` 即返回 compact：元数据后先给出理解说明，再将背景和当前状态压成文字，事件只保留本地时间、重要程度和正文，不传事件 ID/key/内部 evidence；报告仍使用完整冻结正文。需要完整结构时显式使用 `?view=full`。`GET /v1/read/ai/updates` 是无正文、无游标推进的轻量检查：当前业务日存在 reader 游标之后产生的高优先级提醒、报告或心愿/触发器关联事件时返回 `update_mcp=true`。

WebUI 已内置 MCP 连接包入口、reader 状态、最近一次访问、Skill 导出副本、无副作用下一次 compact 原文预览和“清理标记”；裸 AI 配对文本按钮仅隐藏兼容保留。管理接口只接受已注册设备凭据；预览按最新已签发游标筛选但不签发游标、不写日志、不推进标记，清理会增加 reader epoch，使 AI 自动重新读取当前业务日，不提供跳过积压。游标跨日自动切换，`served` 仅表示中央提供了响应。

个人模式只保持一个有效 AI reader；新身份领取会撤销旧 Token，历史记录保留审计。时间线会动态标出当前 AI 是否已获得普通/高优先级事件；所有低优先级事件都不提供给 AI，也不参与读取标记。AI 连接和访问会生成低优先级灰色审计事件。当前仅提供由 AI 轮询的轻量更新标志，不包含服务端主动推送、Webhook、会话注入或 AI 写入。用户已完成真实 AI 配对和读取验收。活动状态由中央按分钟执行步数与定位双来源证据门槛，过滤结果同时供位置视图和 AI 背景使用。

管理端 `GET /v1/ai-readers/{reader_id}/process-status` 仅向注册设备返回当前 Reader 的同机应用检测结果。新 claim 可选提供稳定 `process_binding`：原生应用使用精确 `.exe` 文件名；共享宿主应用使用精确宿主文件名和连续参数路径段。OpenClaw 使用 `node.exe + node_modules/openclaw`，不保存完整绝对路径、PID、启动时间或命令行。只有 `process_running=true` 才在 WebUI 显示绿色提示；该状态不影响 Token 有效性，也不引入心跳或主动投递。

## 心愿、时间线与触发器（v1.13.1；基础生命周期自 v1.12.0）

中央还保存跨设备共享的心愿、固定业务日记录、用户时间线和触发器配置：

- `/v1/wishes`、`/v1/wishes/{wish_id}`、每日评估与旧取消路径返回中央最终记录；新心愿从创建所在业务日开始计第 1 天。文字更新标准入口为 PATCH，同路径 POST 是 HTTPS 兼容入口，二者都只接受 `text`。
- `POST /v1/wishes/{wish_id}/complete` 只在最后业务日结束且所有固定日期已评估后接受手动完结；同一 SQLite 事务会归档心愿、停用关联提醒并追加一条完成天数汇总事件。
- `DELETE /v1/wishes/{wish_id}` 与 `POST /v1/wishes/{wish_id}/delete` 语义相同，可删除任意状态的心愿；同一 SQLite 写事务会删除其日期、关联触发器和纯心愿生命周期时间线。真实触发时间线仍保留但解除索引，原始 `events` 永不删除。
- 触发器更新和删除同样保留标准 PATCH/DELETE，并提供同路径 POST 更新及 `POST .../delete` 兼容入口。PC 与 Android 默认使用 POST，以适配会阻断 PATCH/DELETE 的公网 HTTPS 映射。
- 用户未手动完结时，到期心愿仍在相关读写前以惰性方式检查 72 小时宽限；未确认日期自动固化为未完成、归档并只追加一次周期完成时间线事件。取消或归档会自动禁用关联触发器，但保留其历史记录。
- 面向 AI 的心愿提示按当前业务日区分待填写日期：早于当前业务日的待填写需要提醒用户填写结果；当前业务日的待填写只是今天的进度，不需要提醒。用户已明确记录的“未完成”不属于待填写。
- `/v1/timeline-events` 按 UTC 时间范围读取用户时间线，不使用游标；不会把高频原始采集事件直接加入时间线。响应提供基于完整展示内容的 `ETag`，携带匹配的 `If-None-Match` 时返回无正文 `304`，因此删除、关联解除和 AI 已读取状态变化也不会被简单增量遗漏。
- `/v1/trigger-types` 和 `/v1/event-triggers` 提供四种预设类型的严格目录和配置 CRUD：黑名单用量、设备用量、晚睡检查和心愿定时提醒。用户只能在心愿内选择预设类型，不能创建新的触发器形式。
- 上传后判定器继续负责用量类心愿提醒；分钟调度负责定时心愿提醒、固定系统里程碑和报告槽位。设备用量沿用区间合并与 PC AFK 裁剪口径，黑名单按平台隔离匹配后汇总；晚睡从睡觉时间后 30 分钟开始每 30 分钟统一判断检查点前 15 分钟内的设备事实或心跳。配置的 `updated_at` 是新基线，不补发此前的过期周期；一次恢复只生成最新有效槽位。派生失败不会撤销已确认的原始事件。
- 低频 `custom.event` 仅将 `application.started` 与 `sedentary.reminder_triggered` 幂等投影到时间线；其他自定义键和高频事实不会自动进入时间线。

这些新资源的读取接受独立只读 Token 或已注册设备 Token；写入只接受已注册设备 Token。既有 `/v1/read/*` 与 AI 摘要仍只接受独立只读 Token，设备 Token 仍返回 403。

## 事件背景与分钟调度（v1.13.1）

`GET /v1/event-background?business_date=YYYY-MM-DD` 是受只读或已注册设备凭据保护的动态视图：它复用已有用量（PC AFK 裁剪）、黑名单、位置和活动派生，不写入原始事件。设备和应用只保留 15 分钟内的新鲜状态；位置可显示带更新时间的上次记录。位置与活动摘要会完整列出与生成时刻前 60 分钟有交集的活动区间，跨过一小时前边界的长区间保留原始起止和时长，不按显示窗口裁切。

中央服务启动一个可停止的单实例分钟循环；关闭服务时循环会一并停止。固定系统里程碑、心愿 `scheduled_reminder` 与启用的报告设置均使用稳定去重键，因此重复运行或重启不会重复生成事件。报告目前只保留 v1.13.2 的 `delivery.state=not_configured` 兼容展示，不会调用或模拟 AI；后续 AI 伴侣接入采用独立只读凭据主动读取。晚睡检查从睡觉时间后 30 分钟开始每 30 分钟执行，只看检查点前 15 分钟内的可验证设备事实/心跳，不把前台使用量当成在线时长。

## 设备名称与逻辑删除（v1.9.0）

- `/v1/devices` 返回当前未删除设备的管理名册。客户端最近上报的名称保存在 `reported_name`，用户设置的中央 `custom_name` 优先作为 `display_name`，后续上传不会覆盖它。
- `PATCH /v1/devices/{device_id}` 修改中央名称；同路径 POST 是公网传输兼容入口。
- `DELETE /v1/devices/{device_id}` 逻辑删除设备；`POST .../delete` 语义相同。删除会立即撤销该设备的 SQLite 凭据、从外部中央配置移除永久 Token，并停用明确绑定它的设备用量触发器。
- 删除不改写或删除 `events`、`batches`、稳定 `device_id` 和时间线原始行。当天已经产生的事实仍可进入当天统计；之后没有窗口事实时不再显示该设备。
- 请求设备不能删除自身。已删除设备重新使用时必须重新领取邀请，由中央签发新 Token；旧 Token 在中央重启后也不会恢复。
- 时间线读取会通过稳定设备 ID 动态附加 `device_display_name`；设备用量里程碑标题也按当前中央名称投影，不批量改写历史行。

## 健康信息（v1.10.1）

- 公开 `GET /v1/health` 继续只做服务器存活检查；个人健康数据使用独立的 `GET /v1/health-info?date=YYYY-MM-DD`，接受只读 Token 或已注册设备 Token。
- 睡眠参考以请求日期为醒来日，在前一日 21:00 至当日 12:00 的窗口内综合 Android 前台应用与 PC 前台窗口/明确 `not-afk` 交集。结果是可解释的活动空档估算，不是医疗睡眠数据。
- 当前主要区间一旦被醒后最早真实使用闭合就立即返回 `final`，完成时刻等于该使用的发生时刻，不等待中午；同时返回睡前和醒后边界设备。后续若证明该使用只是一次不超过 30 分钟的夜间中断，动态重算可把区间延伸到下一次闭合边界。
- 睡眠结果同时返回边界处的应用、平台和设备名称，供用户核验“睡前最后应用”和“起床后第一应用”；这些字段只解释估算依据，不额外断言应用由用户主动触发。
- Android `health.steps_observation` 保存计步器累计观察；中央只在同一设备、同一 `counter_session_id` 内差分，并把增量归到后一观察的 `Asia/Shanghai` 本地日期。多台手机分别返回，不自动求和。
- 睡眠与步数均从原始 `events` 动态重算，不增加日汇总表；算法变化不会改写历史原始事件。现役接口和字段以 [`../development/contracts/life-radio-api-v1.yaml`](../development/contracts/life-radio-api-v1.yaml) 为准；形成过程可查阅 [`../development/docs/已归档/健康信息-后端数据设计.md`](../development/docs/已归档/健康信息-后端数据设计.md)。

2026-08-01 已验证本机闭环与花生壳公网入口：中央健康检查正常，PC outbox 得到 ACK，`/api/usage` 从中央库返回了真实的当日应用、网站和设备使用时长；公网入口的匿名读写被拒绝，带 Token 读取和真实事件写入成功。第二台真实 PC 已使用邀请完成注册并连接，PC 领取流程已越过纯自动化测试阶段。

Android 与 PC 使用同一套 `LR1` 邀请和设备权限口径。真实手机已经完成领取、应用用量上传和位置观察上传；当前源码与发布包版本以 `mobile-app/app/build.gradle.kts` 和 `mobile-app/RELEASES.md` 为准，不在本 README 固定易过时的版本号。

## 4. 只读诊断

```powershell
& "$env:LocalAppData\Programs\Python\Python313\python.exe" central_server.py diagnose
```

或：

```powershell
& "$env:LocalAppData\Programs\Python\Python313\python.exe" central_server.py diagnose `
  --config "D:\LifeRadioPrivate\central.json"
```

诊断以 SQLite 只读模式打开数据库，只输出：数据库路径、journal/WAL 模式、设备数、事件数和批次数。它不会创建数据库、变更令牌状态、读取 payload 或打印令牌。

## 配置优先级

1. 命令行的 `--host`、`--port`、`--database`；
2. `LIFE_RADIO_CENTRAL_TOKENS_JSON`、`LIFE_RADIO_CENTRAL_READ_TOKEN`、`LIFE_RADIO_CENTRAL_DATABASE`、`LIFE_RADIO_CENTRAL_HOST`、`LIFE_RADIO_CENTRAL_PORT`；
3. `LIFE_RADIO_CENTRAL_CONFIG` 所选择的外部 JSON 内容；
4. 未显式选择配置时，自动读取 `%USERPROFILE%\LifeLink\central\config.json`；
5. 安全默认值：本机监听、外部数据目录、无令牌并拒绝启动。

`token_bindings` 的 JSON 结构是 `{ "高熵令牌": "稳定设备 ID" }`。配置加载会拒绝少于 32 字符、包含空白或明显低熵的令牌。

## 花生壳固定公网入口

花生壳映射必须指向中央服务 `127.0.0.1:<配置端口>`，不能指向 PC Dashboard 的 `8090`。Dashboard 只应在本机打开。与中央服务同机运行的 PC 客户端可使用相同配置端口的回环地址；手机、远程 PC 和远程 AI 使用中央 `config.json` 中 `public_endpoint.base_url` 的 HTTPS 地址。初始化向导不提供仅本机模式。

先双击 `start_server.bat` 启动中央服务，再双击 `maintenance/configure_public_endpoint.bat` 并粘贴花生壳 HTTPS 地址。配置工具会同时验证：公网可达、`role=central`、只读令牌可用。验证成功后只保存服务商、公开地址和验证时间，不复制或保存任何新令牌。

如需在同一 Tailnet 内私密访问，手动 BAT 的连接菜单会按中央配置端口创建或刷新独立的 `8443 -> 127.0.0.1:<配置端口>` Tailscale HTTPS 转发，并登记 `https://<设备名>.<tailnet>.ts.net:8443`；若 8443 已被其他服务使用，会停止而不覆盖。独立的 `maintenance/configure_tailscale_endpoint.bat` 保留为恢复入口，不触碰其他应用占用的根地址 443。

2026-08-01 已使用花生壳固定地址完成真实公网验收：匿名读取和上传均返回 401；自动生成的独立只读令牌可以读取中央数据；本机设备令牌成功写入一条 `central.public_endpoint_verified` 诊断事件，并收到 1 条确认、0 条拒绝的逐事件 ACK。

## 当前幂等边界

- `batch_id` 是中央库全局唯一的 UUID，不按设备分区；所有客户端必须生成随机 UUID，不能使用自增编号或重复模板值。
- 同一 `batch_id` 与同一规范化请求内容重试，会返回首次持久化的原始 ACK。
- 同一 `batch_id` 携带不同内容会返回 `409 idempotency_conflict`，且不产生部分写入。
- `event_id` 同样全局唯一；显示名、hostname、IP 和 ActivityWatch bucket 都不是设备身份。
