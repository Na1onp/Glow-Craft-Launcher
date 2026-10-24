#!/bin/sh
set -eu
pid="$1"
current="$2"
staged="$3"
backup="$4"
count=0
while kill -0 "$pid" 2>/dev/null; do
  sleep 1
  count=$((count + 1))
  if [ "$count" -ge 60 ]; then
    echo "Timed out waiting for launcher to quit"
    exit 1
  fi
done
[ -d "$current" ] || exit 1
[ -d "$staged" ] || exit 1
if [ -e "$backup" ]; then
  rm -rf "$backup"
fi
mv "$current" "$backup"
if ! mv "$staged" "$current"; then
  mv "$backup" "$current"
  exit 1
fi
if ! /usr/bin/open "$current"; then
  mv "$current" "$staged"
  mv "$backup" "$current"
  /usr/bin/open "$current" || true
  exit 1
fi
echo "Update installed; previous version retained at $backup"
