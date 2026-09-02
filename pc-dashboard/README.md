# Life Link PC 客户端与 WebUI

PC 项目只承担三件事：用 Windows 原生接口采集必要的本机使用事实、可靠上传到中央服务、在本机 WebUI 中代理读取中央数据。它不再依赖 ActivityWatch 主程序，不再发现其他客户端，也不在 PC 之间直接同步。

## 启动

双击 `start_central_client.bat`。首次启动会打开本机设置页，粘贴中央服务生成的 `LR1.` 邀请码即可完成注册；PC 与 Android 使用同一种邀请码和权限口径。

同一 Windows 用户只使用 `%USERPROFILE%\LifeLink\client` 这一份客户端数据。源码版、免安装发行版和不同安装目录共享同一 `identity.json`、配置与 outbox，因此不会因为换目录或覆盖升级而注册成新设备。旧安装目录或 `%LOCALAPPDATA%\LifeRadio\client` 只在唯一目录尚无身份时迁移；若双方身份冲突则拒绝覆盖，由用户明确选择。

注册后，长期配置保存在：

```text
%USERPROFILE%\LifeLink\client\config.json
```

本地 Dashboard 只监听 `127.0.0.1`，默认端口 `8090`。托盘程序负责启动本地服务、打开 Dashboard、显示用时小窗和退出程序。

登记启动项后会反查 `.lnk` 和 EXE 目标。WebUI 会区分 Windows/管理软件明确拦截与“许可仍开启但启动项被移除”，后者可通过重新开启选项修复。

PC 启动器生成或确认存在后，默认立即登记为当前 Windows 用户登录后启动，不需要管理员权限；完成配对后以后台方式启动，不会自动打开浏览器或用时小窗，未配对时则进入客户端自己的配对引导。启动项直接指向带图标的 `LifeLink PC Client.exe`，并会在 WebUI 提示 Windows 是否已拦截它；用户显式重新开启时只恢复 LifeLink 自己的启动许可。源码检出使用本模块根目录 `pc-dashboard/LifeLink PC Client.exe`：它只是 PC 客户端自己的小型入口，按自身位置启动同项目的 `start_central_client.py --background-start`，不打包 PC 项目本体。首次双击 `start_central_client.bat` 会检查 Python 3.13（含 Tkinter）；缺失时询问用户是否通过 Windows `winget` 安装。若本模块的正式启动器尚不存在，会在 `%USERPROFILE%\LifeLink\tools\build-python` 安装仅用于构建的 PyInstaller，并生成 `pc-dashboard/LifeLink PC Client.exe`，随后立即登记启动项；MCP EXE 仍只在需要生成 AI 配对包时构建。项目移动后手动启动一次客户端源码即可刷新启动项路径。发行包升级或更换目录无需复制用户数据，身份和配对状态来自固定用户目录。

PC 客户端只使用 `%USERPROFILE%\LifeLink\client\config.json`。首次启动会写入本地端口、应用使用采集开关和项目公共天地图 Key；完成配对后，同一文件再保存中央地址、设备身份和凭据。旧配置中的 `activitywatch_url` 会在初始化时安全移除。`LIFE_LINK_DATA_ROOT` 可为高级部署整体改写 Life Link 数据根目录。

只读诊断恢复入口位于 `maintenance/diagnose_client.bat`；它不属于日常启动流程。

当中央服务也运行在本机时，客户端配置的 `central_base_url` 使用 `http://127.0.0.1:8091`，上传、WebUI 代理和小窗读取均不经过公网隧道。公网 HTTPS 地址保存在中央唯一配置的 `public_endpoint` 字段，只用于手机、远程 PC、远程 AI 和设备配对码；两种地址用途不得混淆。

## 数据流

```text
Windows 原生应用/输入状态 ─┐
官方 AW 浏览器插件（可选） ├→ PC 本地 outbox → HTTPS → 中央 SQLite
                           ↓
浏览器 WebUI ← PC 本地只读代理 ← 中央查询与 AI 摘要
```

- `POST /api/sync/central`：立即尝试上传本机原生采集器已写入 outbox 的事件。
- `GET /api/sync/central`：查看本机 outbox、最近上传结果与配置状态。
- `/api/devices`、`/api/usage`、`/api/locations`：由本地服务代理中央只读结果；位置响应包含按共享跨日窗口计算的活动状态及可空的区间代表位置，不扫描其他 PC。
- `/api/health-info?date=YYYY-MM-DD`：代理中央健康信息；按日期保存最近一次成功的只读响应，中央暂不可用时返回缓存并标记离线。
- `/api/calendar-days?from=YYYY-MM-DD&to=YYYY-MM-DD`：同源代理中央业务日周历摘要；`from/to` 为含首含尾的业务日期，最多 42 日，返回可用日期及中央长期库的逻辑内容字节数（不是 SQLite 文件体积）。
- `/api/settings`：共享跨日起点、主健康 Android 设备、睡觉时间、早晚报和定时总结设置；AI 显示名称只读投影当前有效 reader 身份，不可手工修改。本地只保存完整的中央确认缓存。
- `/api/ai-readers*`：本机 WebUI 到中央管理接口的同源代理，提供配对文本生成、reader 状态/最近访问、按配对绑定检测的同机应用进程状态、无副作用下一次 compact 原文预览和清理访问标记；`POST /api/ai-reader-skill/open` 只生成并打开 PC 数据目录中的 Skill 副本。`POST /api/ai-reader-connection-package/open` 生成包含 Windows MCP stdio EXE、现役中文 Skill、一次性配对材料、通用配置样例和自解释说明的 ZIP，并打开其所在的最新导出目录，供用户直接交给目标 AI。浏览器不会获得长期 AI Token 或中央设备 Token，进程状态也不参与 Token 有效性判定。
- `/api/live-usage`：读取本机原生采集器的轻量实时快照，不等待中央同步周期。
- `/api/wishes`、`/api/timeline-events`、`/api/trigger-types`、`/api/event-triggers`：同源代理中央资源；包括心愿每日评估、到期后手动完结、72 小时自动兜底和固定时间提醒。小窗和 WebUI 对当前业务日使用同一个固定查询窗口并共享本地时间线副本；30 秒刷新先用 `ETag` 向中央确认版本，未变化时不重复下载完整正文。浏览器仍使用本地 PATCH/DELETE 语义，本地代理向公网中央转换为 POST 兼容请求，浏览器不会获得中央 Token。
- `/api/event-background?business_date=YYYY-MM-DD`：同源代理中央 v1.13.1 的动态背景摘要、当前应用/位置/活动状态与 AI 理解说明；设备和应用只显示 15 分钟内仍在线设备并相邻排列。它与时间线缓存一样仅在网络或响应损坏时使用最近成功的只读副本。
- `/api/device-management`：读取中央当前设备名册；`PATCH/DELETE /api/device-management/{device_id}` 分别执行重命名和逻辑删除，本地代理向中央转换为 v1.9.0 POST 兼容请求。它与按日期读取数据的 `/api/devices` 不是同一接口。
- `/map-tiles/{vec|cva}/{z}/{x}/{y}.png`：天地图瓦片本地代理。程序首次初始化时把项目方共享默认 Key 写入本机客户端配置，使普通用户无需自行申请；环境变量 `LIFE_RADIO_TIANDITU_KEY` 可在部署时覆盖。浏览器请求中不出现 Key，但源码及发行物中的共享 Key 是可被提取的公共配额凭据，不具备个人数据权限并必须支持监控与轮换。`vec` 为矢量底图、`cva` 为矢量注记；坐标系 CGCS2000≈WGS-84，与 Android FusedLocation 原生坐标系一致，无需转换。
- 事件时间线中的 `occurred_at` 始终是 UTC；WebUI 按中央共享 `timezone` 转换后显示，不直接截取 UTC 字符串。
- WebUI 现役页面只有事件时间线、设备管理、应用使用、位置、健康信息和小工具。首页的业务日时间轴按共享跨日起点展示当天事件分布和当前时间，下方事件列表展示同一批事件的正文、优先级与 AI 提供状态；位置页地图按定位观察与停留事实展示轨迹，不依赖步数。共享跨日时间设置归属设备管理；健康信息只展示中央派生的睡眠参考与按 Android 设备拆分的步数；旧模拟时间线、独立自定义事件页以及无入口的账本、笔记、媒体模板已经移除。
- 应用与网站排行最多显示 10 条。现役黑名单管理继续读取中央规则；早期隐藏的黑名单饼图和 `settings.json` 明细表不再属于运行页面。

## WebUI 源码结构

WebUI 不使用前端框架、包管理器或构建步骤。`dashboard.html` 只保留页面骨架，并按顺序加载 `web/styles/` 与 `web/scripts/` 中的静态文件；页面、脚本职责和依赖顺序见 [`web/README.md`](web/README.md)。新增静态文件时，还必须在 `sync_server.py` 的 `WEB_ASSETS` 白名单中显式登记，不能开放目录浏览或任意文件读取。

第三方库仅 Leaflet 1.9.4（地图渲染），vendor 在 `web/vendor/leaflet/` 下本地加载，不依赖 CDN。

## Windows 原生采集与浏览器插件

PC 客户端每秒读取当前前台进程和 Windows 输入空闲状态，按状态变化形成区间；开放区间以稳定事件 ID 定期修订，因此不会每秒生成新文件或新事件。进程路径、窗口标题和窗口句柄不落库。历史 ActivityWatch 事件仍可查询，但启动与同步不再回读 AW window/AFK bucket。

PC 的“设备使用时长”是前台应用区间减去明确 `afk/locked` 的重叠部分；Android 保持应用前台区间口径。网站采集可继续使用官方 AW 浏览器插件：Life Link 只在 `127.0.0.1:5600` 提供最小兼容接口，只保留域名，不保存完整 URL、页面标题或无痕标签页。若 5600 被占用，WebUI 会提示网站采集停用，原生应用与输入状态采集不受影响。

## 本地文件

- 设备身份：`%USERPROFILE%\LifeLink\client\identity.json`
- 客户端配置：`%USERPROFILE%\LifeLink\client\config.json`
- 上传队列：`%USERPROFILE%\LifeLink\client\outbox.sqlite3`
- 黑名单离线缓存：`%USERPROFILE%\LifeLink\client\blacklist_cache.json`
- 天地图瓦片 Key：程序默认值在首次启动时进入本机唯一配置；环境变量可覆盖。共享值按公开配额凭据管理，不得赋予个人数据权限。

共享默认 Key 的目标是降低首次安装门槛，不是把它当作秘密授权。正式发布前应确认天地图许可允许相应分发和代理方式，并准备配额监控、Key 轮换与无地图时的页面降级；地图不可用不得阻断位置摘要、活动状态或其他核心数据链路。
- 共享跨日设置只读缓存：`%USERPROFILE%\LifeLink\client\data\shared_settings_cache.json`
- 心愿、时间线和触发器只读缓存：`%USERPROFILE%\LifeLink\client\data\v17_read_cache.json`（按中央地址及规范化查询键隔离；时间线同时供小窗和 WebUI 使用；每类最多保留 4 项、全局最多 32 项且文件最多 8 MiB）。中央失败时可回退；在线时间线则用条件校验避免重复正文。旧文件超过 8 MiB 时不再解析，会原子改名为带时间戳的 `v17_read_cache.oversized-*.bak` 后重建；备份由用户确认实机恢复正常后自行删除。
- 本地采集与只读缓存：`%USERPROFILE%\LifeLink\client\data`。Windows 原生采集事件直接进入来源无关的 SQLite outbox，不生成按日高频 `devices` JSON；低频 `custom.event` 暂保留本地镜像供离线桌面交互。

这些是用户运行数据，不属于源码仓库。清理项目时不得自动删除。

空间治理规则：启动时只清理 `v17_read_cache.json` 原子写入中断留下的同名隐藏 `.tmp`；中央同步后，当前 PC 的旧 AW 镜像仅在文件内所有当前版本均已获得中央 ACK 时删除。Outbox 每日把两天前已确认的应用、输入状态和网站事实完整正文压成来源无关的 `event_id + revision + content_hash` 指纹，保留版本单调性；pending、rejected 和自定义事件正文不清理，两天前已完成批次可删除并在空闲时实际压缩 SQLite。原生开放区间只修订同一行，不随采样频率膨胀。其他设备遗留数据不自动删除。Dashboard 高频请求不再逐条写入 `sync_server.log`；客户端重启时该日志只保留最近 5 MB，并最多保留 2 份历史尾部。

## AI 上下文

- `/api/ai-context/index.json?date=YYYY-MM-DD`
- `/api/ai-context/usage.md?date=YYYY-MM-DD`
- `/api/ai-context/location.md?date=YYYY-MM-DD`
- `/api/ai-context/README.md`

摘要事实来自中央长期库；本地服务只负责代理和落地当天生成的 Markdown 副本。

## 验证

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

中央协议和跨端行为还需同时运行 `central-server/tests`、`development/contracts/tests` 与 `development/integration-tests`。真实联调时至少确认：本机 outbox 得到逐事件确认、中央能读到 PC/Android 用量与位置、WebUI 不出现本机重复设备卡片。

## 已移除边界

旧 Tailscale 发现、PC-to-PC 广播、`/push` 入站、每远端送达账本、强制同步到全部 PC、旧平面数据迁移工具和双文件注册流程不再属于 PC 客户端。历史需求只在外置参考项目或 Git 历史中查阅，不重新接回当前运行路径。
