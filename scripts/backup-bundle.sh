#!/usr/bin/env bash
# 每日 git bundle 备份：把全部引用打包到项目外的单一文件，滚动保留最近 N 份。
#
# 低频推送策略的补偿措施：GitHub 不可达或推送积压期间，本机故障的丢失
# 窗口由它兜底。注意 bundle 只包含**已提交**内容——未提交改动与未跟踪
# 文件不在其中，工作区成果仍需及时 commit。
#
# 用法：
#   scripts/backup-bundle.sh [备份目录]
#   备份目录默认与项目同级：<项目名>-backups/，保留份数 BACKUP_KEEP（默认 14）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${1:-${REPO_ROOT}/../$(basename "${REPO_ROOT}")-backups}"
KEEP="${BACKUP_KEEP:-14}"

mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BUNDLE="${BACKUP_DIR}/repo-${STAMP}.bundle"

git -C "${REPO_ROOT}" bundle create "${BUNDLE}" --all
git -C "${REPO_ROOT}" bundle verify "${BUNDLE}"
printf '[backup] 已备份全部引用：%s\n' "${BUNDLE}"

# 滚动清理超出保留份数的旧备份。
ls -t "${BACKUP_DIR}"/repo-*.bundle 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "${old}"
  printf '[backup] 清理过期备份：%s\n' "${old}"
done

COUNT="$(ls "${BACKUP_DIR}"/repo-*.bundle 2>/dev/null | wc -l | tr -d ' ')"
printf '[backup] 当前保留 %s 份（上限 %s）\n' "${COUNT}" "${KEEP}"
