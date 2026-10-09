from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.app.domain.restore_metadata import ResticMetadataError, parse_restic_ls_long
from src.app.domain.restore_ownership import RestoreOwnershipPolicy
from src.app.infrastructure.adapters.restic_metadata import ResticMetadataInspectorAdapter


def line(mode="-rw-r--r--", uid=0, gid=0, size=12, path="/data/file"):
    return f"{mode} {uid} {gid} {size} 2026-01-02 03:04:05 {path}"


class RestoreMetadataTests(unittest.TestCase):
    def test_valid_records_mixed_owners_and_file_fields(self):
        evidence = parse_restic_ls_long(
            "\n".join((line(), line(uid=1000, gid=1000, path="/data/n8n"))), snapshot="snap"
        )
        self.assertEqual(
            (evidence.files[0].mode, evidence.files[1].uid, evidence.files[1].gid, evidence.files[1].size),
            ("-rw-r--r--", 1000, 1000, 12),
        )
        self.assertEqual(evidence.files[0].mtime.isoformat(), "2026-01-02T03:04:05")
        self.assertEqual(evidence.files[0].owner_label, "file_owner")
        self.assertEqual(evidence.ownership_classification, "mixed")

    def test_malformed_or_incomplete_output_fails_closed(self):
        for output in ("", "not a restic record", line().replace(" 12 ", " ")):
            with self.subTest(output=output), self.assertRaisesRegex(ResticMetadataError, "inspection"):
                parse_restic_ls_long(output)

    def test_bounds_creator_labels_and_no_mapping(self):
        with self.assertRaisesRegex(ResticMetadataError, "bounded"):
            parse_restic_ls_long(line() * 2, max_output_bytes=40)
        evidence = parse_restic_ls_long(line(uid=1000, gid=1000), creator=(0, 0))
        self.assertEqual(
            (evidence.creator_owner, evidence.backup_creator_label, evidence.inferred_mapping, evidence.restored_metadata_proven),
            ("0:0", "backup_creator", None, False),
        )

    def test_adapter_uses_argv_only_preserves_by_default_and_handles_failure(self):
        runner = Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=line(),
                stderr="warning: non-root metadata was not applied",
            )
        )
        result = ResticMetadataInspectorAdapter(runner=runner).inspect(
            "snap-1", SimpleNamespace(restic_repository="local:/repo", restic_password="secret")
        )
        self.assertTrue(result.success)
        self.assertEqual(result.policy.mode, "preserve")
        args, kwargs = runner.call_args
        self.assertEqual(args[0], ["restic", "ls", "--long", "snap-1"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["RESTIC_REPOSITORY"], "local:/repo")
        runner.return_value = SimpleNamespace(returncode=1, stdout="", stderr="private detail")
        failed = ResticMetadataInspectorAdapter(runner=runner).inspect("snap-1")
        self.assertFalse(failed.success)
        self.assertEqual(failed.category, "restic_metadata_inspection_failed")

    def test_unconfirmed_mapping_fails_before_restic(self):
        runner = Mock()
        result = ResticMetadataInspectorAdapter(runner=runner).inspect(
            "snap", policy=RestoreOwnershipPolicy(mode="map", mappings={"compose:app:data": "1000:1000"})
        )
        self.assertEqual(result.category, "confirmation_required")
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
