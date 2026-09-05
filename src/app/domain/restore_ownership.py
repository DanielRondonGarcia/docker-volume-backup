"""Pure parsing and validation for portable restore ownership policy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
import re
from typing import Any, Optional


SCHEMA_VERSION = 1
_ID = re.compile(r"^[0-9]+:[0-9]+$")
_KEY = re.compile(r"^compose:[A-Za-z0-9][A-Za-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RestoreOwnershipValidationError(ValueError):
    """Validation failure with a stable category for later projections."""

    def __init__(self, message: str, category: str = "validation_error"):
        self.category = category
        super().__init__(message)


def parse_uid_gid(value: Any) -> tuple[int, int]:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise RestoreOwnershipValidationError("ownership must be decimal uid:gid", "invalid_uid_gid")
    uid, gid = value.split(":", 1)
    return int(uid), int(gid)


def _uid_gid_text(value: Any) -> str:
    uid, gid = parse_uid_gid(value)
    return f"{uid}:{gid}"


def validate_stable_volume_key(value: Any) -> str:
    if not isinstance(value, str) or _KEY.fullmatch(value) is None:
        raise RestoreOwnershipValidationError(
            "volume mapping key must be compose:project:volume", "invalid_volume_key"
        )
    return value


def stable_compose_volume_key(
    project: Any,
    volume: Any,
    *,
    labels: Optional[Mapping[str, Any]] = None,
    host_path: Optional[str] = None,
    anonymous: bool = False,
    generated: bool = False,
) -> str:
    """Build a key only from a named, label-identified Compose volume."""
    if labels is not None:
        project, volume = labels.get("com.docker.compose.project"), labels.get("com.docker.compose.volume")
    if host_path is not None or anonymous or generated:
        raise RestoreOwnershipValidationError(
            "host paths and anonymous/generated volumes have no stable mapping key", "invalid_volume_identity"
        )
    if not isinstance(project, str) or not isinstance(volume, str) or not _PART.fullmatch(project) or not _PART.fullmatch(volume):
        raise RestoreOwnershipValidationError(
            "Compose project and volume labels are required for a stable key", "invalid_volume_identity"
        )
    return validate_stable_volume_key(f"compose:{project}:{volume}")


@dataclass(frozen=True)
class RestoreOwnershipPolicy:
    """Versioned preserve/map policy; no ownership mutation is performed here."""

    mode: str = "preserve"
    mappings: Mapping[str, str] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    confirmation: Optional[str] = None
    source: Optional[str] = field(default=None, compare=False, repr=False)
    default_mapping: Optional[str] = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool) or self.schema_version != SCHEMA_VERSION:
            raise RestoreOwnershipValidationError("unsupported restore ownership schema version", "invalid_schema")
        if self.mode not in {"preserve", "map"}:
            raise RestoreOwnershipValidationError("restore ownership mode must be preserve or map", "invalid_mode")
        if not isinstance(self.mappings, Mapping):
            raise RestoreOwnershipValidationError("restore ownership mappings must be an object", "invalid_mappings")
        if self.confirmation not in {None, "confirmed", "declined"}:
            raise RestoreOwnershipValidationError(
                "restore ownership confirmation must be confirmed or declined", "invalid_confirmation"
            )
        mappings = {validate_stable_volume_key(key): _uid_gid_text(value) for key, value in self.mappings.items()}
        default = _uid_gid_text(self.default_mapping) if self.default_mapping is not None else None
        if self.mode == "preserve" and (mappings or default is not None):
            raise RestoreOwnershipValidationError("preserve mode cannot contain mappings", "invalid_mappings")
        object.__setattr__(self, "mappings", mappings)
        object.__setattr__(self, "default_mapping", default)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RestoreOwnershipPolicy":
        if not isinstance(value, Mapping):
            raise RestoreOwnershipValidationError("restore ownership policy must be an object", "invalid_policy")
        mappings = value.get("mappings", {})
        if mappings is None:
            raise RestoreOwnershipValidationError("restore ownership mappings must be an object", "invalid_mappings")
        return cls(
            mode=value.get("mode", "preserve"),
            mappings=mappings,
            schema_version=value.get("schema_version", value.get("version", SCHEMA_VERSION)),
            confirmation=value.get("confirmation"),
        )

    def require_confirmation(self) -> "RestoreOwnershipPolicy":
        if self.confirmation == "declined":
            raise RestoreOwnershipValidationError("restore ownership confirmation was declined", "confirmation_declined")
        if self.mode == "map" and self.confirmation != "confirmed":
            raise RestoreOwnershipValidationError(
                "restore ownership mapping requires explicit confirmation", "confirmation_required"
            )
        return self

    def mapping_text(self, volume_key: str) -> Optional[str]:
        value = self.mappings.get(volume_key, self.default_mapping)
        return _uid_gid_text(value) if value is not None else None

    def effective_mapping(self, volume_key: str) -> Optional[tuple[int, int]]:
        value = self.mapping_text(validate_stable_volume_key(volume_key))
        return parse_uid_gid(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"schema_version": self.schema_version, "mode": self.mode, "mappings": dict(self.mappings)}
        if self.confirmation is not None:
            result["confirmation"] = self.confirmation
        return result


def _coerce_policy(value: Any, source: str) -> Optional[RestoreOwnershipPolicy]:
    if value is None:
        return None
    if isinstance(value, RestoreOwnershipPolicy):
        policy = value
    elif isinstance(value, str) and source == "RESTORE_CHOWN":
        policy = RestoreOwnershipPolicy(mode="map", default_mapping=value, confirmation="confirmed")
    elif isinstance(value, Mapping) and "restore_ownership" in value:
        return _coerce_policy(value["restore_ownership"], source)
    elif isinstance(value, Mapping) and ("mode" in value or "mappings" in value):
        policy = RestoreOwnershipPolicy.from_dict(value)
    else:
        raise RestoreOwnershipValidationError(f"invalid {source} restore ownership policy", "invalid_policy")
    if source == "target defaults":
        policy = replace(policy, confirmation=None)
    return replace(policy, source=source)


@dataclass(frozen=True)
class RestoreVolumeScope:
    """Stable volume identity and service identities used for policy validation."""

    volume_key: str
    target_path: str = ""
    source: Optional[str] = None
    service_identities: tuple[Any, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        validate_stable_volume_key(self.volume_key)
        object.__setattr__(self, "service_identities", tuple(_identity_text(item) for item in _items(self.service_identities)))

    @property
    def effective_identities(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.service_identities)))


def _items(value: Any) -> tuple[Any, ...]:
    return (value,) if isinstance(value, (str, bytes, Mapping)) else tuple(value)


def _identity_text(value: Any) -> str:
    if isinstance(value, Mapping):
        if "identity" in value:
            value = value["identity"]
        elif "uid" in value and "gid" in value:
            value = f"{value['uid']}:{value['gid']}"
        else:
            raise RestoreOwnershipValidationError("service identity is incomplete", "invalid_service_identity")
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        value = f"{value[0]}:{value[1]}"
    return _uid_gid_text(value)


def _coerce_scope(value: Any) -> RestoreVolumeScope:
    if isinstance(value, RestoreVolumeScope):
        return value
    if isinstance(value, str):
        return RestoreVolumeScope(value)
    if not isinstance(value, Mapping):
        raise RestoreOwnershipValidationError("volume scope must be an object", "invalid_volume_scope")
    key = value.get("volume_key", value.get("stable_key"))
    if key is None and (value.get("compose_project") is not None or value.get("labels") is not None):
        key = stable_compose_volume_key(
            value.get("compose_project"), value.get("compose_volume"), labels=value.get("labels"),
            host_path=value.get("host_path"), anonymous=bool(value.get("anonymous")), generated=bool(value.get("generated")),
        )
    if key is None:
        raise RestoreOwnershipValidationError("volume scope has no stable key", "invalid_volume_key")
    if value.get("anonymous") or value.get("generated") or value.get("named") is False:
        raise RestoreOwnershipValidationError("anonymous/generated volumes cannot be mapping identities", "invalid_volume_identity")
    return RestoreVolumeScope(
        volume_key=key,
        target_path=value.get("target_path", value.get("destination", "")),
        source=value.get("source", value.get("host_path")),
        service_identities=value.get("service_identities", value.get("services", ())),
    )


def validate_volume_scopes(
    volume_scopes: Iterable[Any], policy: Optional[RestoreOwnershipPolicy] = None
) -> tuple[RestoreVolumeScope, ...]:
    by_key: dict[str, RestoreVolumeScope] = {}
    for raw in volume_scopes:
        current = _coerce_scope(raw)
        previous = by_key.get(current.volume_key)
        if previous is None:
            by_key[current.volume_key] = current
            continue
        if (previous.source and current.source and previous.source != current.source) or (
            previous.target_path and current.target_path and previous.target_path != current.target_path
        ):
            raise RestoreOwnershipValidationError(f"volume key collision for {current.volume_key}", "volume_key_collision")
        by_key[current.volume_key] = RestoreVolumeScope(
            current.volume_key, previous.target_path or current.target_path, previous.source or current.source,
            previous.service_identities + current.service_identities,
        )
    scopes = tuple(by_key.values())
    if policy is not None:
        unknown = set(policy.mappings) - {scope.volume_key for scope in scopes}
        if unknown:
            raise RestoreOwnershipValidationError(f"unknown volume mapping key: {sorted(unknown)[0]}", "unknown_volume_key")
    for scope in scopes:
        if len(scope.effective_identities) > 1:
            raise RestoreOwnershipValidationError(
                f"ambiguous shared volume identity for {scope.volume_key}", "ambiguous_shared_volume_identity"
            )
    return scopes


def _policy_signature(policy: RestoreOwnershipPolicy, keys: tuple[str, ...]) -> Any:
    return (policy.mode, tuple((key, policy.mapping_text(key)) for key in keys)) if keys else (
        policy.mode, tuple(sorted(policy.mappings.items())), policy.default_mapping
    )


def resolve_restore_ownership(
    request: Any = None,
    target_defaults: Any = None,
    legacy_chown: Optional[str] = None,
    volume_scopes: Optional[Iterable[Any]] = None,
) -> RestoreOwnershipPolicy:
    scopes = validate_volume_scopes(tuple(volume_scopes or ())) if volume_scopes is not None else ()
    keys = tuple(scope.volume_key for scope in scopes)
    request_policy = _coerce_policy(request, "request")
    target_policy = _coerce_policy(target_defaults, "target defaults")
    legacy_policy = _coerce_policy(legacy_chown, "RESTORE_CHOWN")
    candidates = [policy for policy in (request_policy, target_policy, legacy_policy) if policy is not None]
    for policy in candidates:
        validate_volume_scopes(scopes, policy)
    if request_policy is not None and (request_policy.confirmation is not None or request_policy.mode == "map"):
        request_policy.require_confirmation()
    if not candidates:
        return RestoreOwnershipPolicy()
    signature = _policy_signature(candidates[0], keys)
    if any(_policy_signature(policy, keys) != signature for policy in candidates[1:]):
        raise RestoreOwnershipValidationError(
            "restore ownership policies conflict across precedence layers", "policy_conflict"
        )
    selected = candidates[0]
    if keys and selected.default_mapping is not None:
        selected = RestoreOwnershipPolicy(
            mode="map", mappings={key: selected.mapping_text(key) for key in keys},
            schema_version=selected.schema_version, confirmation=selected.confirmation, source=selected.source,
        )
    return selected
