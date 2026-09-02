# Life Link MCP

Life Link MCP 是面向本地 AI 伴侣的只读 stdio 适配程序。AI 宿主把 `life-link-mcp.exe` 作为子进程启动，通过 MCP 工具读取 Life Link 中央已经整理好的个人背景、当前状态和增量事件。

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

## 构建

先使用 `requirements-build.txt` 安装构建依赖，再运行 `build_windows.ps1`。构建结果是 `dist/life-link-mcp.exe`，PC WebUI 生成正式 MCP 连接包时会把该文件装入 ZIP。PyInstaller 只在开发机参与构建，不是最终用户依赖。

## 验证

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

MCP 只提供只读工具，不上传事件、不修改事实、心愿或设置，也不主动向 AI 推送信息。
