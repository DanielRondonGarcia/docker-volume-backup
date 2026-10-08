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
- No push, PR, or merge is authorized. Do not commit without explicit user authorization.
- Forecast: approximately 1,000 authored changed lines across the full feature; keep work units independently reviewable. Delivery strategy: `ask-on-risk`. User-selected chain strategy for any future PR: `feature-branch-chain`. No push or PR is authorized.

## Tasks

### NFS-1 — Add filesystem target contract and Control Plane configuration

- Status: done.
- Route: delegated direct; multi-file Control Plane/API/UI change.
- Scope: target model, SQLite persistence, create/update/dispatch contracts, and explicit path configuration in the existing target UI.
- Acceptance: create/read/update a native filesystem target with one or more explicit paths; existing Docker/Kubernetes target behavior remains unchanged.
- Checks: focused Control Plane target/dispatch tests passed; full-suite check remains scheduled for feature close.
- Evidence: commit `ffdd560` (`feat(control-plane): add native filesystem target configuration`). `test_native_filesystem_targets.py` passed 7 tests; 4 targeted Docker/Kubernetes regression tests passed. Native targets reject cold mode; edits preserve existing Kubernetes target metadata.

### NFS-2 — Execute native filesystem backups

- Status: in progress; implementation and focused checks complete, authorized work-unit commit pending.
- Route: delegated direct; multi-file worker/runtime/engine change.
- Scope: native runtime selection, local backup execution over configured paths, progress/cancellation/result handling, and relevant snapshot/retention operations already supported by the product.
- Acceptance: native worker backs up configured Linux/Windows path sources without Docker; existing storage strategies remain usable when their required executables are installed.
- Checks: 7 native runtime tests, 9 worker integration tests, and 1 backup-source preservation test passed; full-suite check remains scheduled for feature close.
- Evidence: native jobs preserve JSON-encoded paths and spaces, report missing executables, support progress/cancellation/timeouts, and fail closed for restore pending NFS-3.

### NFS-3 — Restore through the native worker

- Status: pending
- Route: delegated direct; multi-file restore/runtime change.
- Scope: native dry-run and restore destinations, platform-aware path validation, and safe overwrite behavior; preserve container stop/start only for Docker targets.
- Acceptance: a native target can preview and restore a selected snapshot to a configured host destination; Docker and Kubernetes restore paths remain unchanged.
- Checks: native restore tests for Linux-style and Windows-style paths, dry-run/overwrite cases, existing restore tests, full suite at feature close.

### NFS-4 — Add CLI/daemon operations and platform guidance

- Status: pending
- Route: delegated direct; multi-file CLI, service integration, tests, and docs.
- Scope: CLI for native worker diagnostics and daemon mode, Linux and Windows service launch/install guidance, configuration/enrollment documentation, and dependency self-check guidance.
- Acceptance: an operator can install/configure and run a native worker daemon on Linux and Windows using the documented CLI/service path; no secrets are printed in diagnostics.
- Checks: CLI argument/configuration tests; Linux service-file validation and Windows-specific structural checks where runnable; full suite at feature close.

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
- NFS-2 implementation is awaiting its explicitly authorized commit. Full suite and Windows execution remain pending for feature close. Pre-existing `.atl/`, `__pycache__`, `.codegraph/`, and `.playwright-mcp/` changes remain excluded.

## Next step

Create the explicitly authorized NFS-2 work-unit commit from only the native runtime, engine, tests/docs, and ODD task file. Then record its identity, close NFS-2, update the mirror and TODO, and delegate NFS-3.
