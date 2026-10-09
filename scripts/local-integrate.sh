#!/usr/bin/env bash
# 本地集成：门禁全绿才允许把 feature 分支合入 main（merge-queue 语义）。
#
# 用法：
#   scripts/local-integrate.sh <feature-branch> [--push]
#
# 流程：
#   0. 防呆：工作区必须干净（无已跟踪改动）；feature 分支必须存在、
#      不是 main、且尚未合入 main。origin/main 可达时要求本地 main
#      已包含它（否则提示先同步）。
#   1. checkout main 后执行 git merge --no-commit --no-ff <feature>：
#      合并结果先落在工作区，冲突或失败即中止还原。
#   2. 在「合并结果」上跑 npm run check:full——与 merge queue 相同语义，
#      验证的是合入后的 main，而不是孤立的 feature 分支。
#   3. 全绿：git commit 完成 merge，message 附带门禁证据路径与耗时。
#      失败：git merge --abort 并切回原分支，现场与日志路径保留。
#   4. 默认不推送（低频推送策略，见 AGENTS.md）；--push 时推送 main。
#
# 集成窗口（步骤 2 的几十分钟）内工作区停在 main 半合并状态，其他会话
# 不得触碰工作区——集成者制度见 AGENTS.md「本地集成与推送纪律」。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

FEATURE="${1:-}"
PUSH=0
[ "${2:-}" = "--push" ] && PUSH=1

abort() {
  printf '[integrate] 中止：%s\n' "$1" >&2
  exit 1
}

[ -n "${FEATURE}" ] || abort "用法：scripts/local-integrate.sh <feature-branch> [--push]"
[ "${FEATURE}" != "main" ] || abort "feature 分支不能是 main"
git show-ref --verify --quiet "refs/heads/${FEATURE}" || abort "分支不存在：${FEATURE}"
[ -z "$(git status --porcelain --untracked-files=no)" ] || abort "工作区有未提交改动，先提交或清理后再集成"

if git merge-base --is-ancestor "${FEATURE}" main; then
  abort "${FEATURE} 已包含在 main 中，无需集成"
fi

# 尽力而为的远端检查：GitHub 不可达（账号被封/断网）时只警告不阻断，
# 本地集成照常进行，推送留待恢复后批量执行。
if ORIGIN_MAIN="$(git rev-parse --verify --quiet origin/main)"; then
  git merge-base --is-ancestor "${ORIGIN_MAIN}" main || \
    printf '[integrate] 警告：origin/main 有本地 main 未包含的提交，推送前需先处理\n'
else
  printf '[integrate] 提示：origin/main 不可达（离线模式），跳过远端一致性检查\n'
fi

ORIG_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
restore() {
  git merge --abort >/dev/null 2>&1 || true
  git checkout "${ORIG_BRANCH}" >/dev/null 2>&1 || true
}

git checkout main || abort "无法切换到 main"
if ! git merge --no-commit --no-ff "${FEATURE}"; then
  restore
  abort "合并冲突：先在 ${FEATURE} 上解决与 main 的冲突"
fi

printf '\n[integrate] 合并结果已就绪，开始 check:full（预计几十分钟，期间工作区归集成者独占）\n'
GATE_STARTED=$SECONDS
if npm run check:full; then
  GATE_PASSED=1
else
  restore
  abort "check:full 未通过，main 已还原到合并前状态（证据日志见 .shard-logs/）"
fi
GATE_SECONDS=$((SECONDS - GATE_STARTED))

GATE_LOG="$(ls -t .shard-logs/full-gate-*.log 2>/dev/null | head -1 || true)"
{
  printf "Merge branch '%s'\n" "${FEATURE}"
  printf '\nLocal gate: check:full PASS（%s，耗时 %ds）\n' "$(date '+%F %T %z')" "${GATE_SECONDS}"
  [ -n "${GATE_LOG}" ] && printf 'Evidence: %s\n' "${GATE_LOG}"
  printf 'Integrator: scripts/local-integrate.sh\n'
} > .git/INTEGRATE_MSG

if ! git commit -F .git/INTEGRATE_MSG; then
  rm -f .git/INTEGRATE_MSG
  restore
  abort "merge commit 创建失败，main 已还原"
fi
rm -f .git/INTEGRATE_MSG

printf '\n[integrate] 已合入 main（%s），门禁耗时 %ds，证据：%s\n' \
  "$(git rev-parse --short HEAD)" "${GATE_SECONDS}" "${GATE_LOG:-<未找到>}"

if [ "${PUSH}" = "1" ]; then
  git push origin main || abort "推送失败（GitHub 不可达或远端前进），本地 main 已保留，稍后重试"
  printf '[integrate] 已推送 origin/main\n'
else
  printf '[integrate] 按低频推送策略未推送；累计的本地 main 由值守会话择时批量推送\n'
fi
