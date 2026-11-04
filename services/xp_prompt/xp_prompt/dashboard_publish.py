"""Publishing a generated dashboard, and the ledger that makes it reversible.

Planning, layout validation and preview all stop short of creating anything.
Publishing is the first step that writes to Superset, so it is the first step
that needs a ledger: every object an attempt creates is recorded with its id,
and a failure compensates in reverse order. A half-finished publish is therefore
either rolled back or *explained* — never left behind as an orphan nobody can
account for.

The rules, one per feature:

* **the mode is explicit (P8-F08).** ``create`` makes a new dashboard,
  ``update`` edits an existing one, ``duplicate`` copies one. A caller cannot
  "just save" and leave the outcome to chance, and two requests carrying the
  same ``idempotency_key`` produce one dashboard rather than two.
* **access is assigned, not inherited (P8-F09).** Owners, roles, tenant
  visibility and guest embedding are decided here, against the capabilities the
  deployment grants the caller. A request the caller may not make is refused
  before Superset is asked to create anything, because deleting a dashboard
  after the fact is not access control.
* **creation is tracked (P8-F10).** ``created_objects`` and ``cleanup`` are
  stored per attempt, so "what did this attempt leave behind" is answerable from
  the row instead of from a log grep.
* **a manual edit is not silently overwritten (P8-F11).** Publishing records the
  layout fingerprint it produced; republishing compares that fingerprint with
  the live dashboard and refuses a changed one unless the caller forces it.

The row holds identifiers, counts, fingerprints and outcomes. It never holds a
result row, a rendered position blob, or a user's data.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

CREATE = "create"
UPDATE = "update"
DUPLICATE = "duplicate"
MODES: tuple[str, ...] = (CREATE, UPDATE, DUPLICATE)

PENDING = "pending"
PUBLISHED = "published"
FAILED = "failed"
STATUSES: tuple[str, ...] = (PENDING, PUBLISHED, FAILED)

PRIVATE = "private"
TENANT = "tenant"
PUBLIC = "public"
VISIBILITIES: tuple[str, ...] = (PRIVATE, TENANT, PUBLIC)

MAX_SLOTS = 8
MAX_OWNERS = 10
MAX_ROLES = 10
MAX_TITLE_CHARS = 256
MAX_GOAL_CHARS = 600
MAX_SLUG_CHARS = 128
MAX_IDEMPOTENCY_CHARS = 96
MAX_CORRELATION_CHARS = 64
MAX_ERROR_CHARS = 512
MAX_DETAIL_CHARS = 160
MAX_OBJECTS = 64


class DashboardPublishError(ValueError):
    """A refused publish; ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DashboardPublishNotFound(LookupError):
    """No such publish in this tenant, or not this user's."""


class DashboardPublishStateError(RuntimeError):
    """The publish is not in a state that allows the requested transition."""


@dataclass(frozen=True, slots=True)
class Policy:
    """What the deployment lets *this* caller do when publishing.

    The capabilities are decided by the proxy, which is the only party that knows
    the caller's Superset roles. This module never assumes a capability: a
    deployment that turns nothing on refuses nothing, and a deployment that
    turns on role assignment gets its allowed role list enforced here.
    """

    can_publish: bool = True
    can_assign_roles: bool = False
    can_share_publicly: bool = False
    can_embed: bool = False
    allowed_role_ids: frozenset[int] = frozenset()

    @classmethod
    def from_payload(cls, payload: Any) -> Policy:
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise DashboardPublishError("invalid_policy", "policy must be an object")
        raw_roles = payload.get("allowed_role_ids") or ()
        if not isinstance(raw_roles, Iterable) or isinstance(raw_roles, str | bytes):
            raise DashboardPublishError(
                "invalid_policy", "allowed_role_ids must be a list"
            )
        roles: set[int] = set()
        for value in raw_roles:
            try:
                roles.add(int(value))
            except (TypeError, ValueError) as exc:
                raise DashboardPublishError(
                    "invalid_policy", "allowed_role_ids must be integers"
                ) from exc
        return cls(
            can_publish=bool(payload.get("can_publish", True)),
            can_assign_roles=bool(payload.get("can_assign_roles", False)),
            can_share_publicly=bool(payload.get("can_share_publicly", False)),
            can_embed=bool(payload.get("can_embed", False)),
            allowed_role_ids=frozenset(roles),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "can_publish": self.can_publish,
            "can_assign_roles": self.can_assign_roles,
            "can_share_publicly": self.can_share_publicly,
            "can_embed": self.can_embed,
            "allowed_role_ids": sorted(self.allowed_role_ids),
        }


@dataclass(frozen=True, slots=True)
class Request:
    """A validated publish request: what to write, and which mode writes it."""

    mode: str
    title: str
    goal: str
    slots: tuple[dict[str, Any], ...]
    owners: tuple[int, ...]
    roles: tuple[int, ...]
    visibility: str
    guest_access: bool
    target_dashboard_id: int | None
    expected_fingerprint: str
    expected_version: int | None
    force: bool
    idempotency_key: str

    @property
    def slot_count(self) -> int:
        return len(self.slots)

    @classmethod
    def parse(cls, payload: Any) -> Request:
        if not isinstance(payload, Mapping):
            raise DashboardPublishError("invalid_request", "payload must be an object")

        mode = str(payload.get("mode") or CREATE).strip().lower()
        if mode not in MODES:
            raise DashboardPublishError(
                "invalid_mode", f"mode must be one of {', '.join(MODES)}"
            )

        title = _text(payload.get("title"), MAX_TITLE_CHARS)
        if not title:
            raise DashboardPublishError("title_required", "a dashboard needs a title")

        slots = _slots(payload.get("slots"))
        owners = _ids(payload.get("owners"), "owners", MAX_OWNERS)
        roles = _ids(payload.get("roles"), "roles", MAX_ROLES)

        visibility = str(payload.get("visibility") or PRIVATE).strip().lower()
        if visibility not in VISIBILITIES:
            raise DashboardPublishError(
                "invalid_visibility",
                f"visibility must be one of {', '.join(VISIBILITIES)}",
            )

        target: int | None = None
        if mode in {UPDATE, DUPLICATE}:
            target = _optional_int(payload.get("dashboard_id"))
            if target is None:
                raise DashboardPublishError(
                    "target_required",
                    f"mode {mode} needs the dashboard it acts on",
                )
        elif payload.get("dashboard_id") not in (None, ""):
            raise DashboardPublishError(
                "target_not_allowed",
                "mode create makes a new dashboard and takes no dashboard id",
            )

        expected_version = _optional_int(payload.get("expected_version"))
        if expected_version is not None and expected_version < 0:
            raise DashboardPublishError(
                "invalid_version", "expected_version must not be negative"
            )

        return cls(
            mode=mode,
            title=title,
            goal=_text(payload.get("goal"), MAX_GOAL_CHARS),
            slots=slots,
            owners=owners,
            roles=roles,
            visibility=visibility,
            guest_access=bool(payload.get("guest_access")),
            target_dashboard_id=target,
            expected_fingerprint=_text(
                payload.get("expected_fingerprint"), 64
            ).lower(),
            expected_version=expected_version,
            force=bool(payload.get("force")),
            idempotency_key=_text(
                payload.get("idempotency_key"), MAX_IDEMPOTENCY_CHARS
            ),
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "title": self.title,
            "goal": self.goal,
            "slot_count": self.slot_count,
            "owners": list(self.owners),
            "roles": list(self.roles),
            "visibility": self.visibility,
            "guest_access": self.guest_access,
            "dashboard_id": self.target_dashboard_id,
            "expected_fingerprint": self.expected_fingerprint,
            "expected_version": self.expected_version,
            "force": self.force,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """The access a publish will actually assign, after policy."""

    owners: tuple[int, ...]
    roles: tuple[int, ...]
    visibility: str
    guest_access: bool
    position_fingerprint: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "owners": list(self.owners),
            "roles": list(self.roles),
            "visibility": self.visibility,
            "guest_access": self.guest_access,
            "position_fingerprint": self.position_fingerprint,
        }


def decide(
    request: Request,
    policy: Policy,
    *,
    caller_id: int,
    position: Any = None,
    live_position: Any = None,
    live_version: int = 0,
) -> Decision:
    """Refuse an unpermitted publish, and settle the access it will assign.

    ``live_position`` is the dashboard as Superset holds it right now; it is only
    consulted for ``update``, where a changed fingerprint means somebody edited
    the dashboard by hand after the last publish (P8-F11).
    """
    if not policy.can_publish:
        raise DashboardPublishError(
            "not_permitted", "this account may not publish dashboards"
        )

    if request.mode == UPDATE:
        conflict = revision_conflict(
            request,
            live_position=live_position,
            live_version=live_version,
        )
        if conflict is not None:
            raise DashboardPublishError(*conflict)

    owners = _resolve_owners(request.owners, caller_id)
    roles = _resolve_roles(request.roles, policy)
    visibility, guest = _resolve_sharing(
        request.visibility, request.guest_access, policy
    )
    return Decision(
        owners=owners,
        roles=roles,
        visibility=visibility,
        guest_access=guest,
        position_fingerprint=position_fingerprint(position),
    )


def revision_conflict(
    request: Request,
    *,
    live_position: Any,
    live_version: int,
) -> tuple[str, str] | None:
    """Is this update aimed at a dashboard that has moved on? (P8-F11)

    Two independent checks, because they catch different collisions: the version
    catches a second publish racing this one, and the fingerprint catches a human
    edit that no publish recorded. Forcing bypasses both, and is the only way a
    manual change is ever overwritten.
    """
    if request.force:
        return None
    if (
        request.expected_version is not None
        and live_version > request.expected_version
    ):
        return (
            "stale_version",
            (
                "the dashboard was published again after the version this "
                "request was built from"
            ),
        )
    if request.expected_fingerprint:
        current = position_fingerprint(live_position)
        if current and current != request.expected_fingerprint:
            return (
                "dashboard_changed",
                (
                    "the dashboard layout changed since it was published; "
                    "force the update to overwrite the manual edit"
                ),
            )
    return None


def position_fingerprint(position: Any) -> str:
    """A stable digest of a layout, insensitive to key order and whitespace."""
    if position in (None, "", {}):
        return ""
    body = position
    if isinstance(body, str | bytes):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            return hashlib.sha256(str(position).encode("utf-8")).hexdigest()
    try:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = str(body)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def start(
    settings: Settings,
    request: Request,
    decision: Decision,
    *,
    tenant_key: str,
    superset_user_id: int,
    correlation_id: str = "",
    conversation_id: str | None = None,
    job_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Open a publish attempt, or return the one this key already opened.

    A replay is detected on the key before any row is written, so a retried
    request cannot create a second dashboard (P8-F08).
    """
    correlation = _text(correlation_id, MAX_CORRELATION_CHARS)
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        if request.idempotency_key:
            existing = _by_idempotency(
                cursor,
                tenant_id=tenant_id,
                superset_user_id=superset_user_id,
                idempotency_key=request.idempotency_key,
            )
            if existing is not None:
                logger.info(
                    "dashboard_publish replay publish=%s mode=%s",
                    existing["id"],
                    existing["mode"],
                )
                return _public(existing), True

        cursor.execute(
            """
                INSERT INTO ai_dashboard_publish (
                    tenant_id, conversation_id, job_id, superset_user_id,
                    correlation_id, mode, title, goal, target_dashboard_id,
                    owners, roles, visibility, guest_access, slot_count,
                    expected_fingerprint, position_fingerprint, idempotency_key
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s
                )
                RETURNING *
                """,
            (
                tenant_id,
                conversation_id,
                job_id,
                int(superset_user_id),
                correlation,
                request.mode,
                request.title,
                request.goal,
                request.target_dashboard_id,
                _json(list(decision.owners)),
                _json(list(decision.roles)),
                decision.visibility,
                bool(decision.guest_access),
                request.slot_count,
                request.expected_fingerprint,
                decision.position_fingerprint,
                request.idempotency_key,
            ),
        )
        row = cursor.fetchone()

    record = _public(row)
    logger.info(
        "dashboard_publish open publish=%s mode=%s slots=%d visibility=%s",
        record["id"],
        record["mode"],
        record["slot_count"],
        record["visibility"],
    )
    return record, False


def complete(
    settings: Settings,
    publish_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    dashboard_id: int,
    dashboard_slug: str = "",
    position: Any = None,
    created: Iterable[Any] = (),
    cleanup: Iterable[Any] = (),
) -> dict[str, Any]:
    """Record the objects this attempt produced, and the dashboard it made.

    ``position`` is the layout that was actually written, so the next update can
    tell a re-publish from a dashboard somebody has edited since (P8-F11).
    """
    identifier = _optional_int(dashboard_id)
    if identifier is None:
        raise DashboardPublishError(
            "dashboard_required", "a published dashboard needs an id"
        )
    objects = _objects(created)
    outcomes = _objects(cleanup)
    fingerprint = position_fingerprint(position)
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE ai_dashboard_publish p
            SET status = 'published',
                dashboard_id = %s,
                dashboard_slug = %s,
                position_fingerprint = COALESCE(NULLIF(%s, ''), p.position_fingerprint),
                created_objects = %s,
                cleanup = %s,
                published_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            FROM ai_tenant t
            WHERE p.tenant_id = t.id
              AND t.key = %s
              AND p.id = %s
              AND p.superset_user_id = %s
              AND p.status = 'pending'
            RETURNING p.*
            """,
            (
                identifier,
                _text(dashboard_slug, MAX_SLUG_CHARS),
                fingerprint,
                _json(objects),
                _json(outcomes),
                tenant_key,
                publish_id,
                int(superset_user_id),
            ),
        )
        row = cursor.fetchone()
    if row is None:
        raise DashboardPublishStateError(
            f"publish {publish_id} could not be marked published"
        )
    record = _public(row)
    logger.info(
        "dashboard_publish published publish=%s dashboard=%s objects=%d",
        publish_id,
        identifier,
        len(objects),
    )
    return record


def fail(
    settings: Settings,
    publish_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    error_code: str,
    error_message: str = "",
    created: Iterable[Any] = (),
    cleanup: Iterable[Any] = (),
) -> dict[str, Any]:
    """Close a failed attempt, keeping the cleanup outcome of every object."""
    objects = _objects(created)
    outcomes = _objects(cleanup)
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                UPDATE ai_dashboard_publish p
                SET status = 'failed',
                    error_code = %s,
                    error_message = %s,
                    created_objects = %s,
                    cleanup = %s,
                    failed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                FROM ai_tenant t
                WHERE p.tenant_id = t.id
                  AND t.key = %s
                  AND p.id = %s
                  AND p.superset_user_id = %s
                  AND p.status = 'pending'
                RETURNING p.*
                """,
            (
                _text(error_code, 64) or "publish_failed",
                _text(error_message, MAX_ERROR_CHARS),
                _json(objects),
                _json(outcomes),
                tenant_key,
                publish_id,
                int(superset_user_id),
            ),
        )
        row = cursor.fetchone()
    if row is None:
        raise DashboardPublishStateError(
            f"publish {publish_id} could not be marked failed"
        )
    record = _public(row)
    logger.warning(
        "dashboard_publish failed publish=%s code=%s cleanup=%d",
        publish_id,
        record["error_code"],
        len(outcomes),
    )
    return record


def get(
    settings: Settings,
    publish_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> dict[str, Any]:
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                SELECT p.*
                FROM ai_dashboard_publish p
                JOIN ai_tenant t ON t.id = p.tenant_id
                WHERE p.id = %s
                  AND t.key = %s
                  AND p.superset_user_id = %s
                """,
            (publish_id, tenant_key, int(superset_user_id)),
        )
        row = cursor.fetchone()
    if row is None:
        raise DashboardPublishNotFound("publish not found")
    return _public(row)


def latest(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int,
    dashboard_id: int,
) -> dict[str, Any] | None:
    """The most recent successful publish of one dashboard, if any."""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                SELECT p.*
                FROM ai_dashboard_publish p
                JOIN ai_tenant t ON t.id = p.tenant_id
                WHERE p.dashboard_id = %s
                  AND p.status = 'published'
                  AND t.key = %s
                  AND p.superset_user_id = %s
                ORDER BY p.published_at DESC NULLS LAST, p.created_at DESC
                LIMIT 1
                """,
            (int(dashboard_id), tenant_key, int(superset_user_id)),
        )
        row = cursor.fetchone()
    return _public(row) if row else None


def version_of(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int,
    dashboard_id: int,
) -> int:
    """How many times this dashboard has been published successfully."""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
                SELECT COUNT(*) AS published
                FROM ai_dashboard_publish p
                JOIN ai_tenant t ON t.id = p.tenant_id
                WHERE p.dashboard_id = %s
                  AND p.status = 'published'
                  AND t.key = %s
                  AND p.superset_user_id = %s
                """,
            (int(dashboard_id), tenant_key, int(superset_user_id)),
        )
        row = cursor.fetchone() or {}
    return int(row.get("published") or 0)


def _resolve_owners(requested: tuple[int, ...], caller_id: int) -> tuple[int, ...]:
    """Keep the caller an owner: publishing a dashboard you lose is not a feature."""
    owners = tuple(dict.fromkeys((*requested, int(caller_id))))
    if len(owners) > MAX_OWNERS:
        raise DashboardPublishError(
            "owner_budget", f"a dashboard may have at most {MAX_OWNERS} owners"
        )
    return owners


def _resolve_roles(requested: tuple[int, ...], policy: Policy) -> tuple[int, ...]:
    if not requested:
        return ()
    if not policy.can_assign_roles:
        raise DashboardPublishError(
            "role_not_allowed", "this account may not assign dashboard roles"
        )
    if policy.allowed_role_ids:
        refused = [role for role in requested if role not in policy.allowed_role_ids]
        if refused:
            raise DashboardPublishError(
                "role_not_allowed",
                f"role {refused[0]} is not assignable for dashboards",
            )
    return tuple(dict.fromkeys(requested))


def _resolve_sharing(
    visibility: str, guest_access: bool, policy: Policy
) -> tuple[str, bool]:
    if visibility == PUBLIC and not policy.can_share_publicly:
        raise DashboardPublishError(
            "share_not_allowed", "this account may not publish a dashboard publicly"
        )
    if guest_access:
        if not policy.can_embed:
            raise DashboardPublishError(
                "embed_not_allowed", "this account may not enable guest access"
            )
        if visibility == PRIVATE:
            raise DashboardPublishError(
                "embed_requires_share",
                "guest access needs tenant or public visibility",
            )
    return visibility, bool(guest_access)


def _slots(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list | tuple):
        raise DashboardPublishError("invalid_slots", "slots must be a list")
    if not raw:
        raise DashboardPublishError(
            "empty_dashboard", "at least one chart is required"
        )
    if len(raw) > MAX_SLOTS:
        raise DashboardPublishError(
            "slot_budget", f"a dashboard may contain at most {MAX_SLOTS} charts"
        )
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise DashboardPublishError("invalid_slot", "each slot must be an object")
        slot_id = _text(entry.get("id"), 64)
        if not slot_id:
            raise DashboardPublishError("invalid_slot", "each slot needs an id")
        if slot_id in seen:
            raise DashboardPublishError(
                "duplicate_slot", f"slot {slot_id} appears more than once"
            )
        seen.add(slot_id)
        chart_id = _optional_int(entry.get("chart_id"))
        slots.append(
            {
                "id": slot_id,
                "title": _text(entry.get("title"), MAX_TITLE_CHARS) or "Untitled",
                "family": _text(entry.get("family"), 32),
                "section": _text(entry.get("section"), 32),
                "viz_type": _text(entry.get("viz_type"), 80),
                "dataset_id": _optional_int(entry.get("dataset_id")),
                "chart_id": chart_id,
            }
        )
    return tuple(slots)


def _ids(raw: Any, label: str, limit: int) -> tuple[int, ...]:
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list | tuple):
        raise DashboardPublishError("invalid_access", f"{label} must be a list")
    values: list[int] = []
    for entry in raw:
        try:
            values.append(int(entry))
        except (TypeError, ValueError) as exc:
            raise DashboardPublishError(
                "invalid_access", f"{label} must be integers"
            ) from exc
    if len(values) > limit:
        raise DashboardPublishError(
            "access_budget", f"at most {limit} {label} may be assigned"
        )
    return tuple(dict.fromkeys(values))


def _objects(raw: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize the ledger entries: what was made, and what became of it."""
    items: list[dict[str, Any]] = []
    for entry in raw or ():
        if not isinstance(entry, Mapping):
            continue
        identifier = _optional_int(entry.get("id"))
        items.append(
            {
                "kind": _text(entry.get("kind"), 32) or "object",
                "id": identifier,
                "action": _text(entry.get("action"), 16) or "create",
                "outcome": _text(entry.get("outcome"), 64),
                "detail": _text(entry.get("detail"), MAX_DETAIL_CHARS),
            }
        )
        if len(items) >= MAX_OBJECTS:
            break
    return items


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise DashboardPublishError(
            "unknown_tenant", "tenant is not provisioned for this deployment"
        )
    return str(row["id"])


def _by_idempotency(
    cursor: Any,
    *,
    tenant_id: str,
    superset_user_id: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT *
        FROM ai_dashboard_publish
        WHERE tenant_id = %s
          AND superset_user_id = %s
          AND idempotency_key = %s
        """,
        (tenant_id, int(superset_user_id), idempotency_key),
    )
    return cursor.fetchone()


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "mode": row["mode"],
        "status": row["status"],
        "title": row["title"],
        "goal": row["goal"] or "",
        "dashboard_id": row["dashboard_id"],
        "dashboard_slug": row["dashboard_slug"] or "",
        "target_dashboard_id": row["target_dashboard_id"],
        "owners": _load(row["owners"], []),
        "roles": _load(row["roles"], []),
        "visibility": row["visibility"],
        "guest_access": bool(row["guest_access"]),
        "slot_count": int(row["slot_count"] or 0),
        "expected_fingerprint": row["expected_fingerprint"] or "",
        "position_fingerprint": row["position_fingerprint"] or "",
        "created_objects": _load(row["created_objects"], []),
        "cleanup": _load(row["cleanup"], []),
        "error_code": row["error_code"] or "",
        "error_message": row["error_message"] or "",
        "correlation_id": row["correlation_id"] or "",
        "created_at": _isoformat(row["created_at"]),
        "published_at": _isoformat(row["published_at"]),
        "failed_at": _isoformat(row["failed_at"]),
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str | bytes):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()
