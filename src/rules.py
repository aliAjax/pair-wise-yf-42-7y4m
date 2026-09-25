from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 配对处于这些状态时会占用亲本在同一繁育周期的档期。
# rejected 不在其中，因此驳回（无论从 proposed 还是 approved）即释放双方档期。
OCCUPYING_STATUSES = ("approved", "completed")

SIDE_SIRE = "sire"
SIDE_DAM = "dam"
SIDE_LABELS = {SIDE_SIRE: "父本", SIDE_DAM: "母本"}

_STATUS_LABELS = {
    "quarantined": "隔离中",
    "deceased": "已死亡",
}


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _find_one(lookup, kind, field, value):
    if lookup is None or value is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _animal_label(animal):
    if not animal:
        return ""
    name = animal["data"].get("name") or animal["id"]
    return "%s(%s)" % (name, animal["id"])


def _pairing_label(pairing):
    data = pairing["data"]
    parts = [pairing["id"]]
    venue = data.get("venue")
    if venue:
        parts.append("场馆:%s" % venue)
    return "、".join(parts)


def describe_issue(issue):
    """把 review_pairing_schedule 产生的问题结构翻译成给协调员看的中文说明。"""
    code = issue["code"]
    if code == "animal_missing":
        side = SIDE_LABELS.get(issue["side"], issue["side"])
        return "%s %s 不存在" % (side, issue.get("animal_id"))
    if code == "animal_status":
        side = SIDE_LABELS.get(issue["side"], issue["side"])
        state = _STATUS_LABELS.get(issue["status"], issue["status"])
        return "%s %s 当前状态为%s，不能参与本周期配对" % (
            side,
            _animal_label(issue.get("animal")),
            state,
        )
    if code == "occupied":
        side = SIDE_LABELS.get(issue["side"], issue["side"])
        return "%s %s 在本周期已被配对 %s 占用" % (
            side,
            _animal_label(issue.get("animal")),
            _pairing_label(issue["occupant"]),
        )
    return code


def review_pairing_schedule(pairing, cycle_pairings, lookup):
    """审查一条配对在同一繁育周期内的档期与亲本状态。

    返回问题列表，每项形如 {"code", "side", ...}，不抛异常。
    ``cycle_pairings`` 为同周期的全部配对（含自身），occupant 冲突会排除自身。
    """
    data = pairing["data"]
    cycle = data.get("cycle")
    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    animals = {SIDE_SIRE: (sire_id, sire), SIDE_DAM: (dam_id, dam)}
    issues = []
    for side, (animal_id, animal) in animals.items():
        if not animal_id:
            continue
        if not animal:
            issues.append({"code": "animal_missing", "side": side, "animal_id": animal_id})
            continue
        if animal["status"] != "active":
            issues.append(
                {
                    "code": "animal_status",
                    "side": side,
                    "animal_id": animal_id,
                    "animal": animal,
                    "status": animal["status"],
                }
            )
            continue
        for other in cycle_pairings:
            if other["id"] == pairing["id"]:
                continue
            if other["status"] not in OCCUPYING_STATUSES:
                continue
            if other["data"].get("cycle") != cycle:
                continue
            if other["data"].get("sire_id") == animal_id or other["data"].get("dam_id") == animal_id:
                issues.append(
                    {
                        "code": "occupied",
                        "side": side,
                        "animal_id": animal_id,
                        "animal": animal,
                        "occupant": other,
                    }
                )
                break
    return issues


def _normalize_text(value, field):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValidationError("missing required field: " + field)
    return text


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")
    return data


def _validate_pairing_create(actor, data, lookup):
    data["cycle"] = _normalize_text(data.get("cycle"), "cycle")
    data["venue"] = _normalize_text(data.get("venue"), "venue")
    # 亲本可以在提建议阶段先留空，批准前补齐；填了就必须存在。
    for field in ("sire_id", "dam_id"):
        value = data.get(field)
        if value:
            if not _find_one(lookup, "animal", "id", value):
                raise ValidationError("unknown %s: %s" % (field, value))
            data[field] = value
    return data


def _validate_pairing(actor, entity, data, lookup):
    data = dict(data or {})
    # sire/dam/cycle/venue 允许在批准时补齐，但以实体已有值为准。
    for field in ("sire_id", "dam_id", "cycle", "venue"):
        value = data.get(field)
        if value is None or value == "":
            value = entity["data"].get(field)
        data[field] = value

    sire_id = data.get("sire_id")
    dam_id = data.get("dam_id")
    if not sire_id or not dam_id:
        raise ValidationError("approve requires sire_id and dam_id")
    sire = _find_one(lookup, "animal", "id", sire_id)
    dam = _find_one(lookup, "animal", "id", dam_id)
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire_id == dam_id:
        raise ValidationError("sire and dam must be different animals")
    if inbreeding_coefficient(sire["data"], dam["data"]) > 0.125:
        raise ValidationError("pairing exceeds inbreeding threshold")

    candidate = dict(entity)
    candidate["data"] = data
    cycle_pairings = lookup("pairing", "cycle", data["cycle"]) if data.get("cycle") else []
    issues = review_pairing_schedule(candidate, cycle_pairings, lookup)
    if issues:
        # 占用冲突（409）与隔离/死亡（400）分开表达，但都不改变当前建议状态。
        blockers = [describe_issue(issue) for issue in issues]
        details = {
            "pairing_id": entity["id"],
            "issues": issues,
            "blockers": blockers,
        }
        occupied = [issue for issue in issues if issue["code"] == "occupied"]
        message = "配对 %s 无法批准：%s" % (
            entity["id"],
            "；".join(blockers),
        )
        if occupied:
            raise ConflictError(message, details=details)
        raise ValidationError(message, details=details)

    data["approved_by"] = actor.user_id
    return data


CUSTOM_CREATE = {'animal': _validate_animal, 'pairing': _validate_pairing_create}
CUSTOM_TRANSITIONS = {('pairing', 'approve'): _validate_pairing}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {
        'animal': {
            'mark_deceased': (('active', 'quarantined'), 'deceased'),
            'quarantine_animal': (('active',), 'quarantined'),
            'release_quarantine': (('quarantined',), 'active'),
        },
        'pairing': {
            'approve': (('proposed',), 'approved'),
            # 已批准但尚未完成的建议也可以驳回，从而释放双方本周期档期。
            'reject': (('proposed', 'approved'), 'rejected'),
            'complete': (('approved',), 'completed'),
        },
        'transfer': {
            'authorize': (('planned',), 'authorized'),
            'ship': (('authorized',), 'in_transit'),
            'arrive': (('in_transit',), 'completed'),
        },
    }
    CREATE_REQUIRED = {
        'animal': ('name', 'sex'),
        'pairing': ('proposed_by', 'cycle', 'venue'),
        'transfer': ('animal_id', 'from_institution', 'to_institution'),
    }
    ACTION_REQUIRED = {
        ('animal', 'mark_deceased'): ('cause',),
        ('animal', 'quarantine_animal'): ('reason',),
        ('pairing', 'approve'): ('approvals',),
        ('pairing', 'reject'): ('reason',),
        ('pairing', 'complete'): ('offspring_ids',),
        ('transfer', 'authorize'): ('permit_id',),
        ('transfer', 'ship'): ('transport_id',),
        ('transfer', 'arrive'): ('arrival_date',),
    }
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar')}

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
        normalized = custom(actor, data, lookup) if custom else data
        return dict(normalized or data)

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
        patch = custom(actor, entity, data, lookup) if custom else dict(data)
        patch = dict(patch)
        return next_status, patch
