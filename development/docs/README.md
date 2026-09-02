# Life Link 文档入口

**Personal Context for AI** · 连接你的设备、数据与 AI

更新时间：2026-08-31

本文件是项目知识的统一入口。开始工作前先阅读仓库根目录的 [`AGENTS.md`](../../AGENTS.md)，再读取 [`项目总控.md`](项目总控.md) 和本次任务对应的模块入口；不要从日期交接记录或 `.workbuddy` 记忆推断现状。

## 一、项目级现役文档

1. [`项目总控.md`](项目总控.md)：冻结架构、模块边界、已决策语义、当前执行项和风险。
2. [`AI 开发导航.md`](AI%20开发导航.md)：面向开发型 AI 的项目结构、数据边界、修改路由和验证规则。
3. [`首次安装与连接流程.md`](首次安装与连接流程.md)：源码首次启动、远程连接、设备配对和失败恢复的设计基线。
4. [`项目地图/README.md`](项目地图/README.md)：Obsidian 主拓扑和可视化更新规则；地图是派生视图，不是事实权威。
5. [`协作/代理执行规范.md`](协作/代理执行规范.md)：Agent 调度、范围、测试和交接规则。
6. [`协作/任务模板.md`](协作/任务模板.md)：跨 Codex、WorkBuddy 或其他对话分配工作的统一任务单。

出现冲突时按以下顺序判断：

```text
共享契约 > 项目总控中的已决策事项 > 对应模块 README > 现役专项规范 > 历史或背景材料
```

## 二、模块入口

| 模块 | 必读入口 | 负责范围 |
| --- | --- | --- |
| 中央服务 | [`central-server/README.md`](../../central-server/README.md) | 身份、鉴权、SQLite 长期库、上传、查询、共享设置和派生视图 |
| PC 客户端 | [`pc-dashboard/README.md`](../../pc-dashboard/README.md) | Windows 原生采集、可选浏览器插件、本地 outbox、中央代理、托盘、小窗和 WebUI 宿主 |
| PC WebUI | [`pc-dashboard/web/README.md`](../../pc-dashboard/web/README.md) | 页面入口、CSS/JS 职责、加载顺序、接口依赖和 Agent 接入方法 |
| Android | [`mobile-app/README.md`](../../mobile-app/README.md) | 权限、手机采集、Room 队列、后台上传和手机界面 |
| 共享契约 | [`contracts/README.md`](../contracts/README.md) | 跨端字段、端点、权限、ACK、幂等和 fixture |
| 集成测试 | [`integration-tests/README.md`](../integration-tests/README.md) | 跨中央、PC 与契约的闭环验证 |
| AI MCP 适配器 | [`life-link-mcp/README.md`](../../life-link-mcp/README.md) | 本地 stdio MCP、Reader 配对、DPAPI 状态和 Windows EXE 构建 |

面向 AI 阅读程序的现役入口是 [`手机数据同步-AI读取说明.md`](手机数据同步-AI读取说明.md)，具体摘要口径由 [`pc-dashboard/ai_context/README.md`](../../pc-dashboard/ai_context/README.md) 维护。

## 三、专项说明

已完成的需求、设计和执行规范统一收入 [`已归档/`](已归档/README.md)，只用于事后调查，不再承担日常更新义务。当前功能事实以共享契约、项目总控和对应模块 README 为准。

现阶段只保留一份面向使用者的专项操作说明：[`手机数据同步-AI读取说明.md`](手机数据同步-AI读取说明.md)。

## 四、历史与背景

- [`已归档/README.md`](已归档/README.md)：已经完成、只供事后调查的需求、设计和执行规范。
- [`已过期/README.md`](已过期/README.md)：日期交接、旧架构分析和被现役文档取代的项目说明。只用于追溯，不约束开发。
- [`背景参考/README.md`](背景参考/README.md)：尚未批准实现的长期愿景与候选数据方向。

## 五、文档卫生规则

- 一个事实只保留一个权威解释；其他位置只放链接或受众专属摘要。
- “代码完成”“自动化测试通过”“真实设备验收通过”必须分开记录。
- 模块内部实现和启动方式写入模块 README；跨模块语义写入契约或项目总控；UI 文件导航写入 WebUI README。
- 已完成但仍需调查价值的专项文档进入 `已归档/`；已被淘汰或取代的资料进入 `已过期/`，二者不继续占用现役索引。
- 历史材料和 `.workbuddy` 记忆只能作为线索，不能覆盖现役文档、契约和代码事实。
- Token、邀请码、真实位置、私有公网地址等秘密不得写入文档、fixture 或提交记录。
