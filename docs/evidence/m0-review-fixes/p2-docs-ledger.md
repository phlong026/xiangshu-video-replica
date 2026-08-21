# M0 评审修复 P2 批次证据（H4 + M8 + M9 + H1 文案 + DB-04 schema diff 补证）

> 对应评审报告：`docs/客户版V3-M0评审报告-2026-08-21.md` §4（H4）、§5（M8、M9）、§8 P1/P2 行动项（H1 文案分支、DB-04 schema diff）
>
> 分支：`fix/customer-v3-m0-review-p2`，基线：`0fa8516`（main）

本批次为纯文档/账本批次（评审 §8 的 P1 文档行动与 P2 文档行动），无代码改动；
P0（PR #35）与 P1/P2 代码（PR #36）已先行合并。

## H4 — T04 清单增补（Alembic 方言参数清单）

`docs/evidence/T04-SQLITE-INVENTORY.md`（自仓库根目录移入，见 M8）：

1. **新增第 5 项风险类别**：`sqlite_where` 部分索引（正是 C1 的根因类别），
   含全部 6 处调用的完整清单表——文件:行号、索引名、谓词、PG 对应项、
   修复状态（5 处 T06 修复 + 1 处 025 追加修复），以及静态契约测试的防回归说明
2. **batch_alter_table 展开为完整表**：16 文件 / 35 调用点，含行号与所涉表。
   同时修正原文错误：原文列出的 `017_generation_task_retry_lineage.py` 与
   `022_internal_billing.py` **均不含** batch_alter_table 调用（实测 grep 证实）
3. **Phase 1 路线图修正**：asyncpg 建议改为 psycopg3（与冻结规格 §8.1、T05
   实际交付一致），并标注 H3/H2 已完成的现状

## M8 — 证据台账统一与 SHA 补齐

- `git mv`：`T02-EVIDENCE.md` / `T03-EVIDENCE.md` / `T04-SQLITE-INVENTORY.md` /
  `T05-EVIDENCE.md` / `T06-EVIDENCE.md` → **`docs/evidence/`**（与
  `docs/evidence/t02/`、`docs/evidence/m0-review-fixes/` 归一）
- `docs/CUSTOMER-TASK-EVIDENCE-V3.md`：
  - T01 的 Branch/SHA 与 Lore SHA 从 `(TBD)` 补齐为
    `7e75576aaf462b5c492d02651b4256734d2a6334`
  - 新增 **T02–T06 Evidence Index**：每任务完整 squash SHA、PR 号、证据文件
    新路径（T02=`7b81df8…`、T03=`66b520e…`、T04=`8130321…`、T05=`c152766…`、
    T06=`d797e6d…`）
  - 头部固化证据文件位置规则；证据文件内的历史自引用路径保留为快照不改

## M9 + H1（文案分支）— 状态账本纠偏

`docs/客户版任务清单-V3.md`：

1. **Header 状态行**（M9）：从"T02 已完成回归基线，M0 其余任务进行中"（与
   T03–T06 全 `[x]` 自相矛盾）改为真实状态——M0（T01–T04）与 M1 前置
   （T05–T06）完成、评审问题全部修复、T07 起待启动
2. **T05（§2）与 DB-03（§12.1）出口门文案**（H1 处理建议的"修订文案"分支，
   与已落地的代码修复配套）：从"API、Worker、测试均可使用 PG"改为如实描述
   ——PG 运行时基座就绪、测试可全量运行于 PG（CI 内嵌 PG16 真实执行）、
   连接回收/超时可观测；API/Worker 业务接入随 T24/T25 落地，此前 Worker 在
   PG 模式显式失败退出（非静默 exit 0）

## DB-04 补证 — schema diff（出口门第三项）

实机核验（两方言各自空库 `alembic upgrade head` 后对比
`wallet_transactions` 的部分唯一索引；复现库已清理）：

| Index | SQLite WHERE | PostgreSQL WHERE | 一致 |
| --- | --- | --- | --- |
| `uq_wallet_transactions_charge_order` | `type = 'CHARGE'` | `(type = 'CHARGE'::text)` | ✅ |
| `uq_wallet_transactions_reserve_round` | `type = 'RESERVE'` | `(type = 'RESERVE'::text)` | ✅ |
| `uq_wallet_transactions_terminal_round` | `type IN ('SETTLE', 'RELEASE')` | `(type = ANY (ARRAY['SETTLE'::text, 'RELEASE'::text]))` | ✅ |

025 之后，两方言在该表上的部分唯一索引语义完全对齐——C1 修复前 PG 的
terminal_round 无 WHERE 子句（评审实机复现已归档）。

## 评审报告入库

`docs/客户版V3-M0评审报告-2026-08-21.md`（独立评审产物，原样入库作为历史
记录；其 §9 的"Lore 提交 SHA：待回填"由本证据文件与账本索引闭环）。

## 验证

- 纯文档批次：无代码改动，后端测试基线与 main 一致（PR #36 合并后 610 项，
  CI 在三门禁中以 Python 3.12 + 内嵌 PG16 真实执行）
- 本地核验：T04 增补中的行号由 `grep -n` 实测生成；schema diff 由 SQLite
  （`sqlite_master`）与 PG（`pg_indexes`）实机对照

## 未测试项

- 无（文档批次）
