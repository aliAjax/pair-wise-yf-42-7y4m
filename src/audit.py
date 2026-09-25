from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditTrail:
    def __init__(self, repository):
        self.repository = repository

    def record(self, entity_id, actor, action, from_status, to_status, detail=None, connection=None):
        args = (
            entity_id,
            actor.user_id,
            actor.role,
            action,
            from_status,
            to_status,
            detail or {},
        )
        if connection is not None:
            self.repository._append_audit_conn(connection, *args)
        else:
            self.repository.append_audit(*args)

    def list(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
