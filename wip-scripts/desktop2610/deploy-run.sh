#!/usr/bin/env bash
# 2.6.10 增量部署启动器（TAT nohup 模式）：seed 镜像仓 + GitHub SSH 增量 fetch + rollout。
# 长操作全部在 setsid 子进程内，TAT 只负责写入与启动（<60s 超时约束）。
set -Eeuo pipefail
ROOT=/opt/video-replica-candidate
SHA=21e837cbe2b3ca942194ee21e37f29fae934056a
EXPECTED_TREE=cb48219c01270c34c37996b181437e1d88ca232c
BASE=0d7f20fa8313218443fd6af4769e17ce7aea9b01
EXPECTED_BASE_IMAGE='video-replica-rehearsal-app:0d7f20f-git'
EVIDENCE="$ROOT/acceptance-desktop2610-$SHA"
MIRROR="$ROOT/source-mirrors/$SHA.git"
STATE="$EVIDENCE/run.state"
mkdir -p "$EVIDENCE"

trap 'code=$?; printf "%s\n" "$code" > "$EVIDENCE/run.rc"; if [ "$code" -ne 0 ]; then printf "FAILED\n" > "$STATE"; fi' EXIT

printf 'SOURCE_IMPORT\n' > "$STATE"
printf 'STARTED=%s\nTARGET=%s\n' "$(date -Is)" "$SHA"

# 钱包快照（前）
docker exec video-replica-candidate-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT COALESCE(SUM(available_credits),0),COALESCE(SUM(reserved_credits),0),COUNT(*) FROM wallets"' > "$EVIDENCE/wallet.before.txt"
printf 'WALLET_BEFORE=%s\n' "$(cat "$EVIDENCE/wallet.before.txt")"

# 陈旧 deploy.lock 是上轮残留的静默失败源（2026-11-15 踩坑）：无 rollout 进程持有时先清。
if ! pgrep -af customer-git-rollout >/dev/null 2>&1 && [ -e "$ROOT/deploy.lock" ]; then
  rm -f "$ROOT/deploy.lock"
  printf 'STALE_DEPLOY_LOCK_REMOVED\n'
fi
if lslocks -o PATH --noheadings 2>/dev/null | grep -Fxq "$ROOT/deploy.lock"; then
  printf 'An active deployment holds the lock.\n' >&2
  exit 42
fi

test -d "$ROOT/releases/$BASE/deploy" || { printf 'BASE release checkout missing\n' >&2; exit 44; }
test -d "$ROOT/source-mirrors/$BASE.git" || { printf 'BASE seed mirror missing\n' >&2; exit 45; }

if [ ! -d "$MIRROR" ]; then
  git -c gc.auto=0 -c maintenance.auto=false clone --bare --local "$ROOT/source-mirrors/$BASE.git" "$MIRROR"
fi

# GitHub 增量 fetch（ssh.github.com:443 + deploy_ed25519 已在 ~/.ssh/config 别名）
GIT_SSH_COMMAND='ssh -oBatchMode=yes -oConnectTimeout=15' timeout 180 \
  git -C "$MIRROR" -c gc.auto=0 -c maintenance.auto=false fetch --no-auto-maintenance --depth=1 \
  git@github.com:phlong026/xiangshu-video-replica.git "$SHA"

git -C "$MIRROR" update-ref refs/heads/deploy-pin "$SHA"
test "$(git -C "$MIRROR" rev-parse refs/heads/deploy-pin)" = "$SHA"
TREE=$(git -C "$MIRROR" rev-parse "$SHA^{tree}")
test "$TREE" = "$EXPECTED_TREE"
printf 'RELEASE_TREE=%s\n' "$TREE"

CURRENT_IMAGE=$(docker inspect video-replica-candidate-api-1 --format '{{.Config.Image}}')
if [ "$CURRENT_IMAGE" != "$EXPECTED_BASE_IMAGE" ]; then
  printf 'Production image changed during prep: %s\n' "$CURRENT_IMAGE" >&2
  exit 43
fi

printf 'ROLLOUT\n' > "$STATE"
CUSTOMER_COMPOSE="$ROOT/compose.yaml" \
VIDEO_REPLICA_GIT_REPO_URL="file://$MIRROR" \
  bash "$ROOT/releases/$BASE/deploy/customer-git-rollout.sh" --commit "$SHA"

printf 'SUCCESS\n' > "$STATE"
printf 'FINISHED=%s\n' "$(date -Is)"
