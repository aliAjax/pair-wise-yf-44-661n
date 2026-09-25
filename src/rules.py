from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def _parse_moment(value, field):
    text = str(value or "").strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(field + " must be an ISO timestamp")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _as_number(value, field):
    if isinstance(value, bool):
        raise ValidationError(field + " must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be a number")


def _unhandled_observations(lookup, change_id):
    if lookup is None:
        return []
    rows = lookup("observation", "change_id", change_id) or []
    return [row["id"] for row in rows if row["status"] == "exceeded"]


def _validate_observation(actor, data, lookup):
    change = _find_one(lookup, "change", "id", data.get("change_id"))
    if not change:
        raise ValidationError("change does not exist")
    if change["status"] != "commissioned":
        raise ValidationError("change is not commissioned yet")
    if not str(data.get("point") or "").strip():
        raise ValidationError("sampling point is required")
    data["limit_value"] = _as_number(data.get("limit_value"), "limit_value")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_assess(actor, entity, data, lookup):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    deadline = _parse_moment(data.get("observation_deadline"), "observation_deadline")
    return {
        "commissioned_by": actor.user_id,
        "observation_deadline": deadline.isoformat(),
    }


def _validate_rollback(actor, entity, data, lookup):
    unhandled = _unhandled_observations(lookup, entity["id"])
    if unhandled and actor.role != "safety":
        raise PermissionDenied(
            "unhandled anomalies must be approved by safety to rollback: "
            + ", ".join(unhandled)
        )
    return {"rolled_back_by": actor.user_id}


def _validate_close(actor, entity, data, lookup):
    if entity["status"] == "commissioned":
        deadline = entity["data"].get("observation_deadline")
        if not deadline:
            raise ValidationError("observation deadline is not set")
        if datetime.now(timezone.utc) < _parse_moment(deadline, "observation_deadline"):
            raise ValidationError("observation period has not ended")
        unhandled = _unhandled_observations(lookup, entity["id"])
        if unhandled:
            raise ValidationError("unhandled anomalies: " + ", ".join(unhandled))
    return {"closed_by": actor.user_id}


def _validate_report(actor, entity, data, lookup):
    limit = _as_number(entity["data"].get("limit_value"), "limit_value")
    value = _as_number(data.get("value"), "value")
    reading_time = _parse_moment(data.get("reading_time"), "reading_time")
    history = list(entity["data"].get("readings") or [])
    history.append(
        {
            "value": value,
            "reading_time": reading_time.isoformat(),
            "reported_by": actor.user_id,
        }
    )
    exceeded = value > limit
    exceed_count = int(entity["data"].get("exceed_count", 0) or 0) + (1 if exceeded else 0)
    return {
        "__status__": "exceeded" if exceeded else "normal",
        "readings": history,
        "exceed_count": exceed_count,
        "latest_value": value,
        "latest_reading_time": reading_time.isoformat(),
    }


def _validate_handle(actor, entity, data, lookup):
    return {"handled_by": actor.user_id}


CUSTOM_CREATE = {'change': _validate_change, 'observation': _validate_observation}
CUSTOM_TRANSITIONS = {('change', 'assess'): _validate_assess, ('change', 'approve'): _validate_approve, ('change', 'commission'): _validate_commission, ('change', 'rollback'): _validate_rollback, ('change', 'close'): _validate_close, ('observation', 'report'): _validate_report, ('observation', 'handle'): _validate_handle}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item', 'observations': 'observation'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open', 'observation': 'monitoring'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back', 'commissioned'), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}, 'observation': {'report': (('monitoring', 'normal', 'exceeded', 'handled'), 'monitoring'), 'handle': (('exceeded',), 'handled')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner'), 'observation': ('change_id', 'point', 'limit_value')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed', 'observation_deadline'), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',), ('observation', 'report'): ('value', 'reading_time'), ('observation', 'handle'): ('disposition',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety'), 'observation': ('admin', 'engineer')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer', 'safety'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier'), 'report': ('admin', 'engineer'), 'handle': ('admin', 'engineer', 'safety')}

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
        if extra and "__status__" in extra:
            next_status = extra.pop("__status__")
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
