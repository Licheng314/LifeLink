---
name: life-link-central-deploy
description: 发布或更新 Life Link 的 Linux/Docker 中央服务时使用；覆盖本机预检、单命令部署和失败后的核对。不用于 Android 打包或本机 PC 客户端。
---

# 中央服务发布

**做什么：**把当前 `codex/` 功能分支的已提交版本发布到中央服务器。脚本负责推送、备份、构建、切换、健康检查；失败时尝试恢复旧镜像。

**先看什么：**阅读 `development/docs/README.md` 与 `central-server/README.md` 的部署段。审查待发布提交和工作区差异；不替用户提交其他模块的修改。缺少本机配置时参考 `central-server/deploy.local.example.json`，配置文件不入库。

**怎么运行：**先执行 `./development/tools/deploy-central.ps1 -DryRun`；确认用户明确要求更新线上服务且本次提交正确，再执行 `./development/tools/deploy-central.ps1`。这两个命令在仓库根目录的 PowerShell 中运行。不要把只检查解释为已经部署。

**怎么交付：**成功时说明提交号、健康检查和备份位置；失败时说明最后的 `DEPLOY_STEP`、`DEPLOY_RECOVERY_OK` 或 `DEPLOY_RECOVERY_FAILED` 以及线上状态。脚本没有给出 `DEPLOY_OK` 就不能报告成功，也不要未经诊断反复重试。自动恢复旧镜像不等于自动回滚数据库。
