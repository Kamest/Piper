#!/bin/sh
# piper-build.sh - compile the Piper GUI: piper/web/src/*.ts -> piper/web/dist.
#
# tsc is the whole toolchain (one ES module per .ts, no
# bundler), dist/ is COMMITTED so no host needs node, --check keeps it honest.
# xterm.js and CodeMirror 6 are vendored under piper/web/vendor/{xterm,codemirror} (VERSIONS,
# SHA256SUMS; CodeMirror is rebuilt by vendor-codemirror.sh) and resolved at runtime by the
# import map in index.html; every build verifies their checksums.
#
# Usage: piper-build.sh [--check]
# Env:   TSC_IMAGE (node:22-alpine), TSC_VERSION (5.9.3)
set -eu

SK=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WEB="$SK/piper/web"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1
TSC_IMAGE="${TSC_IMAGE:-node:22-alpine}"
TSC_VERSION="${TSC_VERSION:-5.9.3}"

# a root-owned ~/.npm (old npm bug) makes npx fail with EPERM; fall back to a temp cache
npm_cache() {
  if [ -z "${npm_config_cache:-}" ] && [ -d "$HOME/.npm" ] && [ ! -w "$HOME/.npm/_cacache" ] 2>/dev/null; then
    npm_config_cache="${TMPDIR:-/tmp}/piper-build-npm"
    export npm_config_cache
    echo "piper-build: ~/.npm is not writable, using $npm_config_cache (fix: sudo chown -R $(id -u):$(id -g) ~/.npm)" >&2
  fi
}

run_tsc() {   # run_tsc <outdir>
  _out=$1
  # only a global tsc of the pinned version: another one emits different source maps (CI images ship one)
  if command -v tsc >/dev/null 2>&1 && [ "$(tsc -v 2>/dev/null)" = "Version $TSC_VERSION" ]; then
    tsc -p "$WEB" --outDir "$_out"
  elif command -v npx >/dev/null 2>&1; then
    npm_cache
    npx -y -p "typescript@$TSC_VERSION" tsc -p "$WEB" --outDir "$_out"
  elif command -v docker >/dev/null 2>&1; then
    docker run --rm -u "$(id -u):$(id -g)" -v "$SK:/sk" -w /sk/piper/web \
      -e HOME=/tmp -e npm_config_cache=/tmp/.npm \
      "$TSC_IMAGE" npx -y -p "typescript@$TSC_VERSION" tsc -p . \
      --outDir "$(printf '%s' "$_out" | sed "s|^$SK|/sk|")"
  else
    echo "piper-build: need tsc, npx or docker (none found)" >&2
    exit 1
  fi
}

if command -v shasum >/dev/null 2>&1; then SHA="shasum -a 256"
elif command -v sha256sum >/dev/null 2>&1; then SHA="sha256sum"
else echo "piper-build: need shasum or sha256sum to verify vendor/xterm" >&2; exit 1; fi
for v in xterm codemirror iroh qrcode; do
  (cd "$WEB/vendor/$v" && $SHA -c SHA256SUMS >/dev/null) \
    || { echo "piper-build: vendored $v does not match vendor/$v/SHA256SUMS" >&2; exit 1; }
done

if [ "$CHECK" -eq 1 ]; then
  TMP=$(mktemp -d "$WEB/.check.XXXXXX")
  trap 'rm -rf "$TMP"' EXIT INT TERM
  run_tsc "$TMP"
  if diff -r -q "$WEB/dist" "$TMP" >/dev/null 2>&1; then
    echo "piper-build: dist/ is up to date"
  else
    echo "piper-build: dist/ is STALE - run sh scripts/piper-build.sh and commit dist/" >&2
    diff -r -q "$WEB/dist" "$TMP" >&2 || true
    exit 1
  fi
else
  rm -rf "$WEB/dist"
  run_tsc "$WEB/dist"
  echo "piper-build: built $WEB/dist"
fi
