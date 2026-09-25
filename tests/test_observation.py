import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


START = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


def iso(moment):
    return moment.isoformat(timespec="seconds")


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [START]
        self.clock = lambda: self.now[0]
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.rules = RuleEngine(clock=self.clock)
        self.service = DomainService(self.repo, self.rules)
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng-1", "engineer")
        self.safety = Actor("safety-1", "safety")
        self.operator = Actor("op-1", "operator")
        self.deadline = iso(START + timedelta(days=2))

    def tearDown(self):
        self.tmp.cleanup()

    def advance(self, **delta):
        self.now[0] = self.now[0] + timedelta(**delta)

    def commission_change(self, deadline=None):
        unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": unit["id"], "description": "alarm threshold"},
        )
        self.service.transition(
            self.admin, change["id"], "assess",
            {"risk_level": "low", "analyst": "E-1"},
        )
        self.service.transition(
            self.safety, change["id"], "approve",
            {"approvals": ["S-1"], "permit_id": "MOC-1"},
        )
        self.service.transition(
            self.admin, change["id"], "implement", {"procedure_version": "v2"}
        )
        change = self.service.transition(
            self.admin, change["id"], "commission",
            {"tests_passed": True, "observation_deadline": deadline or self.deadline},
        )
        return change

    def reading(self, point, value, read_at=None, min_limit=0, max_limit=10):
        return {
            "sample_point": point,
            "min_limit": min_limit,
            "max_limit": max_limit,
            "value": value,
            "read_at": read_at or iso(self.now[0]),
        }

    def test_commission_requires_future_deadline(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U", "location": "L"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "d"}
        )
        for action, actor, data in (
            ("assess", self.admin, {"risk_level": "low", "analyst": "E"}),
            ("approve", self.safety, {"approvals": ["S"], "permit_id": "P"}),
            ("implement", self.admin, {"procedure_version": "v"}),
        ):
            self.service.transition(actor, change["id"], action, data)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "commission", {"tests_passed": True}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, change["id"], "commission",
                {"tests_passed": True, "observation_deadline": iso(START - timedelta(hours=1))},
            )

    def test_operator_cannot_submit_reading(self):
        change = self.commission_change()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.operator, change["id"], "submit_reading", self.reading("P1", 5)
            )

    def test_reading_requires_a_limit(self):
        change = self.commission_change()
        data = self.reading("P1", 5)
        data.pop("min_limit")
        data.pop("max_limit")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.engineer, change["id"], "submit_reading", data
            )

    def test_history_retained_and_anomalies_accumulate(self):
        change = self.commission_change()
        self.advance(minutes=10)
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 11)
        )
        self.advance(minutes=10)
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 12)
        )
        self.advance(minutes=10)
        # Latest reading is back inside the limit, but anomalies stay cumulative.
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 5)
        )
        updated = self.service.get(change["id"])
        observation = updated["data"]["observation"]
        self.assertEqual(3, len(observation["readings"]))
        self.assertEqual([1], observation["readings"][0]["anomaly_ids"])
        self.assertEqual([2], observation["readings"][1]["anomaly_ids"])
        self.assertEqual([], observation["readings"][2]["anomaly_ids"])
        summary = observation["summary"]
        self.assertEqual(3, summary["total_readings"])
        self.assertEqual(2, summary["anomaly_count"])
        self.assertEqual(2, summary["unresolved_count"])
        point = summary["sample_points"][0]
        self.assertEqual("P1", point["sample_point"])
        self.assertEqual(3, point["total_readings"])
        self.assertEqual(2, point["anomaly_count"])
        self.assertEqual(2, point["unresolved_count"])
        self.assertFalse(point["latest_out_of_limit"])
        self.assertEqual(5, point["latest_value"])

    def test_cannot_close_while_window_open(self):
        change = self.commission_change()
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 5)
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety, change["id"], "close", {"outcome": "normal"}
            )

    def test_unresolved_anomaly_blocks_close_until_disposed(self):
        change = self.commission_change()
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 11)
        )
        self.advance(hours=1)
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 4)
        )
        self.advance(days=3)  # observation window ended
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety, change["id"], "close", {"outcome": "normal"}
            )
        with self.assertRaises(NotFoundError):
            self.service.transition(
                self.engineer, change["id"], "dispose_anomaly",
                {"anomaly_id": 99, "disposition": "sensor recalibrated"},
            )
        self.service.transition(
            self.engineer, change["id"], "dispose_anomaly",
            {"anomaly_id": 1, "disposition": "sensor recalibrated"},
        )
        disposed = self.service.get(change["id"])
        disposition = disposed["data"]["observation"]["readings"][0]["dispositions"]["1"]
        self.assertEqual("eng-1", disposition["disposed_by"])
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.engineer, change["id"], "dispose_anomaly",
                {"anomaly_id": 1, "disposition": "again"},
            )
        closed = self.service.transition(
            self.safety, change["id"], "close", {"outcome": "normal"}
        )
        self.assertEqual("closed", closed["status"])
        result = closed["data"]["observation_result"]
        self.assertEqual(2, result["total_readings"])
        self.assertEqual(1, result["anomaly_count"])
        self.assertEqual(self.deadline, result["deadline"])
        self.assertIn("closed_at", result)

    def test_rollback_with_unresolved_anomaly_needs_safety(self):
        change = self.commission_change()
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 20)
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.engineer, change["id"], "rollback", {"reason": "drift"}
            )
        rolled = self.service.transition(
            self.safety, change["id"], "rollback", {"reason": "drift"}
        )
        self.assertEqual("rolled_back", rolled["status"])
        # After the safety-approved rollback, closing no longer applies observation gates.
        closed = self.service.transition(
            self.safety, rolled["id"], "close", {"outcome": "rolled back per safety"}
        )
        self.assertEqual("closed", closed["status"])

    def test_rollback_without_anomaly_allowed_for_engineer(self):
        change = self.commission_change()
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 5)
        )
        rolled = self.service.transition(
            self.engineer, change["id"], "rollback", {"reason": "schedule change"}
        )
        self.assertEqual("rolled_back", rolled["status"])

    def test_submit_reading_only_in_commissioned_status(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U", "location": "L"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "d"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.engineer, change["id"], "submit_reading", self.reading("P1", 5)
            )

    def test_clean_window_closes_after_deadline(self):
        change = self.commission_change()
        self.service.transition(
            self.engineer, change["id"], "submit_reading", self.reading("P1", 5)
        )
        self.advance(days=3)
        closed = self.service.transition(
            self.safety, change["id"], "close", {"outcome": "all clear"}
        )
        self.assertEqual(0, closed["data"]["observation_result"]["anomaly_count"])
        self.assertEqual(1, closed["data"]["observation_result"]["total_readings"])


if __name__ == "__main__":
    unittest.main()
