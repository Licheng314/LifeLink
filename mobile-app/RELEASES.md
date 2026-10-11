# Android 发布流程

Android 源码位于本目录。构建产生的 APK、校验文件和临时目录均不进入 Git；默认候选产物位置是：

```text
build/android/
```

## beta.12 发布记录

- 版本：`0.1.0-beta.12`，`versionCode=51`；此次发布已有、已由用户确认真机验证的候选包，不重新提高版本号。
- 照片页使用四列紧凑网格、单行时间/来源、独立同步勾选和全屏查看；已确认同步用黄色描边与衬底区分，修复标题、说明及同步状态重叠。
- 2026-10-11 重新执行 84 项 JVM 测试全部通过；候选 APK 与原 beta.12 的 SHA-256 完全一致，签名与此前 Release 相同。
- 版本包、SHA-256 与固定别名 `LifeLink-Android.apk` 通过既有显式发布脚本上传；不包含服务器部署或 PC 客户端打包。
- 本轮任务边界与证据见 [发布任务单](../development/docs/协作/任务-Android-beta12发布-2026-10-11.md)。

## 1. 构建候选包

Windows 上运行：

```powershell
.\build-apk.bat
```

该流程会依次运行 Android JVM 单元测试、构建 Debug APK、复制出带 `versionName` 的候选文件，并生成 SHA-256 校验文件：

```text
LifeLink-v<version>-debug.apk
LifeLink-v<version>-debug.apk.sha256
```

默认流程**不会**创建 Git tag、上传 GitHub 或修改对外 Release。构建完成后，应安装候选 APK 到真实手机，验收配对、后台运行、采集、同步和关键页面。

只要候选 APK 会交付给用户安装或覆盖安装，即使尚不创建 GitHub Release，也必须先同时递增 `versionCode` 和 `versionName`。仅供开发机内部编译检查、不会交付安装的临时构建可以保持当前版本号。

如需构建未签名 Release 仅供技术检查，可运行：

```powershell
.\build-apk.bat -Variant release
```

它会明确命名为 `release-unsigned`，不可当作已签名的正式安装包发布。

## 2. 正式 GitHub 发布

确认候选包与真机验收通过后：先提高 `mobile-app/app/build.gradle.kts` 的 `versionCode` 和 `versionName`，提交全部源码与文档，确保工作区干净，并确认 GitHub CLI 已登录。随后显式执行：

```powershell
.\build-apk.bat -Publish -ConfirmPublish
```

发布模式会再次运行测试并构建候选包，然后创建 `android-v<version>` GitHub Release，上传版本 APK、SHA-256 文件，以及固定文件名 `LifeLink-Android.apk`。最后一个文件对应根 README 的“最新 Android 测试版 APK”下载链接。已有同名 Release 时脚本拒绝覆盖。

`-Publish` 会对外创建 GitHub Release，不能作为日常开发收尾自动执行。若只需自定义候选目录，可设置 `LIFE_RADIO_RELEASES_DIR`；Gradle 临时输出始终位于 `app/build/`。
