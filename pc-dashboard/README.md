# Life Link PC 客户端

PC 项目只承担三件事：用 Windows 原生接口采集必要的本机使用事实、可靠上传到中央服务，以及提供托盘与用时状态小窗。完整 Dashboard 已迁移至中央 HTTPS WebUI；PC 不再提供本地数据展示页面或其静态资源。它不再依赖 ActivityWatch 主程序，不再发现其他客户端，也不在 PC 之间直接同步。

## 启动

双击 `start_central_client.bat`。首次启动会打开本机设置页，粘贴中央服务生成的 `LR1.` 邀请码即可完成注册；PC 与 Android 使用同一种邀请码和权限口径。

同一 Windows 用户只使用 `%USERPROFILE%\LifeLink\client` 这一份客户端数据。源码版、免安装发行版和不同安装目录共享同一 `identity.json`、配置与 outbox，因此不会因为换目录或覆盖升级而注册成新设备。旧安装目录或 `%LOCALAPPDATA%\LifeRadio\client` 只在唯一目录尚无身份时迁移；若双方身份冲突则拒绝覆盖，由用户明确选择。

注册后，长期配置保存在：

```text
%USERPROFILE%\LifeLink\client\config.json
```

本地采集服务只监听 `127.0.0.1`，默认端口 `8090`。它只服务采集、上传、浏览器插件兼容和状态小窗；访问其旧 Dashboard 地址会明确返回“已迁移”。托盘程序负责启动本地服务、通过已配对凭据安全打开中央 HTTPS Dashboard、显示用时小窗和退出程序；右键菜单中的“开机启动”是仅属于当前 Windows PC 的勾选项，切换时只修改本客户端的登录后启动快捷方式。

登记启动项后会反查 `.lnk` 和 EXE 目标。WebUI 会区分 Windows/管理软件明确拦截与“许可仍开启但启动项被移除”，后者可通过重新开启选项修复。

PC 启动器生成或确认存在后，默认立即登记为当前 Windows 用户登录后启动，不需要管理员权限；完成配对后以后台方式启动，不会自动打开浏览器或用时小窗，未配对时则进入客户端自己的配对引导。启动项直接指向带图标的 `LifeLink PC Client.exe`，并会在 WebUI 提示 Windows 是否已拦截它；用户显式重新开启时只恢复 Life Link 自己的启动许可。源码检出使用本模块根目录 `pc-dashboard/LifeLink PC Client.exe`：它只是 PC 客户端自己的小型入口，按自身位置启动同项目的 `start_central_client.py --background-start`，不打包 PC 项目本体。首次双击 `start_central_client.bat` 会复用已验证的 Python 3.14 或 3.13（优先 3.14，且必须包含 Tkinter）；两者都没有时才询问是否通过 Windows `winget` 安装 3.14。首次启动、生成的源码启动器和登录启动使用同一选择规则。若本模块的正式启动器尚不存在，会在 `%USERPROFILE%\LifeLink\tools\build-python` 安装仅用于构建的 PyInstaller，并生成 `pc-dashboard/LifeLink PC Client.exe`，随后立即登记启动项；MCP EXE 仍只在需要生成 AI 配对包时构建。项目移动后手动启动一次客户端源码即可刷新启动项路径。发行包升级或更换目录无需复制用户数据，身份和配对状态来自固定用户目录。

PC 客户端只使用 `%USERPROFILE%\LifeLink\client\config.json`。首次启动会写入本地端口、应用使用采集开关和项目公共天地图 Key；完成配对后，同一文件再保存中央地址、设备身份和凭据。旧配置中的 `activitywatch_url` 会在初始化时安全移除。`LIFE_LINK_DATA_ROOT` 可为高级部署整体改写 Life Link 数据根目录。

只读诊断恢复入口位于 `maintenance/diagnose_client.bat`；它不属于日常启动流程。

当中央服务也运行在本机时，客户端配置的 `central_base_url` 使用 `http://127.0.0.1:8091`，上传、WebUI 代理和小窗读取均不经过公网隧道。公网 HTTPS 地址保存在中央唯一配置的 `public_endpoint` 字段，只用于手机、远程 PC、远程 AI 和设备配对码；两种地址用途不得混淆。

## 数据流

```text
Windows 原生应用/输入状态 ─┐
官方 AW 浏览器插件（可选） ├→ PC 本地 outbox → HTTPS → 中央 SQLite
                           ↓
中央 HTTPS WebUI ← 已配对 PC 发起的一次性浏览器会话 ← 中央 SQLite / 派生视图
```

- `POST /api/sync/central`：立即尝试上传本机原生采集器已写入 outbox 的事件。
- `GET /api/sync/central`：查看本机 outbox、最近上传结果与配置状态。
- `/api/live-usage`：读取本机原生采集器的轻量实时快照，不等待中央同步周期。
- `/api/timeline-events`：用时小窗读取当前业务日事件；每 30 秒用 `ETag` 检查中央版本，未变化时不重新下载正文。小窗只显示尚未发生的未来事件之外的内容。
- `/api/settings`：用时小窗读取共享跨日起点；本地保存最近一次中央确认的只读缓存。
- `/api/custom-events`：Windows 客户端写入本机启动、久坐等低频事件，随后通过 outbox 上传中央。
- `/api/0/*`：仅为可选的官方 ActivityWatch 浏览器插件保留的最小兼容接口；不属于 Dashboard，也不展示个人数据。
- 事件时间线中的 `occurred_at` 始终是 UTC；WebUI 按中央共享 `timezone` 转换后显示，不直接截取 UTC 字符串。
- 事件时间线、设备管理、应用使用、位置、健康信息和小工具均由中央 WebUI 直接读取中央事实；PC 本地服务不再代理这些页面的数据或地图瓦片。
- 应用与网站排行最多显示 10 条。现役黑名单管理继续读取中央规则；早期隐藏的黑名单饼图和 `settings.json` 明细表不再属于运行页面。

## 已移除的 PC WebUI

旧 `dashboard.html`、`web/` 静态资源及其页面测试均已移除；中央 WebUI 的现役源码位于 `central-server/management-web/`。不得重新建立本地 Dashboard 路由或复制中央数据展示逻辑回 PC。

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
