import unittest
from pathlib import Path


OPENAPI_PATH = Path(__file__).resolve().parents[1] / "life-radio-api-v1.yaml"


class CalendarDaysContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.openapi = OPENAPI_PATH.read_text(encoding="utf-8")

    def test_calendar_endpoint_declares_inclusive_business_day_contract(self):
        for statement in (
            "/v1/calendar-days:",
            "operationId: listCalendarDays",
            "#/components/parameters/CalendarFrom",
            "#/components/parameters/CalendarTo",
            "at most 42 inclusive business dates",
            "- readBearerAuth: []",
            "- registeredDeviceAuth: []",
            "#/components/schemas/CalendarDaysResponse",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.openapi)

    def test_calendar_size_schema_preserves_mutually_exclusive_modules(self):
        for statement in (
            "CalendarDaysResponse:", "CalendarDay:", "CalendarDayModules:",
            "CalendarModuleSize:", "required: [usage, location, health, timeline, other]",
            "event_json once", "timeline_events is timeline", "not SQLite file size",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.openapi)


if __name__ == "__main__":
    unittest.main()
