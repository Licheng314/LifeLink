# 任务：WebUI 健康信息页签

> **已归档（2026-08-16）**：本任务单对应的功能已完成并经用户验收/接受式验收，仅作历史追溯，不作为当前任务或验收结论。验收记录见 2026-08-16-待验收项验收方案与结论记录.md。

```yaml
任务名称: 用中央健康信息替换旧模拟睡眠页
用户目标: 删除旧硬编码睡眠页，新增真实健康信息页签，展示昨晚、步数和过去七晚睡眠区间
风险等级: 中型（PC 中央代理、静态资源与页面替换）

必须阅读:
  - AGENTS.md
  - docs/README.md
  - docs/健康信息-后端数据设计.md
  - contracts/README.md
  - pc-dashboard/README.md
  - pc-dashboard/web/README.md

允许修改:
  - pc-dashboard/central_client.py
  - pc-dashboard/sync_server.py
  - pc-dashboard/dashboard.html
  - pc-dashboard/web/scripts/sleep.js（删除或退役）
  - pc-dashboard/web/scripts/ 下新健康页面脚本
  - pc-dashboard/web/styles/components.css 及必要的健康专项样式
  - pc-dashboard/web/README.md
  - pc-dashboard/tests/ 对应中央代理与静态页面专项测试

明确不做:
  - 不修改 central-server、contracts、Android
  - 不增加构建器、框架或 npm
  - 不保留旧睡眠评分、入睡效率、睡眠阶段饼图、模拟 AI 文案和硬编码周数据
  - 不设计健康提醒、评分、排行榜或手环接入
  - 不覆盖共享工作区其他未提交改动

实现口径:
  - 导航中的“睡眠”替换为“健康信息”；旧模拟页面整体删除
  - PC 本地新增 GET /api/health-info?date=YYYY-MM-DD，代理中央同名 v1 路径；浏览器不接触 Token
  - 代理保留按日期的最近成功只读缓存；中央失败时可回退并在响应/页面明确“当前离线中”，不得用空数据覆盖缓存
  - 昨晚区域显示：estimated_start→estimated_end、interval_seconds、rest_seconds、interruption_seconds、finalized_at、last_activity_devices、first_activity_devices
  - finalized_at 以 Asia/Shanghai 展示，含义是醒后边界真实发生时间，不是页面刷新时间
  - 步数按 Android 设备分别展示，不自动合计
  - 过去一周并行读取以今日为止连续 7 个 date；展示每晚估算起止区间和跨度，estimating/insufficient_data 显示状态，不伪造 0
  - 周视图延续现有按周浏览思路，但只能使用真实中央数据；布局保持简单并兼容当前窄屏
  - 未知字段忽略，单日失败不应让其他六日消失

验收标准:
  - 静态资源白名单、HTML 引用和脚本加载顺序一致
  - 旧硬编码睡眠内容和 sleepWeekly 不再进入运行页面
  - 代理认证、date 严格转发、缓存回退、七日部分失败及边界设备显示有专项测试
  - PC 相关自动化通过；真实浏览器视觉与中央真实数据验收单独标记
```
