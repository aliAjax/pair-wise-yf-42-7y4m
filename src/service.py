from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value, connection=None):
        return self.repository.find_entities(
            self.rules.normalize_kind(kind), field, value, connection=connection
        )

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
        with self.repository.transaction() as connection:
            entity = self.repository.get_entity(entity_id, connection=connection)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            expected = (
                int(expected_version) if expected_version is not None else entity["version"]
            )
            lookup = lambda kind, field, value: self._lookup(  # noqa: E731
                kind, field, value, connection=connection
            )
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), lookup
            )
            merged = dict(entity["data"])
            merged.update(patch)
            updated = self.repository.update_entity(
                entity_id, expected, next_status, merged, connection=connection
            )
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
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

    @staticmethod
    def _animal_brief(animals, animal_id):
        if not animal_id:
            return None
        animal = animals.get(animal_id)
        if not animal:
            return {"id": animal_id, "name": None, "status": "missing"}
        return {
            "id": animal_id,
            "name": animal["data"].get("name"),
            "status": animal["status"],
        }

    def cycle_overview(self):
        pairings = self.repository.list_entities(kind="pairing")
        animals = {
            animal["id"]: animal
            for animal in self.repository.list_entities(kind="animal")
        }
        cycles = {}
        for pairing in pairings:
            data = pairing["data"]
            cycle = str(data.get("cycle") or "未设置")
            bucket = cycles.setdefault(
                cycle, {"cycle": cycle, "suggestions": [], "occupancy": []}
            )
            entry = {
                "id": pairing["id"],
                "status": pairing["status"],
                "venue": data.get("venue"),
                "sire": self._animal_brief(animals, data.get("sire_id")),
                "dam": self._animal_brief(animals, data.get("dam_id")),
            }
            bucket["suggestions"].append(entry)
            if pairing["status"] == "approved":
                for role in ("sire", "dam"):
                    brief = entry[role]
                    if brief:
                        bucket["occupancy"].append(
                            {
                                "animal_id": brief["id"],
                                "animal_name": brief["name"],
                                "pairing_id": pairing["id"],
                                "venue": data.get("venue"),
                            }
                        )
        return {"cycles": sorted(cycles.values(), key=lambda item: item["cycle"])}

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
