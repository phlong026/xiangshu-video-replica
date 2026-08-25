# T27 Evidence Report - 队列压测、热索引与连接池调优

## Task Summary

**任务**: T27 — 队列压测、热索引与连接池调优  
**状态**: `AUTOMATED_VERIFIED`  
**完成日期**: 2026-08-25  
**前置**: T25（公平队列）、T26（崩溃恢复/不确定路径）  
**DB**: `t27_queue_load_test`（专属 fixture 库,`alembic upgrade head` 到 revision-041,用完即删）

---

## §1 任务门禁与本报告证据对照

| T27 门禁（任务清单） | 证据 | 结果 |
|----------------------|------|------|
| 保存 10,000 任务 explain 证据 | §2 三个热路径 EXPLAIN,全部索引计划,无 Seq Scan | ✅ |
| 锁等待证据 | §3 4 worker 全量 drain:10,000/10,000 恰好一次,`pg_stat_database.deadlocks=0`,`pg_locks` 未授予=0 | ✅ |
| 连接等待证据 | §4 池饱和:第 9 个请求排队,释放 1 条连接后 ~500ms 内完成(默认 pool timeout 30s) | ✅ |
| 吞吐证据 | 10,000 任务 4 worker 全量消费 22.9s / 20.9s / 23.3s ≈ 429–478 tasks/s(三次运行) | ✅ |

### 结论

- **无需新增索引**:现有 `idx_user_queue_cursors_rotation`（030 部分索引）、`idx_generation_tasks_lease`（001）、`idx_generation_tasks_active_attention`（017）、`uq_generation_batches_user_project_key` 在 10k 行下已被规划器全部采用,热路径零 Seq Scan。T27 无新迁移。
- **连接池无需调整**:`min_size=1 / max_size=8 / ceiling=64 / timeout=30s`（`db_pg.py`）在饱和时正确排队且可恢复。

---

## §2 热路径 EXPLAIN（10,000 行）证据

测试: `tests/test_queue_load_10k.py::test_explain_hot_paths_use_indexes_at_10k`

种子: 200 用户 × 50 任务 = 10,000 条 PENDING `generation_tasks`、200 条 `user_queue_cursors`（全部 idle）。

### 2.1 轮转扫描（Pattern A 每轮第一条）

```sql
SELECT user_id FROM user_queue_cursors
WHERE running_tasks_count = 0
ORDER BY last_dispatched_at ASC LIMIT 1
```

```
Limit (actual time=0.014..0.015 rows=1 loops=1)
  ->  Index Scan using idx_user_queue_cursors_rotation on user_queue_cursors (actual time=0.014..0.014 rows=1 loops=1)
Planning Time: 0.656 ms
Execution Time: 0.035 ms
```

`idx_user_queue_cursors_rotation`（部分索引 `WHERE running_tasks_count = 0`）直接命中 idle 用户,0.035 ms。

### 2.2 按用户领取子查询（`_acquire_fair_queue_lease` 的 UPDATE 内子查询,去 FOR UPDATE）

```sql
SELECT t.id FROM generation_tasks t
WHERE (t.status IN ('PENDING','QUEUED') OR (t.status='SUCCEEDED' AND t.archive_status='ARCHIVE_FAILED' ...))
  AND (t.locked_until IS NULL OR t.locked_until::timestamptz <= now())
  AND (t.next_poll_at IS NULL OR t.next_poll_at::timestamptz <= now())
  AND EXISTS (SELECT 1 FROM generation_batches b WHERE b.id = t.batch_id AND b.created_by_user_id = 'u001')
ORDER BY t.created_at, t.id LIMIT 1
```

```
Limit (actual time=0.435..0.437 rows=1 loops=1)
  ->  Sort (actual time=0.434..0.436 rows=1 loops=1)
        Sort Key: t.created_at, t.id
        Sort Method: top-N heapsort  Memory: 25kB
        ->  Nested Loop (actual time=0.350..0.396 rows=50 loops=1)
              ->  Index Scan using uq_generation_batches_user_project_key on generation_batches b (actual time=0.010..0.011 rows=1 loops=1)
                    Index Cond: (created_by_user_id = 'u001'::text)
              ->  Bitmap Heap Scan on generation_tasks t (actual time=0.338..0.377 rows=50 loops=1)
                    Recheck Cond: ((batch_id = b.id) AND ((status = ANY ('{PENDING,QUEUED}'::text[])) OR (status = 'SUCCEEDED'::text)))
                    Filter: (...归档重试/租约/轮询谓词...)
                    Heap Blocks: exact=2
                    ->  BitmapAnd
                          ->  Bitmap Index Scan on idx_generation_tasks_active_attention (actual time=0.020..0.021 rows=50 loops=1)
                                Index Cond: (batch_id = b.id)
                          ->  BitmapOr
                                ->  Bitmap Index Scan on idx_generation_tasks_lease (actual time=0.290..0.290 rows=10000 loops=1)
                                      Index Cond: (status = ANY ('{PENDING,QUEUED}'::text[]))
                                ->  Bitmap Index Scan on idx_generation_tasks_lease (actual time=0.001..0.001 rows=0 loops=1)
                                      Index Cond: (status = 'SUCCEEDED'::text)
Planning Time: 1.907 ms
Execution Time: 0.574 ms
```

规划器组合三个索引：用户批次唯一索引（1 行）+ `idx_generation_tasks_active_attention`（batch 位图）+ `idx_generation_tasks_lease`（status 位图,10k 行全走索引),`BitmapAnd` 交后再回表 2 个 heap block。**无 Seq Scan**。

### 2.3 释放查找（Pattern B/C 挂载点）

```sql
SELECT b.created_by_user_id FROM generation_tasks t
JOIN generation_batches b ON b.id = t.batch_id
WHERE t.id = 'task-u001-0'
```

```
Nested Loop (actual time=0.023..0.024 rows=1 loops=1)
  ->  Index Scan using generation_tasks_pkey on generation_tasks t (actual time=0.015..0.015 rows=1 loops=1)
        Index Cond: (id = 'task-u001-0'::text)
  ->  Index Scan using generation_batches_new_pkey on generation_batches b (actual time=0.006..0.006 rows=1 loops=1)
        Index Cond: (id = t.batch_id)
Planning Time: 2.012 ms
Execution Time: 0.048 ms
```

两个主键索引点查,0.048 ms。

---

## §3 四 Worker 全量消费 10k:无重复领取、零死锁、吞吐

测试: `tests/test_queue_load_10k.py::test_four_workers_drain_10k_without_double_claim`

4 个线程各自循环: fenced `pg_transaction` 内 `acquire_generation_task_lease`（真实业务函数,含 cursor SKIP LOCKED → 任务 UPDATE RETURNING）→ 模拟付费处理 → 第二个 fenced 事务 `SUCCEEDED+ARCHIVED` + `release_user_queue_slot_for_task`。

```text
--- drain: 10000 tasks in 23.3s = 429 tasks/s
--- deadlocks=0 pending_locks=0 terminal=10000 stuck_cursors=0
```

三次运行吞吐: 437 / 478 / 429 tasks/s。

断言（全部通过）:
- `len(collected) == 10000`、每个 task_id 恰被一个 worker 领取（disjoint ∪ complete）
- `pg_stat_database.deadlocks == 0`、`pg_locks WHERE NOT granted == 0`
- terminal 任务数 == 10000、`user_queue_cursors.running_tasks_count > 0` 条数 == 0（无 stuck cursor）

这同时补上了 T25 遗留的「四 Worker 量级验证」挂账（T25 行: 「四 Worker 量级验证随 T27 10k 压测」）。

---

## §4 连接池饱和:排队与恢复

测试: `tests/test_queue_load_10k.py::test_pool_saturation_queues_within_timeout`

8 条连接全部借出（`DEFAULT_POOL_MAX=8`）→ 第 9 个 `pg_transaction()` 请求进入池等待队列 → 0.5s 后确认仍存活（已排队）→ 释放 1 条 → 请求 ~500ms 内完成（等待即被唤醒,无崩溃、无超时异常）。

```text
--- pool wait after freeing one connection: 500 ms
```

关键实现细节（测试注释有记录）: 必须同时持有 `pg_transaction()` 返回的 context manager 对象——若只持有 Connection,生成器对象被 GC 后会在 `yield conn` 处抛 GeneratorExit,内层 `with` 全部展开并 `putconn`,池永远不会真正饱和。

---

## §5 执行命令与文件变更

### 测试执行（PG fixture 端口 5433,专属库 `t27_queue_load_test` 用完即删）

```bash
$ cd server
$ uv --cache-dir ../.uv-cache run --project . --locked python -m pytest tests/test_queue_load_10k.py -q -p no:logging -s
3 passed in 27.90s
```

### 文件变更

| 文件 | 类型 | 说明 |
|------|------|------|
| `server/tests/test_queue_load_10k.py` | 新增 | 3 测试：EXPLAIN 索引证据 / 4 worker 10k drain（恰好一次 + 零死锁 + 吞吐）/ 池饱和排队恢复 |

### 代码质量门禁

| 门禁 | 状态 |
|------|------|
| `ruff check tests/test_queue_load_10k.py` | ✅ Pass |
| `ruff format --check tests/test_queue_load_10k.py` | ✅ Pass |
| `mypy tests/test_queue_load_10k.py` | ✅ 零错误 |

### 无业务代码改动

T27 结论是「现有索引与池配置已达标」,因此本轮**未修改** `server/app/` 下任何文件、无新迁移。回归风险面为零（纯新增测试文件）。

---

## §6 结论与后续

- M4 出口门禁中 T27 压测证据齐备:10k 行索引覆盖、并发消费零死锁、池饱和有界等待。
- 全量回归（~958 用例）按项目节奏在 PR 合并前统一执行一次。

---

*报告生成: 2026-08-25*  
*状态: READY FOR PR REVIEW*
