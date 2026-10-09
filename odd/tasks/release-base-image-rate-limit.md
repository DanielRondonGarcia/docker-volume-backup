# Release Base Image Rate Limit

## Goal

Make release image builds resilient to Docker Hub's anonymous pull-rate limit without changing the application runtime base image or requiring new credentials.

## Problem

The release workflow's image-build job pulls `python:3.11-slim-bookworm` anonymously from Docker Hub. Release workflow runs `37990158351` and `37990563990` both failed with HTTP 429 while resolving that manifest, so the image job failed and the publish job was skipped.

## Evidence and decision

- Docker documents a 100-pull per six-hour limit for unauthenticated users: https://docs.docker.com/docker-hub/usage/pulls/.
- Amazon ECR Public Gallery lists Docker's verified `docker/library/python` official image and the `3.11-slim-bookworm` tag: https://gallery.ecr.aws/docker/library/python.
- AWS documents public ECR pulls and quotas (including unauthenticated usage): https://docs.aws.amazon.com/AmazonECR/latest/public/docker-pull-ecr-image.html and https://docs.aws.amazon.com/AmazonECR/latest/public/public-service-quotas.html.
- Decision: parameterize the Dockerfile base image with the Docker Hub reference as its local/default value; pass the official ECR Public mirror explicitly to all release image builds. This avoids Docker Hub for CI release image builds without adding required credentials or altering the image contents intentionally.
- ECR Public has its own unauthenticated service limits, so this removes dependence on the shared Docker Hub anonymous quota but does not claim unlimited registry capacity.

## Scope

- Add a `PYTHON_BASE_IMAGE` Docker build argument before the first `FROM`.
- Pass `public.ecr.aws/docker/library/python:3.11-slim-bookworm` to each release image build target.
- Add regression coverage for Dockerfile parameterization and release workflow arguments.
- Preserve multi-architecture build/publish targets, output tags, local Docker Hub default behavior, and release workflow dependencies.

## Constraints

- Do not inspect or use ambient Docker Hub credentials, authenticated sessions, or local registry state.
- Do not add or require repository secrets or external AWS credentials.
- Do not change application/runtime dependencies or the selected Python/Debian version.
- Preserve unrelated `.atl/`, `.codegraph/`, `.playwright-mcp/`, and `__pycache__/` workspace artifacts.

## Tasks

### RBR-1 — Use official ECR Public Python mirror for release builds

- Status: complete.
- Route: delegated direct implementation; Dockerfile, workflow, regression test, and task record are one work unit.
- Scope: Docker base-image parameter, release build args, and workflow assertions.
- Acceptance: every release image target uses the ECR Public Docker Official Python tag; local builds retain the existing Docker Hub default; workflow regression tests fail if any target omits the mirror.
- Evidence: focused `test_release_asset_workflow` passed 9 tests. The Dockerfile preserves the Docker Hub local default; regression coverage confirms all three workflow image targets use the ECR mirror and retain existing version/worker args.

### RBR-2 — Verify, commit, and publish the corrective release

- Status: in progress.
- Route: independent verification then parent commit/push/release.
- Scope: release workflow tests, full suite, diff check, and stable patch release.
- Acceptance: release image-build and publish-release jobs succeed; the corrected stable release and multi-architecture images are verified.
- Evidence so far: independent verification passed 9 focused release workflow tests and 523 full tests (2 skipped); `git diff --check` passed. Parent spot-check reran the 9 focused tests successfully. No remote image build has yet validated ECR availability.

## Authorized edit surfaces

- `odd/tasks/release-base-image-rate-limit.md`
- `Dockerfile`
- `.github/workflows/release-dispatch.yml`
- `tests/test_release_asset_workflow.py`

## Checks

- `PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.test_release_asset_workflow`.
- `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'`.
- `git diff --check`.

## Progress

- Root cause verified from release logs: Docker Hub returned 429 on `python:3.11-slim-bookworm` manifest metadata resolution.
- Official ECR Public Gallery confirms the Docker Official Python 3.11 slim Bookworm mirror/tag exists.
- `Dockerfile` now defaults `PYTHON_BASE_IMAGE` to `python:3.11-slim-bookworm` and uses it for `app-base`; all three release image targets explicitly pass the official ECR Public mirror while preserving their existing version and worker Docker CLI arguments.
- Regression coverage checks the local Docker Hub default and requires the ECR mirror argument under each target's `build-args`, while checking `APP_VERSION` and the worker's `INSTALL_DOCKER_CLI=true` remain present.
- TDD evidence: before implementation, the focused command ran 9 tests and failed only the new regression test because the Dockerfile lacked the parameterized default; after implementation, the focused command ran 9 tests and passed (`OK`).
- Independent verification and parent spot-check: focused release workflow suite passed 9 tests; full discovery passed 523 tests with 2 skips; `git diff --check` reported no whitespace errors (Git emitted line-ending conversion warnings).
- No remote image build or release workflow has yet validated ECR registry availability. ECR Public has its own unauthenticated quotas.

## Next step

Commit the four authorized candidate paths, push to `master`, and run the stable patch release workflow. Verify the image-build and publish-release jobs before reporting the version.
