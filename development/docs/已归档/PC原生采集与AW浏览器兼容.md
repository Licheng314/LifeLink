# 任务：PC 原生采集与 AW 浏览器兼容

状态：已完成并通过真实 Windows、官方浏览器插件及跨设备数据验收

## 总目标

以 Life Link PC 客户端内置的 Windows 原生前台应用与输入状态采集替换 ActivityWatch 主程序依赖；可选继续接收官方 AW 浏览器插件的网站心跳。5600 端口冲突时只提示，不抢占、不结束进程，应用采集不受影响。

## 明确不做

- 不删除、迁移或改写历史 AW 事件。
- 不长期双写 AW 与原生前台/AFK 数据。
- 不开发或自动安装自有浏览器扩展。
- 不保存完整 URL、查询参数、页面标题或无痕标签页。
- 不修改 Android 采集。

## 数据与契约

- 原生前台应用继续使用 `app.foreground`，来源为 `windows_native`。
- 输入状态使用来源无关的 PC 状态事实；中央继续兼容旧 `activitywatch.kind=afk`。
- 浏览器插件心跳规范化为网站前台事实，仅保留域名。
- 新实现写入现有 `runtime/client/outbox.sqlite3`，不新增长期数据库。

## 子任务 A：Windows 原生采集器

- 允许修改：新增 `pc-dashboard/windows_native_collector.py` 与对应专项测试。
- 负责 Windows 前台窗口、进程名、键鼠空闲、锁屏/不可采集状态的区间状态机；不得编辑 `sync_server.py`。
- 验收：应用切换、AFK 边界、恢复、异常采样、稳定事件 ID 与可恢复 checkpoint 均有测试。

## 子任务 B：AW 浏览器兼容接收器

- 允许修改：新增 `pc-dashboard/aw_web_compat.py` 与对应专项测试。
- 只实现官方插件所需的 loopback 最小 API、严格 bucket/payload 校验、域名最小化与无痕丢弃；不得编辑 `sync_server.py`。
- 验收：bucket 创建、heartbeat 合并输入、CORS、无痕/非法 URL、5600 占用均有测试。

## 子任务 C：共享契约

- 允许修改：`development/contracts/` 内契约、fixture 和专项测试。
- 采用向后兼容的来源无关字段，保留旧 ActivityWatch 格式读取；不得修改运行代码。
- 验收：契约测试通过，fixture 不含隐私 URL、Token 或真实个人数据。

## 总控集成

- 修改 PC 同步、实时状态、配置、启动/关闭、WebUI/托盘状态提示及中央派生兼容。
- 5600 被占用时网站采集停用并明确提示；不影响应用/AFK 采集和上传。
- 切换后停止 AW window/AFK 回读；旧中央数据保持可查询。

## 最终验收

- 无 ActivityWatch 主程序时，原生应用与 AFK 事实可进入 outbox 并被中央确认。
- 官方浏览器插件可把域名写入同一链路；无痕和完整 URL 不落库。
- 5600 冲突只降低网站采集并显示提示。
- 应用统计、网站统计、黑名单、睡眠、触发器和实时小窗专项回归通过。
- 自动化通过与真实 Windows/真实浏览器插件验收分开报告。

## 实现结果（2026-08-27）

- PC 已使用 Windows 原生前台进程与输入状态区间；开放区间以同一事件 ID 修订，不按秒堆积记录。
- 中央已接受 `windows_native`、`device.input_state` 与域名级 `web.foreground`，并继续兼容旧 AW 历史事件。
- 官方浏览器插件可连接 `127.0.0.1:5600` 的最小接口；无痕、完整 URL 和标题不落库。
- 5600 冲突时 WebUI 明确提示，应用与 AFK/锁屏采集继续工作。
- 配置初始化会移除已废弃的 `activitywatch_url`；`app_usage_collection_enabled` 统一控制本机使用采集。
