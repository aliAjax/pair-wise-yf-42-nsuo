from datetime import datetime, timedelta  # noqa: F401  (kept for date helpers)

from .domain import (
    ConflictError,
    InvalidTransition,
    PedigreeVersionConflict,
    PermissionDenied,
    ValidationError,
)

# A pairing whose inbreeding coefficient is strictly above this threshold
# cannot be approved; an already approved pairing is pushed back to review.
INBREEDING_LIMIT = 0.125

# Pairing statuses whose decision may still be revisited after a pedigree fix.
OPEN_PAIRING_STATUSES = ("proposed", "approved")
# Statuses that are never touched by cascade recomputation.
TERMINAL_PAIRING_STATUSES = ("completed", "rejected")


def _round(value):
    return round(float(value), 6)


def build_animal_index(animals):
    """Map animal id -> entity/record for pedigree traversal.

    Accepts either entity rows (with a ``data`` mapping) or plain data dicts
    that already carry an ``id`` field.
    """
    index = {}
    for animal in animals or []:
        data = animal.get("data", animal)
        animal_id = animal.get("id") or data.get("id")
        if animal_id is not None:
            index[animal_id] = animal
    return index


def _record(index, animal_id):
    animal = index.get(animal_id)
    if animal is None:
        return {}
    return animal.get("data", animal)


def _version(index, animal_id):
    animal = index.get(animal_id)
    return int(animal["version"]) if animal and "version" in animal else 1


def _parent_ids(data):
    return data.get("sire_id"), data.get("dam_id")


def _depth(animal_id, index, memo, visiting):
    if animal_id is None or animal_id not in index:
        return 0
    if animal_id in memo:
        return memo[animal_id]
    if animal_id in visiting:  # cycle guard: treat malformed depth as shallow
        return 0
    visiting.add(animal_id)
    sire_id, dam_id = _parent_ids(_record(index, animal_id))
    depth = 1 + max(
        _depth(sire_id, index, memo, visiting),
        _depth(dam_id, index, memo, visiting),
    )
    visiting.discard(animal_id)
    memo[animal_id] = depth
    return depth


def coancestry(first_id, second_id, index, memo=None, visiting=None):
    """Kinship coefficient f(x, y) via Wright's tabular recursion.

    Runs across the full multi-generation ancestor graph. Missing ancestors
    are treated as unrelated founders (coancestry 0).
    """
    if first_id is None or second_id is None:
        return 0.0
    if first_id not in index or second_id not in index:
        return 0.0
    memo = {} if memo is None else memo
    visiting = set() if visiting is None else visiting
    key = (first_id, second_id) if first_id <= second_id else (second_id, first_id)
    if key in memo:
        return memo[key]
    if key in visiting:  # defensive: malformed cyclic legacy pedigree
        return 0.0
    if first_id == second_id:
        # f(x, x) = 1/2 * (1 + F_x)
        sire_id, dam_id = _parent_ids(_record(index, first_id))
        f_x = (
            coancestry(sire_id, dam_id, index, memo, visiting)
            if sire_id is not None and dam_id is not None
            else 0.0
        )
        result = 0.5 * (1.0 + f_x)
        memo[key] = result
        return result

    depths = {
        first_id: _depth(first_id, index, {}, set()),
        second_id: _depth(second_id, index, {}, set()),
    }
    # Expand the younger (deeper in the pedigree) animal; ties are broken
    # deterministically so the recursion is stable regardless of argument order.
    if depths[first_id] > depths[second_id]:
        target, other = first_id, second_id
    elif depths[second_id] > depths[first_id]:
        target, other = second_id, first_id
    else:
        target, other = (first_id, second_id) if first_id <= second_id else (second_id, first_id)

    visiting.add(key)
    sire_id, dam_id = _parent_ids(_record(index, target))
    if sire_id is None and dam_id is None:
        # Two distinct founders are unrelated.
        result = 0.0
    else:
        result = 0.5 * (
            coancestry(sire_id, other, index, memo, visiting)
            if sire_id is not None
            else 0.0
        ) + 0.5 * (
            coancestry(dam_id, other, index, memo, visiting)
            if dam_id is not None
            else 0.0
        )
    visiting.discard(key)
    memo[key] = result
    return result


def mating_coefficient(sire_id, dam_id, index):
    """Inbreeding coefficient of an offspring of sire x dam = f(sire, dam)."""
    if sire_id is None or dam_id is None:
        # Conservative reading for callers that do not supply a pedigree index.
        return 1.0 if index is None else 0.0
    if sire_id == dam_id:
        return 0.5
    return coancestry(sire_id, dam_id, index)


def animal_inbreeding(animal_id, index):
    """Inbreeding coefficient of an individual = f(sire, dam)."""
    sire_id, dam_id = _parent_ids(_record(index, animal_id))
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    return coancestry(sire_id, dam_id, index)


def ancestor_versions(animal_id, index):
    """Version stamp of an animal together with its full ancestor chain.

    The returned mapping (ancestor id -> version) includes the animal itself
    and is used as a pedigree-version snapshot on pairing submissions.
    """
    versions = {}
    stack = [animal_id]
    seen = set()
    while stack:
        current = stack.pop()
        if current is None or current in seen:
            continue
        seen.add(current)
        if current in index:
            versions[current] = _version(index, current)
            sire_id, dam_id = _parent_ids(_record(index, current))
            stack.extend((sire_id, dam_id))
    return versions


def pedigree_has_cycle(sire_id, dam_id, index, self_id=None):
    """Return True if making self_id a child of the given parents closes a loop.

    A loop appears when self_id is already an ancestor of either new parent
    (i.e. a parent is a descendant of self_id), or when the same animal is
    used as both parents.
    """
    def is_ancestor(ancestor_id, descendant_id):
        """True when ancestor_id occurs anywhere above descendant_id."""
        seen = set()
        stack = [descendant_id]
        while stack:
            node = stack.pop()
            if node is None or node in seen:
                continue
            node_sire, node_dam = _parent_ids(_record(index, node))
            for parent in (node_sire, node_dam):
                if parent == ancestor_id:
                    return True
                seen.add(parent) if parent else None
                stack.append(parent)
        return False

    if self_id is None:
        return False
    for parent_id in (sire_id, dam_id):
        if parent_id == self_id:
            return True
        if parent_id is not None and is_ancestor(self_id, parent_id):
            return True
    return sire_id is not None and sire_id == dam_id


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")
    sire_id, dam_id = _parent_ids(data)
    sire = _find_one(lookup, "animal", "id", sire_id) if sire_id is not None else None
    dam = _find_one(lookup, "animal", "id", dam_id) if dam_id is not None else None
    if sire_id is not None and not sire:
        raise ValidationError("sire_id does not reference an existing animal")
    if dam_id is not None and not dam:
        raise ValidationError("dam_id does not reference an existing animal")
    if sire and sire["data"].get("sex") == "female":
        raise ValidationError("sire animal is recorded as female")
    if dam and dam["data"].get("sex") == "male":
        raise ValidationError("dam animal is recorded as male")
    if sire_id is None or dam_id is None:
        return {"inbreeding_coefficient": 0.0}
    index = build_animal_index(lookup("animal", "*", None) or [])
    return {"inbreeding_coefficient": _round(mating_coefficient(sire_id, dam_id, index))}


def inbreeding_coefficient(sire, dam):
    """Backward compatible shorthand used by the original rule suite.

    Only the immediate generation is visible without a pedigree index.
    """
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    index = build_animal_index([sire, dam])
    return mating_coefficient(sire_id, dam_id, index)


def _animal_index_from_lookup(lookup):
    return build_animal_index(lookup("animal", "*", None) or [])


def _resolve_parents(sire_id, dam_id, lookup):
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    return sire, dam


def _stale_ancestors(snapshots, index):
    mismatches = {}
    for snapshot in snapshots or []:
        for ancestor_id, snapshot_version in (snapshot or {}).items():
            current_version = _version(index, ancestor_id)
            if ancestor_id in index and current_version != int(snapshot_version):
                mismatches[ancestor_id] = {
                    "submitted_version": int(snapshot_version),
                    "current_version": current_version,
                }
    return mismatches


def _validate_pairing_create(actor, data, lookup):
    sire_id, dam_id = data.get("sire_id"), data.get("dam_id")
    if (sire_id is None) != (dam_id is None):
        raise ValidationError("pairing requires both sire_id and dam_id")
    if sire_id is None:
        return {}
    sire, dam = _resolve_parents(sire_id, dam_id, lookup)
    index = _animal_index_from_lookup(lookup)
    coefficient = mating_coefficient(sire_id, dam_id, index)
    # A risky proposal may be entered; it simply cannot be approved yet.
    return {
        "inbreeding_coefficient": _round(coefficient),
        "pedigree_snapshot": [
            ancestor_versions(sire_id, index),
            ancestor_versions(dam_id, index),
        ],
        "needs_reconfirm": False,
    }


def _approve_payload(entity, sire_id, dam_id, index, lookup, refresh_snapshot):
    if sire_id is None or dam_id is None:
        raise ValidationError("pairing requires both sire_id and dam_id")
    sire, dam = _resolve_parents(sire_id, dam_id, lookup)
    coefficient = mating_coefficient(sire_id, dam_id, index)
    if coefficient > INBREEDING_LIMIT:
        raise ValidationError("pairing exceeds inbreeding threshold")
    snapshot = [
        ancestor_versions(sire_id, index),
        ancestor_versions(dam_id, index),
    ]
    if not refresh_snapshot:
        stored = entity["data"].get("pedigree_snapshot") or []
        mismatches = _stale_ancestors(stored, index)
        if mismatches:
            raise PedigreeVersionConflict(
                "pedigree changed since submission; pairing must be reconfirmed",
                mismatches,
            )
    return {
        "inbreeding_coefficient": _round(coefficient),
        "pedigree_snapshot": snapshot,
        "needs_reconfirm": False,
    }


def _validate_pairing(actor, entity, data, lookup):
    sire_id = data.get("sire_id", entity["data"].get("sire_id"))
    dam_id = data.get("dam_id", entity["data"].get("dam_id"))
    index = _animal_index_from_lookup(lookup)
    payload = _approve_payload(entity, sire_id, dam_id, index, lookup, False)
    payload["approved_by"] = actor.user_id
    return payload


def _validate_reconfirm(actor, entity, data, lookup):
    sire_id = data.get("sire_id", entity["data"].get("sire_id"))
    dam_id = data.get("dam_id", entity["data"].get("dam_id"))
    index = _animal_index_from_lookup(lookup)
    payload = _approve_payload(entity, sire_id, dam_id, index, lookup, True)
    payload["reconfirmed_by"] = actor.user_id
    return payload


def _validate_correct_pedigree(actor, entity, data, lookup):
    if "sire_id" not in data and "dam_id" not in data:
        raise ValidationError("correct_pedigree requires sire_id and/or dam_id")
    sire_id = data.get("sire_id", entity["data"].get("sire_id"))
    dam_id = data.get("dam_id", entity["data"].get("dam_id"))
    sire = _find_one(lookup, "animal", "id", sire_id) if sire_id is not None else None
    dam = _find_one(lookup, "animal", "id", dam_id) if dam_id is not None else None
    if sire_id is not None and not sire:
        raise ValidationError("sire_id does not reference an existing animal")
    if dam_id is not None and not dam:
        raise ValidationError("dam_id does not reference an existing animal")
    if sire and sire["data"].get("sex") == "female":
        raise ValidationError("sire animal is recorded as female")
    if dam and dam["data"].get("sex") == "male":
        raise ValidationError("dam animal is recorded as male")
    index = build_animal_index(lookup("animal", "*", None) or [])
    if pedigree_has_cycle(sire_id, dam_id, index, self_id=entity["id"]):
        raise ValidationError("pedigree correction would create an ancestor cycle")
    patch = {"sire_id": sire_id, "dam_id": dam_id}
    index[entity["id"]] = {"id": entity["id"], "version": entity["version"] + 1, "data": dict(entity["data"], **patch)}
    patch["inbreeding_coefficient"] = _round(
        animal_inbreeding(entity["id"], index)
    )
    return patch


CUSTOM_CREATE = {
    "animal": _validate_animal,
    "pairing": _validate_pairing_create,
}
CUSTOM_TRANSITIONS = {
    ("pairing", "approve"): _validate_pairing,
    ("pairing", "reconfirm"): _validate_reconfirm,
    ("animal", "correct_pedigree"): _validate_correct_pedigree,
}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active'), 'correct_pedigree': (('active', 'quarantined', 'deceased'), None)}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'reconfirm': (('proposed',), 'proposed'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('approvals',), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'correct_pedigree': ('admin', 'registrar'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'reconfirm': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        extra = custom(actor, data, lookup) if custom else {}
        payload = dict(data)
        if extra:
            payload.update(extra)
        return payload

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        # A None target status means the transition keeps the current status.
        resolved_status = next_status or entity["status"]
        return resolved_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None or value is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
