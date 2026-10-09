# Native Filesystem Backup

## Objective

Deliver an end-to-end native filesystem backup workflow for Linux and Windows, managed through the existing Control Plane and executed by a native worker daemon installed on each source host.

## Problem and rationale

The product currently models sources as Docker volumes or Kubernetes PVCs. The Control Plane dispatches jobs to workers; the Control Plane itself does not read remote host filesystems. The backup engine is also coupled to Docker in its entrypoint and receives POSIX `/backup` paths. Users need to select host files/directories and back them up without Docker.

## Accepted architecture

- The Control Plane remains the scheduler/API/UI.
- A native worker/daemon runs on every source host, including the Control Plane host if its local filesystem is a source. That worker performs local filesystem I/O with the installer's configured OS access; remote host roots are never read directly by the Control Plane.
- Filesystem sources are explicit paths configured for a target. Do not automatically back up the entire root filesystem.
- Reuse the existing backup/storage engine where practical; preserve Docker and Kubernetes flows.
- Native filesystem support does not promise application-consistent snapshots in this MVP. Consistency hooks/snapshots remain a later extension.
- The initial UI may accept paths explicitly rather than recursively crawling the host filesystem. Confirm against existing UI patterns during implementation; return a product question to the parent if a browser is essential.

## Scope

- Add a filesystem source type and persist configured paths in Control Plane targets.
- Expose native filesystem target configuration in Control Plane API and UI.
- Execute backup, snapshot operations supported by the current engine, dry-run restore, and restore through a native Linux/Windows worker runtime.
- Provide a CLI entrypoint for native worker setup/diagnostics and long-running daemon mode; document platform service integration.
- Add focused tests and user-facing docs alongside behavior.

## Non-goals

- Rewriting the Control Plane or the entire product in another language.
- Direct Control Plane access to remote host roots.
- Automatic whole-disk/root backup by default.
- VSS/LVM/Btrfs application-consistent snapshots, database-specific connectors, or arbitrary remote shell execution.
- Replacing existing Docker or Kubernetes target behavior.

## Constraints and verification

- Treat Linux and Windows filesystem paths distinctly; do not normalize Windows drive/UNC paths into POSIX `/backup` paths.
- Keep the native worker on the existing enrollment, heartbeat, job-lease, progress, and result-reporting lifecycle where applicable.
- Native execution must not invoke Docker or Kubernetes. Missing external backup/storage executables must be reported clearly by a local diagnostic/self-check; no third-party executable is silently downloaded.
- Existing local modifications in `.atl/`, tracked `__pycache__`, `.codegraph/`, and `.playwright-mcp/` predate this feature and must remain untouched and excluded from feature commits.
- Full test command: `python -m unittest discover tests` (from `.github/workflows/ci.yml:22-23`). Run focused tests per task first. Report any platform checks unavailable in this environment honestly.
- The NFS work units were authorized and committed locally; any later push or release evidence belongs to the relevant delivery record, not this task.
- Forecast: approximately 1,900 authored changed lines across the full feature, revised from the initial estimate using observed NFS-1/2/3 diffs and the remaining CLI/service work; keep work units independently reviewable. Delivery strategy: `ask-on-risk`. User-selected chain strategy for any future PR: `feature-branch-chain`.

## Tasks

### NFS-1 — Add filesystem target contract and Control Plane configuration

- Status: done.
- Route: delegated direct; multi-file Control Plane/API/UI change.
- Scope: target model, SQLite persistence, create/update/dispatch contracts, and explicit path configuration in the existing target UI.
- Acceptance: create/read/update a native filesystem target with one or more explicit paths; existing Docker/Kubernetes target behavior remains unchanged.
- Checks: focused Control Plane target/dispatch tests passed; full-suite check remains scheduled for feature close.
- Evidence: commit `ffdd560` (`feat(control-plane): add native filesystem target configuration`). `test_native_filesystem_targets.py` passed 7 tests; 4 targeted Docker/Kubernetes regression tests passed. Native targets reject cold mode; edits preserve existing Kubernetes target metadata.

### NFS-2 — Execute native filesystem backups

- Status: done.
- Route: delegated direct; multi-file worker/runtime/engine change.
- Scope: native runtime selection, local backup execution over configured paths, progress/cancellation/result handling, and relevant snapshot/retention operations already supported by the product.
- Acceptance: native worker backs up configured Linux/Windows path sources without Docker; existing storage strategies remain usable when their required executables are installed.
- Checks: 7 native runtime tests, 9 worker integration tests, and 1 backup-source preservation test passed; full-suite check remains scheduled for feature close.
- Evidence: commit `388ab98` (`feat(worker): add native filesystem backup runtime`). Native jobs preserve JSON-encoded paths and spaces, report missing executables, support progress/cancellation/timeouts, and fail closed for restore pending NFS-3.

### NFS-3 — Restore through the native worker

- Status: done.
- Route: delegated direct; multi-file restore/runtime change.
- Scope: native dry-run and restore destinations, platform-aware path validation, and safe overwrite behavior; preserve container stop/start only for Docker targets.
- Acceptance: a native target can preview and restore a selected snapshot to a configured host destination; Docker and Kubernetes restore paths remain unchanged.
- Checks: 11 native restore tests, 16 backup integrity tests (1 skipped), and 86 Control Plane dispatch tests passed; full-suite check remains scheduled for feature close.
- Evidence: commit `c72bb54` (`feat(restore): support native filesystem restores`). Native restore requires explicit host destination, preserves dry-run/overwrite, avoids container stop/start, and treats Windows ownership mapping as unsupported/reportable.

### NFS-4 — Add CLI/daemon operations and platform guidance

- Status: done.
- Evidence: commit `81e9805` (`feat(worker): add native worker CLI and service guidance`); CLI, tests, systemd unit, and quickstart guidance were committed as one work unit.
- Route: delegated direct; multi-file CLI, service integration, tests, and docs.
- Scope: CLI for native worker diagnostics and daemon mode, Linux and Windows service launch/install guidance, configuration/enrollment documentation, and dependency self-check guidance.
- Decision: use a systemd unit on Linux and a Windows Task Scheduler startup task; explicitly document that the latter is not a Windows SCM service. Avoid adding an unrequested third-party service-wrapper dependency.
- Acceptance: an operator can install/configure and run a native worker daemon on Linux and Windows using the documented CLI and OS task/service manager path; diagnostics never print secrets.
- Checks: 3 CLI tests passed; diff whitespace check passed. Systemd execution could not be validated because `systemd-analyze` is unavailable; Windows Task Scheduler syntax was checked against official parameter sets but not executed on this host. Full suite passed in NFS-5.

### NFS-5 — Fix test isolation and run feature-level close checks

- Status: done.
- Evidence: commit `0777a33` (`test: restore feature flag environment after tests`); includes the test isolation correction and ODD record.
- Route: inline, one-file test-environment cleanup discovered by the full-suite run; focused and full-suite verification delegated.
- Scope: ensure environment-mutating tests restore absent variables, rerun the full suite, and report Linux/Windows service validation limits accurately.
- Acceptance: full unittest discovery passes without cross-test environment leakage; any platform-specific execution unavailable on this host is recorded as pending.
- Checks: focused interaction tests passed (3); full suite passed (452 run, 1 skipped, 0 failures); `systemd-analyze` unavailable; Windows runtime/service execution was not available on this host.

## Progress and evidence

- Read-only exploration confirmed `RuntimePort` exists and current workers support Docker/Kubernetes; the Control Plane schedules work to workers.
- Read-only exploration found the backup engine invokes external `restic`, `tar`, `gpg`, `rclone`, `aws`, and `ssh/scp` commands and currently assumes container/POSIX paths in several entrypoints. Native execution must make path handling OS-aware and diagnose missing tools.
- User authorized a full vertical delivery and selected files/folders as the native MVP source.
- Created `feat/native-filesystem-backup` from `master` with existing local changes preserved.
- NFS-1 implementation adds persisted `filesystem_paths`, native target create/update/dispatch, and explicit paths in the Control Plane UI/spec. Native targets are hot-only; Kubernetes edit submissions retain runtime metadata.
- NFS-1 commit: `ffdd560` (`feat(control-plane): add native filesystem target configuration`). Only its code, tests, spec, and ODD task file were committed; existing dirty `.atl/`, `__pycache__`, `.codegraph/`, and `.playwright-mcp/` paths were excluded.
- NFS-2 implementation adds native worker selection and a local process RuntimePort; backup sources use JSON so spaces and Windows path spelling are preserved. The host backup engine avoids Docker for native mode; missing external tools are reported, cancellation/progress/timeouts are handled, and native restore fails closed pending NFS-3.
- NFS-2 TDD evidence: native runtime tests first failed because the adapter was absent; timeout tests later failed before bounded timeout/cleanup was implemented. Final focused checks passed: `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_native_runtime.py'` (7 tests), `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_worker_runtime_integration.py'` (9 tests), and `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_backup_integrity.BackupIntegrityTests.test_native_runtime_main_consumes_backup_sources_json_without_splitting_paths` (1 test).
- NFS-1 verification: `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_native_filesystem_targets.py'` passed (7 tests); four targeted existing Docker/Kubernetes dispatch/route tests passed. One earlier attempt used incorrect test class names and failed; the corrected command passed. Tests emitted the existing `datetime.utcnow()` deprecation warning. `git diff --cached --check` passed before NFS-1 commit.
- `gentle_review assess` was unassessable because the worktree contains untracked paths; RDD is off, so a separate verifier ran for each task and confirmed the final focused results and diff behavior.
- NFS-2 commit: `388ab98` (`feat(worker): add native filesystem backup runtime`). It contains the native runtime, backup engine integration, focused tests, specification update, and ODD task file; pre-existing dirty paths were excluded. The cohesive native execution unit is 602 authored changed lines; the selected feature-branch chain strategy applies if the user later requests a PR.
- NFS-2 verification: 7 native runtime tests, 9 worker integration tests, and 1 backup-source path preservation test passed. Independent verification confirmed timeout/cancellation status handling, JSON path preservation, no Docker/Kubernetes runtime selection, and native restore fail-closed. `git diff --cached --check` passed before commit.
- NFS-3 adds native restore destination handling, dry-run/overwrite semantics, worker-side restore, no container stop/start, and explicit Windows ownership limitations. Windows path containment comparisons casefold drive/UNC paths but preserve Linux case sensitivity.
- NFS-3 TDD evidence: `test_native_restore.py` initially failed before restore support and later failed four path-overlap cases before the flavor-aware correction. Final checks passed: `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_native_restore.py'` (11 tests), `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_backup_integrity.py'` (16 tests, 1 skipped), and `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_control_plane_dispatch.py'` (86 tests).
- Independent verification confirmed Windows drive/UNC case-insensitive overlap, Linux case-sensitive behavior, dry-run/overwrite/no-stop semantics, no `/backup` rewriting, and Docker/Kubernetes regressions.
- NFS-3 commit: `c72bb54` (`feat(restore): support native filesystem restores`). It contains the native restore flow, tests, specification update, and ODD task file; pre-existing dirty paths were excluded. This cohesive restore unit is 517 authored changed lines.
- NFS-3 verification: 11 native restore tests, 16 backup integrity tests (1 skipped), and 86 Control Plane dispatch tests passed. Independent verification confirmed Windows drive/UNC case-insensitive containment, Linux case-sensitive behavior, dry-run/overwrite/no-stop semantics, no `/backup` rewriting, and Docker/Kubernetes regressions. `git diff --cached --check` passed before commit.
- `gentle_review assess` was unassessable because the worktree contains untracked paths; RDD is off, so a separate verifier ran for completed work units. Pre-existing `.atl/`, `__pycache__`, `.codegraph/`, and `.playwright-mcp/` changes remain excluded.
- NFS-4 exploration found the existing worker already has a native runtime selector, foreground loop, health endpoint, credential storage, and executable self-check, but no service-manager wrapper or packaging entrypoint. The bounded implementation uses a stdlib CLI wrapper, Linux systemd unit, and Windows Task Scheduler guidance; it does not present a scheduled task as a Windows SCM service.
- NFS-4 implementation adds a redacted native self-check and foreground daemon CLI, a systemd unit example, and Spanish guidance for credentials and OS runners. Commit `81e9805` (`feat(worker): add native worker CLI and service guidance`) contains these files. Its 3 focused CLI tests and the final post-correction rerun passed. PowerShell cmdlet syntax was checked against Microsoft's current `Register-ScheduledTask` and `New-ScheduledTaskAction` reference; actual Windows execution remains unavailable.
- The full suite ran 452 tests with one skipped and one failure: `FeatureFlagEnvTests.test_from_env_parses_values` left `SNAPSHOT_EXPLORER_NO_LOCK=true` in `os.environ` when the key was initially absent; a later native runtime test then received `--no-lock`. Both native-runtime-only and failing-test-isolated commands pass, confirming a test-order leak rather than a native runtime behavior regression.
- NFS-5 fix removes keys that were absent before `FeatureFlagEnvTests.test_from_env_parses_values` in its `finally` cleanup. RED: full suite previously ran 452 tests with one skip and one failure from leaked `SNAPSHOT_EXPLORER_NO_LOCK`; GREEN: the focused interaction tests passed 3/3 and full discovery passed 452 tests (1 skipped, 0 failures). Commit `0777a33` (`test: restore feature flag environment after tests`) contains the fix and task record.
- Feature-wide close checks are complete except platform executions unavailable on this host: `systemd-analyze` is not installed and Windows Task Scheduler/worker execution was not run. Microsoft Learn parameter references were reviewed for Task Scheduler syntax.

## Next step

Implementation is complete in local commits `81e9805` and `0777a33`; the ODD task record documents both identities. The published native-worker release is recorded separately in `odd/tasks/native-worker-installers.md`.
