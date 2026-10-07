#!/usr/bin/env bash
# Compile les décodeurs natifs embarqués dans Orsat-Decoder.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HERE/bin"
make -s -C "$HERE/ft8" clean >/dev/null 2>&1 || true
make -s -C "$HERE/ft8" decode_ft8 gen_ft8
cp "$HERE/ft8/decode_ft8" "$HERE/ft8/gen_ft8" "$HERE/bin/"
make -s -C "$HERE/wspr" clean >/dev/null 2>&1 || true
make -s -C "$HERE/wspr" wspr_decode_iq 2>/dev/null
cp "$HERE/wspr/wspr_decode_iq" "$HERE/bin/"
echo "décodeurs natifs compilés : $(ls "$HERE/bin" | tr '\n' ' ')"
