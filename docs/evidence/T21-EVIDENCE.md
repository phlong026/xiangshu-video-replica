# T21 — 客户会话事务内 fencing 接入业务写路由（SES-04）+ 读路由 owner/IDOR 审查（SES-05）

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T21 / SES-04 / SES-05 |
| **Owner** | Backend (Agent) |
| **Reviewer** | 独立评审待 PR 轮次（connector/Codex 评审后登记） |
| **Branch / Base SHA** | `feat/customer-v3-t21-business-fencing` / base `a32bc56`（main，T23 合入后 rebase） |
| **Date** | 2026-08-24 |
| **Evidence Level** | `AUTOMATED_VERIFIED`（PG 16 fixture，localhost:5433；SQLite lane 全量回归） |

## Exit-Gate Verification

Task exit gate（任务清单 §4 T21）：*项目、上传、任务、充值、设备写入均阻断迟到旧请求* —— 客户写路由在 fenced 事务内重新核验 session（SES-04），全仓业务+内部 SQL 为 PG-canonical（`%s`），SES-05 读矩阵锁跨用户边界。

```bash
$ uv run python -m pytest tests -q   # 1081 passed, 0 failed
  # 客户写路由：fenced_pg_transaction 内 verify_session_context（epoch/lease/码/设备复查）
  # 快照依赖只做早期 401；最终裁决永远在事务内（§9.2 红线）
  # SES-04 门禁矩阵：27 条迁移写路由 × 未知 session token → 401（无一条回落内网 lane）
  # SES-05 矩阵：跨用户读项目/资产/拆解/帧/Prompt/批次 → 403 无侧信道
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
  # All checks passed! / 122 files already formatted / Success: 68 source files
```

## Implementation Highlights

### 数据库双道抽象（`db_portable.py`，地基）
- `translate_to_sqlite`：有界 fail-closed 翻译层（`%s`→`?` 跳过字面量/注释、`FOR UPDATE` 剥离、`now()±interval`、`::cast`、未识别构造 ValueError）
- `SQLiteBackend`/`PostgresBackend` + `BusinessConnection` 门面（execute/with/commit/rollback/transaction/in_transaction/set_trace_callback/iterdump；PG 后端 commit no-op、行工厂支持 `row["col"]` 命名访问）
- `get_database` 返回 BusinessConnection（SQLite lane）；全仓业务 service 注解 `sqlite3.Connection`→`BusinessConnection`

### fencing 接线（`customer_fence.py`）
- `customer_session_snapshot`：PG 运行时优先判定；客户 lane 缺 token → 401（不回落内网）
- `fenced_pg_transaction`：事务内 verify_session_context（行锁 + expected_* 含 lease + 码/设备状态 + clock_timestamp 租约）；SessionFencingError → 401 全回滚
- `BusinessDb.write()`：双道分发（客户 lane fenced 事务 / 内网 lane SQLite + 内部 AuthenticatedUser）

### 客户写路由接入（db.write()）
项目 create/rename、media upload-intent/complete、recharge create（幂等重试捕 UniqueViolation）、analysis create/update-shots、first/source frames generate/confirm/extract、simple_character 全流、character_reference selection、main-character、generation scripts/prompts/batches/tasks（13 路由）。

### SQL `%s` 迁移（全仓）
业务（media/recharge/zpay/analysis/frames/simple_character/characters 域/generation 103 处/repositories）+ 内部/admin（auth/control/settings/billing/wallet/gate1）全部 PG-canonical；保留 URL/正则的 `?`。

### SES-05 读审查
- 跨用户读矩阵（9 域 + wallet）：非 owner 读外国项目资源 → 403，无侧信道
- 人物域共享语义决策：客户 lane owner-scoped（published 可见性仅内部），记录待 T28+ 客户读接线

## Files Changed（P1 概览）
- 新建：`app/db_portable.py`、`app/customer_fence.py`、`tests/test_db_portable.py`
- 迁移：`app/rbac_routes.py`、`media*.py`、`recharge_routes.py`、`zpay_payments.py`、`analysis*.py`、`first_frame*`、`source_frame*`、`simple_character*`、`character*`、`generation*.py`、`repositories.py`、`auth.py`、`control_routes.py`、`settings*.py`、`internal_*`、`wallet_routes.py`、`gate1_bootstrap.py` 等
- 测试：`test_customer_fencing.py`（+PG 两码：A 建项目 owner / B 改 A 项目 403 / 未知 token 401）、`test_rbac.py`（+SES-05 矩阵）、21+ 测试文件 fixture 适配（BusinessConnection + VIDEO_REPLICA_DB_PATH）

## Section 14 Ledger Record

```text
任务/工作包：T21 / SES-04、SES-05（P1 阶段：地基+fencing+全仓 SQL 迁移+读矩阵）
Owner / Reviewer：后端（Agent 执行）/ 独立评审待 PR 轮次
分支 / 基线 SHA：feat/customer-v3-t21-business-fencing / 基线 a32bc56（T23 合入后 rebase）
上游规格段落：任务清单 §4 T21、§12.3 SES-04/SES-05；代码清单 §9.2 customer_auth.py、§3.3 test_customer_fencing.py；激活码开发文档 §12.4 写请求 fencing
改动文件：db_portable.py（新建）、customer_fence.py（新建）、全仓业务+内部 SQL %s 迁移、客户写路由 db.write() 接线（项目/media/recharge/analysis/帧/simple_character/characters/generation 13 路由）、test_db_portable.py（新建 15 用例）、test_customer_fencing.py（+3 PG 两码）、test_rbac.py（+SES-05 矩阵 10 用例）、21+ 测试文件 fixture 适配
失败测试或回归锁定：db_portable 15 例（翻译规则/fail-closed/门面）；fenced_pg_transaction 3 例（happy/过期 401 零写入/业务回滚）；PG 两码 3 例（A 建项目 owner=A/B 改名 403 零落库/未知 token 401 零落库）；SES-04 门禁矩阵 27 例（每条迁移写路由未知 token → 401）；SES-05 读矩阵 10 例（跨用户 9 域 403 + wallet owner 隔离）
实现结果：① 双道数据库抽象（翻译层+门面）使单一实现全 PG 化成立；② 客户写路由在 fenced 事务内重验 session（快照仅早期 401）；③ 全仓 SQL %s-canonical（PG 兼容）；④ SES-05 跨用户读矩阵锁 owner 边界
验证命令与通过数：全量 1081 passed 零回归（T20 994 基线 + db_portable 15 + fencing 3 + PG 两码 3 + T23 增量 + SES-05 10 + SES-04 门禁 27）；ruff/format/mypy 全绿（122 files，68 source files）
证据层级：AUTOMATED_VERIFIED
安全与可观测性：写路由 session 在事务内重新核验（epoch 回跳不可能）；快照依赖不裁决；跨用户读统一 403/404 无侧信道；token 只以 digest 过库
迁移与回滚：无新迁移；回滚=还原代码（SQLite lane 由翻译层兼容）
外部授权记录：无
未测试项：fencing 接入设备写路由之外的 admin 路由（非客户 lane 不需）；客户端 OpenAPI 重新生成（T28）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
