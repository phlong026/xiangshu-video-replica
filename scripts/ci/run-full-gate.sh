#!/usr/bin/env bash
# 本地全量门禁：逐段等价复刻 ci.yml「Linux quality gate」的全部检查。
#
# 用作 check:full 的实现（package.json），也是 local-integrate.sh 合并放行的
# 唯一依据。刻意不提供任何跳过开关——CI 有什么，本地就有什么。
#
# 段序对齐 ci.yml：
#   1. check:sharded        secret 扫描 + 静态检查 + 四分片 pytest
#   2. 分片清单覆盖断言      build-test-shards.py --check-coverage
#   3. 浏览器 E2E           pg-fixture 起停 + Playwright Chromium + customer E2E
#   4. Rust 测试            cargo test --locked
#   5. 双前端构建           build + build:admin
#   6. 依赖审计             npm audit --audit-level=high
#
# 全程双写终端与证据日志（.shard-logs/full-gate-<时间戳>.log），
# 该路径会被 local-integrate.sh 写进 merge commit，作为门禁证据。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

LOG_DIR="${CI_SHARD_LOG_DIR:-${REPO_ROOT}/.shard-logs}"
mkdir -p "${LOG_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_DIR}/full-gate-${STAMP}.log"
exec > >(tee -a "${LOG_FILE}") 2>&1

fail() {
  printf '\n[full-gate] FAIL：%s\n[full-gate] 完整日志：%s\n' "$1" "${LOG_FILE}" >&2
  exit 1
}

run_segment() {
  local name="$1"; shift
  printf '\n===== [%s] %s =====\n' "$(date '+%F %T')" "${name}"
  local started=$SECONDS
  if "$@"; then
    printf '===== [%s] PASS %s（耗时 %ds）=====\n' "$(date '+%F %T')" "${name}" "$((SECONDS - started))"
  else
    fail "${name}"
  fi
}

printf '[full-gate] 开始，证据日志：%s\n' "${LOG_FILE}"

# 1+2. 静态门禁与分片 pytest；覆盖断言单独跑：清单漏登记会让 CI 静默跳过
# 新测试文件（CW-044 §18.2 的 fail-open 教训），本地门禁必须同样钉住。
run_segment "静态检查 + 四分片 pytest" npm run check:sharded
run_segment "分片清单覆盖断言" python3 scripts/ci/build-test-shards.py --check-coverage

# 3. 浏览器 E2E：依赖本机 PG fixture（默认 5433，与 ci.yml 相同约定）。
# 端口被并行会话或遗留容器占用时，用 CI_E2E_PG_PORT 整体避让。
E2E_PG_PORT="${CI_E2E_PG_PORT:-5433}"
E2E_DSN="postgresql://testuser:testpass@localhost:${E2E_PG_PORT}/customer_v3_test"
FIXTURE_STARTED=0
cleanup() {
  if [ "${FIXTURE_STARTED}" = "1" ]; then
    PG_FIXTURE_NAME="fullgate-e2e" PG_FIXTURE_PORT="${E2E_PG_PORT}" \
      bash scripts/pg-fixture.sh stop >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
if PG_FIXTURE_NAME="fullgate-e2e" PG_FIXTURE_PORT="${E2E_PG_PORT}" bash scripts/pg-fixture.sh start; then
  FIXTURE_STARTED=1
else
  fail "浏览器 E2E 前置：pg-fixture 启动（端口被占时用 CI_E2E_PG_PORT 避让）"
fi
export TEST_POSTGRESQL_URL="${E2E_DSN}"
run_segment "安装 Playwright Chromium" npx playwright install chromium
run_segment "客户浏览器 E2E" npm run test:customer-e2e
trap - EXIT
cleanup

# 4-6. Rust、构建与依赖审计。
run_segment "Rust 单测（cargo test）" cargo test --manifest-path client/src-tauri/Cargo.toml --locked
run_segment "客户前端构建" npm run build
run_segment "管理端前端构建" npm run build:admin
run_segment "依赖漏洞审计（npm audit）" npm audit --audit-level=high

printf '\n[full-gate] 全部通过。证据日志：%s\n' "${LOG_FILE}"
