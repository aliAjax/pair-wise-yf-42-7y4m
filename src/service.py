from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import (
    OCCUPYING_STATUSES,
    RuleEngine,
    describe_issue,
    review_pairing_schedule,
)


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
        # 整个「读取当前版本 → 规则校验（含跨配对占用重查）→ 写入 → 审计」
        # 在一个 BEGIN IMMEDIATE 事务里完成：后到的批准会看到先提交的占用，
        # 因此两个协调员同时批准也不会让同一动物在同周期被占用两次。
        with self.repository.immediate_transaction() as connection:
            entity = self.repository._get_entity_conn(connection, entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)

            def lookup(kind, field, value):
                return self.repository.find_entities_conn(
                    connection, self.rules.normalize_kind(kind), field, value
                )

            expected = int(expected_version) if expected_version is not None else entity["version"]
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), lookup
            )
            merged = dict(entity["data"])
            merged.update(patch)
            updated = self.repository._update_entity_conn(
                connection, entity_id, expected, next_status, merged
            )
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
                connection=connection,
            )
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

    def overview(self):
        """按繁育周期汇总配对建议与占用情况，供首页现场判断。"""
        animals = self.repository.list_entities(kind="animal")
        pairings = self.repository.list_entities(kind="pairing")

        def lookup(kind, field, value):
            if self.rules.normalize_kind(kind) != "animal":
                return []
            return [animal for animal in animals if animal["id"] == value]

        by_cycle = {}
        for pairing in pairings:
            cycle = pairing["data"].get("cycle") or "(未排周期)"
            by_cycle.setdefault(cycle, []).append(pairing)

        cycles = []
        for cycle in sorted(by_cycle):
            cycle_pairings = by_cycle[cycle]
            occupied_animal_ids = set()
            items = []
            for pairing in cycle_pairings:
                data = pairing["data"]
                if pairing["status"] in OCCUPYING_STATUSES:
                    for field in ("sire_id", "dam_id"):
                        if data.get(field):
                            occupied_animal_ids.add(data[field])
                # 待审批/已批准建议都重新做一次占用与亲本状态审查，直接暴露撞车风险。
                issues = []
                if pairing["status"] != "rejected":
                    issues = review_pairing_schedule(pairing, cycle_pairings, lookup)
                items.append(
                    {
                        "id": pairing["id"],
                        "status": pairing["status"],
                        "version": pairing["version"],
                        "sire_id": data.get("sire_id"),
                        "dam_id": data.get("dam_id"),
                        "venue": data.get("venue"),
                        "proposed_by": data.get("proposed_by"),
                        "created_at": pairing["created_at"],
                        "issues": issues,
                        "warnings": [describe_issue(issue) for issue in issues],
                    }
                )
            cycles.append(
                {
                    "cycle": cycle,
                    "venues": sorted(
                        {item["venue"] for item in items if item["venue"]}
                    ),
                    "occupied_animal_ids": sorted(occupied_animal_ids),
                    "pairings": items,
                }
            )
        return {
            "animals": [
                {
                    "id": animal["id"],
                    "name": animal["data"].get("name"),
                    "sex": animal["data"].get("sex"),
                    "status": animal["status"],
                }
                for animal in animals
            ],
            "occupying_statuses": list(OCCUPYING_STATUSES),
            "cycles": cycles,
        }

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
