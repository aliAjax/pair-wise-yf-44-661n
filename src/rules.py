from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)


class FullPatch(dict):
    """Marker payload that replaces the accumulated transition patch wholesale."""


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _parse_datetime(value, field):
    text = str(value or "").strip()
    if not text:
        raise ValidationError("missing required field: " + field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(field + " must be an ISO-8601 datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field + " must be a number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValidationError(field + " must be a finite number")
    return value


def _validate_change(actor, data, lookup, clock):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def _validate_assess(actor, entity, data, lookup, clock):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup, clock):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup, clock):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    deadline = _parse_datetime(data.get("observation_deadline"), "observation_deadline")
    if deadline <= clock():
        raise ValidationError("observation_deadline must be in the future")
    return {
        "commissioned_by": actor.user_id,
        "observation_deadline": data["observation_deadline"],
        "observation": {"deadline": data["observation_deadline"], "readings": []},
    }


def _observation(entity):
    observation = entity["data"].get("observation")
    if not isinstance(observation, dict) or not isinstance(observation.get("readings"), list):
        raise ValidationError("observation window was not opened at commission")
    return observation


def _summarize_observation(observation):
    """Rebuild per-point summaries; history is retained, latest reading drives status."""
    points = {}
    for reading in observation["readings"]:
        point = points.setdefault(
            reading["sample_point"],
            {"sample_point": reading["sample_point"], "total_readings": 0,
             "anomaly_count": 0, "unresolved_count": 0},
        )
        point["total_readings"] += 1
        point["latest"] = reading
        point["latest_value"] = reading["value"]
        point["latest_read_at"] = reading["read_at"]
        point["latest_out_of_limit"] = bool(reading.get("out_of_limit"))
        if reading.get("out_of_limit"):
            point["anomaly_count"] += 1
        for anomaly_id in reading.get("anomaly_ids", []):
            if not reading.get("dispositions", {}).get(str(anomaly_id)):
                point["unresolved_count"] += 1
    total = len(observation["readings"])
    anomalies = sum(item["anomaly_count"] for item in points.values())
    unresolved = sum(item["unresolved_count"] for item in points.values())
    latest_reading = None
    if observation["readings"]:
        latest_reading = observation["readings"][-1]
    return {
        "deadline": observation["deadline"],
        "total_readings": total,
        "anomaly_count": anomalies,
        "unresolved_count": unresolved,
        "latest_reading_at": latest_reading["read_at"] if latest_reading else None,
        "sample_points": [points[key] for key in sorted(points)],
    }


def _validate_submit_reading(actor, entity, data, lookup, clock):
    observation = _observation(entity)
    sample_point = str(data.get("sample_point", "")).strip()
    if not sample_point:
        raise ValidationError("missing required field: sample_point")
    has_min = data.get("min_limit") is not None
    has_max = data.get("max_limit") is not None
    if not has_min and not has_max:
        raise ValidationError("min_limit or max_limit is required")
    min_limit = _number(data["min_limit"], "min_limit") if has_min else None
    max_limit = _number(data["max_limit"], "max_limit") if has_max else None
    if has_min and has_max and min_limit > max_limit:
        raise ValidationError("min_limit must not exceed max_limit")
    value = _number(data.get("value"), "value")
    read_at = str(data["read_at"])
    _parse_datetime(read_at, "read_at")
    out_of_limit = (
        (min_limit is not None and value < min_limit)
        or (max_limit is not None and value > max_limit)
    )
    anomaly_seq = sum(
        1 for item in observation["readings"] if item.get("out_of_limit")
    ) + 1
    reading = {
        "sample_point": sample_point,
        "min_limit": min_limit,
        "max_limit": max_limit,
        "value": value,
        "read_at": read_at,
        "reported_by": actor.user_id,
        "reported_at": clock().isoformat(timespec="seconds"),
        "out_of_limit": out_of_limit,
        "anomaly_ids": [],
        "dispositions": {},
    }
    if out_of_limit:
        reading["anomaly_ids"] = [anomaly_seq]
    observation["readings"].append(reading)
    observation["summary"] = _summarize_observation(observation)
    return FullPatch({"observation": observation})


def _validate_dispose_anomaly(actor, entity, data, lookup, clock):
    observation = _observation(entity)
    if data.get("anomaly_id") is None:
        raise ValidationError("missing required field: anomaly_id")
    try:
        anomaly_id = int(data["anomaly_id"])
    except (TypeError, ValueError):
        raise ValidationError("anomaly_id must be an integer")
    target = None
    for reading in observation["readings"]:
        if anomaly_id in reading.get("anomaly_ids", []):
            target = reading
            break
    if target is None:
        raise NotFoundError("anomaly not found: %s" % anomaly_id)
    key = str(anomaly_id)
    if target.get("dispositions", {}).get(key):
        raise ConflictError("anomaly already disposed: %s" % anomaly_id)
    target.setdefault("dispositions", {})[key] = {
        "disposition": data["disposition"],
        "disposed_by": actor.user_id,
        "disposed_at": clock().isoformat(timespec="seconds"),
    }
    observation["summary"] = _summarize_observation(observation)
    return FullPatch({"observation": observation})


def _observation_open(observation, clock):
    return _parse_datetime(observation["deadline"], "observation_deadline") > clock()


def _validate_rollback(actor, entity, data, lookup, clock):
    observation = entity["data"].get("observation")
    unresolved = (
        isinstance(observation, dict)
        and isinstance(observation.get("summary"), dict)
        and observation["summary"].get("unresolved_count", 0) > 0
    )
    if unresolved and actor.role != "safety":
        raise PermissionDenied(
            "unresolved observation anomalies require safety approval to roll back"
        )
    return {"rolled_back_by": actor.user_id}


def _validate_close(actor, entity, data, lookup, clock):
    extra = {"closed_by": actor.user_id}
    observation = entity["data"].get("observation")
    if entity["status"] == "commissioned" and isinstance(observation, dict):
        if _observation_open(observation, clock):
            raise ValidationError(
                "observation window is still open until " + observation["deadline"]
            )
        unresolved = observation.get("summary", {}).get("unresolved_count", 0)
        if unresolved:
            raise ValidationError(
                "%s unresolved observation anomaly(ies) must be disposed or rolled back"
                % unresolved
            )
        extra["observation_result"] = {
            "deadline": observation["deadline"],
            "total_readings": observation["summary"]["total_readings"],
            "anomaly_count": observation["summary"]["anomaly_count"],
            "sample_points": observation["summary"]["sample_points"],
            "closed_at": clock().isoformat(timespec="seconds"),
        }
    return extra


CUSTOM_CREATE = {'change': _validate_change}
CUSTOM_TRANSITIONS = {
    ('change', 'assess'): _validate_assess,
    ('change', 'approve'): _validate_approve,
    ('change', 'commission'): _validate_commission,
    ('change', 'submit_reading'): _validate_submit_reading,
    ('change', 'dispose_anomaly'): _validate_dispose_anomaly,
    ('change', 'rollback'): _validate_rollback,
    ('change', 'close'): _validate_close,
}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'submit_reading': (('commissioned',), 'commissioned'), 'dispose_anomaly': (('commissioned',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back', 'commissioned'), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed', 'observation_deadline'), ('change', 'submit_reading'): ('sample_point', 'value', 'read_at'), ('change', 'dispose_anomaly'): ('anomaly_id', 'disposition'), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'submit_reading': ('admin', 'engineer'), 'dispose_anomaly': ('admin', 'engineer'), 'rollback': ('admin', 'engineer', 'safety'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier')}

    def __init__(self, clock=None):
        # Injectable clock for deterministic observation-window tests.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def clock(self):
        now = self._clock()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)

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
            custom(actor, data, lookup, self.clock)
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
        extra = custom(actor, entity, data, lookup, self.clock) if custom else {}
        if isinstance(extra, FullPatch):
            return next_status, dict(extra)
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
