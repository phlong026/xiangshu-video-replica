#!/usr/bin/env bash
set -euo pipefail

scan_paths=(
  "server/app"
  "server/migrations"
  "client/src"
  "client/src-tauri"
  "package.json"
  "client/package.json"
  "server/pyproject.toml"
  "packaging_tools"
  "scripts"
  "e2e"
  "docs"
)

patterns=(
  'sk-[A-Za-z0-9_-]{20,}'
  'AKIA[0-9A-Z]{16}'
  '-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
  '(api[_-]?key|secret[_-]?access[_-]?key|authorization|bearer|token|password)[[:space:]]*[:=][[:space:]]*["'\''][^"'\'']{8,}["'\'']'
)

for pattern in "${patterns[@]}"; do
  if git grep -n -I -E -e "$pattern" -- "${scan_paths[@]}"; then
    echo "Potential secret detected by pattern: $pattern" >&2
    exit 1
  elif [[ $? -gt 1 ]]; then
    echo "Secret scan failed while evaluating pattern: $pattern" >&2
    exit 2
  fi
done

echo "No hardcoded secrets detected in runtime contract surface."
