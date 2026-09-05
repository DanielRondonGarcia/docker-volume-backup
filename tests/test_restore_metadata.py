from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.app.domain.restore_metadata import ResticMetadataError, parse_restic_ls_long
from src.app.domain.restore_ownership import RestoreOwnershipPolicy
from src.app.infrastructure.adapters.restic_metadata import ResticMetadataInspectorAdapter


def line(mode="-rw-r--r--", uid=0, gid=0, size=12, path="/data/file"):
    return f"{mode} {uid} {gid} {size} 2026-01-02 03:04:05 {path}"


def test_valid_records_mixed_owners_and_file_fields():
    evidence = parse_restic_ls_long("\n".join((line(), line(uid=1000, gid=1000, path="/data/n8n"))), snapshot="snap")
    assert (evidence.files[0].mode, evidence.files[1].uid, evidence.files[1].gid, evidence.files[1].size) == ("-rw-r--r--", 1000, 1000, 12)
    assert evidence.files[0].mtime.isoformat() == "2026-01-02T03:04:05" and evidence.files[0].owner_label == "file_owner"
    assert evidence.ownership_classification == "mixed"


@pytest.mark.parametrize("output", ["", "not a restic record", line().replace(" 12 ", " ")])
def test_malformed_or_incomplete_output_fails_closed(output):
    with pytest.raises(ResticMetadataError, match="inspection"):
        parse_restic_ls_long(output)


def test_bounds_creator_labels_and_no_mapping():
    with pytest.raises(ResticMetadataError, match="bounded"):
        parse_restic_ls_long(line() * 2, max_output_bytes=40)
    evidence = parse_restic_ls_long(line(uid=1000, gid=1000), creator=(0, 0))
    assert (evidence.creator_owner, evidence.backup_creator_label, evidence.inferred_mapping, evidence.restored_metadata_proven) == ("0:0", "backup_creator", None, False)


def test_adapter_uses_argv_only_preserves_by_default_and_handles_failure():
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=line(), stderr="warning: non-root metadata was not applied"))
    result = ResticMetadataInspectorAdapter(runner=runner).inspect("snap-1", SimpleNamespace(restic_repository="local:/repo", restic_password="secret"))
    assert result.success and result.policy.mode == "preserve"
    args, kwargs = runner.call_args
    assert args[0] == ["restic", "ls", "--long", "snap-1"] and "shell" not in kwargs and kwargs["env"]["RESTIC_REPOSITORY"] == "local:/repo"
    runner.return_value = SimpleNamespace(returncode=1, stdout="", stderr="private detail")
    failed = ResticMetadataInspectorAdapter(runner=runner).inspect("snap-1")
    assert not failed.success and failed.category == "restic_metadata_inspection_failed"


def test_unconfirmed_mapping_fails_before_restic():
    runner = Mock()
    result = ResticMetadataInspectorAdapter(runner=runner).inspect("snap", policy=RestoreOwnershipPolicy(mode="map", mappings={"compose:app:data": "1000:1000"}))
    assert result.category == "confirmation_required"
    runner.assert_not_called()
