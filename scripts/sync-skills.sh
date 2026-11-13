#!/usr/bin/env bash
# Copy the shared skills (skills/) into every pack that ships its own copy.
#   scripts/sync-skills.sh          copy
#   scripts/sync-skills.sh --check  fail if any copy differs from skills/ (run before every commit)
# To add a pack, add its skills directory to TARGETS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/skills"
TARGETS=(
  mcp/mist_mcp/skills
  plugins/claude/skills
  plugins/codex/skills
  plugins/hermes/skills
  plugins/opencode/skills
  adapters/langchain/mist_langchain/skills
  adapters/strands/mist_strands/skills
)

# OpenCode loads skills from a URL that serves index.json (https://opencode.ai/docs/skills/).
opencode_index() {
  local version
  version="$(cd "$SRC" && find . -type f | LC_ALL=C sort | xargs cat | shasum | cut -c1-12)"
  printf '{\n  "skills": [\n'
  local first=1
  for dir in "$SRC"/*/; do
    local name files
    name="$(basename "$dir")"
    files="$(cd "$dir" && find . -type f | sed 's#^\./##' | LC_ALL=C sort | sed 's/.*/"&"/' | paste -sd, -)"
    [ $first -eq 1 ] || printf ',\n'
    first=0
    printf '    {"name": "%s", "version": "%s", "files": [%s]}' "$name" "$version" "$files"
  done
  printf '\n  ]\n}\n'
}

if [ "${1:-}" = "--check" ]; then
  status=0
  for t in "${TARGETS[@]}"; do
    if ! diff -r -x index.json "$SRC" "$ROOT/$t" >/dev/null 2>&1; then
      echo "out of date: $t (run scripts/sync-skills.sh)" >&2
      status=1
    fi
  done
  if ! diff <(opencode_index) "$ROOT/plugins/opencode/skills/index.json" >/dev/null 2>&1; then
    echo "out of date: plugins/opencode/skills/index.json (run scripts/sync-skills.sh)" >&2
    status=1
  fi
  [ $status -eq 0 ] && echo "skills in sync: ${#TARGETS[@]} copies"
  exit $status
fi

for t in "${TARGETS[@]}"; do
  rm -rf "${ROOT:?}/$t"
  mkdir -p "$(dirname "$ROOT/$t")"
  cp -R "$SRC" "$ROOT/$t"
done
opencode_index > "$ROOT/plugins/opencode/skills/index.json"
echo "copied skills/ into ${#TARGETS[@]} packs"
