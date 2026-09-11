# Life Link Android 客户端

Android 模块只负责手机本机的权限、事实采集、Room 待上传队列、后台同步和手机界面。它通过已注册设备凭据上传本机事实并访问获准的中央资源，不定义中央长期数据语义，也不与 PC 或其他手机直接同步。

## 当前运行边界

- 统一使用一行 `LR1.` 邀请领取独立设备身份和中央地址。
- 应用用量来自 Android `UsageStatsManager`；位置采集由用户主动开启；同步失败时事件保留在 Room 队列。
- 位置采集以 Google 融合定位为主；连续 15 分钟未收到有效定位时会重新请求，并同时短暂启用 Android 系统定位备用通道。页面的“采集中”以最近有效定位为依据，不把前台服务仍在运行误报为正常采集。
- 首次使用应在设置页完成“后台运行”引导：关闭系统电池优化、按手机品牌允许自启动，并按页面提示加入后台保活范围；OEM 自启动开关没有统一的 Android 状态读取接口，因此应用只记录用户确认。
- 只有中央逐事件确认的 revision 才标记送达，重复扫描和重试必须保持幂等。
- 共享跨日设置从中央读取并只读缓存；手机同步间隔仍是本机参数。
- 心愿、每日评估、到期后手动完结、时间线和心愿内提醒挂接使用中央资源；用户不能在手机端创造新的触发器类型。
- 事件背景摘要、AI 理解说明、报告投送状态和系统里程碑均由中央生成；Android 只读展示，时间线固定按 `Asia/Shanghai` 格式化。事件列表只请求并展示当前业务日，避免超出中央单次查询上限；离线缓存明确只读，不能成为共享设置权威。
- 已绑定设备可从“设置 → 中央服务”创建一次性 HTTPS WebUI 会话并交给浏览器打开；浏览器不接触设备长期凭据。此入口要求中央已验证公网 HTTPS 地址，未配置时显示失败原因。
- 客户端只上传本机事实，不重新引入 ActivityWatch 依赖、Tailscale、P2P 发现或 PC 转发。

详细绑定、同步和心愿/触发器行为见 [`CENTRAL_SYNC.md`](CENTRAL_SYNC.md)。健康事实采集与中央派生边界见 [`HEALTH.md`](HEALTH.md)。APK 发布位置和构建约定见 [`RELEASES.md`](RELEASES.md)。跨端字段和权限以 [`../development/contracts/README.md`](../development/contracts/README.md) 为准。

## 源码入口

- `app/src/main/java/com/liferadio/sync/data/`：Room、模型、中央客户端和本机设置。
- `app/src/main/java/com/liferadio/sync/service/`：采集与后台同步服务。
- `app/src/main/java/com/liferadio/sync/ui/`：Compose 页面和 ViewModel。
- `app/src/test/`：JVM 专项测试。
- `app/build.gradle.kts`：当前 `versionCode`、`versionName`、SDK 和依赖事实来源。

## 构建与验证

```powershell
.\gradlew.bat testDebugUnitTest --console=plain
.\gradlew.bat assembleDebug --console=plain
```

也可以运行 `build-apk.bat` 构建并暂存调试候选 APK；正式 GitHub 发布必须显式使用 `-Publish -ConfirmPublish`，流程见 [`RELEASES.md`](RELEASES.md)。自动化测试通过、APK 构建成功和真实手机验收必须分别汇报；不要把仓库中旧 APK 文件名当作当前源码版本。
