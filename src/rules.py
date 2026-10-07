from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 近交系数阈值：超过此值的配对建议不可批准
INBREEDING_THRESHOLD = 0.125


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


# ---------------------------------------------------------------------------
# 多代近交系数（Wright 亲缘系数）
#
# 配对所产后代的近交系数 F = 0.5 * r(sire, dam)，其中 r 为父母间的亲缘
# 系数（numerator relationship）。r 按递归关系向上回溯祖先计算：
#   r(X, X)       = 1 + F_X，F_X = 0.5 * r(sire_X, dam_X)
#   r(X, Y)       = 0.5 * (r(X, sire_Y) + r(X, dam_Y)) （Y 为较晚代个体）
#   无亲缘关系的奠基者之间 r = 0
# 该方法可正确处理自交、回交、全/半同胞、表亲等情形，并对近交祖先
# 自动计入 (1 + F_A)。
# ---------------------------------------------------------------------------

def _individual(value):
    """从实体或数据字典中提取 (id, data)。"""
    if value is None:
        return None, None
    if isinstance(value, dict):
        if isinstance(value.get("data"), dict):
            return value.get("id"), value["data"]
        return value.get("id"), value
    return None, None


def _resolve_animal(animal_id, lookup):
    if lookup is None or animal_id is None:
        return None
    rows = lookup("animal", "id", animal_id) or []
    return rows[0] if rows else None


def _legacy_coefficient(sire, dam):
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _depth(animal, lookup, memo):
    aid, data = _individual(animal)
    if data is None:
        return 0
    if aid is not None and aid in memo:
        return memo[aid]
    sire = _resolve_animal(data.get("sire_id"), lookup)
    dam = _resolve_animal(data.get("dam_id"), lookup)
    if sire is None and dam is None:
        if aid is not None:
            memo[aid] = 0
        return 0
    ds = _depth(sire, lookup, memo) if sire is not None else 0
    dd = _depth(dam, lookup, memo) if dam is not None else 0
    result = 1 + max(ds, dd)
    if aid is not None:
        memo[aid] = result
    return result


def _relationship(X, Y, lookup, memo, depth_memo, computing):
    xid, xdata = _individual(X)
    yid, ydata = _individual(Y)
    if xid is None or yid is None:
        return 0.0
    key = (xid, yid) if xid <= yid else (yid, xid)
    if key in memo:
        return memo[key]
    if xid == yid:
        if key in computing:
            return 1.0
        computing.add(key)
        sire = _resolve_animal(xdata.get("sire_id"), lookup)
        dam = _resolve_animal(xdata.get("dam_id"), lookup)
        if sire is None or dam is None:
            computing.discard(key)
            memo[key] = 1.0
            return 1.0
        f_x = 0.5 * _relationship(sire, dam, lookup, memo, depth_memo, computing)
        computing.discard(key)
        memo[key] = 1.0 + f_x
        return 1.0 + f_x
    dx = _depth(X, lookup, depth_memo)
    dy = _depth(Y, lookup, depth_memo)
    if dx >= dy:
        # X 为较晚代个体，向上回溯 X 的父母
        px = _resolve_animal(xdata.get("sire_id"), lookup)
        mx = _resolve_animal(xdata.get("dam_id"), lookup)
        if px is None and mx is None:
            memo[key] = 0.0
            return 0.0
        total = 0.0
        if px is not None:
            total += _relationship(px, Y, lookup, memo, depth_memo, computing)
        if mx is not None:
            total += _relationship(mx, Y, lookup, memo, depth_memo, computing)
    else:
        py = _resolve_animal(ydata.get("sire_id"), lookup)
        my = _resolve_animal(ydata.get("dam_id"), lookup)
        if py is None and my is None:
            memo[key] = 0.0
            return 0.0
        total = 0.0
        if py is not None:
            total += _relationship(X, py, lookup, memo, depth_memo, computing)
        if my is not None:
            total += _relationship(X, my, lookup, memo, depth_memo, computing)
    result = 0.5 * total
    memo[key] = result
    return result


def inbreeding_coefficient(sire, dam, lookup=None):
    """计算配对所产后代的近交系数。

    未提供 lookup 时退化为简化的亲缘判定（保留旧行为）；提供 lookup 时
    沿多代祖先按 Wright 亲缘系数递归计算。sire/dam 可以是实体或数据字典。
    """
    if not sire or not dam:
        return 1.0
    if lookup is None:
        _, xdata = _individual(sire)
        _, ydata = _individual(dam)
        return _legacy_coefficient(xdata, ydata)
    memo = {}
    depth_memo = {}
    computing = set()
    r = _relationship(sire, dam, lookup, memo, depth_memo, computing)
    return 0.5 * r


def _is_ancestor(ancestor_id, descendant_id, lookup):
    if ancestor_id is None or descendant_id is None:
        return False
    stack = [descendant_id]
    seen = set()
    while stack:
        current = stack.pop()
        if current == ancestor_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        entity = _find_one(lookup, "animal", "id", current)
        if entity is None:
            continue
        data = entity["data"]
        for parent_key in ("sire_id", "dam_id"):
            pid = data.get(parent_key)
            if pid and pid not in seen:
                stack.append(pid)
    return False


def _validate_pairing_create(actor, data, lookup):
    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    if not sire_id or not dam_id:
        # 现有流程允许先建建议、审批时再提供父母，此处不强制
        return {}
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    coeff = inbreeding_coefficient(sire, dam, lookup)
    if coeff > INBREEDING_THRESHOLD:
        raise ValidationError("pairing exceeds inbreeding threshold")
    data["inbreeding_coefficient"] = coeff
    return {}


def _validate_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    coeff = inbreeding_coefficient(sire, dam, lookup)
    # 审批时按提交那一刻的血统版本再核：对不上就退回重新确认
    recorded_sire = entity["data"].get("sire_id")
    recorded_dam = entity["data"].get("dam_id")
    if recorded_sire is not None and recorded_sire != data.get("sire_id"):
        raise ConflictError("sire changed since proposal; re-confirm")
    if recorded_dam is not None and recorded_dam != data.get("dam_id"):
        raise ConflictError("dam changed since proposal; re-confirm")
    snapshot = entity["data"].get("inbreeding_coefficient")
    if snapshot is not None and abs(float(snapshot) - coeff) > 1e-9:
        raise ConflictError("pedigree changed since proposal; re-confirm")
    if coeff > INBREEDING_THRESHOLD:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id, "inbreeding_coefficient": coeff}


def _validate_correct_pedigree(actor, entity, data, lookup):
    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    if not sire_id and not dam_id:
        raise ValidationError("at least one of sire_id or dam_id is required")
    animal_id = entity["id"]
    merged = dict(entity["data"])
    if sire_id:
        if sire_id == animal_id:
            raise ValidationError("animal cannot be its own sire")
        sire = _find_one(lookup, "animal", "id", sire_id)
        if not sire:
            raise ValidationError("sire does not exist")
        if sire["data"].get("sex") not in ("male", "unknown"):
            raise ValidationError("sire must be male")
        if _is_ancestor(animal_id, sire_id, lookup):
            raise ValidationError("cannot sire a descendant (cycle)")
        merged["sire_id"] = sire_id
    if dam_id:
        if dam_id == animal_id:
            raise ValidationError("animal cannot be its own dam")
        dam = _find_one(lookup, "animal", "id", dam_id)
        if not dam:
            raise ValidationError("dam does not exist")
        if dam["data"].get("sex") not in ("female", "unknown"):
            raise ValidationError("dam must be female")
        if _is_ancestor(animal_id, dam_id, lookup):
            raise ValidationError("cannot dam a descendant (cycle)")
        merged["dam_id"] = dam_id
    if sire_id and dam_id and sire_id == dam_id:
        raise ValidationError("sire and dam must be different animals")
    return merged


CUSTOM_CREATE = {'animal': _validate_animal, 'pairing': _validate_pairing_create}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing,
    ('animal', 'correct_pedigree'): _validate_correct_pedigree,
}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {
        'animal': {
            'mark_deceased': (('active',), 'deceased'),
            'quarantine_animal': (('active',), 'quarantined'),
            'release_quarantine': (('quarantined',), 'active'),
            'correct_pedigree': (('active',), 'active'),
        },
        'pairing': {
            'approve': (('proposed',), 'approved'),
            'reject': (('proposed',), 'rejected'),
            'complete': (('approved',), 'completed'),
        },
        'transfer': {
            'authorize': (('planned',), 'authorized'),
            'ship': (('authorized',), 'in_transit'),
            'arrive': (('in_transit',), 'completed'),
        },
    }
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {
        ('animal', 'mark_deceased'): ('cause',),
        ('animal', 'quarantine_animal'): ('reason',),
        ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'),
        ('pairing', 'reject'): ('reason',),
        ('pairing', 'complete'): ('offspring_ids',),
        ('transfer', 'authorize'): ('permit_id',),
        ('transfer', 'ship'): ('transport_id',),
        ('transfer', 'arrive'): ('arrival_date',),
    }
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {
        'mark_deceased': ('admin', 'veterinarian'),
        'quarantine_animal': ('admin', 'veterinarian'),
        'release_quarantine': ('admin', 'veterinarian'),
        'correct_pedigree': ('admin', 'registrar'),
        'approve': ('admin', 'coordinator'),
        'reject': ('admin', 'coordinator'),
        'complete': ('admin', 'coordinator'),
        'authorize': ('admin', 'registrar'),
        'ship': ('admin', 'registrar'),
        'arrive': ('admin', 'registrar'),
    }

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
        if custom:
            custom(actor, data, lookup)
        return dict(data)

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
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
