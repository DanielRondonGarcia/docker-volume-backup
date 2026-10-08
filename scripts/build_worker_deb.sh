#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s VERSION ARCH FROZEN_BINARY [OUTPUT_DIR]\n' "$0" >&2
  printf '  VERSION: bare semver such as 1.2.3\n' >&2
  printf '  ARCH: amd64 or arm64\n' >&2
}

if [[ $# -lt 3 || $# -gt 4 ]]; then
  usage
  exit 2
fi

VERSION="$1"
ARCH="$2"
FROZEN_BINARY="$3"
OUTPUT_DIR="${4:-dist}"

if ! [[ ${VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'error: VERSION must be a bare semver tag like 1.2.3\n' >&2
  exit 2
fi

case "${ARCH}" in
  amd64|arm64)
    ;;
  *)
    printf 'error: ARCH must be amd64 or arm64\n' >&2
    exit 2
    ;;
esac

if [[ ! -f "${FROZEN_BINARY}" || ! -x "${FROZEN_BINARY}" ]]; then
  printf 'error: FROZEN_BINARY must be an existing executable file\n' >&2
  exit 2
fi

if ! command -v dpkg-deb >/dev/null 2>&1; then
  printf 'error: dpkg-deb is required on the build host\n' >&2
  exit 127
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_ROOT="${REPO_ROOT}/packaging/debian"
PACKAGE_NAME="vaultline-worker"
ASSET_NAME="vaultline-worker_${VERSION}_${ARCH}.deb"
OUTPUT_PATH="${OUTPUT_DIR%/}/${ASSET_NAME}"
WORKDIR="$(mktemp -d)"
PACKAGE_ROOT="${WORKDIR}/${PACKAGE_NAME}"

cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

mkdir -p "${OUTPUT_DIR}"
if [[ -e "${OUTPUT_PATH}" ]]; then
  printf 'error: output already exists: %s\n' "${OUTPUT_PATH}" >&2
  exit 2
fi

install -d -m 0755 "${PACKAGE_ROOT}/DEBIAN"
install -d -m 0755 "${PACKAGE_ROOT}/usr/bin"
install -d -m 0755 "${PACKAGE_ROOT}/lib/systemd/system"
install -d -m 0755 "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker"

sed \
  -e "s/@VERSION@/${VERSION}/g" \
  -e "s/@ARCH@/${ARCH}/g" \
  "${TEMPLATE_ROOT}/control.in" > "${PACKAGE_ROOT}/DEBIAN/control"
chmod 0644 "${PACKAGE_ROOT}/DEBIAN/control"

install -m 0755 "${TEMPLATE_ROOT}/postinst" "${PACKAGE_ROOT}/DEBIAN/postinst"
install -m 0755 "${FROZEN_BINARY}" "${PACKAGE_ROOT}/usr/bin/vaultline-worker"
install -m 0644 "${TEMPLATE_ROOT}/lib/systemd/system/docker-volume-backup-worker.service" \
  "${PACKAGE_ROOT}/lib/systemd/system/docker-volume-backup-worker.service"
install -m 0644 "${TEMPLATE_ROOT}/usr/share/doc/vaultline-worker/worker.env.example" \
  "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/worker.env.example"

test -f "${PACKAGE_ROOT}/DEBIAN/control"
test -x "${PACKAGE_ROOT}/DEBIAN/postinst"
test -x "${PACKAGE_ROOT}/usr/bin/vaultline-worker"
test -f "${PACKAGE_ROOT}/lib/systemd/system/docker-volume-backup-worker.service"
test -f "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/worker.env.example"

LC_ALL=C dpkg-deb --build --root-owner-group "${PACKAGE_ROOT}" "${OUTPUT_PATH}"

test -f "${OUTPUT_PATH}"
printf '%s\n' "${OUTPUT_PATH}"
