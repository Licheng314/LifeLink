# Life Link MCP

Life Link MCP 是面向本地 AI 伴侣的只读 stdio 适配程序。AI 宿主在自己的 Windows、Linux 或 Docker 环境中以本地 Python stdio 子进程启动它，通过 MCP 工具读取 Life Link 中央已经整理好的个人背景、当前状态和增量事件。

正式连接包只接受已验证的 HTTPS 外部地址，即使 AI 和中央服务在同一台机器也一样。它不使用 `localhost`、HTTP 或管理 WebUI 的 `8092` 端口；管理 WebUI 绝不能由反向代理公开。配对、上下文读取和更新检查均保持系统 TLS 证书与主机名校验，且任何携带 Bearer Token 的重定向都会失败，不会跟随到新地址。更换域名、隧道或证书身份后，请重新验证网络、重新生成连接包并重新配对。

## 工具

- `lifelink_connection_status`：只检查本地配对状态，不访问上下文、不推进游标。
- `lifelink_check_updates`：轻量检查尚未读取的高优先级提醒和报告；返回 `update_mcp=true` 时再读取上下文。检查本身不返回正文、不推进游标。
- `lifelink_read_context`：首次调用自动使用连接包中的一次性凭据完成配对，后续按保存的游标读取增量事件；默认使用 `compact`，可显式请求 `full`。

连接包中的 `reader.json` 是通用稳定身份文件。目标 AI 可以填写自己的显示名；能够准确确认同机 Windows 进程时，可以按现役 Skill 规则加入 `process_binding`。共享宿主绑定必须来自当前真实运行进程的命令行，不能根据安装目录或推测的入口文件猜测；OpenClaw 通常可使用 `node.exe + node_modules/openclaw`，但仍以实际命令行为准。必须在首次 `lifelink_read_context` 前完成配置，因为首次读取会领取一次性配对并固定 Reader 绑定。MCP 程序只校验和提交该身份，不识别具体 AI 应用。

首次读取后再次调用 `lifelink_connection_status`，可确认提交的 `reader.process_binding`，但它不代表服务端已经检测到该进程；最终检测结果以 Life Link WebUI 的绿色提示为准。

`update_mcp` 表示当前业务日中存在游标之后产生的高优先级提醒、报告，或心愿/心愿触发器关联事件；正式读取并保存新游标后恢复为 `false`。其他普通事件不会唤醒 AI，但会在下一次正式读取时一并提供。

## 私密状态

Windows 默认保存到：

```text
%USERPROFILE%\LifeLink\ai\mcp\profiles\<profile-id>.json
```

Token、游标和理解版本整体使用当前 Windows 用户的 DPAPI 加密。文件外层只保留中央实例、Reader 身份、到期时间等管理信息；日志和 MCP 错误不输出 Token、游标或完整配对材料。

Linux 使用 `$XDG_STATE_HOME/life-link/mcp`；未设置时使用 `~/.local/state/life-link/mcp`。Docker 应将该目录挂载为仅供运行 MCP 的单一用户使用的持久卷。Linux/Docker 不具有 Windows DPAPI，因此程序会创建 `0700` 状态目录和 `0600` 状态文件；不要使用多人可读的共享卷。成功首次配对后，连接包内的 `pairing.json` 会被删除，长期 Token 只保存在上述私密状态中。

远端 AI 主机或容器需要自行提供 Python 3。服务器上的 Python 不能代替远端运行环境。典型命令是：

```text
python /absolute/path/to/life_link_mcp.py serve --package-dir /absolute/path/to/connection-package
```

MCP JSON 中必须填入实际的 Python 命令与绝对解压路径；AI 若没有修改本机配置文件的权限，应将这两项告诉用户完成配置。

## 构建

先使用 `requirements-build.txt` 安装构建依赖，再运行 `build_windows.ps1`。构建结果是 `dist/life-link-mcp.exe`，仅用于 Windows 可执行文件验证或兼容发布；跨平台连接包应携带标准库 `life_link_mcp.py`，不要求最终用户安装 PyInstaller。

## 验证

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

MCP 只提供只读工具，不上传事件、不修改事实、心愿或设置，也不主动向 AI 推送信息。
