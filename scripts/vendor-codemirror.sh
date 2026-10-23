#!/bin/sh
# vendor-codemirror.sh - rebuild piper/web/vendor/codemirror.
#
# The repo has no bundler in its build (piper-build.sh = tsc only). CodeMirror 6 is many small
# npm packages, so it is bundled ONCE here into one ES module + one self-contained .d.ts and the
# result is committed with VERSIONS and SHA256SUMS (checked by piper-build.sh), like vendor/xterm.
# Pins: edit the versions below, run this script, review the diff of VERSIONS, commit.
#
# Usage: vendor-codemirror.sh            (needs node + npm and registry.npmjs.org)
set -eu
SK=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT="$SK/piper/web/vendor/codemirror"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/piper-cm.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM
cp "$SK/scripts/vendor/codemirror-entry.ts" "$TMP/cm.ts"
cd "$TMP"
npm init -y >/dev/null
npm i --silent --no-audit --no-fund \
  @codemirror/state@6.7.6 @codemirror/view@6.43.13 @codemirror/commands@6.11.1 @codemirror/language@6.12.4 \
  @codemirror/merge@6.12.1 @codemirror/search@6.7.2 @codemirror/lint@6.9.7 @codemirror/autocomplete@6.20.3 @codemirror/lang-java@6.0.2 \
  @codemirror/lang-javascript@6.2.5 @codemirror/lang-xml@6.1.0 @codemirror/lang-sql@6.10.0 @codemirror/lang-yaml@6.1.3 \
  @codemirror/lang-markdown@6.5.2 @codemirror/lang-json@6.0.2 @codemirror/lang-python@6.2.1 @codemirror/lang-html@6.4.12 \
  @codemirror/lang-css@6.3.1 @codemirror/legacy-modes@6.5.4 @lezer/highlight@1.2.5 \
  esbuild@0.28.2 rollup@4.64.0 rollup-plugin-dts@6.5.1 typescript@5.9.3
./node_modules/.bin/esbuild cm.ts --bundle --format=esm --minify --legal-comments=none --target=es2022 --outfile=out/cm.mjs
printf '%s\n' "import { dts } from 'rollup-plugin-dts';" \
  "export default { input: 'cm.ts', output: { file: 'out/cm.d.ts', format: 'es' }, plugins: [dts({ respectExternal: true })] };" > rollup.config.mjs
./node_modules/.bin/rollup -c rollup.config.mjs
mkdir -p "$OUT/typings"
cp out/cm.mjs "$OUT/cm.mjs"
sed 's/[[:blank:]]*$//' out/cm.d.ts > "$OUT/typings/cm.d.ts"
(cd "$OUT" && shasum -a 256 cm.mjs typings/cm.d.ts > SHA256SUMS)
echo "vendor-codemirror: wrote $OUT (update VERSIONS if the pins changed)"
