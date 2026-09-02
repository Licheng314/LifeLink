# Life Link Android 客户端

Android 模块只负责手机本机的权限、事实采集、Room 待上传队列、后台同步和手机界面。它通过已注册设备凭据上传本机事实并访问获准的中央资源，不定义中央长期数据语义，也不与 PC 或其他手机直接同步。

## 当前运行边界

- 统一使用一行 `LR1.` 邀请领取独立设备身份和中央地址。
- 应用用量来自 Android `UsageStatsManager`；位置采集由用户主动开启；同步失败时事件保留在 Room 队列。
- 只有中央逐事件确认的 revision 才标记送达，重复扫描和重试必须保持幂等。
- 共享跨日设置从中央读取并只读缓存；手机同步间隔仍是本机参数。
- 心愿、每日评估、到期后手动完结、时间线和心愿内提醒挂接使用中央资源；用户不能在手机端创造新的触发器类型。
- 事件背景摘要、AI 理解说明、报告投送状态和系统里程碑均由中央生成；Android 只读展示，时间线固定按 `Asia/Shanghai` 格式化。离线缓存明确只读，不能成为共享设置权威。
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

也可以运行 `build-apk.bat` 构建并发布调试 APK。自动化测试通过、APK 构建成功和真实手机验收必须分别汇报；不要把仓库中旧 APK 文件名当作当前源码版本。
