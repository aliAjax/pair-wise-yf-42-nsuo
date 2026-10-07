import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, inbreeding_coefficient
from src.service import DomainService


class PedigreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.registrar = Actor("registrar", "registrar")
        self.viewer = Actor("viewer", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _animal(self, name, sex, sire_id=None, dam_id=None):
        data = {"name": name, "sex": sex}
        if sire_id:
            data["sire_id"] = sire_id
        if dam_id:
            data["dam_id"] = dam_id
        return self.service.create(self.admin, "animal", data)

    def _pairing(self, sire_id, dam_id, approve=False):
        p = self.service.create(
            self.admin, "pairing",
            {"proposed_by": "coord", "sire_id": sire_id, "dam_id": dam_id},
        )
        if approve:
            p = self.service.transition(
                self.admin, p["id"], "approve",
                {"sire_id": sire_id, "dam_id": dam_id, "approvals": ["vet"]},
            )
        return p

    def test_multigeneration_coefficients(self):
        animals = {}

        def add(aid, sex, sire_id=None, dam_id=None):
                animals[aid] = {"id": aid, "sex": sex, "sire_id": sire_id, "dam_id": dam_id}

        def lookup(kind, field, value):
            a = animals.get(value)
            return [{"id": a["id"], "kind": "animal", "status": "active", "data": a}] if a else []

        add("F0", "male"); add("M0", "female")
        add("F1", "male", "F0", "M0"); add("M1", "female", "F0", "M0")
        add("F2", "male", "F0", "M0")
        add("X0", "female"); add("Y0", "female")
        add("F3", "male", "F0", "X0"); add("M3", "female", "F0", "Y0")
        add("Z0", "female"); add("W0", "male")
        add("F4", "male", "F1", "Z0"); add("M4", "female", "W0", "M1")
        add("U1", "male"); add("U2", "female")
        add("M5", "female", "U1", "M1")  # F2 is M5's uncle

        cases = [
            ("selfing", "F0", "F0", 0.5),
            ("parent-offspring", "F0", "M1", 0.25),
            ("full siblings", "F1", "M1", 0.25),
            ("half siblings", "F3", "M3", 0.125),
            ("uncle-niece", "F2", "M5", 0.125),
            ("first cousins", "F4", "M4", 0.0625),
            ("unrelated", "U1", "U2", 0.0),
        ]
        for name, s, d, expected in cases:
            got = inbreeding_coefficient(animals[s], animals[d], lookup)
            self.assertAlmostEqual(got, expected, places=9, msg=name)

    def test_correct_pedigree_recalculates_approved_to_proposed(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"], approve=True)
        self.assertEqual(pairing["status"], "approved")
        self.assertEqual(pairing["data"]["inbreeding_coefficient"], 0.0)

        # 把 D 的父亲改成 S -> D 是 S 的女儿，配对变为父女（0.25 > 阈值）
        self.service.transition(
            self.admin, dam["id"], "correct_pedigree",
            {"sire_id": sire["id"]}, expected_version=dam["version"],
        )

        updated = self.service.get(pairing["id"])
        self.assertEqual(updated["status"], "proposed")  # 已批准退回待审
        self.assertAlmostEqual(updated["data"]["inbreeding_coefficient"], 0.25)

    def test_correct_pedigree_recalculates_proposed_keeps_proposed(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self.assertEqual(pairing["status"], "proposed")

        self.service.transition(
            self.admin, dam["id"], "correct_pedigree",
            {"sire_id": sire["id"]}, expected_version=dam["version"],
        )

        updated = self.service.get(pairing["id"])
        self.assertEqual(updated["status"], "proposed")
        self.assertAlmostEqual(updated["data"]["inbreeding_coefficient"], 0.25)

    def test_completed_pairing_keeps_original_judgment(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"], approve=True)
        self.service.transition(
            self.admin, pairing["id"], "complete",
            {"offspring_ids": ["offspring-1"]}, expected_version=pairing["version"],
        )

        self.service.transition(
            self.admin, dam["id"], "correct_pedigree",
            {"sire_id": sire["id"]}, expected_version=dam["version"],
        )

        updated = self.service.get(pairing["id"])
        self.assertEqual(updated["status"], "completed")  # 已完成保留原判定
        self.assertEqual(updated["data"]["inbreeding_coefficient"], 0.0)

    def test_shipped_pairing_keeps_original_judgment(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"], approve=True)
        # 为 dam 创建运输并到达（已运输）
        transfer = self.service.create(
            self.admin, "transfer",
            {"animal_id": dam["id"], "from_institution": "Zoo-A", "to_institution": "Zoo-B"},
        )
        self.service.transition(self.admin, transfer["id"], "authorize",
                                {"permit_id": "P-1"}, expected_version=transfer["version"])
        self.service.transition(self.admin, transfer["id"], "ship",
                                {"transport_id": "T-1"},
                                expected_version=self.service.get(transfer["id"])["version"])
        self.service.transition(self.admin, transfer["id"], "arrive",
                                {"arrival_date": "2026-05-01"},
                                expected_version=self.service.get(transfer["id"])["version"])

        self.service.transition(
            self.admin, dam["id"], "correct_pedigree",
            {"sire_id": sire["id"]}, expected_version=dam["version"],
        )

        updated = self.service.get(pairing["id"])
        self.assertEqual(updated["status"], "approved")  # 已运输保留原判定
        self.assertEqual(updated["data"]["inbreeding_coefficient"], 0.0)

    def test_approval_mismatch_rereconfirms(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        other = self._animal("S2", "male")

        # 审批时提交的 sire 与配对记录不一致 -> 退回重新确认
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, pairing["id"], "approve",
                {"sire_id": other["id"], "dam_id": dam["id"], "approvals": ["vet"]},
                expected_version=pairing["version"],
            )
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")

    def test_approval_snapshot_mismatch_rereconfirms(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self.assertEqual(pairing["data"]["inbreeding_coefficient"], 0.0)

        # 绕过服务直接改动血统（模拟未重算的血统变更），再审批
        founder = self._animal("F", "male")
        self.repo.update_entity(dam["id"], dam["version"], "active",
                                {**dam["data"], "sire_id": founder["id"]})
        self.repo.update_entity(sire["id"], sire["version"], "active",
                                {**sire["data"], "sire_id": founder["id"]})

        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, pairing["id"], "approve",
                {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet"]},
                expected_version=pairing["version"],
            )
        self.assertEqual(self.service.get(pairing["id"])["status"], "proposed")

    def test_approval_over_threshold_rejected(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"])

        # 通过服务更正 D 的血统（使其成为 S 的女儿），触发重算
        self.service.transition(
            self.admin, dam["id"], "correct_pedigree",
            {"sire_id": sire["id"]}, expected_version=dam["version"],
        )
        # 重算后配对为待审、系数 0.25 > 阈值，批准应被拒
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, pairing["id"], "approve",
                {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet"]},
                expected_version=self.service.get(pairing["id"])["version"],
            )

    def test_version_conflict_on_concurrent_correction(self):
        animal = self._animal("A", "female")
        # 第一人正常提交（版本 1 -> 2）
        self.service.transition(
            self.admin, animal["id"], "correct_pedigree",
            {"dam_id": self._animal("M1", "female")["id"]},
            expected_version=animal["version"],
        )
        # 第二人用旧版本提交 -> 冲突
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, animal["id"], "correct_pedigree",
                {"dam_id": self._animal("M2", "female")["id"]},
                expected_version=animal["version"],
            )
        # 第二人按最新版本重取后提交成功
        latest = self.service.get(animal["id"])
        self.assertEqual(latest["version"], 2)
        self.service.transition(
            self.admin, animal["id"], "correct_pedigree",
            {"dam_id": self._animal("M2", "female")["id"]},
            expected_version=latest["version"],
        )
        self.assertEqual(self.service.get(animal["id"])["version"], 3)

    def test_correct_pedigree_permission(self):
        animal = self._animal("A", "female")
        sire = self._animal("S", "male")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.viewer, animal["id"], "correct_pedigree",
                {"sire_id": sire["id"]},
                expected_version=animal["version"],
            )
        # 录入员可以更正
        self.service.transition(
            self.registrar, animal["id"], "correct_pedigree",
            {"sire_id": sire["id"]},
            expected_version=animal["version"],
        )
        self.assertEqual(self.service.get(animal["id"])["data"]["sire_id"], sire["id"])

    def test_correct_pedigree_validates(self):
        animal = self._animal("A", "female")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, animal["id"], "correct_pedigree",
                {"sire_id": "nonexistent"}, expected_version=animal["version"],
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, animal["id"], "correct_pedigree",
                {"sire_id": animal["id"]}, expected_version=animal["version"],
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, animal["id"], "correct_pedigree",
                {"sire_id": self._animal("F", "female")["id"]},
                expected_version=animal["version"],
            )

    def test_backfill_legacy_coefficients(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        # 直接写入一条没有系数的旧配对数据
        self.repo.create_entity(
            "legacy-pairing", "pairing", "proposed",
            {"proposed_by": "coord", "sire_id": sire["id"], "dam_id": dam["id"]},
            "admin",
        )
        self.assertNotIn("inbreeding_coefficient",
                         self.repo.get_entity("legacy-pairing")["data"])

        self.service.backfill_coefficients()

        filled = self.repo.get_entity("legacy-pairing")
        self.assertIn("inbreeding_coefficient", filled["data"])
        self.assertEqual(filled["data"]["inbreeding_coefficient"], 0.0)

    def test_backfill_skips_existing_coefficients(self):
        sire = self._animal("S", "male")
        dam = self._animal("D", "female")
        pairing = self._pairing(sire["id"], dam["id"])
        self.assertEqual(pairing["data"]["inbreeding_coefficient"], 0.0)
        # backfill 不应覆盖已有系数
        self.service.backfill_coefficients()
        self.assertEqual(
            self.service.get(pairing["id"])["data"]["inbreeding_coefficient"], 0.0,
        )


if __name__ == "__main__":
    unittest.main()
