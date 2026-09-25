import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class SchedulingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.coordinator = Actor("coord-1", "coordinator")
        self.cycle = "2026-spring"

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex):
        return self.service.create(
            self.admin, "animal", {"name": name, "sex": sex}
        )["id"]

    def _pairing(self, sire_id, dam_id, venue="A馆", cycle=None):
        return self.service.create(
            self.coordinator,
            "pairing",
            {
                "proposed_by": "coord-1",
                "cycle": cycle or self.cycle,
                "venue": venue,
                "sire_id": sire_id,
                "dam_id": dam_id,
            },
        )

    def _approve(self, pairing, actor=None):
        return self.service.transition(
            actor or self.coordinator,
            pairing["id"],
            "approve",
            {"approvals": ["vet-1"]},
            expected_version=pairing["version"],
        )

    def _refresh(self, pairing):
        return self.service.get(pairing["id"])

    def test_pairing_requires_cycle_and_venue(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "pairing",
                {"proposed_by": "coord-1", "sire_id": sire, "dam_id": dam},
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "pairing",
                {
                    "proposed_by": "coord-1",
                    "cycle": self.cycle,
                    "sire_id": sire,
                    "dam_id": dam,
                },
            )

    def test_same_animal_same_cycle_blocks_second_approval(self):
        sire = self._animal("M-1", "male")
        dam1 = self._animal("F-1", "female")
        dam2 = self._animal("F-2", "female")
        first = self._pairing(sire, dam1, venue="A馆")
        second = self._pairing(sire, dam2, venue="B馆")

        self._approve(first)
        with self.assertRaises(ConflictError) as caught:
            self._approve(second)

        # 错误必须说清冲突个体与占用编号，且当前建议仍留在待审批。
        message = str(caught.exception)
        self.assertIn("M-1", message)
        self.assertIn(sire, message)
        self.assertIn(first["id"], message)
        self.assertEqual(self._refresh(second)["status"], "proposed")
        issues = caught.exception.details["issues"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "occupied")
        self.assertEqual(issues[0]["animal_id"], sire)
        self.assertEqual(issues[0]["occupant"]["id"], first["id"])

    def test_same_animal_different_cycle_is_allowed(self):
        sire = self._animal("M-1", "male")
        dam1 = self._animal("F-1", "female")
        dam2 = self._animal("F-2", "female")
        first = self._pairing(sire, dam1, cycle="2026-spring")
        second = self._pairing(sire, dam2, cycle="2026-autumn")
        self._approve(first)
        self.assertEqual(self._approve(second)["status"], "approved")

    def test_quarantined_and_deceased_parents_block_approval(self):
        sire = self._animal("M-1", "male")
        dam = self._animal("F-1", "female")
        self.service.transition(
            self.admin, sire, "quarantine_animal", {"reason": "观察"}
        )
        pairing = self._pairing(sire, dam)
        with self.assertRaises(ValidationError) as caught:
            self._approve(pairing)
        self.assertIn("M-1", str(caught.exception))
        self.assertIn("隔离", str(caught.exception))
        self.assertEqual(self._refresh(pairing)["status"], "proposed")

        self.service.transition(self.admin, sire, "release_quarantine", {})
        self.service.transition(
            self.admin, dam, "mark_deceased", {"cause": "illness"}
        )
        pairing = self._refresh(pairing)
        with self.assertRaises(ValidationError) as caught:
            self._approve(pairing)
        self.assertIn("F-1", str(caught.exception))
        self.assertIn("死亡", str(caught.exception))
        self.assertEqual(self._refresh(pairing)["status"], "proposed")

    def test_rejecting_approved_pairing_releases_schedule(self):
        sire = self._animal("M-1", "male")
        dam1 = self._animal("F-1", "female")
        dam2 = self._animal("F-2", "female")
        first = self._pairing(sire, dam1, venue="A馆")
        second = self._pairing(sire, dam2, venue="B馆")

        self._approve(first)
        with self.assertRaises(ConflictError):
            self._approve(second)

        # 旧建议驳回（已批准也允许驳回），双方档期释放。
        rejected = self.service.transition(
            self.coordinator,
            first["id"],
            "reject",
            {"reason": "改期"},
        )
        self.assertEqual(rejected["status"], "rejected")
        second = self._refresh(second)
        self.assertEqual(self._approve(second)["status"], "approved")

    def test_completed_pairing_still_occupies_the_cycle(self):
        sire = self._animal("M-1", "male")
        dam1 = self._animal("F-1", "female")
        dam2 = self._animal("F-2", "female")
        first = self._pairing(sire, dam1)
        second = self._pairing(sire, dam2)

        self._approve(first)
        self.service.transition(
            self.coordinator, first["id"], "complete",
            {"offspring_ids": ["offspring-1"]},
        )
        with self.assertRaises(ConflictError) as caught:
            self._approve(second)
        self.assertIn(first["id"], str(caught.exception))

    def test_concurrent_approvals_cannot_double_book(self):
        sire = self._animal("M-1", "male")
        dam1 = self._animal("F-1", "female")
        dam2 = self._animal("F-2", "female")
        first = self._pairing(sire, dam1)
        second = self._pairing(sire, dam2)

        barrier = threading.Barrier(2)
        results = []

        def approve(pairing):
            try:
                barrier.wait(timeout=10)
                self.service.transition(
                    self.coordinator, pairing["id"], "approve",
                    {"approvals": ["vet-1"]},
                    expected_version=pairing["version"],
                )
                results.append(("ok", pairing["id"]))
            except ConflictError as exc:
                results.append(("conflict", pairing["id"], str(exc)))

        t1 = threading.Thread(target=approve, args=(first,))
        t2 = threading.Thread(target=approve, args=(second,))
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)

        statuses = [self._refresh(first)["status"], self._refresh(second)["status"]]
        self.assertEqual(sorted(statuses), ["approved", "proposed"])
        self.assertEqual(len([r for r in results if r[0] == "ok"]), 1)
        loser = next(r for r in results if r[0] == "conflict")
        self.assertIn(sire, loser[2])

    def test_overview_groups_by_cycle_and_marks_occupancy(self):
        sire = self._animal("M-1", "male")
        dam1 = self._animal("F-1", "female")
        dam2 = self._animal("F-2", "female")
        first = self._pairing(sire, dam1, venue="A馆")
        second = self._pairing(sire, dam2, venue="B馆")
        self._approve(first)

        overview = self.service.overview()
        self.assertEqual(len(overview["cycles"]), 1)
        cycle = overview["cycles"][0]
        self.assertEqual(cycle["cycle"], self.cycle)
        self.assertEqual(set(cycle["venues"]), {"A馆", "B馆"})
        self.assertIn(sire, cycle["occupied_animal_ids"])
        by_id = {item["id"]: item for item in cycle["pairings"]}
        self.assertEqual(by_id[first["id"]]["status"], "approved")
        waiting = by_id[second["id"]]
        self.assertEqual(waiting["status"], "proposed")
        self.assertTrue(
            any(first["id"] in warning for warning in waiting["warnings"])
        )


if __name__ == "__main__":
    unittest.main()
