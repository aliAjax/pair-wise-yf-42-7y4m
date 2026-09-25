import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PairingScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.vet = Actor("vet-1", "veterinarian")
        self.coordinator = Actor("coord-1", "coordinator")
        self.sire = self._animal("M-1", "male")
        self.dam = self._animal("F-1", "female")
        self.dam2 = self._animal("F-2", "female")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex):
        return self.service.create(self.admin, "animal", {"name": name, "sex": sex})

    def _pairing(self, cycle="2026-S1", venue="东区繁育馆", actor=None):
        return self.service.create(
            actor or self.coordinator,
            "pairing",
            {"proposed_by": "coord-1", "cycle": cycle, "venue": venue},
        )

    def _approve(self, pairing_id, sire_id, dam_id, actor=None):
        return self.service.transition(
            actor or self.coordinator,
            pairing_id,
            "approve",
            {"sire_id": sire_id, "dam_id": dam_id, "approvals": ["vet-1"]},
        )

    def test_pairing_requires_cycle_and_venue(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator, "pairing", {"proposed_by": "coord-1"}
            )

    def test_same_cycle_conflict_reports_animal_and_occupant(self):
        first = self._pairing()
        self._approve(first["id"], self.sire["id"], self.dam["id"])
        second = self._pairing()
        with self.assertRaises(ConflictError) as caught:
            self._approve(second["id"], self.sire["id"], self.dam2["id"])
        message = str(caught.exception)
        self.assertIn(self.sire["id"], message)
        self.assertIn(first["id"], message)
        self.assertEqual(caught.exception.details["animal_id"], self.sire["id"])
        self.assertEqual(caught.exception.details["occupied_by"], first["id"])
        self.assertEqual(caught.exception.details["cycle"], "2026-S1")
        # 当前建议仍留在待审批
        self.assertEqual(self.service.get(second["id"])["status"], "proposed")

    def test_dam_conflict_also_detected(self):
        first = self._pairing()
        self._approve(first["id"], self.sire["id"], self.dam["id"])
        sire2 = self._animal("M-2", "male")
        second = self._pairing()
        with self.assertRaises(ConflictError) as caught:
            self._approve(second["id"], sire2["id"], self.dam["id"])
        self.assertEqual(caught.exception.details["animal_id"], self.dam["id"])
        self.assertEqual(caught.exception.details["occupied_by"], first["id"])

    def test_different_cycle_does_not_conflict(self):
        first = self._pairing(cycle="2026-S1")
        self._approve(first["id"], self.sire["id"], self.dam["id"])
        second = self._pairing(cycle="2026-S2")
        approved = self._approve(second["id"], self.sire["id"], self.dam2["id"])
        self.assertEqual(approved["status"], "approved")

    def test_quarantined_parent_blocks_with_clear_error(self):
        self.service.transition(
            self.vet, self.sire["id"], "quarantine_animal", {"reason": "体检"}
        )
        pairing = self._pairing()
        with self.assertRaises(ValidationError) as caught:
            self._approve(pairing["id"], self.sire["id"], self.dam["id"])
        self.assertIn(self.sire["id"], str(caught.exception))
        self.assertEqual(caught.exception.details["animal_id"], self.sire["id"])
        self.assertEqual(caught.exception.details["status"], "quarantined")
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")

    def test_deceased_parent_blocks_with_clear_error(self):
        self.service.transition(
            self.vet, self.dam["id"], "mark_deceased", {"cause": "疾病"}
        )
        pairing = self._pairing()
        with self.assertRaises(ValidationError) as caught:
            self._approve(pairing["id"], self.sire["id"], self.dam["id"])
        self.assertEqual(caught.exception.details["animal_id"], self.dam["id"])
        self.assertEqual(caught.exception.details["status"], "deceased")
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")

    def test_reject_releases_schedule_for_other_suggestions(self):
        first = self._pairing()
        self._approve(first["id"], self.sire["id"], self.dam["id"])
        second = self._pairing()
        with self.assertRaises(ConflictError):
            self._approve(second["id"], self.sire["id"], self.dam2["id"])
        rejected = self.service.transition(
            self.coordinator, first["id"], "reject", {"reason": "调整计划"}
        )
        self.assertEqual(rejected["status"], "rejected")
        approved = self._approve(second["id"], self.sire["id"], self.dam2["id"])
        self.assertEqual(approved["status"], "approved")

    def test_concurrent_approvals_never_double_book_animal(self):
        first = self._pairing()
        second = self._pairing()
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def approve(pairing_id, dam_id, actor):
            barrier.wait()
            try:
                result = self._approve(pairing_id, self.sire["id"], dam_id, actor=actor)
                with lock:
                    outcomes.append(("ok", result["status"]))
            except ConflictError as exc:
                with lock:
                    outcomes.append(("conflict", str(exc)))

        threads = [
            threading.Thread(
                target=approve,
                args=(first["id"], self.dam["id"], Actor("coord-1", "coordinator")),
            ),
            threading.Thread(
                target=approve,
                args=(second["id"], self.dam2["id"], Actor("coord-2", "coordinator")),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        kinds = sorted(kind for kind, _ in outcomes)
        self.assertEqual(kinds, ["conflict", "ok"])
        approved = self.repo.list_entities(kind="pairing", status="approved")
        self.assertEqual(len(approved), 1)
        occupants = [
            pairing["data"]["sire_id"]
            for pairing in approved
            if pairing["data"].get("cycle") == "2026-S1"
        ]
        self.assertEqual(occupants.count(self.sire["id"]), 1)

    def test_cycle_overview_groups_suggestions_and_occupancy(self):
        first = self._pairing(cycle="2026-S1", venue="东区繁育馆")
        self._approve(first["id"], self.sire["id"], self.dam["id"])
        second = self.service.create(
            self.coordinator,
            "pairing",
            {
                "proposed_by": "coord-1",
                "cycle": "2026-S1",
                "venue": "西区繁育馆",
                "sire_id": self.sire["id"],
                "dam_id": self.dam2["id"],
            },
        )
        overview = self.service.cycle_overview()
        self.assertEqual(len(overview["cycles"]), 1)
        bucket = overview["cycles"][0]
        self.assertEqual(bucket["cycle"], "2026-S1")
        self.assertEqual(len(bucket["suggestions"]), 2)
        venues = {item["venue"] for item in bucket["suggestions"]}
        self.assertEqual(venues, {"东区繁育馆", "西区繁育馆"})
        occupied = {item["animal_id"]: item["pairing_id"] for item in bucket["occupancy"]}
        self.assertEqual(
            occupied, {self.sire["id"]: first["id"], self.dam["id"]: first["id"]}
        )
        pending = [
            item for item in bucket["suggestions"] if item["id"] == second["id"]
        ][0]
        self.assertEqual(pending["status"], "proposed")
        self.assertEqual(pending["sire"]["name"], "M-1")
        self.assertEqual(pending["dam"]["name"], "F-2")

    def test_create_with_unknown_parent_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.coordinator,
                "pairing",
                {
                    "proposed_by": "coord-1",
                    "cycle": "2026-S1",
                    "venue": "东区繁育馆",
                    "sire_id": "no-such-animal",
                },
            )


if __name__ == "__main__":
    unittest.main()
