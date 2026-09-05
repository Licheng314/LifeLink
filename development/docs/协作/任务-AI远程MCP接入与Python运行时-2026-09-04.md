# 任务：AI 远程 MCP 接入与 Python 运行时（2026-09-04）

## 目标

让中央服务在已验证的 HTTPS 外部入口上直接生成并下载跨平台 AI MCP 配对包。AI 所在的 Windows、Linux 或 Docker 主机以本地 Python stdio 进程运行 MCP；首次读取使用包内一次性凭据向中央 HTTPS 领取专用只读 Token，后续继续通过 HTTPS 读取。

同时调整 Windows 源码启动器：已安装并经验证的 Python 3.13 或 3.14 必须直接复用，不能因已有 3.14 而另装 3.13；首次启动、EXE 启动器和登录后启动必须使用同一套解释器选择规则。

## 已决策

- 网络设置在 AI 配对之前；没有已验证的外部 HTTPS 地址，不能生成远程 MCP 包。
- **正式用户路径统一 HTTPS。** 即使 AI 与中央服务在同一台机器，也使用已验证的 HTTPS origin；不再生成 loopback MCP 包或维护第二套本机配对协议。回环地址只允许测试和明确的本机诊断使用，不能进入用户 ZIP。
- 管理 WebUI 仍只绑定 `127.0.0.1:8092`，绝不由公网域名或反向代理暴露。
- 由中央服务生成 ZIP，浏览器下载；不依赖 PC Dashboard 在线，不打开资源管理器，不把 Windows EXE 作为正常方案。
- ZIP 使用标准库 Python MCP 程序；运行它的 AI 主机/容器必须自行具备 Python。Life Link 服务器的 Python 不能替代远端 AI 主机的 Python。
- ZIP 内保留运行所需的 `life_link_mcp.py`、`manifest.json`、`pairing.json`、`reader.json`，并附带 `mcp-config.json` 模板、`README.md` 与 `life-link-ai-reader/SKILL.md`。
- `README.md` 负责解压、Python 前置条件、修改 MCP JSON、让 AI 自行配置或指导用户；Skill 保持 AI 的只读语义、隐私边界、配对与读取规范。
- 管理 WebUI 同时展示可复制 MCP JSON；`<PYTHON_COMMAND>`、`<LIFE_LINK_MCP_DIR>` 等待替换值用红色显示，并有一行提示说明必须换成实际绝对路径。复制的原始 JSON 保留占位符。
- 首次成功领取后删除本地 `pairing.json`；长期 Token 只保存在 MCP 主机私密状态中，绝不出现在 WebUI JSON、日志、文档、URL 或下载以外的响应中。

## HTTPS 远程化的必做安全边界

1. **外部地址是受控 HTTPS origin。** 只接受 `https://host[:port]`，禁止用户名、密码、路径、查询和片段；生成包前重新确认该地址返回预期的 `central_instance_id`。配置不仅保存地址和验证时间，还应保存验证到的中央实例身份，避免误指向另一台 Life Link。
2. **不信任请求 Host 或转发头。** claim URL、context URL 和更新 URL 一律从已验证、持久化的外部 origin 构造；不得使用 `Host`、`X-Forwarded-Host`、`Forwarded` 等客户端可伪造值。
3. **HTTPS 全链路、禁止降级。** MCP 包、claim、context、updates 只允许 HTTPS；Python 的证书、主机名和系统信任链校验必须保持开启，不提供“忽略证书错误”的开关。
4. **Token 请求不跟随重定向。** 当前 Python `urlopen` 的默认重定向行为不能继续用于带 Bearer Token 的 claim/context/updates 请求；必须把 `3xx` 当作失败，防止 Token 被带往另一域名或错误入口。
5. **反向代理只转发中央数据 API。** 公网代理只能将必要的 `/v1/*` 请求转到中央数据 API 的 loopback 端口；必须原样转发 `Authorization`、`Content-Type` 和状态码，同时在访问日志中脱敏 Authorization、请求体和查询中的私密值。`8092` 管理端口不允许映射或代理。
6. **域名变化不是静默迁移。** 已领取 Token 的 MCP 状态保存完整 context origin。更换域名、隧道地址或 TLS 身份后，旧包/旧连接应明确失败并要求重新验证网络、重新生成包、重新配对；不得用携带 Token 的 HTTP 重定向“迁移”。推荐稳定自有域名，临时隧道仅作临时入口。
   同机 AI 也遵循该规则；网络部署必须保证中央主机自身能够通过这个 HTTPS origin 回连（例如正确的 DNS、反向代理或 split-horizon DNS），不能以 `localhost` 作为隐藏回退。
7. **Tailscale 的边界明确。** Tailscale HTTPS 地址可作为外部 origin，但远端 AI 主机也必须在同一 tailnet 且能解析/信任该地址；它不是对任意互联网 AI 主机开放的公网方案。
8. **降低公开面。** 配对 Token 维持高熵、24 小时、一次性；claim 路由对失败做限速/审计但不泄露 Token 或 reader 信息。AI Token 继续专用只读、可撤销、有限期，数据库只保存其哈希。
9. **容器私密状态。** Linux/Docker 的长期状态目录需要持久卷、单一运行用户和 `0700` 目录/`0600` 文件权限；它没有 Windows DPAPI，不能把多人可读的共享卷当作安全存储。

## 允许修改

- `central-server/central/`、`central-server/management-web/`、中央相关测试与 README。
- `life-link-mcp/`、其测试与 README。
- `development/tools/bootstrap_windows.ps1`、两个 Windows 源码启动器及其测试。
- 相关共享契约、项目总控、首次安装说明和普通用户说明。

## 明确不做

- 不公开中央管理 WebUI，不给 AI 写入事实、心愿或设置的能力。
- 不使用 Webhook、服务端主动推送或将 MCP 改为网络常驻服务；MCP 仍是 AI 主机启动的本地 stdio 子进程。
- 不为没有 Python 的远端环境静默安装系统 Python，也不修改 AI 应用的配置文件，除非该 AI 明确具有本机 Agent 权限并由用户授权。
- 不把临时隧道、DNS 或反向代理产品写入数据语义；它们只是 HTTPS 传输层。

## 数据与契约影响

- 权威网络配置：中央外部配置中的 `public_endpoint`；需要扩展为含已验证的中央实例身份。
- 鉴权影响：AI pairing claim 与 AI read URL 从 loopback-only 改为受验证 HTTPS origin；Token 语义、有效期、一次性领取和只读 scope 不变。
- 长期数据：不迁移或删除事件；可能新增/更新仅用于配对和网络验证元数据的字段，须幂等兼容旧配置。
- 共享契约：需要先更新 AI reader 的 claim/response URL 约束与 fixture，再改中央、MCP 和 WebUI。

## 验收标准

1. Python 3.13、3.14 分别通过中央、PC 与 MCP 自动化回归；已装 3.14 的 Windows 不调用安装流程，未装受支持版本时才提示安装。
2. 中央服务在没有 PC Dashboard 的情况下，能从管理 WebUI 下载完整 ZIP；网络未验证时按钮给出明确阻止反馈。
3. ZIP 在 Windows、Linux、Docker（带持久卷）各自可由本机 Python 启动 stdio MCP；缺少 Python 时给出无秘密的明确提示。
4. MCP JSON 展示、复制、占位符提示与 README 一致；AI 有 Agent 权限时可按 README 自行完成配置，不能改文件的 AI 能正确指导用户完成。
5. 真实 HTTPS 域名经反向代理完成首次 claim、首次读取、更新检查、断网重试、Token 失效重新配对和撤销；全程不访问 `8092`。
6. 非 HTTPS、错误实例、含路径/凭据/查询的地址、TLS 失败、`3xx` 重定向、错误 Host/Forwarded 头均被拒绝，且不会泄露 Token。
7. 成功领取后 `pairing.json` 删除；检查 ZIP、WebUI、日志、异常响应和 MCP 状态文件，确认无长期 Token 明文泄露；Linux/Docker 状态目录权限符合约束。
8. 更换域名或隧道地址后，旧 MCP 连接不静默跳转；界面和 README 明确指引重新验证并重新配对。

## 风险等级与分工

风险等级：大型（跨中央、MCP、Windows 启动器、共享契约与鉴权边界）。

执行时按“契约/中央发包与 HTTPS URL / MCP 跨平台校验 / WebUI 与文档 / Windows Python 运行时”拆分，避免多代理同时改同一文件。执行代理跑专项测试；总控检查差异并跑必要全量回归；真实 HTTPS、Windows、Linux/Docker 验收单独记录，不与自动化测试混写。

## 执行记录

- 2026-09-04：Windows Python 运行时子项已完成代码与专项自动化。bootstrap 和中央/PC 源码启动器均只接受 Python 3.14、3.13，优先顺序为显式 `LIFE_LINK_SOURCE_PYTHON`、每用户 3.14、每用户 3.13、PATH 中的非 WindowsApps `python[w]`；候选均经实际版本探测，PC 额外探测 Tkinter。已有合格 3.14 不触发安装，只有均不可用时才通过 winget 提示安装 3.14。真实 Windows 首次启动与登录启动验收待后续发布验收记录。
- 2026-09-04：中央、MCP、管理 WebUI 与共享契约代码完成。中央在每次生成前重新验证 HTTPS origin 和中央实例身份，直接下载包含 Python stdio 程序、Skill、README、配对材料与 JSON 模板的 ZIP；MCP 严格校验 HTTPS/实例/同一 origin、拒绝所有携带 Token 的重定向，Linux/Docker 使用私密持久状态目录。中央专项 26 项、中央全量回归、MCP 专项 13 项（3 项条件跳过）通过；真实域名、Windows 启动器与 Linux/Docker 仍待人工验收。
