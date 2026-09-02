# 任务：Tailscale HTTPS 入口初始化

```yaml
任务名称: Tailscale HTTPS 入口初始化
用户目标: 提供一个可双击运行的中央服务批处理，安全创建独立的 Tailscale 8443 HTTPS 入口并登记为 Life Link 远程地址。
风险等级: 中型

允许修改:
  - central-server/maintenance/configure_tailscale_endpoint.bat
  - central-server/configure_tailscale_endpoint.py
  - central-server/central_endpoint.py
  - central-server/tests/test_configure_tailscale_endpoint.py
  - central-server/README.md
  - development/docs/协作/任务-Tailscale-HTTPS入口初始化-2026-08-30.md

明确不做:
  - 不执行或覆盖现有 Tailscale Serve 配置。
  - 不修改 OpenClaw、花生壳、设备身份、Token 或既有客户端配置。
  - 不实现客户端免重新领取的邀请码地址迁移入口。

数据与契约影响:
  权威数据来源: 中央外部 config.json 的 public_endpoint。
  是否修改共享契约: 否
  是否迁移或删除数据: 否
  是否影响认证或同步: 仅更新后续邀请码和配对包使用的远程 HTTPS 地址。

验收标准:
  - 8443 无现有 Serve 配置时，脚本建立 8443 -> 127.0.0.1:8091 并完成 HTTPS 探测后登记地址。
  - 8443 已指向其他目标时，脚本在写入前停止。
  - 根地址 443 的既有 OpenClaw Serve 配置不被读取后改写。
```

状态：代码完成，离线单元测试通过；待用户真实 Tailscale 环境验收。

执行结果：

- 新增 `central-server/maintenance/configure_tailscale_endpoint.bat`：在管理员 PowerShell 或“以管理员身份运行”的终端执行；无论成功或失败，窗口都会停留显示结果。自动提权包装器因 Windows 路径引号不可靠已移除。
- 新增初始化器：先验证本机中央 `8091`，再检查 Tailscale 状态与 `8443` 是否已被占用；仅在空闲时建立 `8443 -> 127.0.0.1:8091`，通过 HTTPS 和带 Token 的中央探测后才写入当前远程地址。
- `tailscale` 已成为中央端点配置的正式提供商标记；既有花生壳、ngrok、Cloudflare 和 custom 配置不变。
