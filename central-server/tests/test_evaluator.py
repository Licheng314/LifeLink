import json
import tempfile
import unittest
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from central.evaluator import evaluate_all_milestones, evaluate_scheduled_system
from central.storage import CentralStore


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CentralStore(Path(self.temp.name) / "central.sqlite3", {})
        self.connection = self.store._connect()
        self.now = datetime(2026, 8, 9, 15, 5, tzinfo=timezone.utc)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def device(self, device_id, platform="desktop"):
        self.connection.execute(
            """INSERT OR REPLACE INTO devices(
                   device_id, platform, display_name, first_seen_at, last_seen_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (device_id, platform, device_id, "2026-08-09T00:00:00Z", "2026-08-09T00:00:00Z"),
        )

    def event(self, device_id, occurred_at, duration, payload, event_type="app.foreground"):
        event_id = str(uuid.uuid4())
        encoded = json.dumps(payload)
        self.connection.execute(
            """INSERT INTO events(
                   event_id, device_id, occurred_at, event_type, source_json,
                   duration_seconds, revision, payload_json, event_json,
                   content_hash, is_mutable, received_at, updated_at
               ) VALUES (?, ?, ?, ?, '{}', ?, 0, ?, '{}', ?, 0, ?, ?)""",
            (event_id, device_id, occurred_at, event_type, duration, encoded,
             event_id, occurred_at, occurred_at),
        )
        return event_id

    def trigger(self, trigger_type, parameters, interval, *, now=None, enabled=True):
        request_id = str(uuid.uuid4())
        return self.store.create_trigger(
            request_id=request_id, request_hash=request_id, wish_id=None,
            trigger_type=trigger_type, config_version=1, parameters=parameters,
            interval_minutes=interval, enabled=enabled, now=now or self.now,
        )

    def timeline(self, trigger_id=None):
        if trigger_id is None:
            return self.connection.execute(
                "SELECT * FROM timeline_events ORDER BY occurred_at"
            ).fetchall()
        return self.connection.execute(
            "SELECT * FROM timeline_events WHERE trigger_id = ? ORDER BY occurred_at",
            (trigger_id,),
        ).fetchall()

    def test_device_usage_uses_union_and_afk_without_backfilling_existing_milestones(self):
        self.device("desktop-a")
        self.event("desktop-a", "2026-08-09T13:00:00Z", 7200, {
            "app": {"display_name": "Editor"},
            "activitywatch": {"kind": "window", "data": {"app": "Editor"}},
        })
        self.event("desktop-a", "2026-08-09T13:30:00Z", 1800, {
            "activitywatch": {"kind": "afk", "data": {"status": "afk"}},
        })
        created = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)
        trigger = self.trigger("device_usage_milestone", {"device_id": "desktop-a"}, 15, now=created)

        evaluate_all_milestones(self.connection, self.store, now=created)
        evaluate_all_milestones(self.connection, self.store, now=self.now)
        evaluate_all_milestones(self.connection, self.store, now=self.now)

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["subject_json"])["milestone_minutes"], 90)

    def test_blacklist_scope_does_not_cross_platform_or_double_count_rules(self):
        self.device("desktop-a")
        self.device("android-a", "android")
        for device_id in ("desktop-a", "android-a"):
            self.event(device_id, "2026-08-09T14:00:00Z", 3600, {
                "app": {"display_name": "YouTube"},
            })
        self.connection.execute(
            "DELETE FROM blacklist_rules"
        )
        for pattern in ("youtube", "tube"):
            self.store.create_blacklist_rule("app", pattern, pattern, platform_scope="pc")
        trigger = self.trigger("blacklist_usage_milestone", {"platform_scope": "pc"}, 60, now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc))

        evaluate_all_milestones(self.connection, self.store, now=self.now)

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["evidence_json"])["total_seconds"], 3600)

    def test_web_blacklist_uses_derived_site_duration(self):
        self.device("desktop-a")
        self.event("desktop-a", "2026-08-09T14:00:00Z", 3600, {
            "app": {"package_name": "chrome.exe", "display_name": "Google Chrome"},
            "activitywatch": {"kind": "window", "data": {"app": "chrome.exe"}},
        })
        self.event("desktop-a", "2026-08-09T14:00:00Z", 0, {
            "activitywatch": {
                "kind": "web", "bucket_id": "aw-watcher-web-chrome",
                "data": {"url": "https://www.bilibili.com/video/1"},
            },
        })
        self.connection.execute("DELETE FROM blacklist_rules")
        self.store.create_blacklist_rule("domain", "bilibili.com", "Bilibili", platform_scope="web")
        trigger = self.trigger("blacklist_usage_milestone", {"platform_scope": "web"}, 60, now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc))

        evaluate_all_milestones(self.connection, self.store, now=self.now)

        self.assertEqual(len(self.timeline(trigger["trigger_id"])), 1)

    def test_all_blacklist_scope_sums_isolated_pc_android_and_web_rules(self):
        self.device("desktop-a")
        self.device("android-a", "android")
        self.event("desktop-a", "2026-08-09T14:00:00Z", 900, {
            "app": {"display_name": "DesktopGame"},
        })
        self.event("android-a", "2026-08-09T14:00:00Z", 900, {
            "app": {"display_name": "MobileGame"},
        })
        self.event("desktop-a", "2026-08-09T14:15:00Z", 1800, {
            "app": {"package_name": "chrome.exe", "display_name": "Google Chrome"},
            "activitywatch": {"kind": "window", "data": {"app": "chrome.exe"}},
        })
        self.event("desktop-a", "2026-08-09T14:15:00Z", 0, {
            "activitywatch": {
                "kind": "web", "bucket_id": "aw-watcher-web-chrome",
                "data": {"url": "https://blocked.example/video"},
            },
        })
        self.connection.execute("DELETE FROM blacklist_rules")
        self.store.create_blacklist_rule("app", "desktopgame", "PC", platform_scope="pc")
        self.store.create_blacklist_rule("app", "mobilegame", "Android", platform_scope="android")
        self.store.create_blacklist_rule("domain", "blocked.example", "Web", platform_scope="web")
        trigger = self.trigger("blacklist_usage_milestone", {"platform_scope": "all"}, 60, now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc))

        evaluate_all_milestones(self.connection, self.store, now=self.now)

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["evidence_json"])["total_seconds"], 3600)

    def test_late_usage_repeats_by_period_across_midnight_only_when_in_use(self):
        self.store.update_shared_day_start_hour(4)
        self.device("desktop-a")
        self.event("desktop-a", "2026-08-09T15:50:00Z", 2700, {
            "app": {"display_name": "Editor"},
            "activitywatch": {"kind": "window", "data": {"app": "Editor"}},
        })
        trigger = self.trigger(
            "late_usage_milestone",
            {"device_id": "desktop-a", "start_local_time": "23:30"},
            30,
        )
        now = datetime(2026, 8, 9, 16, 35, tzinfo=timezone.utc)

        evaluate_all_milestones(self.connection, self.store, now=now)
        evaluate_all_milestones(self.connection, self.store, now=now)

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual([row["occurred_at"] for row in rows], [
            "2026-08-09T16:30:00Z",
        ])

    def test_late_usage_all_devices_includes_recent_online_server_without_foreground_use(self):
        self.store.update_shared_day_start_hour(4)
        self.device("desktop-active")
        self.device("server-idle")
        self.event("desktop-active", "2026-08-09T15:50:00Z", 1200, {
            "app": {"display_name": "Editor"},
            "activitywatch": {"kind": "window", "data": {"app": "Editor"}},
        })
        self.event(
            "server-idle", "2026-08-09T15:55:00Z", None,
            {"event_key": "application.started", "title": "Server running"},
            event_type="custom.event",
        )
        trigger = self.trigger(
            "late_usage_milestone",
            {"device_id": "all", "start_local_time": "23:30"},
            30,
        )

        evaluate_all_milestones(
            self.connection, self.store,
            now=datetime(2026, 8, 9, 16, 5, tzinfo=timezone.utc),
        )

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual(len(rows), 1)
        evidence = json.loads(rows[0]["evidence_json"])
        self.assertEqual(evidence["active_device_ids"], ["desktop-active", "server-idle"])

    def test_late_usage_accepts_15_minutes_but_not_16_minutes_old_fact(self):
        self.store.update_shared_day_start_hour(4)
        self.device("desktop-a")
        trigger = self.trigger("late_usage_milestone", {"device_id": "desktop-a", "start_local_time": "23:30"}, 30)
        self.event("desktop-a", "2026-08-09T15:45:00Z", None, {"event_key": "application.started", "title": "alive"}, event_type="custom.event")
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(len(self.timeline(trigger["trigger_id"])), 1)
        self.connection.execute("DELETE FROM timeline_events")
        self.connection.execute("DELETE FROM events")
        self.event("desktop-a", "2026-08-09T15:44:00Z", None, {"event_key": "application.started", "title": "old"}, event_type="custom.event")
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(len(self.timeline(trigger["trigger_id"])), 0)

    def test_late_usage_does_not_use_future_last_seen_for_old_checkpoint(self):
        self.store.update_shared_day_start_hour(4)
        self.device("desktop-a")
        self.connection.execute("UPDATE devices SET last_seen_at='2026-08-09T16:35:00Z' WHERE device_id='desktop-a'")
        trigger = self.trigger("late_usage_milestone", {"device_id": "desktop-a", "start_local_time": "23:30"}, 30)
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 16, 35, tzinfo=timezone.utc))
        self.assertEqual(self.timeline(trigger["trigger_id"]), [])

    def test_periodic_first_slot_is_after_start_and_uses_first_real_usage(self):
        self.device("desktop-a")
        self.store.update_shared_settings({"periodic_summary": {"enabled": True, "interval_minutes": 120, "start_local_time": "10:00", "end_local_time": "22:00"}})
        self.event("desktop-a", "2026-08-09T02:15:00Z", 600, {"app": {"display_name": "Editor"}, "activitywatch": {"kind": "window", "data": {"app": "Editor"}}})
        evaluate_scheduled_system(self.connection, self.store, now=datetime(2026, 8, 9, 4, 0, tzinfo=timezone.utc))
        row = self.connection.execute("SELECT occurred_at, detail, statistics_window_json FROM timeline_events WHERE event_key='report.periodic'").fetchone()
        self.assertEqual(row["occurred_at"], "2026-08-09T04:00:00Z")
        self.assertEqual(row["detail"], "10:15–12:00 定时总结已准备就绪。等待 AI 接入。")
        self.assertEqual(json.loads(row["statistics_window_json"])["from"], "2026-08-09T02:15:00Z")

    def test_morning_report_uses_today_sleep_but_yesterday_steps(self):
        self.store.update_shared_day_start_hour(4)
        self.store.update_shared_settings({"morning_report": {
            "enabled": True,
            "mode": "after_first_usage",
            "delay_minutes": 60,
            "local_time": None,
        }})
        self.device("android-a", "android")
        self.event(
            "android-a", "2026-08-09T02:02:00Z", 60,
            {"app": {"display_name": "Launcher"}},
        )

        def health_for_date(_connection, target_date, *, now=None):
            if target_date == date(2026, 8, 9):
                sleep = {
                    "estimated_start": "2026-08-08T18:50:00Z",
                    "estimated_end": "2026-08-09T02:02:00Z",
                    "rest_seconds": 25920,
                }
                steps = 5
            else:
                sleep = {
                    "estimated_start": "2026-08-07T17:40:00Z",
                    "estimated_end": "2026-08-07T23:45:00Z",
                    "rest_seconds": 21900,
                }
                steps = 8796
            return {"sleep": sleep, "steps": {"devices": [{"steps": steps}]}}

        with mock.patch("central.evaluator.build_health_info", side_effect=health_for_date) as health:
            evaluate_scheduled_system(
                self.connection,
                self.store,
                now=datetime(2026, 8, 9, 3, 10, tzinfo=timezone.utc),
            )

        row = self.connection.execute(
            "SELECT occurred_at, detail, evidence_json FROM timeline_events "
            "WHERE event_key='report.morning'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["occurred_at"], "2026-08-09T03:02:00Z")
        self.assertEqual(row["detail"], "今日早报已准备就绪。等待 AI 接入。")
        body = json.loads(row["evidence_json"])["body"]
        self.assertIn("睡眠参考区间：02:50–10:02", body)
        self.assertIn("昨日步数：8796 步", body)
        self.assertNotIn("01:40–07:45", body)
        self.assertNotIn("昨日步数：5 步", body)
        requested_dates = [call.args[1] for call in health.call_args_list]
        self.assertIn(date(2026, 8, 8), requested_dates)
        self.assertIn(date(2026, 8, 9), requested_dates)

    def test_system_late_online_check_runs_every_half_hour(self):
        self.store.update_shared_day_start_hour(4)
        self.device("late-desktop")
        # The production business day runs until 04:00, so 23:00 Shanghai is
        # 15:00 UTC and 00:00 Shanghai remains the same late-check business date.
        # A foreground fact covering 23:20-23:35 Shanghai keeps the device online at 23:30.
        self.event("late-desktop", "2026-08-09T15:20:00Z", 900, {
            "app": {"display_name": "Browser"},
            "activitywatch": {"kind": "window", "data": {"app": "Browser"}},
        })
        # A second foreground fact covers the 16:00 checkpoint (00:00 Shanghai).
        self.event("late-desktop", "2026-08-09T15:55:00Z", 600, {
            "app": {"display_name": "Browser"},
            "activitywatch": {"kind": "window", "data": {"app": "Browser"}},
        })

        evaluate_scheduled_system(self.connection, self.store, now=datetime(2026, 8, 9, 15, 29, tzinfo=timezone.utc))
        self.assertEqual([], self.connection.execute(
            "SELECT occurred_at FROM timeline_events WHERE event_key='system.late_online_check'"
        ).fetchall())

        evaluate_scheduled_system(self.connection, self.store, now=datetime(2026, 8, 9, 15, 30, tzinfo=timezone.utc))
        rows = self.connection.execute(
            "SELECT occurred_at, detail, evidence_json FROM timeline_events WHERE event_key='system.late_online_check' ORDER BY occurred_at"
        ).fetchall()
        self.assertEqual([row["occurred_at"] for row in rows], ["2026-08-09T15:30:00Z"])
        self.assertIn("30分钟", rows[0]["detail"])
        evidence = json.loads(rows[0]["evidence_json"])
        self.assertEqual(evidence["rule"], "late_online_half_hourly")
        self.assertEqual(evidence["online_device_ids"], ["late-desktop"])

        # Re-running is idempotent; reaching 00:00 Shanghai creates the next
        # half-hour checkpoint instead of waiting one hour.
        evaluate_scheduled_system(self.connection, self.store, now=datetime(2026, 8, 9, 15, 30, tzinfo=timezone.utc))
        evaluate_scheduled_system(self.connection, self.store, now=datetime(2026, 8, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(
            [row["occurred_at"] for row in self.connection.execute(
                "SELECT occurred_at FROM timeline_events WHERE event_key='system.late_online_check' ORDER BY occurred_at"
            ).fetchall()],
            ["2026-08-09T15:30:00Z", "2026-08-09T16:00:00Z"],
        )

    def test_system_device_usage_is_per_device_with_combined_top_five(self):
        self.device("desktop-a")
        self.device("android-a", "android")
        for index, name in enumerate(("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot")):
            self.event("desktop-a", f"2026-08-09T04:{index * 10:02d}:00Z", 600, {"app": {"display_name": name}})
        self.event("android-a", "2026-08-09T04:00:00Z", 3600, {"app": {"display_name": "Mobile"}})

        evaluate_scheduled_system(self.connection, self.store, now=datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc))

        rows = self.connection.execute(
            "SELECT * FROM timeline_events WHERE event_key='system.device_usage_milestone' ORDER BY title"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        desktop = next(row for row in rows if "desktop-a" in row["title"])
        self.assertNotIn("累计使用", desktop["title"])
        self.assertIn("1小时", desktop["detail"])
        snapshot = json.loads(desktop["evidence_json"])["usage_snapshot"]["devices"][0]
        self.assertEqual(len(snapshot["top_items"]), 5)
        self.assertNotIn("Foxtrot", [item["name"] for item in snapshot["top_items"]])

    def test_linked_wish_trigger_stops_after_fixed_end_date(self):
        self.device("desktop-a")
        wish = self.store.create_wish(
            request_id=str(uuid.uuid4()), request_hash=str(uuid.uuid4()), text="按时休息",
            duration_days=3, ai_tracking_enabled=False, source_device_id="desktop-a",
            now=datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc),
        )
        request_id = str(uuid.uuid4())
        trigger = self.store.create_trigger(
            request_id=request_id, request_hash=request_id, wish_id=wish["wish_id"],
            trigger_type="device_usage_milestone", config_version=1,
            parameters={"device_id": "desktop-a"}, interval_minutes=15, enabled=True,
            now=datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc),
        )
        self.event("desktop-a", "2026-08-12T14:00:00Z", 3600, {"app": {"display_name": "Editor"}})

        evaluate_all_milestones(
            self.connection, self.store, now=datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(self.timeline(trigger["trigger_id"]), [])

    def test_new_trigger_at_135_minutes_waits_for_150_and_is_idempotent(self):
        self.device("desktop-a")
        self.event("desktop-a", "2026-08-09T12:45:00Z", 10800, {
            "app": {"display_name": "Editor"},
            "activitywatch": {"kind": "window", "data": {"app": "Editor"}},
        })
        created = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)
        trigger = self.trigger("device_usage_milestone", {"device_id": "desktop-a"}, 15, now=created)

        evaluate_all_milestones(self.connection, self.store, now=created)
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 15, 15, tzinfo=timezone.utc))
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 15, 15, tzinfo=timezone.utc))

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["subject_json"])["milestone_minutes"], 150)

    def test_interval_edits_and_reenable_start_new_dedupe_generations(self):
        self.device("desktop-a")
        self.event("desktop-a", "2026-08-09T13:00:00Z", 10800, {
            "app": {"display_name": "Editor"},
            "activitywatch": {"kind": "window", "data": {"app": "Editor"}},
        })
        trigger = self.trigger(
            "device_usage_milestone", {"device_id": "desktop-a"}, 15,
            now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc),
        )
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc))
        self.store.patch_trigger(trigger["trigger_id"], {"interval_minutes": 30}, now=datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc))
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc))
        self.store.patch_trigger(trigger["trigger_id"], {"interval_minutes": 15}, now=datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc))
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 14, 45, tzinfo=timezone.utc))
        self.store.patch_trigger(trigger["trigger_id"], {"enabled": False}, now=datetime(2026, 8, 9, 14, 45, tzinfo=timezone.utc))
        self.store.patch_trigger(trigger["trigger_id"], {"enabled": True}, now=datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc))
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc))

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual([json.loads(row["subject_json"])["milestone_minutes"] for row in rows], [60, 90, 105])
        self.assertEqual(len({row["dedupe_key"] for row in rows}), 3)

    def test_blacklist_jump_only_emits_latest_and_android_afk_is_not_subtracted(self):
        self.device("android-a", "android")
        self.event("android-a", "2026-08-09T13:00:00Z", 3600, {
            "app": {"display_name": "YouTube"},
        })
        self.event("android-a", "2026-08-09T13:30:00Z", 1800, {
            "activitywatch": {"kind": "afk", "data": {"status": "afk"}},
        })
        self.connection.execute("DELETE FROM blacklist_rules")
        self.store.create_blacklist_rule("app", "youtube", "YouTube", platform_scope="android")
        trigger = self.trigger(
            "blacklist_usage_milestone", {"platform_scope": "android"}, 15,
            now=datetime(2026, 8, 9, 13, 0, tzinfo=timezone.utc),
        )

        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc))

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["subject_json"])["milestone_minutes"], 60)
        self.assertEqual(json.loads(rows[0]["evidence_json"])["total_seconds"], 3600)

    def test_late_trigger_does_not_backfill_configuration_time_periods(self):
        self.store.update_shared_day_start_hour(4)
        self.device("desktop-a")
        self.event("desktop-a", "2026-08-09T15:50:00Z", 3600, {
            "app": {"display_name": "Editor"},
            "activitywatch": {"kind": "window", "data": {"app": "Editor"}},
        })
        trigger = self.trigger(
            "late_usage_milestone", {"device_id": "desktop-a", "start_local_time": "23:30"}, 30,
            now=datetime(2026, 8, 9, 16, 5, tzinfo=timezone.utc),
        )

        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 16, 5, tzinfo=timezone.utc))
        evaluate_all_milestones(self.connection, self.store, now=datetime(2026, 8, 9, 16, 35, tzinfo=timezone.utc))

        rows = self.timeline(trigger["trigger_id"])
        self.assertEqual([row["occurred_at"] for row in rows], ["2026-08-09T16:30:00Z"])

    def test_only_whitelisted_custom_events_are_projected_idempotently(self):
        self.device("desktop-a")
        supported_id = self.event(
            "desktop-a", "2026-08-09T14:00:00Z", None,
            {"event_key": "application.started", "title": "服务启动", "detail": "ready"},
            event_type="custom.event",
        )
        self.event(
            "desktop-a", "2026-08-09T14:01:00Z", None,
            {"event_key": "unapproved.event", "title": "不应投影"},
            event_type="custom.event",
        )

        evaluate_all_milestones(self.connection, self.store, now=self.now)
        evaluate_all_milestones(self.connection, self.store, now=self.now)

        rows = self.timeline()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dedupe_key"], f"custom.event|{supported_id}")
        self.assertEqual(rows[0]["source_device_id"], "desktop-a")
        self.assertEqual(rows[0]["title"], "Life Link 已启动 · desktop-a")
        self.assertIn("desktop-a（Windows）", rows[0]["detail"])
        self.assertEqual(json.loads(rows[0]["evidence_json"])["platform"], "Windows")
