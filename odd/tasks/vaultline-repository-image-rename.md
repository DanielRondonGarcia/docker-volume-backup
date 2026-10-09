# Vaultline Repository and Image Rename

## Goal

Align repository links, GHCR image references, deployment defaults, examples, and tests with the renamed `DanielRondonGarcia/vaultline` repository.

## User-approved decisions

- The GitHub repository has already been renamed to `https://github.com/DanielRondonGarcia/vaultline`.
- Canonical GHCR images are `ghcr.io/danielrondongarcia/vaultline`, `ghcr.io/danielrondongarcia/vaultline-control-plane`, and `ghcr.io/danielrondongarcia/vaultline-worker`.
- Cut over to the new image names only; do not add or publish legacy aliases.
- Update the existing Docker Hub-style example to use the canonical GHCR runtime image; the release workflow currently publishes GHCR only.
- Preserve stable Docker/Kubernetes labels, environment variable names, Compose/Kubernetes object and network names, native service filename, and config/state paths. Do not delete existing registry packages.
- Source changes and local work-unit commits are authorized. Pushes, release/workflow dispatches, and registry publication require separate user authorization; the user later granted that delivery authorization.

## Exploration findings

- Local `origin` now points at `https://github.com/DanielRondonGarcia/vaultline.git`; `upstream` remains `jareware/docker-volume-backup`, and the renamed repository's `master` tip matches local `7c206f1`.
- Release workflow image names derive from `${{ github.repository }}` and `${{ github.event.repository.name }}`, so they resolve to the new GHCR names after the rename.
- Runtime and deployment cutovers are complete; VRI-3 is correcting the remaining active documentation and test boundary while preserving historical release evidence.
- No Docker Hub publish workflow was found; the example now uses the canonical GHCR runtime image.

## Tasks

### VRI-1 — Update GitHub repository and release links

- Status: done.
- Scope: update Control Plane release API/UI URLs and native service `Documentation=` URLs to `DanielRondonGarcia/vaultline`; update local `origin` without changing `upstream`.
- Evidence: test-first RED/GREEN; `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_release_asset_api` passed 6 tests. Independent verification confirmed the new URLs, no old URL in the allowed paths, image snippet and runtime identifiers unchanged, and `git diff --check` clean.
- Assessment: native ASSESS returned `unassessable` because pre-existing untracked paths were not declared; RDD is off. Writer self-check and independent verification passed.
- Commit: `0579330 refactor(repository): point release links to Vaultline`.
- Local remote: `origin` now targets `https://github.com/DanielRondonGarcia/vaultline.git`; `upstream` remains unchanged.
- Acceptance: release metadata and links target the renamed repository; tests cover the new URL.

### VRI-2 — Cut over runtime and deployment image references

- Status: done.
- Scope: update runtime image defaults, worker helper fallback, GHCR Compose/Kubernetes image paths, and generated UI Compose snippets to the three canonical GHCR names; update relevant assertions.
- Evidence: test-first RED/GREEN; `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_scheduler_deployment tests.test_worker_configuration tests.test_worker_runtime_integration` passed 27 tests. Independent verifier confirmed new image names, no legacy GHCR names in scope, stable identifiers unchanged, and `git diff --check` clean (line-ending warnings only).
- Assessment: native ASSESS returned `unassessable` because preserved untracked paths were not declared; RDD is off. Writer self-check and independent verification passed.
- Commit: `a156fbb refactor(images): cut over GHCR references to Vaultline`.
- Acceptance: all runtime defaults use the intended new image for their role; existing labels, environment contracts, resource names, networks, and host paths remain unchanged.

### VRI-3 — Update user documentation and verify the rename boundary

- Status: in progress.
- Scope: update README, quickstart/deployment guides, examples, and image-reference tests; audit remaining old-name matches and document intentional compatibility identifiers. Run focused tests and the full suite.
- Acceptance: no active repository/image URL references point to the old name; remaining `docker-volume-backup` identifiers are intentionally preserved deployment contracts or historical records. Commit the work units on this feature branch and record their identities here.

## Out of scope

- Renaming or removing old GHCR packages, publishing new image tags, pushing branches, dispatching a release, or changing external GitHub settings.
- Renaming stable Docker/Kubernetes identifiers, environment keys, native service/config/state paths, or data locations.
