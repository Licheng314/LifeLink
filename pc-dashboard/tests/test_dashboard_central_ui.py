import unittest
from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard.html"
WEB_ROOT = DASHBOARD.parent / "web"


class DashboardCentralUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")
        cls.scripts = {
            path.name: path.read_text(encoding="utf-8")
            for path in (WEB_ROOT / "scripts").glob("*.js")
        }
        cls.source = cls.html + "\n" + "\n".join(cls.scripts.values())

    def test_dashboard_uses_small_static_assets_without_a_build_step(self):
        expected_styles = ("base", "components", "wishes-events")
        expected_scripts = (
            "shared-ui", "wishes-events", "usage", "health-info",
            "devices", "location", "app",
        )
        for name in expected_styles:
            self.assertIn(f'href="/assets/styles/{name}.css', self.html)
            self.assertTrue((WEB_ROOT / "styles" / f"{name}.css").is_file())
        for name in expected_scripts:
            self.assertIn(f'src="/assets/scripts/{name}.js', self.html)
            self.assertTrue((WEB_ROOT / "scripts" / f"{name}.js").is_file())
        self.assertNotIn("<style>", self.html)
        self.assertNotIn("<script>\n", self.html)
        self.assertNotIn('/assets/styles/tools.css', self.html)
        self.assertNotIn('/assets/scripts/tools.js', self.html)
        self.assertTrue((WEB_ROOT / "styles" / "tools.css").is_file())
        self.assertTrue((WEB_ROOT / "scripts" / "tools.js").is_file())
        self.assertLess(len(self.html.splitlines()), 400)

    def test_central_mode_has_central_only_upload_action_and_status_polling(self):
        self.assertIn("立即上传到中央服务", self.source)
        self.assertIn("fetch('/api/sync/central', { method: 'POST' })", self.source)
        self.assertIn("const statusResponse = await fetch('/api/sync/central')", self.source)
        self.assertIn("centralStatus?.outbox", self.source)
        self.assertIn("centralStatus?.configured === true", self.source)
        self.assertIn("无法读取中央同步状态，请稍后刷新", self.source)

    def test_device_management_omits_retired_raw_sync_sections(self):
        for retired in (
            "中央数据状态", "同步历史", "同步概览（可复制给AI）",
            "sync-heartbeat-list", "sync-history-body", "sync-ai-text",
        ):
            self.assertNotIn(retired, self.source)

    def test_legacy_p2p_actions_are_absent(self):
        self.assertNotIn("强制同步到全部远端 PC", self.source)
        self.assertNotIn("/api/sync/outbound", self.source)
        self.assertNotIn("Tailscale 服务可访问", self.source)

    def test_dashboard_does_not_load_removed_received_file_api(self):
        self.assertNotIn("/api/received", self.source)

    def test_usage_page_uses_device_duration_without_online_series(self):
        self.assertIn("今日设备使用时长", self.source)
        self.assertIn("本小时设备使用时长", self.source)
        self.assertIn("label: '设备使用时长'", self.source)
        self.assertNotIn("在线时长", self.source)

    def test_usage_chart_has_stable_minimum_scale_and_current_time_marker(self):
        usage = self.scripts["usage.js"]
        self.assertIn("suggestedMax: 60", usage)
        self.assertIn("plugins: [usageNowLinePlugin]", usage)
        self.assertIn("fractionalIndex - 0.5", usage)
        self.assertIn("isHistoricalDataView()) return", usage)

    def test_sidebar_brand_uses_the_shared_cyan_logo(self):
        styles = (WEB_ROOT / "styles" / "base.css").read_text(encoding="utf-8")
        logo = WEB_ROOT / "assets" / "life-link-logo.png"
        self.assertIn('class="sidebar-brand-logo"', self.html)
        self.assertIn('/assets/images/life-link-logo.png', self.html)
        self.assertTrue(logo.is_file())
        self.assertGreater(logo.stat().st_size, 10_000)
        self.assertEqual(
            logo.read_bytes()[16:24],
            b"\x00\x00\x04\x00\x00\x00\x04\x00",
        )
        self.assertIn('width:44px; height:44px; flex:0 0 44px', styles)
        self.assertIn('line-height: 1.15', styles)
        self.assertIn('line-height: 1.35', styles)
        self.assertIn('.sidebar-brand-copy, .business-calendar { display:none; }', styles)

    def test_activity_chart_current_time_marker_is_current_day_only(self):
        location = self.scripts["location.js"]
        self.assertIn("const nowMs = Date.now()", location)
        self.assertIn("(nowMs - from) / (to - from)", location)
        self.assertIn("!isHistoricalDataView()", location)

    def test_usage_device_cards_scroll_horizontally_without_wrapping(self):
        styles = (WEB_ROOT / "styles" / "components.css").read_text(encoding="utf-8")
        base_styles = (WEB_ROOT / "styles" / "base.css").read_text(encoding="utf-8")
        self.assertIn("#usage-device-cards {", styles)
        self.assertIn("flex-wrap: nowrap", styles)
        self.assertIn("overflow-x: auto", styles)
        self.assertIn("#usage-device-cards .device-card", styles)
        self.assertIn("flex: 0 0 auto", styles)
        self.assertIn(".main {\n  margin-left: 240px;\n  flex: 1;\n  min-width: 0;", base_styles)

    def test_event_header_separates_subtitle_from_date_and_clock(self):
        self.assertIn("<div class=\"desc\">心愿与事件记录</div>", self.html)
        self.assertIn('class="timeline-header-meta"', self.html)
        self.assertEqual(self.html.count('id="events-date-label"'), 1)
        styles = (WEB_ROOT / "styles" / "wishes-events.css").read_text(encoding="utf-8")
        self.assertIn("#events-date-label { color:var(--text); font-size:28px", styles)

    def test_usage_local_card_prefers_central_custom_name_over_hostname(self):
        devices = self.scripts["devices.js"]
        self.assertIn(
            "display_name: local.display_name || local.hostname || '本机'",
            devices,
        )
        self.assertNotIn(
            "display_name: local.hostname || local.display_name || '本机'",
            devices,
        )

    def test_usage_page_explains_browser_extension_port_conflict(self):
        self.assertIn('id="usage-collection-warning"', self.html)
        self.assertIn("fetch('/api/live-usage')", self.source)
        self.assertIn("browserStatus === 'port_in_use'", self.source)
        self.assertIn("应用用量统计不受影响", self.source)

    def test_system_event_fallback_uses_life_link_branding(self):
        self.assertIn("'Life Link 系统'", self.source)


    def test_sync_status_distinguishes_local_health_from_read_failure(self):
        self.assertIn("async function isLocalClientHealthy()", self.source)
        self.assertIn("renderSyncReadFailure(error)", self.source)
        self.assertIn("if (localClientHealthy) renderSyncReadFailure(error)", self.source)
        self.assertIn("else renderSyncUnavailable()", self.source)

    def test_sync_page_restores_shared_state_and_retries(self):
        self.assertIn("const MULTI_DEVICE_SELECTION_KEY = 'life-radio-selected-device'", self.source)
        self.assertIn("const multiDeviceState = {", self.source)
        # 定时轮询已移除，改为进入设备管理页时按需刷新
        self.assertNotIn("setInterval(() => refreshSyncData()", self.source)
        self.assertIn("if (page === 'devices' && typeof refreshSyncData === 'function')", self.source)

    def test_device_refresh_does_not_replace_an_active_name_edit(self):
        self.assertIn("function hasPendingDeviceNameEdit()", self.source)
        self.assertIn("document.activeElement === input", self.source)
        self.assertIn("if (!hasPendingDeviceNameEdit())", self.source)

    def test_device_management_first_paint_does_not_wait_for_usage(self):
        start = self.source.index("async function refreshSyncData()")
        end = self.source.index("const MULTI_DEVICE_SELECTION_KEY", start)
        refresh_source = self.source[start:end]
        self.assertIn("const rosterPromise = loadDeviceManagementRoster()", refresh_source)
        self.assertIn("const snapshotPromise = loadMultiDeviceSnapshot(true)", refresh_source)
        self.assertIn("const centralStatusPromise = fetch('/api/sync/central')", refresh_source)
        self.assertIn("const roster = await rosterPromise", refresh_source)
        self.assertIn("renderSyncDeviceCards(multiDeviceState.snapshot, null, roster)", refresh_source)
        self.assertNotIn("loadMultiDeviceUsage()", refresh_source)

    def test_retired_demo_pages_and_old_blacklist_views_are_absent(self):
        for retired_id in (
            'page-timeline',
            'page-custom-events',
            'page-finance',
            'page-notes',
            'page-media',
            'chartBlacklistPie',
            'blacklist-tbody',
        ):
            self.assertNotIn(f'id="{retired_id}"', self.source)
        self.assertNotIn("loadMultiDeviceUsageLegacy", self.source)
        self.assertNotIn("loadAppUsageData", self.source)
        self.assertIn("全应用使用时长排行 (Top 10)", self.source)
        self.assertIn("const top = rows.slice(0, 10)", self.source)
        self.assertIn("renderRankAxis", self.source)

    def test_location_page_uses_activity_projection_instead_of_old_segments(self):
        self.assertIn("function drawActivityChart(data)", self.source)
        self.assertIn("activity_state?.intervals", self.source)
        self.assertIn("primary_health_device_id", self.source)
        self.assertIn("活动状态区间", self.source)
        self.assertIn("/api/locations${date ?", self.source)
        self.assertIn("当前业务日", self.source)
        self.assertNotIn("ACTIVITY_PREVIEW_DATE", self.source)
        self.assertIn("setupActivityChartHover", self.source)
        self.assertIn("return interval.state === 'stationary' ? 0.15", self.source)
        self.assertIn("activity-state-address", self.source)
        self.assertIn("activity-state-coordinates", self.source)
        self.assertIn("activity-distance-track", self.source)
        self.assertIn("activityDisplayRatio(interval)", self.source)
        self.assertNotIn("function locationSegmentCard(segment", self.source)

    def test_day_boundary_restores_confirmed_value_when_central_update_fails(self):
        self.assertIn("let confirmedValue = select.value", self.source)
        self.assertIn("if (!response.ok) throw new Error('central shared settings update failed')", self.source)
        self.assertIn("select.value = previousValue", self.source)
        self.assertIn("跨日时间修改失败，已保留原设置。", self.source)

    def test_day_boundary_lives_in_device_management(self):
        sync_page = self.html.index('id="page-sync"')
        day_boundary = self.html.index('id="day-boundary-hour"')
        self.assertGreater(day_boundary, sync_page)
        self.assertEqual(self.html.count('id="day-boundary-hour"'), 1)
        self.assertIn("fetch('/api/settings')", self.scripts["devices.js"])
        self.assertIn('id="pc-login-startup"', self.html)
        self.assertIn('id="pc-login-startup-status"', self.html)
        self.assertIn("/api/runtime/login-startup", self.scripts["devices.js"])
        self.assertIn("state.state === 'missing'", self.scripts["devices.js"])
        self.assertIn("启动项缺失或被管理软件移除", self.scripts["devices.js"])
        self.assertNotIn("day-boundary-hour", self.scripts["app.js"])
        self.assertNotIn("device-pairing-create", self.source)
        self.assertNotIn("/api/device-pairings", self.source)

    def test_wish_days_and_timeline_use_business_day_semantics(self):
        self.assertIn("computeBizDate(wish.business_day_snapshot).bizDate", self.source)
        self.assertIn("await fSharedSettings()", self.source)

    def test_home_progressively_renders_core_before_optional_panels(self):
        self.assertIn("await Promise.allSettled([fWishes(), fTriggers(), fTimeline()])", self.source)
        self.assertIn("loadEventsSecondaryPanels(generation).catch(console.warn)", self.source)
        self.assertIn("await fDevices()", self.source)
        self.assertIn("await fAIReaders()", self.source)
        self.assertNotIn("fTimeline(), fEventBackground(), fDevices(), fAIReaders()", self.source)
        core_render = self.source.index("renderEventSettings(); renderWishCards(); renderEventsTimeline();")
        optional_start = self.source.index("loadEventsSecondaryPanels(generation).catch(console.warn)")
        self.assertLess(core_render, optional_start)

    def test_home_timeline_reuses_the_desktop_window_refresh_cache(self):
        events = self.scripts["wishes-events.js"]
        shared = self.scripts["shared-ui.js"]
        self.assertIn("const EVENTS_TIMELINE_REFRESH_MILLISECONDS = 30_000", events)
        self.assertIn("async function refreshEventsTimelineFromSharedCache()", events)
        self.assertIn("const changed = await fTimeline()", events)
        self.assertIn("if (!changed) return", events)
        self.assertIn("const nextSignature = JSON.stringify(nextEvents)", events)
        self.assertIn("eventsTimelineSignature = null", events)
        self.assertIn("renderEventsTimeline()", events)
        self.assertIn("renderBizDayTimeline()", events)
        self.assertIn("document.visibilityState === 'hidden'", events)
        self.assertIn(
            "page === 'timeline-events' && typeof refreshEventsTimelineFromSharedCache === 'function'",
            shared,
        )

    def test_business_calendar_keeps_historical_date_in_the_url_and_uses_central_summary(self):
        shared = self.scripts["shared-ui.js"]
        styles = (WEB_ROOT / "styles" / "base.css").read_text(encoding="utf-8")
        self.assertIn('id="business-calendar-days"', self.html)
        self.assertIn('id="calendar-return-today"', self.html)
        self.assertIn('/api/calendar-days?from=', shared)
        self.assertIn("window.history.pushState", shared)
        self.assertIn("url.searchParams.set('date', date)", shared)
        self.assertIn("today_business_date", shared)
        self.assertIn("earliest_available_date", shared)
        self.assertIn("total_bytes", shared)
        self.assertIn("calendarBreakdown", shared)
        self.assertIn("calendarMonthLabel", shared)
        self.assertIn("changeBusinessCalendarWeek", shared)
        self.assertIn("generation !== calendarLoadGeneration", shared)
        self.assertIn("calendar-day${day?.available ? ' has-data'", shared)
        self.assertNotIn("size.title =", shared)
        self.assertIn(".historical-data-badge[hidden]", styles)
        self.assertIn(".calendar-day.has-data", styles)
        self.assertIn(".calendar-day.is-today { color:var(--accent)", styles)
        self.assertNotIn(".calendar-day.is-today::after", styles)
        self.assertIn('id="calendar-return-today" hidden>返回</button>', self.html)
        self.assertLess(self.html.index('class="sidebar-title-row"'), self.html.index('class="business-calendar"'))
        self.assertLess(self.html.index('class="business-calendar"'), self.html.index('class="sidebar-nav"'))
        self.assertLess(self.html.index('id="calendar-day-size"'), self.html.index('class="sidebar-footer"'))

    def test_business_calendar_reconciles_automatic_selection_to_central_business_day(self):
        shared = self.scripts["shared-ui.js"]
        self.assertIn("calendarState.todayDate = data.today_business_date || calendarState.todayDate", shared)
        self.assertIn("if (!requested && calendarState.selectedDate !== calendarState.todayDate)", shared)
        self.assertIn("calendarState.selectedDate = calendarState.todayDate", shared)
        self.assertIn("An explicit ?date= remains the user's historical-view choice", shared)

    def test_selected_date_does_not_change_history_wish_dialog(self):
        events = self.scripts["wishes-events.js"]
        location = self.scripts["location.js"]
        health = self.scripts["health-info.js"]
        self.assertIn("getSelectedBusinessDate", events)
        self.assertIn("isHistoricalDataView", events)
        self.assertIn("getSelectedBusinessDate", location)
        self.assertIn("/api/locations${date ?", location)
        self.assertIn("/api/usage${date ?", location)
        self.assertIn("historical ? Promise.resolve(null)", location)
        self.assertIn("getSelectedBusinessDate", health)
        self.assertIn("历史数据不修改当前主手机设置", location)
        self.assertIn("没有事件记录", events)
        self.assertIn("没有活动状态记录", location)
        self.assertIn("listPanel.hidden = false", location)
        self.assertNotIn("历史事件记录", events)
        self.assertIn("if (typeof isHistoricalDataView === 'function' && isHistoricalDataView()) return;", events)
        self.assertIn("async function fHistoryWishes()", events)
        self.assertIn("/api/wishes?include_archived=true", events)
        self.assertIn("wish.status !== 'active'", events)
        self.assertIn("async function showWishHistoryDialog()", events)
        self.assertIn("function renderWishHistory(container)", events)
        self.assertIn("wishState.historyWishesLoaded", events)
        self.assertIn("已完成' : day.evaluation === 'not_completed' ? '未完成' : '未评估'", events)
        self.assertIn("不带入触发器", events)
        self.assertIn('id="wish-cards-title"', self.html)
        self.assertIn('id="wish-history-open"', self.html)
        self.assertNotIn('id="wish-history-panel"', self.html)
        self.assertEqual(self.html.count('data-history-badge'), 1)
        self.assertIn('<h1>Life Link</h1><span class="historical-data-badge" data-history-badge', self.html)

    def test_expired_wish_has_explicit_completion_with_missing_day_guard(self):
        self.assertIn("✓ 完结心愿", self.source)
        self.assertIn("canCompleteWish(wish)", self.source)
        self.assertIn("bizDate > wish.ends_on", self.source)
        self.assertIn("日期结果还未填写", self.source)
        self.assertIn("/complete`, { method: 'POST' }", self.source)
        self.assertIn("Date.UTC(y, m-1, d, dayStartHour, 0, 0) - 8 * 3600 * 1000", self.source)
        self.assertNotIn("multiDeviceState?.sharedSettings?.day_start_hour", self.source)

    def test_event_timeline_converts_utc_to_shared_timezone(self):
        self.assertIn("new Date(e.occurred_at || '')", self.source)
        self.assertIn("timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai'", self.source)
        self.assertIn("hourCycle: 'h23'", self.source)
        self.assertNotIn("(e.occurred_at || '').slice(11, 16)", self.source)

    def test_v113_event_background_settings_and_delivery_presentation(self):
        styles = (WEB_ROOT / "styles" / "wishes-events.css").read_text(encoding="utf-8")
        self.assertIn("EVENT_DISPLAY_BUSINESS_DAY_OFFSET", self.source)
        self.assertIn("/api/event-background?business_date=", self.source)
        self.assertIn("renderEventBackground()", self.source)
        self.assertIn("renderEventSettings()", self.source)
        self.assertIn("sleep_local_time", self.source)
        self.assertNotIn("AI 名称（由配对身份提供）", self.source)
        self.assertNotIn("pairedAI?.display_name", self.source)
        self.assertIn("/api/ai-readers", self.source)
        self.assertIn("clear-reading-progress", self.source)
        self.assertIn("/process-status", self.source)
        self.assertIn("processStatus.process_running === true", self.source)
        self.assertIn("processStatus.process_display_name", self.source)
        self.assertIn("已连接 AI：", self.source)
        self.assertIn("processProductName", self.source)
        self.assertIn("进程正在运行", self.source)
        self.assertIn(".ai-reader-detection", styles)
        self.assertIn(".ai-reader-detection-dot", styles)
        self.assertNotIn("reader.process_running === true", self.source)
        self.assertIn("/api/usage?date=", self.source)
        self.assertIn("hourly_online", self.source)
        self.assertIn("运行时长不可用", self.source)
        self.assertIn("deviceData.local?.device_id", self.source)
        self.assertIn("device-connection-dot", self.source)
        self.assertIn("已连接</span>", self.source)
        self.assertIn('id="ai-pairing-create" hidden aria-hidden="true"', self.source)
        self.assertIn("生成 AI 配对文本", self.source)
        self.assertIn("fetch('/api/ai-readers/pairings'", self.source)
        self.assertIn("生成 AI 配对包", self.source)
        self.assertIn("/api/ai-reader-connection-package/open", self.source)
        self.assertIn("查看 Skill", self.source)
        self.assertIn("/api/ai-reader-skill/open", self.source)
        self.assertIn("查看发送原文", self.source)
        self.assertIn("/context-preview", self.source)
        self.assertIn("JSON.stringify(data.context || data, null, 2)", self.source)
        self.assertIn("event-ai-mark", self.source)
        self.assertIn("e.importance === 'low' ? 'not_applicable'", self.source)
        self.assertIn('aria-label="已推送给 ', self.source)
        self.assertIn('>待推送</span>', self.source)
        self.assertIn('>已推送</span>', self.source)
        self.assertIn("正在获取发送原文，请稍候", self.source)
        self.assertIn("previewBox.setBody", self.source)
        self.assertNotIn("不提供给 AI</span>", self.source)
        self.assertNotIn("最近申请：", self.source)
        self.assertNotIn("最近提供：", self.source)
        self.assertIn("periodic_summary", self.source)
        self.assertIn("scheduled_reminder", self.source)
        self.assertIn("tt === 'scheduled_reminder' ? 1", self.source)
        self.assertIn("const high = e.importance === 'high'", self.source)
        self.assertIn("const tone = high ? 'high' : (e.category === 'system' ? 'system' : 'normal')", self.source)
        self.assertIn(".event-item.event-high", styles)
        self.assertIn("var(--warning)", styles)
        self.assertIn(".event-item.event-system", styles)
        self.assertIn("var(--text-secondary)", styles)
        self.assertIn("border-left:4px solid var(--accent)", styles)
        self.assertIn("delivery.state", self.source)
        self.assertIn("当前离线中，设置不可修改", self.source)

    def test_event_background_renders_central_current_state_items(self):
        self.assertIn("background.real_time_items", self.source)
        self.assertIn("background-current-status", self.source)
        self.assertIn("item.kind === 'device_online' || item.kind === 'current_app'", self.source)
        self.assertIn("function formatRealTimeBackgroundItem(item)", self.source)
        self.assertIn("if (!item.is_stale || /上次更新\\s*[:：]/.test(text)) return text", self.source)
        self.assertIn("上次更新：${time}", self.source)
        self.assertIn("timeZone: wishState.sharedSettings?.timezone || 'Asia/Shanghai'", self.source)

    def test_event_cards_are_prominent_and_highlight_duration(self):
        styles = (WEB_ROOT / "styles" / "wishes-events.css").read_text(encoding="utf-8")
        self.assertIn("function eventDetailHtml(detail)", self.source)
        self.assertIn("event-duration-emphasis", self.source)
        self.assertIn(".event-duration-emphasis", styles)
        self.assertIn("font-size:16px", styles)

    def test_morning_report_uses_two_cancellable_radio_rows(self):
        self.assertEqual(self.source.count('name="morning_mode" type="radio"'), 2)
        self.assertIn("首次使用设备", self.source)
        self.assertIn("radio.dataset.wasChecked", self.source)
        self.assertIn("enabled: !!selectedMode", self.source)
        self.assertNotIn('name="morning_enabled"', self.source)

    def test_wish_trigger_form_uses_catalog_intervals_and_fixed_aggregate_scopes(self):
        self.assertIn("tt.interval_minutes.allowed_values", self.source)
        self.assertNotIn("data-key=\"platform_scope\"", self.source)
        self.assertIn("提醒时限", self.source)
        self.assertIn("delete params.interval_minutes", self.source)
        self.assertIn("params.platform_scope = 'all'", self.source)
        self.assertIn("params.device_id = 'all'", self.source)
        self.assertNotIn("tt.allowed_intervals", self.source)


if __name__ == "__main__":
    unittest.main()
