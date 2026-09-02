# 黑名单 WebUI 编辑面板：设计方案

状态：中央规则、平台匹配、WebUI 管理面板、图表行内加号和移除交互均已实现，并于 2026-08-16 完成真实页面与多设备接受式验收；本文保留为交互规范，不是待实施任务单
创建日期：2026-08-08
UI 决策更新：2026-08-09
现状核对：2026-08-12；早期隐藏的黑名单饼图和 `settings.json` 明细已删除，不属于待恢复面板
依赖：黑名单中央化基础（已完成）

## 0. 总控审批结论（2026-08-08）

本方案按 **中型跨模块语义变更** 执行，批准范围如下：

- 黑名单应用规则增加平台适用范围：PC 应用规则可命中所有 PC，但不得命中 Android；Android 应用规则可命中所有 Android，但不得命中 PC。
- 字段命名为 `platform_scope`，表达“规则适用于哪里”，不表达从哪个界面创建。
- 网站规则使用 `platform_scope=web`；当前网站统计不再额外绑定某一台设备。
- 允许依次修改 `contracts/`、`central-server/`、`pc-dashboard/` 及其专项测试；不得修改 Android 采集、身份、上传 ACK 或其他数据类型。
- 现有应用规则迁移为 `pc`，现有域名规则迁移为 `web`。迁移必须幂等，不删除规则，不修改或删除历史原始事件。
- WebUI 最多允许添加到 10 条；这是前端交互限制，不是中央数据库或 API 的存储限制。
- 中央已有 10 条或更多规则时，WebUI 全部正常显示并禁用新增；即使已有 11 条，也不额外显示“请移除至 10 条以下”等提示，标签编辑和删除继续可用。
- 不重新引入 Focus-Guard，不改变现有设备凭据权限口径。
- 代码完成、自动化测试通过和真实双 PC 验证分别汇报。

下文可直接作为执行任务依据。

允许修改范围：

- `contracts/life-radio-api-v1.yaml`、受影响 fixture 和契约测试；
- `central-server/central/storage.py`、`http.py`、`read_model.py`、`ai_summary.py` 及对应测试；
- `pc-dashboard/central_client.py`、`blacklist_cache.py`、`sync_server.py`、`dashboard.html` 及对应测试；
- 仅在实现实际改变启动、配置或外部行为说明时更新对应模块 README。

明确不做：修改 Android 源码、位置/用量事件格式、上传与 ACK、邀请注册、设备身份、权限体系、历史事件和范围外页面。

## 1. 目标

在应用使用页面最下方新增单列黑名单管理区域，显示黑名单条目、横向用量柱状图和确认移除。取消右侧候选面板；新增入口直接放到上方“全应用使用时长排行”和“网站访问使用时长”图表的每一行名称旁，以行内加号完成添加。

颜色规范在全应用使用时长排行、网站访问使用时长和黑名单用量图表中统一生效。

## 2. 数据结构与契约变更

### 2.1 新增 platform_scope 字段

`blacklist_rules` 表和 `BlacklistRule` 契约新增 `platform_scope`：

| `rule_type` | `platform_scope` | 含义 |
| --- | --- | --- |
| `app` | `pc` | 所有 PC 的应用规则 |
| `app` | `android` | 所有 Android 设备的应用规则 |
| `domain` | `web` | 网站域名规则 |

其他组合必须返回明确的参数错误。创建规则示例：

```json
{
  "rule_type": "app",
  "pattern": "steam",
  "label": "Steam 游戏",
  "platform_scope": "pc",
  "enabled": true
}
```

`platform_scope` 在创建时确定，PATCH 不修改。需要改变适用平台时删除旧规则并重新添加。

为兼容尚未更新的 v1 调用方，POST 缺少该字段时服务端按 `app → pc`、`domain → web` 处理；更新后的 WebUI 必须显式提交。响应始终返回 `platform_scope`。

数据库唯一约束调整为 `(rule_type, platform_scope, normalized_pattern)`，允许相同应用模式分别建立 PC 和 Android 规则。

已有数据迁移：

- 现有 `app` 规则填充为 `pc`；
- 现有 `domain` 规则填充为 `web`；
- 重复启动不得重复迁移、重复种子或改写时间字段。
- 新列与迁移应保持旧数据库可启动；不得要求用户删除或重建中央数据库。

### 2.2 PC 代理与 WebUI 映射

- PC 代理透传 `platform_scope`，不建立第二份规则权威数据。
- `loadBlacklistRules()` 保留完整规则记录，供编辑和删除使用。
- 统计匹配继续使用仅包含启用规则的轻量快照。
- 管理列表按 `platform_scope` 显示“电脑应用”“网站”或“手机 APP”。

### 2.3 匹配口径

- 中央用量视图和 AI 摘要先按设备平台选择规则，再执行现有应用子串或域名等值/子域匹配。
- PC 本地实时用时只读取 `pc` 应用规则和 `web` 域名规则。
- Android 规则只影响中央派生统计；本任务不在 Android 客户端增加本地拦截或黑名单执行逻辑。
- 规则变化只重算派生结果，不修改历史事件。

## 3. 布局与交互

### 3.1 图表行内新增

- “全应用使用时长排行”和“网站访问使用时长”各显示当前图表范围内的 Top 10；
- 每一行在名称旁绘制加号，点击后立即调用 `POST /api/blacklist/rules`；
- 应用加号必须根据该行真实来源设备的平台创建 `platform_scope=pc` 或 `platform_scope=android`，不得只按应用名称猜测平台；
- 网站加号固定创建 `rule_type=domain, platform_scope=web`；
- 同一应用在 PC 和 Android 分别出现时，必须允许分别建立两条规则；
- 已存在的同平台规则以及规则总数达到 10 条后的加号显示为不可新增状态；
- 添加成功后刷新规则、图表颜色、管理列表和数量限制状态。

### 3.2 黑名单列表 + 柱状图

**位置**：应用使用标签页最下方，在两个图表下方

**内容**：

- 每个黑名单条目一行，包含：
  - 适用平台标签小字（`电脑应用` / `网站` / `手机 APP`）
  - 名称
  - 横向柱状条（用量秒数 → 分钟，由高到低排列，颜色遵循 4.1 节规范）
  - 移除按钮

**交互**：

- 点击移除 → 按钮变为"确认移除" → 再次点击 → 调用 `DELETE /api/blacklist/rules/{rule_id}` → 刷新
- 点击管理面板之外的其他区域时取消确认并恢复为"移除"
- 支持修改标签；保存时调用现有 PATCH 接口
- WebUI 不提供启用/停用控件；中央契约中的 `enabled` 字段继续保留用于兼容既有数据和调用方
- 当前规则总数达到或超过 10 条时禁用所有新增按钮，但不影响现有规则操作，也不显示额外超限提示

### 3.3 移除确认

- 点击"移除" → 按钮文案变为"确认移除"，颜色变红
- 再次点击 → 调用 DELETE → 刷新
- 点击其他区域 → 恢复为"移除"

## 4. 颜色规范

### 4.1 颜色表

| 类别      | 正常色           | 黑名单色          |
| ------- | ------------- | ------------- |
| 电脑/手机应用 | `#4f8cff`（蓝色） | `#9b59b6`（紫色） |
| 网站      | `#f39c12`（橙色） | `#e74c3c`（红色） |

### 4.2 适用位置

1. **全应用使用时长排行**（`renderAllAppsChart`）：每条柱子按颜色规范着色
2. **网站访问使用时长**（`renderSiteUsage`）：类似
3. **黑名单用量图表**（左侧列表的横向柱状条）：按颜色规范着色

### 4.3 实现方式

抽取公共函数：

```javascript
function getUsageColor(appOrSiteName, isBlacklisted, isWeb) {
  if (isBlacklisted) return isWeb ? '#e74c3c' : '#9b59b6';
  return isWeb ? '#f39c12' : '#4f8cff';
}
```

注：手机 APP 和电脑应用在应用颜色上不做区分（都是蓝色/紫色），靠适用平台标签区分。

## 5. 空状态处理

- 黑名单列表为空：显示"暂无黑名单条目"
- 图表没有可添加的数据：保留图表现有空状态，不增加独立候选占位
- 所有平台均无任何数据：显示"暂无数据"占位

## 6. 数据流

```
WebUI 平台页签切换
  → 触发用量图表与 renderBlacklistManagementPanel()
    → 读取 blacklistRules（已缓存）
    → 读取 usage_view 数据（已有）
    → 渲染 Top 10 图表行内加号与单列管理面板

加入黑名单
  → POST /api/blacklist/rules {rule_type, pattern, label, platform_scope, enabled:true}
    → PC 代理转发到中央 /v1/settings/blacklist-rules
      → invalidate_blacklist_memory()
    → WebUI 重新 loadBlacklistRules() + renderUsagePageState()

移除黑名单
  → 确认移除
    → DELETE /api/blacklist/rules/{rule_id}
      → PC 代理转发到中央 /v1/settings/blacklist-rules/{rule_id}
        → invalidate_blacklist_memory()
    → WebUI 重新 loadBlacklistRules() + renderUsagePageState()
```

## 7. 开发注意事项

### 7.1 前端数量限制、重复与失败处理

- 已存在的同类型、同平台、同规范化模式规则，其图表行内加号不可再次添加。
- 10 条限制只在 WebUI 判断，不在中央 POST 增加数量校验。
- 中央返回超过 10 条时全部渲染，禁用新增即可，不显示要求用户删减的额外提示。
- POST / PATCH / DELETE 失败时保留当前有效页面状态并显示简短错误，不把失败响应当作空规则覆盖。

### 7.2 颜色函数复用

不要在三处分别写 `if (isBlacklisted) ... else ...`。抽取为 `getUsageColor(name, isWeb)` 公共函数。

### 7.3 删除 fgFallbackState 后的提醒引用

确认 `reminderSettings.intervalMinutes`（原 `fgSettings.remind_interval_minutes`）仍被正确读取。

### 7.4 图表行内应用的平台判定

手机应用行创建 `rule_type=app, platform_scope=android`；电脑应用行创建 `rule_type=app, platform_scope=pc`。匹配口径仍是不区分大小写的子串匹配，但规则不得跨 PC/Android 平台命中。总集合若将同名 PC/Android 应用合并为一行，就不能从名称推断单一平台；实现必须保留平台来源、拆分展示，或在来源不唯一时不提供直接添加。

### 7.5 与现有 UI 一致

- 卡片圆角、阴影、字体大小跟随 `var(--...)` CSS 变量
- 图表的 canvas 尺寸和坐标区间参考 `renderUsageHourly` 和 `renderAllAppsChart`
- 使用已有的 `blColors` 颜色映射（可扩展）

## 8. 实施步骤

1. 契约：更新 `BlacklistRule`、POST 请求和 fixture，写明合法组合、缺省兼容与唯一性语义
2. 中央：幂等新增列、迁移现有规则并调整唯一约束
3. 中央：CRUD 返回并校验 `platform_scope`，按设备平台应用规则
4. 中央：用量视图与 AI 摘要专项测试覆盖 PC/Android 不交叉命中
5. PC：代理和离线缓存透传新字段，本地实时用时只使用 `pc/web` 规则
6. WebUI：保留完整规则记录，并生成按平台筛选的启用规则快照
7. WebUI：实现 Top 10 图表行内加号、单列管理面板、标签修改和点击其他区域取消的两步移除确认；不提供启用/停用控件
8. WebUI：实现仅前端 10 条限制、颜色、空状态、重复候选和失败处理
9. 增加契约、中央和 PC 专项自动化测试
10. 总控检查实际差异并运行一次必要回归；之后另行进行真实双 PC 验证

## 9. 验收标准

- 平台页签切换时，Top 10 图表和行内加号使用当前范围内的真实平台数据
- 加入/移除后管理列表实时刷新，上方的黑名单统计同步更新
- PC 应用规则不命中 Android，Android 应用规则不命中 PC；同一模式可以分别建立两条规则
- 现有应用规则幂等迁移为 `pc`，域名规则迁移为 `web`，历史原始事件不变
- 标签修改通过现有 PATCH 接口生效；WebUI 不提供启用/停用控件
- 重复图表项不可再次添加，请求失败不清空现有规则
- WebUI 在第 10 条后禁用新增；中央 API 本身不限制总数
- 中央已有 11 条时 WebUI 全部显示、禁用新增且不显示额外删减提示，标签编辑和删除正常
- 颜色规范在所有指定位置生效
- 无 Focus-Guard 残留引用
- 契约、中央和 PC 专项测试通过；总控验收时再决定必要回归范围
