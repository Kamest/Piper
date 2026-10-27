#!/bin/sh
# Build the optional iroh browser client. Normal Piper builds do not need Rust.
set -eu
SK=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CARGO=${PIPER_MOBILE_CARGO:-cargo}
BINDGEN=${PIPER_MOBILE_BINDGEN:-wasm-bindgen}
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-$SK/development/mobile-iroh/target}
"$CARGO" build --locked --manifest-path "$SK/piper/mobile-wasm/Cargo.toml" --target wasm32-unknown-unknown --release
OUT="$SK/piper/web/vendor/iroh"
mkdir -p "$OUT"
"$BINDGEN" --target web --out-dir "$OUT" --out-name piper_mobile \
  "$CARGO_TARGET_DIR/wasm32-unknown-unknown/release/piper_mobile.wasm"
(cd "$OUT" && shasum -a 256 piper_mobile.js piper_mobile.d.ts piper_mobile_bg.wasm piper_mobile_bg.wasm.d.ts VERSIONS THIRD_PARTY_NOTICES.txt > SHA256SUMS)
echo 'piper-mobile: browser transport built; run piper-build.sh before exporting the client'
