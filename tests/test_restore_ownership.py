"""Policy-level coverage for portable restore ownership."""

import unittest

from src.app.application.ports.ports import RestoreOwnershipPort
from src.app.domain.models import RestoreConfig, RestoreResult
from src.app.domain.restore_ownership import (
    RestoreOwnershipPolicy,
    RestoreOwnershipValidationError,
    RestoreVolumeScope,
    parse_uid_gid,
    resolve_restore_ownership,
    stable_compose_volume_key,
)


DATA_KEY = stable_compose_volume_key("app", "data")
LOGS_KEY = stable_compose_volume_key("app", "logs")


def scope(key=DATA_KEY, *, source="docker-volume-data", identities=()):
    return RestoreVolumeScope(key, f"/var/lib/{key.rsplit(':', 1)[-1]}", source, identities)


def map_request(key=DATA_KEY, value="1000:1000"):
    return {"schema_version": 1, "mode": "map", "mappings": {key: value}, "confirmation": "confirmed"}


class RestoreOwnershipTests(unittest.TestCase):
    def test_schema_ids_and_confirmation_are_strict(self):
        policy = RestoreOwnershipPolicy.from_dict(map_request(value="01000:02000"))
        self.assertEqual(policy.schema_version, 1)
        self.assertEqual(policy.effective_mapping(DATA_KEY), (1000, 2000))
        self.assertEqual(policy.to_dict()["confirmation"], "confirmed")
        for value in ("1000", "1000:-1", "1:two", "1:2:3", 1000):
            with self.subTest(value=value), self.assertRaises(RestoreOwnershipValidationError):
                parse_uid_gid(value)
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "schema"):
            RestoreOwnershipPolicy.from_dict({"schema_version": 2})
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "confirmation"):
            RestoreOwnershipPolicy.from_dict({"mode": "map", "mappings": {DATA_KEY: "1:1"}}).require_confirmation()
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "confirmation"):
            RestoreOwnershipPolicy.from_dict(
                {"mode": "map", "mappings": {DATA_KEY: "1:1"}, "confirmation": "declined"}
            ).require_confirmation()

    def test_request_precedence_and_conflicts_are_fail_closed(self):
        resolved = resolve_restore_ownership(
            request=map_request(), target_defaults=map_request(), legacy_chown="1000:1000", volume_scopes=[scope()]
        )
        self.assertEqual(resolved.source, "request")
        self.assertEqual(resolved.effective_mapping(DATA_KEY), (1000, 1000))
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "conflict"):
            resolve_restore_ownership(
                request=map_request(),
                target_defaults={"mode": "map", "mappings": {DATA_KEY: "2000:2000"}},
                volume_scopes=[scope()],
            )

    def test_stable_compose_keys_reject_host_and_anonymous_identity(self):
        self.assertEqual(
            stable_compose_volume_key(
                "ignored", "ignored", labels={"com.docker.compose.project": "app", "com.docker.compose.volume": "data"}
            ),
            DATA_KEY,
        )
        with self.assertRaises(RestoreOwnershipValidationError):
            stable_compose_volume_key("app", "data", host_path="/host/path")
        with self.assertRaises(RestoreOwnershipValidationError):
            stable_compose_volume_key("app", "anonymous", anonymous=True)

    def test_unknown_collision_and_shared_identity_rejections(self):
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "unknown"):
            resolve_restore_ownership(request=map_request("compose:app:missing"), volume_scopes=[scope()])
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "collision"):
            resolve_restore_ownership(request={"mode": "preserve"}, volume_scopes=[scope(source="a"), scope(source="b")])
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "ambiguous"):
            resolve_restore_ownership(
                request={"mode": "preserve"},
                volume_scopes=[scope(identities=["1000:1000"]), scope(identities=["2000:2000"])],
            )

    def test_preserve_default_zero_mapping_and_legacy_alias_are_only_resolved(self):
        preserved = resolve_restore_ownership(volume_scopes=[scope(), scope(LOGS_KEY)])
        self.assertEqual(preserved.mode, "preserve")
        self.assertEqual(preserved.mappings, {})
        zero = resolve_restore_ownership(request=map_request(value="0:0"), volume_scopes=[scope()])
        self.assertEqual(zero.effective_mapping(DATA_KEY), (0, 0))
        legacy = resolve_restore_ownership(legacy_chown="1000:1000", volume_scopes=[scope(), scope(LOGS_KEY)])
        self.assertEqual(legacy.source, "RESTORE_CHOWN")
        self.assertEqual(legacy.confirmation, "confirmed")
        self.assertEqual(legacy.effective_mapping(DATA_KEY), legacy.effective_mapping(LOGS_KEY))
        self.assertEqual(legacy.effective_mapping(DATA_KEY), (1000, 1000))

    def test_per_volume_request_mappings_and_legacy_fallback_are_separate(self):
        request = {
            "schema_version": 1,
            "mode": "map",
            "mappings": {DATA_KEY: "1000:1000", LOGS_KEY: "2000:2000"},
            "confirmation": "confirmed",
        }
        mapped = resolve_restore_ownership(request=request, volume_scopes=[scope(), scope(LOGS_KEY)])
        self.assertEqual(mapped.source, "request")
        self.assertEqual(mapped.effective_mapping(DATA_KEY), (1000, 1000))
        self.assertEqual(mapped.effective_mapping(LOGS_KEY), (2000, 2000))

        legacy = resolve_restore_ownership(request=None, legacy_chown="1000:1000", volume_scopes=[scope(), scope(LOGS_KEY)])
        self.assertEqual(legacy.source, "RESTORE_CHOWN")
        self.assertEqual(legacy.effective_mapping(DATA_KEY), legacy.effective_mapping(LOGS_KEY))
        self.assertEqual(legacy.effective_mapping(DATA_KEY), (1000, 1000))

    def test_target_defaults_confirmation_is_not_reused_and_models_keep_policy_only(self):
        resolved = resolve_restore_ownership(target_defaults=map_request(), volume_scopes=[scope()])
        self.assertIsNone(resolved.confirmation)
        with self.assertRaisesRegex(RestoreOwnershipValidationError, "confirmation"):
            resolved.require_confirmation()
        policy = RestoreOwnershipPolicy()
        self.assertIs(RestoreConfig("/restore", restore_ownership=policy).restore_ownership, policy)
        self.assertFalse(hasattr(RestoreResult(timestamp=None, duration=0, success=True), "restore_ownership"))
        self.assertTrue(hasattr(RestoreOwnershipPort, "resolve_policy"))
        self.assertFalse(hasattr(RestoreOwnershipPort, "plan"))


if __name__ == "__main__":
    unittest.main()
