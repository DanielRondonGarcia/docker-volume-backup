# Vaultline Product Rebrand

## Objective

Replace the visible product brand "Docker Volume Backup" with "Vaultline" so the product presentation reflects Docker, Kubernetes, and native filesystem backup support.

## Accepted direction

- New visible brand: **Vaultline**.
- First stage is presentation-only: update user-facing product name and scope wording, while retaining deployment compatibility identifiers.
- The public GitHub repository slug, GHCR/Docker Hub image names, environment-variable names/values, Docker/Kubernetes labels and resource names, service unit filename, compose project/network names, and config/state paths stay unchanged in this change.
- No repository rename, registry migration, package transfer, or external account action is included.

## Scope

- Update README title and introductory product description to cover Docker, Kubernetes, and native filesystems.
- Update Control Plane browser title, loader, sidebar, login page, and change-password branding; adjust visible initials from `DV` to `VL`.
- Update product-prose overview in the Control Plane spec, worker demo file descriptions, and systemd `Description` only.
- Keep legacy image references, links, commands, labels, IDs, and paths where they serve compatibility or installation.

## Non-goals

- Rename the GitHub repository or change git remotes.
- Rename published images, GHCR packages, Docker Hub tags, compose project/network names, environment contracts, persistent labels, Kubernetes names, or on-disk paths.
- Rename Python package/module directories or API routes.
- Change backup behavior or architecture.

## Verification

- Inspect all intended UI/README/service/demo display strings for consistent `Vaultline` branding and generic source coverage.
- Confirm compatibility identifiers remain exactly unchanged in executable examples and deployment/runtime contracts.
- Run `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_native_filesystem_targets tests.test_ui_states` and `git diff --check` for changed files. This is a presentation-only change; no full suite unless exploration finds applicable behavioral tests.
- A read-only search found no tests asserting the old product-brand strings; existing UI-related coverage is in `tests/test_native_filesystem_targets.py` and `tests/test_ui_states.py`.

## Allowed edit surfaces

- `README.md`
- `src/control_plane/ui/index.html`
- `src/control_plane/ui/login.html`
- `src/control_plane/ui/change-password.html`
- `doc/control-plane-spec.md`
- `deploy/worker/docker-compose.yml`
- `deploy/worker/docker-compose.ghcr.yml`
- `deploy/worker/native/docker-volume-backup-worker.service`

## Worktree safety

- Do not commit without explicit user authorization. No push, PR, or merge is authorized.
- Preserve already-existing modifications in `.atl/`, `__pycache__`, `.codegraph/`, `.playwright-mcp/`, and the post-commit `odd/tasks/native-filesystem-backup.md` evidence update.

## Tasks

### VBR-1 — Apply Vaultline presentation branding

- Status: in progress (implementation and checks complete; explicit authorization received for one local commit).
- Route: delegated bounded writer; update only user-facing brand and scope wording.
- Acceptance: README and visible UI identify Vaultline; login/password pages use VL initials; product prose names Docker, Kubernetes, and native filesystem support; compatibility identifiers stay unchanged.
- Evidence: updated the eight allowed presentation surfaces; no runtime contracts were changed.

### VBR-2 — Verify branding and compatibility boundary

- Status: done.
- Route: delegated read-only verification.
- Acceptance: no unintended old product branding remains in the approved display surfaces; repo/image/label/path contracts remain unchanged; focused checks and whitespace validation pass.
- Checks: `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_native_filesystem_targets tests.test_ui_states` passed 80 tests; `git diff --check` passed with only LF-to-CRLF warnings. Independent diff inspection confirmed only the allowed copy changes and an empty index.

## Progress and evidence

- User selected **Vaultline** for visible product branding and accepted the presentation-only first stage; repo/image slugs, labels, environment/runtime identifiers, compose/Kubernetes names, and config/state paths remain unchanged.
- The current branch contains the previous filesystem feature commits. Existing `.atl/`, generated `__pycache__`, `.codegraph/`, `.playwright-mcp/`, and the prior feature's ODD evidence update were preserved.
- The user explicitly authorized one local commit limited to the eight presentation surfaces and this feature task record; all unrelated/pre-existing paths remain excluded.

## Next step

Create the authorized local Vaultline presentation-rebrand commit from only the allowed surfaces and this task record. Do not rename repository/image identifiers or push, open a PR, or merge.