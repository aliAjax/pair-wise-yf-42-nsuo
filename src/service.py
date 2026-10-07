from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError
from .rules import INBREEDING_THRESHOLD, RuleEngine, inbreeding_coefficient


SYSTEM_ACTOR = Actor("system", "admin")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if action == "correct_pedigree":
            # 血统更正后，沿最新血统重算待审和已批准的配对建议
            self._recalculate_pairings(actor)
        return updated

    def _is_shipped(self, animal_id):
        """该动物是否已有在途或已完成的运输（已运输的保留原判定）。"""
        if not animal_id:
            return False
        for transfer in self.repository.list_entities(kind="transfer"):
            if transfer["status"] in ("in_transit", "completed") and \
                    transfer["data"].get("animal_id") == animal_id:
                return True
        return False

    def _recalculate_pairings(self, actor=None):
        """重算待审/已批准配对建议的近交系数。

        超阈值的已批准建议退回待审；已完成或已运输的保留原判定。
        返回被更新的配对实体列表。
        """
        pairings = self.repository.list_entities(kind="pairing")
        updated = []
        for pairing in pairings:
            if pairing["status"] not in ("proposed", "approved"):
                continue
            data = dict(pairing["data"])
            sire_id = data.get("sire_id")
            dam_id = data.get("dam_id")
            if not sire_id or not dam_id:
                continue
            # 已运输的动物保留原判定，不重算
            if self._is_shipped(sire_id) or self._is_shipped(dam_id):
                continue
            sire = self.repository.find_entities("animal", "id", sire_id)
            dam = self.repository.find_entities("animal", "id", dam_id)
            if not sire or not dam:
                continue
            coeff = inbreeding_coefficient(sire[0], dam[0], self._lookup)
            data["inbreeding_coefficient"] = coeff
            new_status = pairing["status"]
            if coeff > INBREEDING_THRESHOLD and pairing["status"] == "approved":
                new_status = "proposed"  # 超阈值，退回待审
            if new_status != pairing["status"] or data != pairing["data"]:
                updated_entity = self.repository.update_entity(
                    pairing["id"], pairing["version"], new_status, data
                )
                self.audit.record(
                    pairing["id"],
                    actor or SYSTEM_ACTOR,
                    "recalculate",
                    pairing["status"],
                    new_status,
                    {"inbreeding_coefficient": coeff},
                )
                updated.append(updated_entity)
        return updated

    def backfill_coefficients(self):
        """旧数据升级后，按现存血统补齐缺失的近交系数。"""
        pairings = self.repository.list_entities(kind="pairing")
        updated = []
        for pairing in pairings:
            data = dict(pairing["data"])
            if "inbreeding_coefficient" in data:
                continue
            sire_id = data.get("sire_id")
            dam_id = data.get("dam_id")
            if not sire_id or not dam_id:
                continue
            sire = self.repository.find_entities("animal", "id", sire_id)
            dam = self.repository.find_entities("animal", "id", dam_id)
            if not sire or not dam:
                continue
            coeff = inbreeding_coefficient(sire[0], dam[0], self._lookup)
            data["inbreeding_coefficient"] = coeff
            updated_entity = self.repository.update_entity(
                pairing["id"], pairing["version"], pairing["status"], data
            )
            updated.append(updated_entity)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
