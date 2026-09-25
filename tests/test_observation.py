import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

PAST_DEADLINE = "2020-01-01T00:00:00+00:00"
FUTURE_DEADLINE = "2099-01-01T00:00:00+00:00"


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.engineer = Actor("eng-1", "engineer")
        self.safety = Actor("saf-1", "safety")

    def tearDown(self):
        self.tmp.cleanup()

    def _commissioned_change(self, deadline=PAST_DEADLINE):
        unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin,
            "change",
            {"unit_id": unit["id"], "description": "Change alarm threshold"},
        )
        self.service.transition(
            self.admin, change["id"], "assess", {"risk_level": "low", "analyst": "E-1"}
        )
        self.service.transition(
            self.safety,
            change["id"],
            "approve",
            {"approvals": ["S-1"], "permit_id": "MOC-1"},
        )
        self.service.transition(
            self.engineer, change["id"], "implement", {"procedure_version": "v2"}
        )
        return self.service.transition(
            self.engineer,
            change["id"],
            "commission",
            {"tests_passed": True, "observation_deadline": deadline},
        )

    def _observation(self, change_id, limit=80.0):
        return self.service.create(
            self.engineer,
            "observation",
            {"change_id": change_id, "point": "T-101", "limit_value": limit},
        )

    def _report(self, obs_id, value, reading_time="2026-09-25T08:00:00+00:00"):
        return self.service.transition(
            self.engineer,
            obs_id,
            "report",
            {"value": value, "reading_time": reading_time},
        )

    def test_commission_requires_observation_deadline(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "d"}
        )
        self.service.transition(
            self.admin, change["id"], "assess", {"risk_level": "low", "analyst": "E-1"}
        )
        self.service.transition(
            self.safety,
            change["id"],
            "approve",
            {"approvals": ["S-1"], "permit_id": "MOC-1"},
        )
        self.service.transition(
            self.engineer, change["id"], "implement", {"procedure_version": "v2"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.engineer, change["id"], "commission", {"tests_passed": True}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.engineer,
                change["id"],
                "commission",
                {"tests_passed": True, "observation_deadline": "not-a-date"},
            )

    def test_observation_requires_commissioned_change(self):
        unit = self.service.create(
            self.admin, "unit", {"name": "U-1", "location": "Plant-A"}
        )
        change = self.service.create(
            self.admin, "change", {"unit_id": unit["id"], "description": "d"}
        )
        with self.assertRaises(ValidationError):
            self._observation(change["id"])

    def test_report_keeps_history_and_latest_reading_decides(self):
        change = self._commissioned_change()
        obs = self._observation(change["id"])
        self.assertEqual(obs["status"], "monitoring")

        normal = self._report(obs["id"], 70.0, "2026-09-25T08:00:00+00:00")
        self.assertEqual(normal["status"], "normal")
        self.assertEqual(normal["data"]["exceed_count"], 0)

        over = self._report(obs["id"], 95.0, "2026-09-25T09:00:00+00:00")
        self.assertEqual(over["status"], "exceeded")
        self.assertEqual(over["data"]["exceed_count"], 1)
        self.assertEqual(over["data"]["latest_value"], 95.0)

        over_again = self._report(obs["id"], 90.0, "2026-09-25T10:00:00+00:00")
        self.assertEqual(over_again["data"]["exceed_count"], 2)

        back = self._report(obs["id"], 60.0, "2026-09-25T11:00:00+00:00")
        self.assertEqual(back["status"], "normal")
        self.assertEqual(back["data"]["latest_value"], 60.0)
        self.assertEqual(back["data"]["exceed_count"], 2)
        readings = back["data"]["readings"]
        self.assertEqual(len(readings), 4)
        self.assertEqual(readings[0]["value"], 70.0)
        self.assertEqual(readings[-1]["reading_time"], "2026-09-25T11:00:00+00:00")

    def test_report_rejects_invalid_reading(self):
        change = self._commissioned_change()
        obs = self._observation(change["id"])
        with self.assertRaises(ValidationError):
            self._report(obs["id"], "hot")
        with self.assertRaises(ValidationError):
            self._report(obs["id"], 10.0, "not-a-time")

    def test_close_blocked_before_observation_period_ends(self):
        change = self._commissioned_change(deadline=FUTURE_DEADLINE)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety, change["id"], "close", {"outcome": "done"}
            )

    def test_close_after_observation_period(self):
        change = self._commissioned_change()
        obs = self._observation(change["id"])
        self._report(obs["id"], 50.0)
        closed = self.service.transition(
            self.safety, change["id"], "close", {"outcome": "观察期满，运行正常"}
        )
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["data"]["closed_by"], "saf-1")

    def test_unhandled_anomaly_blocks_close_and_restricts_rollback(self):
        change = self._commissioned_change()
        obs = self._observation(change["id"])
        self._report(obs["id"], 120.0)

        with self.assertRaises(ValidationError):
            self.service.transition(
                self.safety, change["id"], "close", {"outcome": "done"}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.engineer, change["id"], "rollback", {"reason": "drift"}
            )
        rolled_back = self.service.transition(
            self.safety, change["id"], "rollback", {"reason": "超限未处置，安全员批准回退"}
        )
        self.assertEqual(rolled_back["status"], "rolled_back")
        closed = self.service.transition(
            self.safety, change["id"], "close", {"outcome": "已回退"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_handled_anomaly_allows_close(self):
        change = self._commissioned_change()
        obs = self._observation(change["id"])
        self._report(obs["id"], 120.0)
        handled = self.service.transition(
            self.safety, obs["id"], "handle", {"disposition": "已调整联锁值"}
        )
        self.assertEqual(handled["status"], "handled")
        self.assertEqual(handled["data"]["handled_by"], "saf-1")
        closed = self.service.transition(
            self.safety, change["id"], "close", {"outcome": "异常已处置"}
        )
        self.assertEqual(closed["status"], "closed")

    def test_rollback_without_anomalies_keeps_default_roles(self):
        change = self._commissioned_change()
        rolled_back = self.service.transition(
            self.engineer, change["id"], "rollback", {"reason": "unexpected drift"}
        )
        self.assertEqual(rolled_back["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
