# Android beta.12 发布

```yaml
任务名称: Android beta.12 Release 更新
用户目标: 将新的已验收 Android 包发布到 GitHub Release，更新固定下载入口。
风险等级: 中型（对外发布）
允许修改:
  - mobile-app 当前七个待发布文件及 RELEASES.md
  - 本任务单和忽略目录 build/android 下的发布产物
明确不做:
  - 新增功能、权限、依赖或数据迁移
  - PC 打包、中央部署、旧 Release 删除或覆盖、分支清理
验收标准:
  - JVM 测试通过、版本及签名正确、与已验收 APK 校验一致
  - 发布对应已提交源码、上传版本包及校验文件和固定别名
  - 核对远端 Release 资产和 latest 下载入口
```

## 候选与验收证据

- 版本 `0.1.0-beta.12` / `51`；包名 `com.liferadio.sync`，最低 Android 8（API 26）。本次发布现有已交付候选，不再次递增版本。
- 用户于 2026-10-11 明确确认 beta.12 已在手机验证、可正式发布；这不扩展为所有机型或长期后台验收。
- JVM `testDebugUnitTest --rerun`：84 项通过，0 失败、错误或跳过；Debug APK 构建成功。
- 新构建与原候选逐字节 SHA-256 相同：`e6e67254cce07981dc4d73fdc53daaa743d4ed5d8fd42d05695d4036b8faeeb4`。
- APK 签名与旧 Release 的安装包相同，可覆盖升级；未读取或上传签名私钥。
- 主要变化：四列照片网格、单行时间/来源、全屏查看、独立勾选、黄色已同步标记、照片标题区排版修复，以及此前照片权限、排序、解码与失败恢复修复。

## 发布边界

- 使用现有 `build-apk.ps1 -Publish -ConfirmPublish`，目标标签 `android-v0.1.0-beta.12`；拒绝覆盖同名 Release。
- 先提交并推送待发布 Android 源码与文档，保持工作区干净；APK 和校验产物仍在忽略目录，不进入源码提交。
- 对外上传版本 APK、SHA-256 文件及 `LifeLink-Android.apk` 固定别名；Release 展示 Android 专属说明，不将 PC 番茄钟称为 Android 更新。
- 不改变中央运行环境、不删除旧候选包或 Release；发布失败保留候选和提交供恢复。
