# Release Base Image Rate Limit

## Goal

Make release image builds resilient to Docker Hub's anonymous pull-rate limit without changing the application runtime base image or requiring new credentials.

## Problem

The release workflow initially pulled `python:3.11-slim-bookworm` anonymously from Docker Hub. After moving that base to ECR Public, run `37993803231` showed `docker/setup-buildx-action` still booted its builder from `moby/buildkit:buildx-stable-1` on Docker Hub; the Docker Hub token request timed out and the image-build job failed before builds began.

## Evidence and decision

- Docker documents a 100-pull per six-hour limit for unauthenticated users: https://docs.docker.com/docker-hub/usage/pulls/.
- Amazon ECR Public Gallery lists Docker's verified `docker/library/python` official image and the `3.11-slim-bookworm` tag: https://gallery.ecr.aws/docker/library/python.
- Amazon ECR Public Gallery lists `public.ecr.aws/vend/moby/buildkit:buildx-stable-1` as a recent BuildKit mirror: https://gallery.ecr.aws/vend/moby/buildkit.
- `docker/setup-buildx-action` supports its `driver-opts` input for selecting the BuildKit image: https://github.com/docker/setup-buildx-action.
- AWS documents public ECR pulls and quotas (including unauthenticated usage): https://docs.aws.amazon.com/AmazonECR/latest/public/docker-pull-ecr-image.html and https://docs.aws.amazon.com/AmazonECR/latest/public/public-service-quotas.html.
- Decision: parameterize the Dockerfile base image with the Docker Hub reference as its local/default value; pass the official ECR Public Python mirror to all release image builds and configure Buildx to boot from the ECR Public BuildKit mirror. This avoids Docker Hub for CI release image builds without adding required credentials or changing local defaults.
- ECR Public has its own unauthenticated service limits, so this removes dependence on the shared Docker Hub anonymous quota but does not claim unlimited registry capacity.

## Scope

- Add a `PYTHON_BASE_IMAGE` Docker build argument before the first `FROM`.
- Pass `public.ecr.aws/docker/library/python:3.11-slim-bookworm` to each release image build target.
- Set the Buildx driver image to `public.ecr.aws/vend/moby/buildkit:buildx-stable-1`.
- Add regression coverage for Dockerfile parameterization and release workflow arguments.
- Preserve multi-architecture build/publish targets, output tags, local Docker Hub default behavior, and release workflow dependencies.

## Constraints

- Do not inspect or use ambient Docker Hub credentials, authenticated sessions, or local registry state.
- Do not add or require repository secrets or external AWS credentials.
- Do not change application/runtime dependencies or the selected Python/Debian version.
- Preserve unrelated `.atl/`, `.codegraph/`, `.playwright-mcp/`, and `__pycache__/` workspace artifacts.

## Tasks

### RBR-1 — Use official ECR Public Python base for release builds

- Status: complete.
- Route: delegated direct implementation; Dockerfile, workflow, regression test, and task record were one work unit.
- Scope: Docker base-image parameter, release build args, and workflow assertions.
- Acceptance: all three release image targets use the ECR Public Docker Official Python tag; local builds retain the existing Docker Hub default.
- Commit: `66a85a0 fix(release): avoid Docker Hub base image throttling`.
- Evidence: focused `test_release_asset_workflow` passed 9 tests and all release image targets received the ECR Python base argument.

### RBR-2 — Verify, commit, and publish the corrective release

- Status: in progress.
- Route: independent verification then parent commit/push/release.
- Scope: Buildx bootstrap mirror, workflow regression test, full suite, diff check, and stable patch release.
- Acceptance: release image-build and publish-release jobs succeed; the corrected stable release and multi-architecture images are verified.
- Evidence so far: independent verification passed 10 focused release workflow tests and 524 full tests (2 skipped); `git diff --check` passed. The Dockerfile base parameter is already committed in RBR-1; this follow-up only changes Buildx's builder image, its regression test, and this task record. No remote Buildx bootstrap/image build has yet validated the ECR mirrors.

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

- Root causes verified from release logs: Docker Hub returned 429 on `python:3.11-slim-bookworm` manifest resolution (runs `37990158351`, `37990563990`); after redirecting that base to ECR Public, run `37993803231` failed earlier because Buildx itself still tried to pull `moby/buildkit:buildx-stable-1` from Docker Hub and timed out fetching the Docker Hub auth token.
- Official ECR Public Gallery confirms the Docker Official Python 3.11 slim Bookworm mirror/tag exists.
- ECR Public Gallery lists the `buildx-stable-1` BuildKit mirror at `public.ecr.aws/vend/moby/buildkit`; the release setup-buildx step uses `public.ecr.aws/vend/moby/buildkit:buildx-stable-1` through `driver-opts`.
- `Dockerfile` defaults `PYTHON_BASE_IMAGE` to `python:3.11-slim-bookworm` for local builds; all release image targets pass the official ECR Public Python mirror.
- Regression coverage checks the local Docker Hub default, all three base-image build args, and Buildx's ECR builder image while retaining `APP_VERSION` and worker `INSTALL_DOCKER_CLI=true`.
- TDD evidence for the Python base change: before implementation, the focused command ran 9 tests and failed only the new regression test because the Dockerfile lacked the parameterized default; after implementation, the focused command ran 9 tests and passed (`OK`). For the Buildx follow-up, the focused command first failed because the setup step lacked ECR `driver-opts`; after configuring the mirror, the final focused suite passed 10 tests (`OK`).
- Independent verification: focused release workflow suite passed 10 tests; full discovery passed 524 tests with 2 skips; `git diff --check` reported no whitespace errors (Git emitted line-ending conversion warnings).
- No remote Buildx bootstrap/image build or corrected release workflow has yet validated ECR availability. ECR Public has its own unauthenticated quotas.
- Completion of RBR-2 remains pending the actual corrected release workflow and successful image/publish jobs; this implementation does not establish remote release success.

## Next step

Commit the Buildx ECR mirror follow-up and regression test, push to `master`, and run the stable patch release workflow. Verify Buildx bootstrap, image-build, and publish-release before reporting the version.
