# WebUI 源码导航

这里是 PC Dashboard 的零构建静态前端。修改后直接重启本地服务或刷新页面，不需要 npm、打包器或前端框架。

WebUI 是 `pc-dashboard` 的内部子系统，不是与中央、Android 或契约并列的新顶层模块。浏览器只访问本机同源 `/api/*`，由 `sync_server.py` 代理中央服务；页面不得接触中央秘密凭据。

## 现役页面

| 页面 | 主要脚本 | 主要数据入口 | 说明 |
| --- | --- | --- | --- |
| 事件时间线与心愿 | `wishes-events.js` | `/api/wishes`、`/api/timeline-events`、`/api/event-triggers`、`/api/event-background`、`/api/settings`、`/api/ai-readers*` | 今日总控首页：实时本地时间、AI 配对/访问状态，以及当前 Reader 配对时保存的同机应用进程检测；仅检测成功时显示带绿灯的独立提示，不改变 Reader 配对状态。业务日时间轴将完整跨日窗口映射为横向事件总览，按发生时刻放置事件图标并显示当前时间线；下方今日时间线展示同一事件集合的正文、重要程度和 AI 提供状态。当前心愿区提供“往期心愿”按钮，按需在只读弹窗中显示全部往期心愿的固定每日评估，不展示触发器。AI 操作区以 MCP 连接 ZIP 为正常入口，并可单独导出现役 Skill；原配对文本按钮隐藏但实现保留。另含设备今日连接窄条、发送原文预览/清理访问标记、报告设置和心愿全生命周期 |
| 设备管理 | `devices.js` | `/api/device-management`、`/api/sync/central`、`/api/settings` | 设备名册、中央名称编辑、逻辑删除、共享跨日时间和同步状态；客户端不签发设备邀请码 |
| 应用使用 | `usage.js` | `/api/usage`、`/api/blacklist/rules` | 用量、网站、AFK 裁剪后的设备使用时长和黑名单统计 |
| 健康信息 | `health-info.js` | `/api/health-info?date=...` | 昨晚/本周睡眠参考、睡前/醒后边界应用与今日/本周步数；按 Android 主设备独立显示，不跨设备合计 |
| 位置轨迹 | `location.js` | `/api/locations`、`/api/settings`、`/map-tiles/*` | 停留地图（Leaflet+天地图，仅用定位数据）、业务日活动状态图、带代表地址/坐标及横向距离条的区间列表和主手机选择；不再展示旧位置段行程列表 |

## 页面骨架与样式

- `../dashboard.html`：导航、各页面容器和脚本加载顺序；不要重新塞入大段样式或业务脚本。
- `styles/base.css`：全局变量、基础布局、导航和通用响应式规则。
- `styles/components.css`：卡片、图表、弹窗、表单、设备和睡眠等通用组件。
- `styles/wishes-events.css`：心愿与事件时间线、业务日时间轴、停留地图。
- `vendor/leaflet/leaflet.css` + `leaflet.js`：Leaflet 1.9.4 本地 vendor，地图渲染依赖；不通过 CDN 加载。

## 脚本职责

- `scripts/wishes-events.js`：事件背景摘要、AI 理解说明、AI reader 内置管理、共享报告设置、心愿、每日评估、提醒挂接、业务日事件总览时间轴、事件明细时间线和中央黑名单规则读取；浏览器不接触 AI Token 或中央设备 Token。
- `scripts/shared-ui.js`：页面切换、图表公共色板和黑名单基础匹配。
- `scripts/usage.js`：应用/网站用量、黑名单展示与统计图。
- `scripts/health-info.js`：健康信息页的昨晚睡眠、睡前/醒后应用核验信息、本周睡眠、今日步数、本周每日步数与离线状态。
- `scripts/location.js`：位置页的停留地图（Leaflet+天地图瓦片代理，停留标记按时长渐变、渐变色轨迹连线+方向箭头、起终点标记、GPS 漂移离群点过滤）、分钟级活动状态图、活动区间列表及主手机共享设置。
- `scripts/devices.js`：设备状态、同步状态和设备管理。
- `scripts/app.js`：全局状态、定时刷新和启动编排。

全页面共用左侧业务日周历：`GET /api/calendar-days?from=...&to=...` 提供可用日期和逻辑数据量摘要；选中的历史业务日保留在 `?date=YYYY-MM-DD`。时间线、用量、位置和健康读取该日期；当前心愿、按需打开的往期心愿弹窗与设备/报告等管理状态不随日期变化。往期弹窗通过既有 `/api/wishes?include_archived=true` 读取，并逐条显示固定每日评估结果（已完成、未完成或未评估），不展示触发器。历史日不执行 30 秒时间线刷新。

这些文件仍使用经典脚本共享同一页面作用域，必须保持 `dashboard.html` 中的现有加载顺序。当前分段刻意保留了原单文件的执行顺序，因此少量共享状态仍位于相邻职责文件中；后续只能在有专项测试时继续搬动。只属于单页的新逻辑应留在对应文件，避免重新形成一个巨型脚本。

## 首页性能门禁

- 首页先确认共享跨日边界，再并行读取并渲染心愿、提醒和时间线；设备/用量摘要、设备管理名称和 AI reader 管理诊断随后分层补齐，单项失败不得阻塞首屏。
- 首页没有背景摘要容器，不自动请求 `/api/event-background`；发送原文按用户操作读取，避免同一轮与 `/api/usage`、`/api/devices` 重复派生。
- PC 的 v1.7+ 只读缓存使用规范化精确键、分资源和全局双重淘汰、8 MiB 文件上限及进程内快照；超限旧缓存移出活跃路径并保留带时间戳备份，不再逐请求读取和重写持续膨胀的总 JSON。

## Agent 最短接入

1. 读取仓库 `AGENTS.md`、`development/docs/README.md`、`pc-dashboard/README.md` 和本文件。
2. 检查当前 Git 提交与未提交差异，只打开本次页面对应的 JS、CSS、HTML 容器和测试。
3. 修改前明确目标、不做事项和允许文件；同一前端文件同时只保留一个主要负责人。
4. 状态以 Git 差异和自动化测试为准，不在本文件追加开发日志。尚未完成的跨代理任务使用 `development/docs/协作/任务模板.md`，完成后把稳定结论并回现役文档并结束任务单。
5. 至少运行 `tests.test_dashboard_central_ui` 和受影响代理测试；代码完成、自动化通过和真实页面验收分别汇报。

## 静态资源边界

本地服务只允许访问 `sync_server.py` 中 `WEB_ASSETS` 列出的精确路径。新增或改名 CSS/JS 时，需要同时修改 HTML 引用、白名单和静态资源测试；不要改成任意目录映射。`vendor/leaflet/` 下的 Leaflet 资源同样已在 `WEB_ASSETS` 中登记。
