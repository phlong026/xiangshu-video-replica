# T08 — Billing provider / pricing_scope conditional constraints (DB-07)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T08 / DB-07 |
| **Owner** | Billing/DB (Agent) |
| **Reviewer** | chatgpt-codex-connector (PR review) |
| **Branch / Base SHA** | `feat/customer-v3-t08-billing-provider-constraints` / base `9f60eea` |
| **Date** | 2026-08-22 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PG 16 fixture; CI PG16 service in the Linux gate) |

## Exit-Gate Verification

Task exit gate: *支持 zpay、activation_code、admin_adjustment；非法形状由 PG 拒绝* — delivered
as an automated PG16 integration test (`test_pg_billing_provider_shapes_accepted_and_rejected`):

```bash
$ uv run python -m pytest tests/test_postgres_migrations.py -q
9 passed
  # 4 legal shapes accepted: zpay+INTERNAL+PENDING; zpay+CUSTOMER_STANDARD+PAID(+trade_no,
  #   charged 1500 >= base 1000); activation_code+CUSTOMER_STANDARD+PAID (no trade_no,
  #   amount 1500 below the internal minimum — face value defined by the batch);
  #   admin_adjustment+INTERNAL+PAID (amount 5000 below minimum, off the step ladder)
  # + a CHARGE ledger row referencing the activation_code order keeps the 022 shape
  # 12 illegal shapes rejected by CheckViolation: unknown provider 'wechat'; scope
  #   'CHANNEL_A' (frozen until PRICE-01); activation_code+INTERNAL (scope pairing);
  #   activation_code+PENDING (must land PAID atomically); activation_code with a
  #   trade number; admin_adjustment+PENDING; zpay PAID without a trade number;
  #   CUSTOMER_STANDARD charged 500 < base 1000 (price floor); zpay below minimum;
  #   zpay off the step ladder; credits*price != amount (all providers); 'REFUNDED'
```

## Constraint Model (revision `026_customer_security_and_billing`, PG-only)

Replaced 022 constraints (`provider='zpay'`, `pricing_scope='INTERNAL'`,
unconditional min/step ladders) with provider-conditional CHECK constraints:

| Constraint | Rule |
| --- | --- |
| `ck_recharge_orders_provider` | `provider IN ('zpay', 'activation_code', 'admin_adjustment')` |
| `ck_recharge_orders_pricing_scope` | `pricing_scope IN ('INTERNAL', 'CUSTOMER_STANDARD')` |
| `ck_recharge_orders_provider_scope` | activation_code is customer-only; zpay and admin_adjustment work in both scopes |
| `ck_recharge_orders_provider_status` | non-zpay orders must land `PAID` atomically (activation §12.1, audited adjustment); only zpay runs a PENDING lifecycle |
| `ck_recharge_orders_provider_trade_no` | a paid ZPay order must carry its third-party trade number (verified notify flow); activation/adjustment orders must not have one |
| `ck_recharge_orders_customer_price_floor` | non-INTERNAL scopes: `charged >= base` (PRICE-01: customer prices never undercut the internal base price) |
| `ck_recharge_orders_amount_minimum` | min-recharge ladder governs zpay only |
| `ck_recharge_orders_amount_step` | step ladder governs zpay only |

Untouched 022 invariants: price snapshots > 0, amount/credits > 0,
`amount % charged = 0`, `credits * charged = amount`, status enum, trade-no
not-blank, partial unique indexes (trade_no / charge_order / reserve_round /
terminal_round), and the whole `wallet_transactions` ledger shape (the CHARGE
row is provider-agnostic and verified against an activation_code order).

## Design Decisions

1. **PG-only revision (025 precedent).** PostgreSQL is the customer production
   source of truth. SQLite remains the internal P0 runtime and the T07
   read-only import source; its `recharge_orders` only ever holds zpay/INTERNAL
   rows, which the published 022 constraints already model exactly, so 026 is a
   no-op there and the internal runtime is untouched.
2. **`CUSTOMER_STANDARD` only.** Channel/special/individual prices stay out of
   the enum until PRICE-01 freezes the pricing-layer decision — an order with
   any other scope is rejected by the database today.
3. **Downgrade guard.** Once activation/adjustment/CUSTOMER rows exist, the
   022 zpay/INTERNAL-only set cannot hold them; confirmed billing rows must
   never be deleted (No-Go), so downgrade fails loudly with the recovery path
   (`test_pg_billing_constraints_downgrade_guard`). An empty ledger downgrades
   symmetrically and restores the verbatim 022 shapes.
4. **zpay PAID ⇒ trade number** matches the existing verified notify flow
   (`confirm_recharge_payment` rejects empty trade numbers before marking PAID).

## Files Changed

| File | Change |
| --- | --- |
| `server/migrations/versions/026_customer_security_and_billing.py` | new PG-only revision: drop 4 replaced constraints, add 8 conditional ones, guarded downgrade |
| `server/tests/test_postgres_migrations.py` | +2 T08 tests (shapes accepted/rejected, downgrade guard); head-revision assertions updated to 026 |
| 7 further test files | head-revision assertions updated to 026 (`test_db`, `test_internal_billing`, `test_character_domain`, `test_characters`, `test_settings`, `test_recharge_orders`, `test_sqlite_to_postgres`) |
| ledgers | task list T08/DB-07 → `[x]` + header line; `docs/CUSTOMER-TASK-EVIDENCE-V3.md` T08 entry; this file |

File integrity (git blob SHA, final squash merge is authoritative):

| File | Git blob SHA |
| --- | --- |
| `server/migrations/versions/026_customer_security_and_billing.py` | `12922fcb2942e02ae8a9c1f72cb428b50d4a56b6` |
| `server/tests/test_postgres_migrations.py` | `e367d1d9e1970cb118d45712bf448f11e90ef57b` |

## Regression

```text
$ uv run python -m pytest tests -q          → 636 passed, 1 warning
$ uv run ruff check .                       → All checks passed!
$ uv run ruff format --check .              → 123 files already formatted
$ uv run mypy app                           → Success: no issues found in 53 source files
$ npm run check (repo root)                 → full gate green (client + server)
```

Red-green record: the two new tests were written first and failed against the
025 head (unknown revision / activation_code rejected by the 022
`provider='zpay'` CHECK), then turned green after 026 landed.

## Section 14 Ledger Record

```text
任务/工作包：T08 / DB-07
Owner / Reviewer：Billing/DB（Agent 执行）/ chatgpt-codex-connector（PR 评审）
分支 / 基线 SHA：feat/customer-v3-t08-billing-provider-constraints / 基线 9f60eea615ab9dee177eb0892b3789dabda196dd
上游规格段落：客户版任务清单 V3 §2 T08、§12.1 DB-07；客户版代码开发清单 V3 §3.1（026 迁移名冻结）；客户版测试与验收规格 V3 §7（provider/price scope 形状由 PG check constraints 验证）；激活码开发文档 §12.1
改动文件：server/migrations/versions/026_customer_security_and_billing.py（新增）、server/tests/test_postgres_migrations.py、7 个测试文件 head 断言、任务与证据账本
失败测试或回归锁定：先红后绿——4 组合法形状 + 12 组非法形状的 PG16 CheckViolation 测试、downgrade 守卫测试（有客户行时拒绝降级、空账本对称回退、回退后 CUSTOMER 形状恢复被拒）
实现结果：026 PG-only 迁移以 8 条 provider 条件 CHECK 约束扩展账务来源（zpay/activation_code/admin_adjustment）、价格域（INTERNAL/CUSTOMER_STANDARD）、价格下限（客户价不低于内部基础价）与 min/step 阶梯适用范围（仅 zpay）；非 zpay 订单必须 PAID 落库且无第三方流水号；zpay PAID 必带流水号
验证命令与通过数：pytest tests/test_postgres_migrations.py → 9 passed；pytest 全量 → 636 passed；ruff/format/mypy 全绿；npm run check 全仓门禁通过
证据层级：AUTOMATED_VERIFIED（本地与 CI 均为真实 PostgreSQL 16 执行）
安全与可观测性：约束全部由数据库层强制（应用层校验不能替代数据库约束）；SQLite 内部运行时零改动
迁移与回滚：PG-only、025 先例模式；downgrade 带数据守卫（已确认账务行不删除），空账本对称回退并逐字恢复 022 约束
外部授权记录：无
未测试项：activation_code/admin_adjustment 订单的业务写入路径（T13 激活事务、T23 调账 API 分别落地时验收）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
