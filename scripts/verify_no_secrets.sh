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
  "deploy"
)

patterns=(
  # sk- 前必须是行首或非词字符（ERE 无后行断言，用捕获组锚定）：
  # 避免 task-warning--toolbar 一类 CSS 类名里的 “ta**sk-**warning…” 子串
  # 误报；真实密钥前必是引号/空白/行首，不会嵌在标识符中间。
  '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}'
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

if ! command -v rg >/dev/null 2>&1; then
  echo "Secret scan requires rg for deployment token checks." >&2
  exit 2
fi

deploy_token_matches=""
if deploy_token_matches="$(rg --hidden --no-ignore -n --no-heading 'proxy_set_header[[:space:]]+X-Control-Proxy-Token' deploy)"; then
  while IFS= read -r match; do
    if [[ "$match" == *'proxy_set_header X-Control-Proxy-Token "";'* ]]; then
      continue
    fi
    if [[ "$match" == *.example:*'proxy_set_header X-Control-Proxy-Token "REPLACE_WITH_32_BYTE_RANDOM_TOKEN";'* ]]; then
      continue
    fi
    echo "Unexpected raw control proxy token in deployment file: $match" >&2
    exit 1
  done <<< "$deploy_token_matches"
else
  deploy_token_scan_status=$?
  if [[ $deploy_token_scan_status -ne 1 ]]; then
    echo "Deployment token scan failed." >&2
    exit 2
  fi
fi

echo "No hardcoded secrets detected in runtime contract surface."
