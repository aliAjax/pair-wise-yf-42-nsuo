from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    ConflictError,
    NotFoundError,
    PedigreeVersionConflict,
)
from .rules import (
    INBREEDING_LIMIT,
    OPEN_PAIRING_STATUSES,
    ancestor_versions,
    animal_inbreeding,
    build_animal_index,
    mating_coefficient,
    RuleEngine,
)

BACKFILL_META_KEY = "pedigree_backfill_v1"
SYSTEM_ACTOR = Actor(user_id="system", role="admin")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self._backfill_legacy_coefficients()

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        if field == "*":
            return self.repository.list_entities(kind=kind)
        return self.repository.find_entities(kind, field, value)

    def _animal_index(self):
        return build_animal_index(self.repository.list_entities(kind="animal"))

    def _locked_pairing_ids(self):
        """Pairings protected from recomputation.

        A pairing is locked once completed, or when a transfer tied to either
        partner has already shipped (in transit / arrived).
        """
        locked = set()
        transfers = self.repository.list_entities(kind="transfer")
        shipped = {
            transfer["data"].get("animal_id")
            for transfer in transfers
            if transfer["status"] in ("in_transit", "completed")
        }
        for pairing in self.repository.list_entities(kind="pairing"):
            data = pairing["data"]
            if pairing["status"] == "completed":
                locked.add(pairing["id"])
            elif data.get("sire_id") in shipped or data.get("dam_id") in shipped:
                locked.add(pairing["id"])
        return locked

    def _recompute_animal_coefficients(self):
        """Refresh every individual's coefficient against the current pedigree.

        Coefficients are system-derived, so they are rewritten without bumping
        versions — only the registrar's correction itself moves the version.
        """
        animals = self.repository.list_entities(kind="animal")
        index = build_animal_index(animals)
        for animal in animals:
            coefficient = round(float(animal_inbreeding(animal["id"], index)), 6)
            if animal["data"].get("inbreeding_coefficient") != coefficient:
                data = dict(animal["data"])
                data["inbreeding_coefficient"] = coefficient
                self.repository.rewrite_entity(animal["id"], data)

    def _recompute_pairings(self, reason, actor=None):
        """Re-evaluate open pairing proposals against the live pedigree.

        Pending and approved suggestions get fresh coefficients. Approved
        suggestions pushed over the threshold fall back to pending review.
        Completed / transferred pairings keep their original judgement.
        """
        actor = actor or SYSTEM_ACTOR
        index = self._animal_index()
        locked = self._locked_pairing_ids()
        results = {"recalculated": [], "returned_for_review": [], "kept": []}
        for pairing in self.repository.list_entities(kind="pairing"):
            if pairing["id"] in locked or pairing["status"] not in OPEN_PAIRING_STATUSES:
                results["kept"].append(pairing["id"])
                continue
            data = pairing["data"]
            sire_id, dam_id = data.get("sire_id"), data.get("dam_id")
            if sire_id is None or dam_id is None:
                continue
            coefficient = round(float(mating_coefficient(sire_id, dam_id, index)), 6)
            previous_coefficient = data.get("inbreeding_coefficient")
            over_limit = coefficient > INBREEDING_LIMIT
            previous_status = pairing["status"]
            next_status = "proposed" if over_limit else previous_status
            demoted = previous_status == "approved" and next_status == "proposed"
            if previous_coefficient == coefficient and not demoted:
                results["kept"].append(pairing["id"])
                continue
            data["inbreeding_coefficient"] = coefficient
            if over_limit:
                data["needs_reconfirm"] = True
                data["return_reason"] = "inbreeding coefficient %.6f exceeds %.3f" % (
                    coefficient,
                    INBREEDING_LIMIT,
                )
            elif data.get("return_reason") and not demoted:
                # A later correction brought the risk back below the limit.
                data["needs_reconfirm"] = False
                data.pop("return_reason", None)
            updated = self.repository.update_entity(
                pairing["id"], pairing["version"], next_status, data
            )
            results["recalculated"].append(
                {"id": pairing["id"], "inbreeding_coefficient": coefficient}
            )
            if demoted:
                results["returned_for_review"].append(pairing["id"])
                self.audit.record(
                    pairing["id"],
                    actor,
                    "return_for_review",
                    previous_status,
                    next_status,
                    {"reason": reason, "inbreeding_coefficient": coefficient},
                )
        return results

    def _backfill_legacy_coefficients(self):
        """One-time upgrade: fill coefficients missing from old records."""
        if self.repository.get_meta(BACKFILL_META_KEY):
            return
        index = self._animal_index()
        for animal in self.repository.list_entities(kind="animal"):
            if "inbreeding_coefficient" in animal["data"]:
                continue
            data = dict(animal["data"])
            data["inbreeding_coefficient"] = round(
                float(animal_inbreeding(animal["id"], index)), 6
            )
            self.repository.rewrite_entity(animal["id"], data)
        for pairing in self.repository.list_entities(kind="pairing"):
            data = pairing["data"]
            if "inbreeding_coefficient" not in data:
                sire_id, dam_id = data.get("sire_id"), data.get("dam_id")
                if sire_id is not None and dam_id is not None:
                    data["inbreeding_coefficient"] = round(
                        float(mating_coefficient(sire_id, dam_id, index)), 6
                    )
            # Older proposals carry no snapshot; baseline it against the
            # pedigree that exists now so the first approval is not rejected
            # merely for having lived through the upgrade.
            if not data.get("pedigree_snapshot"):
                sire_id, dam_id = data.get("sire_id"), data.get("dam_id")
                if sire_id is not None and dam_id is not None:
                    data["pedigree_snapshot"] = [
                        ancestor_versions(sire_id, index),
                        ancestor_versions(dam_id, index),
                    ]
            # Backfill never reopens a decision that was already made.
            self.repository.rewrite_entity(pairing["id"], data)
        self.repository.set_meta(BACKFILL_META_KEY, "done")

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
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
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
        try:
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), self._lookup
            )
        except PedigreeVersionConflict as exc:
            # Approval was filed against an outdated pedigree: the pairing
            # stays pending and must be reconfirmed against the current one.
            data = dict(entity["data"])
            data["needs_reconfirm"] = True
            data["version_conflicts"] = exc.mismatches
            self.repository.rewrite_entity(entity_id, data)
            self.audit.record(
                entity_id,
                actor,
                "approve_rejected_stale",
                entity["status"],
                entity["status"],
                {"mismatches": exc.mismatches},
            )
            raise
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
        if entity["kind"] == "animal" and action == "correct_pedigree":
            # Parents or grandparents changed: recompute along the ancestor
            # graph and refresh every open pairing suggestion.
            self._recompute_animal_coefficients()
            updated = self._recompute_after_correction(entity_id, actor)
        return updated

    def _recompute_after_correction(self, corrected_id, actor):
        self._recompute_pairings(
            reason="pedigree corrected for %s" % corrected_id, actor=actor
        )
        return self.repository.get_entity(corrected_id)

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
