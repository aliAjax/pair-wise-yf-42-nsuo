import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PedigreeVersionConflict, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, mating_coefficient, build_animal_index
from src.service import DomainService


class PedigreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.registrar = Actor("reg-1", "registrar")
        self.coordinator = Actor("coord-1", "coordinator")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex, sire_id=None, dam_id=None, actor=None):
        data = {"name": name, "sex": sex}
        if sire_id is not None:
            data["sire_id"] = sire_id
        if dam_id is not None:
            data["dam_id"] = dam_id
        return self.service.create(actor or self.admin, "animal", data)

    def _pairing(self, sire_id, dam_id, actor=None):
        return self.service.create(
            actor or self.coordinator,
            "pairing",
            {"proposed_by": "coord-1", "sire_id": sire_id, "dam_id": dam_id},
        )

    def _approve(self, pairing, actor=None, sire_id=None, dam_id=None, expected=None):
        return self.service.transition(
            actor or self.coordinator,
            pairing["id"],
            "approve",
            {
                "sire_id": sire_id if sire_id is not None else pairing["data"]["sire_id"],
                "dam_id": dam_id if dam_id is not None else pairing["data"]["dam_id"],
                "approvals": ["vet-1"],
            },
            expected_version=expected,
        )

    # --- 多代系数 -------------------------------------------------------

    def test_multi_generation_coefficients(self):
        # half siblings share one parent -> 0.125 (allowed at the limit)
        f = self._animal("F", "male")
        m1 = self._animal("M1", "female")
        m2 = self._animal("M2", "female")
        h1 = self._animal("H1", "male", sire_id=f["id"], dam_id=m1["id"])
        h2 = self._animal("H2", "female", sire_id=f["id"], dam_id=m2["id"])
        index = build_animal_index(self.repo.list_entities(kind="animal"))
        self.assertEqual(
            mating_coefficient(h1["id"], h2["id"], index), 0.125
        )
        self.assertEqual(h1["data"]["inbreeding_coefficient"], 0.0)

        # father-daughter mating via grandparent correction -> 0.25
        g = self._animal("G", "female")
        pairing = self._pairing(f["id"], g["id"])
        self._approve(pairing)
        # correct g's dam to m1 makes g a daughter of f
        corrected = self.service.transition(
            self.registrar,
            g["id"],
            "correct_pedigree",
            {"sire_id": f["id"], "dam_id": m1["id"]},
        )
        self.assertEqual(corrected["version"], g["version"] + 1)
        pairing_after = self.service.get(pairing["id"])
        self.assertEqual(pairing_after["status"], "proposed")
        self.assertGreater(pairing_after["data"]["inbreeding_coefficient"], 0.125)
        self.assertTrue(pairing_after["data"]["needs_reconfirm"])

    # --- 已批准超阈值退回，阈值内保留 -----------------------------------

    def test_approved_pairing_returned_over_limit_kept_within_limit(self):
        f = self._animal("F", "male")
        m1 = self._animal("M1", "female")
        m2 = self._animal("M2", "female")
        unrelated = self._animal("X", "male")
        h1 = self._animal("H1", "male", sire_id=f["id"], dam_id=m1["id"])
        h2 = self._animal("H2", "female", sire_id=f["id"], dam_id=m2["id"])

        half_sib_pairing = self._pairing(h1["id"], h2["id"])  # 0.125
        self._approve(half_sib_pairing)
        unrelated_pairing = self._pairing(unrelated["id"], h2["id"])  # 0
        self._approve(unrelated_pairing)

        # change h2's dam to m1: h1/h2 become full siblings (0.25)
        self.service.transition(
            self.registrar,
            h2["id"],
            "correct_pedigree",
            {"sire_id": f["id"], "dam_id": m1["id"]},
        )
        self.assertEqual(self.service.get(half_sib_pairing["id"])["status"], "proposed")
        # unrelated sire X stays unrelated to h2 -> approved judgement kept
        kept = self.service.get(unrelated_pairing["id"])
        self.assertEqual(kept["status"], "approved")

    # --- 已完成保留原判定 -----------------------------------------------

    def test_completed_pairing_keeps_original_judgement(self):
        s = self._animal("S", "male")
        d = self._animal("D", "female")
        x = self._animal("X", "female")
        pairing = self._pairing(s["id"], d["id"])
        self._approve(pairing)
        self.service.transition(
            self.coordinator,
            pairing["id"],
            "complete",
            {"offspring_ids": ["baby-1"]},
        )
        # making d a daughter of s afterwards must not reopen a finished pairing
        self.service.transition(
            self.registrar, d["id"], "correct_pedigree",
            {"sire_id": s["id"], "dam_id": x["id"]},
        )
        finished = self.service.get(pairing["id"])
        self.assertEqual(finished["status"], "completed")

    # --- 已运输保留 -----------------------------------------------------

    def test_shipped_transfer_locks_pairing(self):
        s = self._animal("S", "male")
        d = self._animal("D", "female")
        x = self._animal("X", "female")
        pairing = self._pairing(s["id"], d["id"])
        self._approve(pairing)
        transfer = self.service.create(
            self.registrar,
            "transfer",
            {
                "animal_id": s["id"],
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
            },
        )
        self.service.transition(
            self.registrar, transfer["id"], "authorize", {"permit_id": "P-1"}
        )
        self.service.transition(
            self.registrar, transfer["id"], "ship", {"transport_id": "T-1"}
        )
        # dam becomes sire's daughter after shipping: pairing must stay approved
        self.service.transition(
            self.registrar, d["id"], "correct_pedigree",
            {"sire_id": s["id"], "dam_id": x["id"]},
        )
        self.assertEqual(self.service.get(pairing["id"])["status"], "approved")

    # --- 审批按提交时版本再核 -------------------------------------------

    def test_approval_checked_against_submitted_pedigree_version(self):
        s = self._animal("S", "male")
        d = self._animal("D", "female")
        x = self._animal("X", "female")
        pairing = self._pairing(s["id"], d["id"])  # snapshot captured while unrelated
        # a pedigree change stays below the threshold (d now has a dam x,
        # unrelated to s) but moves d's recorded version past the snapshot
        corrected = self.service.transition(
            self.registrar, d["id"], "correct_pedigree",
            {"dam_id": x["id"]},
        )
        self.assertLessEqual(
            corrected["data"]["inbreeding_coefficient"], 0.125
        )
        with self.assertRaises(PedigreeVersionConflict):
            self._approve(pairing)
        flagged = self.service.get(pairing["id"])
        self.assertTrue(flagged["data"]["needs_reconfirm"])
        self.assertEqual(flagged["status"], "proposed")
        self.assertIn(d["id"], flagged["data"]["version_conflicts"])

        # reconfirm against the current pedigree refreshes the snapshot;
        # approval against the new version then succeeds
        self.service.transition(
            self.coordinator, pairing["id"], "reconfirm",
            {"sire_id": s["id"], "dam_id": d["id"]},
        )
        approved = self._approve(pairing)
        self.assertEqual(approved["status"], "approved")

    # --- 并发：后到校正基于最新版本 -------------------------------------

    def test_concurrent_corrections_later_one_recomputes_on_latest(self):
        s = self._animal("S", "male")
        d = self._animal("D", "female")
        m = self._animal("M", "female")
        first = self.service.transition(
            self.registrar, d["id"], "correct_pedigree", {"dam_id": m["id"]}
        )
        # second correction was prepared against the old version
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.registrar,
                d["id"],
                "correct_pedigree",
                {"dam_id": None},
                expected_version=1,
            )
        # resubmitted against the latest version, recomputed fresh
        second = self.service.transition(
            self.registrar,
            d["id"],
            "correct_pedigree",
            {"dam_id": None},
            expected_version=first["version"],
        )
        self.assertEqual(second["version"], first["version"] + 1)
        self.assertIsNone(second["data"]["dam_id"])

    # --- 旧数据升级补齐 -------------------------------------------------

    def test_legacy_records_get_coefficients_backfilled(self):
        s = self._animal("S", "male")
        d = self._animal("D", "female")
        pairing = self._pairing(s["id"], d["id"])
        # simulate pre-upgrade records: strip coefficients and snapshot
        for entity_id in (s["id"], d["id"], pairing["id"]):
            entity = self.repo.get_entity(entity_id)
            data = dict(entity["data"])
            data.pop("inbreeding_coefficient", None)
            data.pop("pedigree_snapshot", None)
            self.repo.rewrite_entity(entity_id, data)
        # forget that the upgrade ever ran, then reopen the database service
        from src.service import BACKFILL_META_KEY
        self.repo.delete_meta(BACKFILL_META_KEY)
        service2 = DomainService(self.repo, RuleEngine())
        self.assertEqual(service2.get(s["id"])["data"]["inbreeding_coefficient"], 0.0)
        backfilled_pairing = service2.get(pairing["id"])
        self.assertIn("inbreeding_coefficient", backfilled_pairing["data"])
        self.assertTrue(backfilled_pairing["data"]["pedigree_snapshot"])
        # backfill is idempotent and never rewrites statuses/versions
        self.assertEqual(backfilled_pairing["status"], "proposed")
        DomainService(self.repo, RuleEngine())
        self.assertEqual(
            self.repo.get_entity(pairing["id"])["version"], pairing["version"]
        )

    # --- 校正校验 -------------------------------------------------------

    def test_correction_rejects_unknown_parent_cycle_and_wrong_sex(self):
        s = self._animal("S", "male")
        d = self._animal("D", "female")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, d["id"], "correct_pedigree",
                {"sire_id": "does-not-exist"},
            )
        # female animal cannot be a sire
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, d["id"], "correct_pedigree",
                {"sire_id": d["id"]},
            )
        # cycle: making S a descendant of his own descendant
        child = self._animal("C", "male", sire_id=s["id"], dam_id=d["id"])
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(
                self.registrar, s["id"], "correct_pedigree",
                {"sire_id": child["id"]},
            )
        self.assertIn("cycle", str(ctx.exception))
        # a viewer cannot correct pedigrees
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("v", "viewer"), s["id"], "correct_pedigree",
                {"dam_id": None},
            )

    def test_grandparent_correction_cascades_to_descendant_pairing(self):
        # f, m1, m2 founders; c = f x m1; g = c x m2 (grandchild)
        f = self._animal("F", "male")
        m1 = self._animal("M1", "female")
        m2 = self._animal("M2", "female")
        c = self._animal("C", "male", sire_id=f["id"], dam_id=m1["id"])
        g = self._animal("G", "female", sire_id=c["id"], dam_id=m2["id"])
        # pairing f x g is grandparent-grandchild = 0.125 (within the limit)
        pairing = self._pairing(f["id"], g["id"])
        self.assertEqual(pairing["data"]["inbreeding_coefficient"], 0.125)
        self._approve(pairing)
        # correct the *grandparent* generation: m2 becomes a child of f,
        # so g's dam is also f's descendant -> coefficient rises past 0.125
        self.service.transition(
            self.registrar, m2["id"], "correct_pedigree",
            {"sire_id": f["id"], "dam_id": m1["id"]},
        )
        pushed = self.service.get(pairing["id"])
        self.assertEqual(pushed["status"], "proposed")
        self.assertGreater(pushed["data"]["inbreeding_coefficient"], 0.125)
        self.assertTrue(pushed["data"]["needs_reconfirm"])

    def test_risky_proposal_cannot_be_approved_until_reconfirmed_safe(self):
        s = self._animal("S", "male")
        d = self._animal("D", "female")
        x = self._animal("X", "female")
        # d becomes s's daughter -> mating s x d is father-daughter (0.25)
        self.service.transition(
            self.registrar, d["id"], "correct_pedigree",
            {"sire_id": s["id"], "dam_id": x["id"]},
        )
        # a proposal may still be entered
        pairing = self._pairing(s["id"], d["id"])
        self.assertGreater(pairing["data"]["inbreeding_coefficient"], 0.125)
        with self.assertRaises(ValidationError):
            self._approve(pairing)


if __name__ == "__main__":
    unittest.main()
