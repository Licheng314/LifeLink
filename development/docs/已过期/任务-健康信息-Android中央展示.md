# 任务：Android 接入中央健康信息

> **已归档（2026-08-16）**：本任务单对应的功能已完成并经用户验收/接受式验收，仅作历史追溯，不作为当前任务或验收结论。验收记录见 2026-08-16-待验收项验收方案与结论记录.md。

```yaml
任务名称: Android 健康页改用中央睡眠与步数结果
用户目标: 手机直接使用中央跨设备睡眠估算和按设备步数，不再展示本地手机单端估算为权威结果
风险等级: 中型（只读 API、缓存与现有 Compose 健康 UI）

必须阅读:
  - AGENTS.md
  - docs/README.md
  - docs/健康信息-后端数据设计.md
  - contracts/README.md
  - mobile-app/README.md
  - mobile-app/HEALTH.md

允许修改:
  - mobile-app/app/src/main/java/com/liferadio/sync/data/model/ 中健康响应模型
  - mobile-app/app/src/main/java/com/liferadio/sync/data/remote/ 中健康读取客户端
  - mobile-app/app/src/main/java/com/liferadio/sync/data/local/SettingsStore.kt 中只读健康缓存
  - mobile-app/app/src/main/java/com/liferadio/sync/ui/screens/MainViewModel.kt
  - mobile-app/app/src/main/java/com/liferadio/sync/ui/screens/MainScreen.kt
  - mobile-app/app/src/test/ 健康客户端和展示策略专项测试
  - mobile-app/HEALTH.md

明确不做:
  - 不修改中央、contracts、PC/WebUI、采集/outbox/Room schema
  - 不重新实现睡眠算法，不把 SleepEstimator 结果作为回退权威
  - 不做 Android 七日图表、健康评分、提醒、Health Connect 或 APK 发布
  - 不覆盖共享工作区其他未提交改动

实现口径:
  - 读取 GET /v1/health-info?date=本日，日期按 Asia/Shanghai
  - 解析 v1.10.1 的 finalized_at、interval_seconds、last_activity_devices、first_activity_devices
  - 健康卡与详情显示：估算区间、区间跨度、短暂中断、睡前最后使用设备、醒后最早使用设备、按手机设备拆分的当日步数
  - status=estimating 显示仍在估算；insufficient_data 显示证据不足，不伪造区间
  - 只读缓存必须原子保存完整响应；网络失败可显示最近缓存并明确标记“当前离线中”
  - 缓存损坏时忽略，不崩溃；中央成功响应覆盖缓存
  - 不显示本地传感器累计值为“今日步数”；本地权限/传感器状态可保留为诊断信息

验收标准:
  - device token 可完成请求，404/401/损坏 body/离线缓存有可解释状态
  - 睡眠完成时刻使用 finalized_at，不使用刷新时间
  - 多个边界设备和多个步数设备均可展示
  - JVM 专项与 Android 全量 JVM 通过，代码编译通过
  - 真实手机验收单独标记未完成
```
