#!/bin/bash
# CW-025: 开发环境 PG wrapper
#
# 自动拉起 pg-fixture 并注入 VIDEO_REPLICA_DATABASE_URL，然后执行传入的命令。
# 解决 CW-025 全环境 fail-closed 后开发环境缺 DSN 无法启动的问题。
#
# 用法：bash scripts/dev-with-pg.sh <command> [args...]
# 示例：bash scripts/dev-with-pg.sh uv run python -m uvicorn app.main:app
#
# 注意：
# - pg-fixture.sh start 是幂等的（已运行则跳过）
# - DSN 与 pg-fixture.sh 约定一致（端口 5433，testuser/testpass，customer_v3_test）
# - 如果已设置 VIDEO_REPLICA_DATABASE_URL，则使用已有值（允许覆盖）
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 登录/会话链路依赖一组 HMAC/AEAD 密钥，缺任一即 503（fail-closed）。
# 开发密钥放在根目录 .env.dev.local（.gitignore 的 .env.* 规则已忽略，不入库）；
# 文件不存在只影响登录等功能，服务本身仍可启动。文件里的显式导出优先于本文件。
if [ -f "$SCRIPT_DIR/../.env.dev.local" ]; then
  set -a
  . "$SCRIPT_DIR/../.env.dev.local"
  set +a
fi

# 确保 pg-fixture 运行（幂等）
echo "[dev-with-pg] Ensuring PostgreSQL fixture is running..."
"$SCRIPT_DIR/pg-fixture.sh" start

# 注入开发环境 DSN（与 pg-fixture.sh 约定一致）
export VIDEO_REPLICA_DATABASE_URL="${VIDEO_REPLICA_DATABASE_URL:-postgresql://testuser:testpass@localhost:5433/customer_v3_test}"
echo "[dev-with-pg] VIDEO_REPLICA_DATABASE_URL=$VIDEO_REPLICA_DATABASE_URL"

# 执行传入的命令
echo "[dev-with-pg] Executing: $@"
exec "$@"
