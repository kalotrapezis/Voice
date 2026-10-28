#!/bin/sh
# Fetches VOICE's offline models into ./models from the project's own models repository (stable links),
# checking each file against SHA256SUMS. The packages need models/bergamot (translation, shipped inside);
# whisper and the MMS voice are optional here, since the app downloads those itself.
# Usage: tools/fetch-models.sh [all]
set -e
cd "$(dirname "$0")/.."
URL=https://github.com/kalotrapezis/voice-models/releases/download/v1
mkdir -p models
curl -fsSL "$URL/SHA256SUMS" -o models/SHA256SUMS
[ "$1" = all ] && pat=. || pat='  bergamot__'
grep -E "$pat" models/SHA256SUMS | while read -r sum name; do
  dest="models/$(echo "$name" | sed 's#__#/#g')"
  [ -f "$dest" ] && echo "$sum  $dest" | sha256sum -c --quiet 2>/dev/null && continue
  mkdir -p "$(dirname "$dest")"
  echo "↓ $name"
  curl -fL -C - "$URL/$name" -o "$dest.part"
  echo "$sum  $dest.part" | sha256sum -c --quiet && mv "$dest.part" "$dest"
done
echo "models ready"
