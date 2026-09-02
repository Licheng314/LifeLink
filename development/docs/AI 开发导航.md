# Life Link 开发导航（面向开发 AI）

本页帮助开发型 AI 在修改 Life Link 前快速建立正确的工程边界。它面向“修改项目功能”的 AI，不面向读取用户个人信息的 AI Reader；后者应使用 `手机数据同步-AI读取说明.md` 与 MCP Skill。

## 先做什么

1. 阅读仓库根目录 `AGENTS.md`、本页和 `项目总控.md`。
2. 只进入本任务相关模块的 README；跨端字段、接口、确认或鉴权变更先阅读 `development/contracts/README.md`。
3. 先确认事实权威位置、修改范围和最小验证方式；共享工作区有未提交修改时，不覆盖、回滚或格式化无关文件。

## 项目结构

```text
central-server/       中央服务：SQLite 长期库、鉴权、上传、查询、派生与分钟调度
pc-dashboard/         Windows PC 客户端：原生采集、outbox、中央代理、托盘/小窗与 WebUI 宿主
pc-dashboard/web/     WebUI：页面、样式和浏览器端状态；只访问本机 PC 代理
mobile-app/           Android：权限、原生采集、Room 队列、后台上传和手机界面
life-link-mcp/        本地 stdio MCP：AI Reader 的只读适配器与 Windows EXE 构建
development/contracts/ 跨端协议、fixture、接口与数据语义
development/integration-tests/ 跨中央与 PC 的闭环测试
development/docs/     现役文档、归档和项目治理
build/                可再生的本地构建产物；不提交 Git
%USERPROFILE%\LifeLink\  当前 Windows 用户唯一运行数据根目录；不在项目内
```

完整模块入口、启动方式和专项规则以 `development/docs/README.md` 的索引为准。

## 运行关系与事实流

```text
PC 原生采集 ──┐
                ├─ HTTPS ─→ 中央服务 / SQLite ─→ WebUI（经本机 PC 代理）
Android 采集 ──┘                            └→ AI Reader / MCP（只读）
```

- 客户端只采集、缓存并上传本机事实；未被中央逐事件确认的事实必须保留在本地队列。
- 中央服务是设备、事件、共享设置、心愿、时间线和派生结果的唯一长期权威。
- WebUI 不直接保存共享权威数据，也不持有中央 Token；浏览器只访问 `127.0.0.1:8090` 的 PC 代理。
- AI Reader 只能通过独立只读身份读取；不得用它上传、修改心愿、设置或事实。
- 当前架构没有 P2P、Tailscale 发现、旧 `/push` 或客户端互相转发；不要重新引入。

## 修改应落在哪一层

| 目标 | 首选位置 | 先确认 |
| --- | --- | --- |
| 新增长期事件、字段、端点、确认或鉴权 | `development/contracts/`，再中央与客户端 | 契约、fixture、兼容性、时间语义 |
| 中央持久化、派生、报告、心愿或读取口径 | `central-server/central/` | 原始事实是否保留、动态派生是否可重算 |
| Windows 采集、outbox、托盘、小窗、PC 本地代理 | `pc-dashboard/` | 是否只影响本机、断网与重启行为 |
| WebUI 页面与交互 | `pc-dashboard/web/` | 使用已有本机 API；历史业务日与当前管理状态的区别 |
| Android 权限、采集、Room、后台上传或手机 UI | `mobile-app/` | 用户授权、离线队列、上传 ACK 和本地数据保留 |
| AI Reader/MCP 工具或配对材料 | `life-link-mcp/` 与 AI Reader 契约 | 独立只读权限、Token 不落日志、无主动推送 |

不要为 UI 便利直接读写 SQLite，不要在浏览器暴露 Token，也不要让某一客户端创建新的共享数据语义。

## 关键边界

- **时间：** 所有跨日展示和查询以中央共享的 `Asia/Shanghai` 与 `day_start_hour` 计算业务日；浏览器自然日不是权威。
- **数据：** 原始事件默认追加或受控 revision 更新；展示和算法变化优先动态重算，不批量改写历史。
- **隐私：** Token、邀请码、真实位置、私有公网地址、运行数据和配对材料不得进入源码、fixture、文档或日志。
- **网络：** PC 与中央同机时固定使用 `http://127.0.0.1:8091`；公网入口只服务远程设备和 AI，不得影响同机 PC。
- **容错：** 新功能不能阻断采集与上传主链路；无网络、重试、重复请求、服务重启、旧数据、空数据和未认证访问是基本回归面。

## 本机文件与配置

```text
%USERPROFILE%\LifeLink\central\config.json       中央配置、SQLite、日志与媒体
%USERPROFILE%\LifeLink\client\config.json        PC 配置、唯一设备身份、outbox 与缓存
%USERPROFILE%\LifeLink\ai\mcp\                  MCP 的 DPAPI 加密状态
%USERPROFILE%\LifeLink\tools\                    Life Link 管理的构建或可选工具依赖
```

源码检出与发行目录只保存程序文件，任意副本都共享上述唯一用户数据；`LIFE_LINK_DATA_ROOT` 仅供高级部署整体覆盖。首次启动负责补齐缺失配置；不要提交真实配置，也不要为了测试改写用户现有配置。发布相关默认值与初始化逻辑应在程序中明确，秘密值只能在用户本机首次生成或输入。

## 验证与交付

- 小改动运行对应模块的最小测试；跨契约或跨端变更再运行契约和集成测试。
- 清楚区分“代码完成”“自动化测试通过”“真实设备验收通过”。
- 交付时说明修改范围、契约/数据影响、验证证据、风险与用户需要执行的步骤。
- 发现范围外问题只记录，不顺手扩建；需要迁移、删除、网络服务或新的长期数据类型时先取得总控确认。

## 推荐阅读顺序

1. `development/docs/项目总控.md`
2. 本页对应模块的 README
3. 涉及跨端时的 `development/contracts/README.md` 与相关 fixture
4. 目标代码及紧邻的测试
5. 只在需要追溯时查阅 `development/docs/已归档/` 或 `已过期/`
