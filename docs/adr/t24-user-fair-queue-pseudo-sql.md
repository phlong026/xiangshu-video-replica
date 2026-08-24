# T24: 用户公平队列 - 伪 SQL 与边界条件分析（修订版）

## 状态

- **任务 ID**: T24
- **名称**: 冻结公平队列 ADR 和伪 SQL
- **阶段**: M4 (后续充值与用户公平队列)
- **优先级**: P1 (关键路径依赖)
- **规模**: S (0.5–1 人日)
- **Status**: [~] IN_PROGRESS（修订后冻结）

## 交付物清单

### ✅ 已完成

- [x] `docs/adr/adr-user-fair-queue-v1.md` - ADR 架构设计文档（含状态机与锁顺序章节）
- [x] 本文档 - 伪 SQL + 边界条件详细分析

### 🚧 待完成 (T25)

- [ ] Migration（`user_queue_cursors` 表 + 种子 + runtime_settings 开关）
- [ ] `generation.py::acquire_generation_task_lease` 重构（含存量 `datetime()` 方言替换）
- [ ] `test_customer_queue_fairness.py` 专项测试
- [ ] 压测报告（10,000 任务）

---

## 修订记录

本文件是 T24 分支原始版本（commit 6a1a325）的修订版，修订原因（与 ADR 修订记录对应）：

| # | 原问题 | 修订 |
| --- | --- | --- |
| 1 | DDL 用 `code_id UUID PRIMARY KEY` | 改为 `user_id TEXT PRIMARY KEY`（主线三表主键均 TEXT） |
| 2 | 粒度：主键 code_id 但语义"每用户并发 1" | 粒度统一为用户，主键 `user_id` |
| 3 | `datetime(locked_until) <= CURRENT_TIMESTAMP`（SQLite 方言） | `locked_until::timestamptz <= now()`（PG 原生）；存量代码同类问题见 §0.1 |
| 4 | `FOR UPDATE SKIP LOCKED WITH HOLD`（游标语法误用） | 删除 `WITH HOLD` |
| 5 | Step 3 失败"显式 UPDATE 回滚 Step 2"（伪回滚） | 删除；同一事务内 cursor 行已被锁，UPDATE 必然命中 |
| 6 | Pattern C 对 FAILED 不递减计数（用户永久饥饿） | 失败路径同样释放计数 |
| 7 | `ac.code_id`/`gt.code_id` 列不存在；`ac.batch_id = gb.id` 混用两个 batch 表 | 按真实链路 `task → batch → created_by_user_id` 重写 |
| 8 | 按行 `active_migration` 列做迁移开关 | `runtime_settings` 全局开关 |

### 0.1 存量 SQLite 方言（T25 一并处理）

`server/app/generation.py`（L3518-3574 等处）与 `server/app/character_image_generation.py` 中存在 `datetime(locked_until) <= CURRENT_TIMESTAMP`，这是 SQLite 函数，PostgreSQL 运行时**不存在** `datetime(text)` 函数。`generation_tasks.locked_until`/`next_poll_at` 是 TEXT 列（存 ISO 8601 字符串），PG 下正确写法是 `locked_until::timestamptz <= now()`。T25 实现新队列时须将相关 SQL 一并替换（当前无 PG 侧测试覆盖该函数，属存量遗留）。

### 0.2 真实表结构核对（主线，迁移 001/027/028/029）

- `users.id` TEXT PK
- `generation_batches`: `id` TEXT PK / `project_id` FK / `created_by_user_id` FK→users / `idempotency_key` / `request_hash` / `request_snapshot_json` / `status`
- `generation_tasks`: `id` TEXT PK / `batch_id` FK→generation_batches / `status`（PENDING/QUEUED/SUBMITTING/RUNNING/ARCHIVING/SUCCEEDED/FAILED…）/ `locked_by` / `locked_until` TEXT / `next_poll_at` TEXT / `attempt` / …—— **无 `code_id` 列**
- `activation_codes`: `id` TEXT PK / `batch_id` FK→**activation_code_batches**（不是 generation_batches）/ `bound_user_id` / `masked_code` / `code_digest` / …—— **无 `code_id` 列**
- 激活码↔任务无直接关联：任务经 `batch_id → generation_batches.created_by_user_id` 归属用户

---

## 伪 SQL 规范

### 1. Schema DDL

```sql
-- ================================
-- Table: user_queue_cursors
-- Purpose: Per-user queue cursor for fair scheduling
-- Owner: T24/T25 (M4 Fair Queue)
-- Granularity: one row per user (每用户并发 1 的语义)
-- ================================

CREATE TABLE user_queue_cursors (
    -- Primary key: user dimension (per-user concurrency = 1)
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,

    -- Last time this user was given a task opportunity (round-robin key)
    last_dispatched_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Current running tasks count for this user (per-user concurrency limit)
    running_tasks_count INT NOT NULL DEFAULT 0,

    -- Audit fields (append-only principle)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Constraint: Cannot have negative or >1 running tasks (configurable)
    CONSTRAINT valid_running_tasks
        CHECK (running_tasks_count >= 0 AND running_tasks_count <= 1)
);

-- ================================
-- Indexes for hot path performance
-- ================================

-- Rotation hot path: idle users ordered by least-recently-dispatched
CREATE INDEX idx_user_queue_cursors_rotation
    ON user_queue_cursors (last_dispatched_at ASC)
    WHERE running_tasks_count = 0;

-- (删除原版 idx_user_queue_cursors_active_lock / _covering：
--  主键即 user_id，无 code 维度的"活动锁"查询；覆盖索引收益由
--  部分索引 + 行宽（4 列）权衡后放弃，T27 压测可按 EXPLAIN 再加)

-- ================================
-- Triggers for updated_at maintenance
-- ================================
-- 核对结果：主线 001 迁移只有 _updated_at() 列定义
-- （server_default CURRENT_TIMESTAMP），**没有** BEFORE UPDATE trigger；
-- updated_at 的维护需在 T25 迁移中随表一并定义（下述内联 trigger）。

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER trg_user_queue_cursors_updated_at
    BEFORE UPDATE ON user_queue_cursors
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
```

### 2. Core Transaction Patterns

> 锁序约定（详见 ADR 锁顺序章节）：**所有路径先 cursor 行、后 task 行**。

#### Pattern A: User Candidate Check + Task Lease Acquisition

**Use Case**: Worker 领取任务时的核心事务，保证原子性的用户检查和任务锁定

```sql
-- 注意：无 WITH HOLD（普通 SELECT 无此子句）；无 datetime()（SQLite 方言）
BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- Step 1: Get next idle user with row-level lock (SKIP LOCKED 防重复领取)
SELECT user_id
INTO cur_user_id
FROM user_queue_cursors
WHERE running_tasks_count = 0
ORDER BY last_dispatched_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;

-- If no idle user, rollback and return None
IF cur_user_id IS NULL THEN
    ROLLBACK;
    RETURN NULL;
END IF;

-- Step 2: Acquire generation task lease within that user (per-user FIFO)
UPDATE generation_tasks
SET status = 'SUBMITTING',
    locked_by = v_worker_id,
    locked_until = (now() + INTERVAL '60 seconds'),
    updated_at = now()
WHERE id = (
    SELECT t.id
    FROM generation_tasks t
    JOIN generation_batches b ON b.id = t.batch_id
    WHERE b.created_by_user_id = cur_user_id
      AND t.status IN ('PENDING', 'QUEUED')
      AND (t.locked_until IS NULL OR t.locked_until::timestamptz <= now())
      AND (t.next_poll_at IS NULL OR t.next_poll_at::timestamptz <= now())
    ORDER BY t.created_at, t.id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id INTO v_task_id;

-- 该用户队列空或唯一可用任务被其他 worker 锁定 → 子查询返回 NULL，
-- UPDATE 不命中。此时：ROLLBACK 释放 cursor 锁，重试下一个用户
-- （外层循环重新执行 Step 1，最多 N 次，N 为配置的空转上限）。
-- 注意：不要用"显式 UPDATE 回滚 Step 2"——同一事务内 ROLLBACK 即撤销
-- 一切；Step 2 的 UPDATE 在事务内只对命中的行可见，无需补偿。
IF v_task_id IS NULL THEN
    ROLLBACK;
    RETURN NULL;  -- 调用方重试（或本函数内循环）
END IF;

-- Step 3: Update cursor counter and rotation timestamp
-- （cursor 行已在 Step 1 被 FOR UPDATE 锁定，此 UPDATE 必然命中，
--   不需要 RETURNING 校验——同一事务内的防御性检查是伪防御）
UPDATE user_queue_cursors
SET running_tasks_count = running_tasks_count + 1,
    last_dispatched_at = now()
WHERE user_id = cur_user_id;

-- Step 4: Commit success
COMMIT;

-- Return task details to worker
RETURN load_worker_task(v_task_id);
```

**事务隔离级别说明**：READ COMMITTED 足够——cursor 行靠显式 `FOR UPDATE` 串行化，任务行靠 `FOR UPDATE SKIP LOCKED` 串行化，不需要 SERIALIZABLE 的开销。（原版标注 SERIALIZABLE 是过度设计；T19 会话租约同款路径亦用行锁 + READ COMMITTED。）

#### Pattern B: Task Completion - Release Counter

**Use Case**: Provider 提交成功，归档完成

```sql
-- 锁序：先 cursor（Step 1），再 task（Step 2）
BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- Step 1: Decrement running count and update timestamp
UPDATE user_queue_cursors
SET running_tasks_count = GREATEST(running_tasks_count - 1, 0),
    last_dispatched_at = now()
WHERE user_id = v_user_id;

-- Step 2: Mark task terminal state
UPDATE generation_tasks
SET status = 'ARCHIVING',   -- 或 SUCCEEDED（按完成路径既有状态机）
    locked_by = NULL,
    locked_until = NULL,
    updated_at = now()
WHERE id = v_task_id AND locked_by = v_worker_id;

COMMIT;
```

#### Pattern C: Task Failure - Release Counter（修订：必须释放）

**Use Case**: Provider 返回失败，RESERVE 阶段回滚

```sql
-- 修订要点：FAILED 是终态，running_tasks_count 必须 -1。
-- 原版"不递减"会让用户永久占用并发名额（running_tasks_count=1），
-- 该用户从此不再被 Pattern A 选中 → 永久饥饿。
BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- Step 1: Release the concurrency slot (same order: cursor first)
UPDATE user_queue_cursors
SET running_tasks_count = GREATEST(running_tasks_count - 1, 0),
    last_dispatched_at = now()
WHERE user_id = v_user_id;

-- Step 2: Mark task failed and clear the lock
UPDATE generation_tasks
SET status = 'FAILED',
    locked_by = NULL,
    locked_until = NULL,
    updated_at = now()
WHERE id = v_task_id AND locked_by = v_worker_id;

COMMIT;
```

#### Pattern D: Cold Start - Initialize New User Cursor

**Use Case**: 新用户首次激活时创建 cursor 记录

```sql
BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED;

-- Upsert cursor for new user (idempotent on code activation)
INSERT INTO user_queue_cursors (user_id, last_dispatched_at, running_tasks_count)
VALUES (v_user_id, now(), 0)
ON CONFLICT (user_id) DO NOTHING;

-- If already exists, do nothing (race condition guard)
COMMIT;
```

#### Pattern E: Cleanup Long-tail Inactive Users

**Use Case**: Maintenance job for idle users without recent activity

```sql
-- Risk: Over-cleanup can create fairness holes → Only run during low-traffic window
-- Safety: Use conservative timeout (e.g., 7 days) + dry-run mode first
-- 注意：清理后用户再次提交任务时经 Pattern D 重建 cursor。

BEGIN TRANSACTION ISOLATION LEVEL READ COMMITTED;

DELETE FROM user_queue_cursors
WHERE user_id IN (
    SELECT user_id FROM user_queue_cursors
    WHERE last_dispatched_at < (now() - INTERVAL '7 days')
      AND running_tasks_count = 0
    LIMIT 1000  -- Batch cleanup to avoid long transactions
    FOR UPDATE SKIP LOCKED
)
RETURNING user_id;

-- 审计表 user_queue_cleanup_audit 随 T25 迁移一并定义（DDL 见 §5），
-- 原版引用不存在的表，此处标注为待建。
-- INSERT INTO user_queue_cleanup_audit (user_id, cleaned_at, reason)
-- SELECT user_id, now(), 'idle_timeout' FROM deleted;

COMMIT;
```

### 3. Migration Data Seeding Strategy

**Use Case**: 从现有数据初始化 `user_queue_cursors`

```sql
-- Run as part of the T25 migration (dry-run first, then apply)
-- 修订要点：按 generation_batches.created_by_user_id 聚合用户任务，
-- 不 JOIN activation_codes（ac.batch_id 引用的是 activation_code_batches，
-- 与 generation_batches 是两张不同的表；任务与激活码无直接关联）。

-- Step 1: Dry-run analysis (DO NOT COMMIT)
SELECT
    b.created_by_user_id AS user_id,
    COUNT(t.id) AS total_tasks,
    SUM(CASE WHEN t.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING') THEN 1 ELSE 0 END) AS running_count,
    MAX(t.created_at) AS last_task_created_at
FROM generation_batches b
LEFT JOIN generation_tasks t ON t.batch_id = b.id
GROUP BY b.created_by_user_id
ORDER BY running_count DESC, last_task_created_at DESC;

-- Step 2: Apply seeding (once verified safe)
-- 注意：running_count 只能来自非终态任务；CHECK 约束会拒绝 >1 的行，
-- 若发现 running_count > 1 说明存量数据已违反每用户并发 1，先处置再种。
INSERT INTO user_queue_cursors (user_id, last_dispatched_at, running_tasks_count)
SELECT
    b.created_by_user_id AS user_id,
    COALESCE(MAX(t.created_at)::timestamptz, now()) AS last_dispatched_at,
    SUM(CASE WHEN t.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING') THEN 1 ELSE 0 END) AS running_tasks_count
FROM generation_batches b
LEFT JOIN generation_tasks t ON t.batch_id = b.id
GROUP BY b.created_by_user_id
ON CONFLICT (user_id) DO UPDATE SET
    last_dispatched_at = EXCLUDED.last_dispatched_at,
    running_tasks_count = EXCLUDED.running_tasks_count;

-- Verify results
SELECT COUNT(*) FROM user_queue_cursors;
SELECT SUM(running_tasks_count) FROM user_queue_cursors;
```

---

## 边界条件与竞态处理

### 1. Cold Start Race (多 Worker 同时领取同一用户任务)

**Scenario**: 两个 Worker 几乎同时发起查询，都找到同一个空闲 cursor

**Solution**: PG `FOR UPDATE SKIP LOCKED` 确保串行化

```sql
-- Worker 1: Acquires lock on user X
SELECT user_id FROM user_queue_cursors
WHERE running_tasks_count = 0
ORDER BY last_dispatched_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;  -- OK, gets lock

-- Worker 2: Skips X because it's locked
SELECT user_id FROM user_queue_cursors
WHERE running_tasks_count = 0
ORDER BY last_dispatched_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;  -- SKIPS X, moves to next user

-- Result: Only one worker proceeds, other retries later
```

### 2. Stuck Cursor (任务长时间不释放)

**Scenario**: Worker crash 后 cursor 的 `running_tasks_count` 无法自动降低

**Solution**: 结合 `generation_tasks.locked_until` + 后台维护任务

```sql
-- Maintenance timer runs every 5 minutes
-- 修订要点：原版 SELECT gt.code_id 无此列；经 batch 归属用户。
-- 锁序：维护任务先锁 cursor（Step 1）再改 task（Step 2），与主路径一致。
BEGIN;

-- Step 1: Release slots for expired leases, restore tasks to QUEUED
UPDATE generation_tasks t
SET status = 'QUEUED',
    locked_by = NULL,
    locked_until = NULL,
    updated_at = now()
WHERE t.id IN (
    SELECT t2.id
    FROM generation_tasks t2
    JOIN generation_batches b ON b.id = t2.batch_id
    WHERE t2.status IN ('SUBMITTING', 'RUNNING')
      AND t2.locked_by IS NOT NULL
      AND t2.locked_until::timestamptz < (now() - INTERVAL '5 minutes')
    FOR UPDATE SKIP LOCKED
);

-- Step 2: Decrement the affected users' counts
UPDATE user_queue_cursors uqc
SET running_tasks_count = GREATEST(running_tasks_count - 1, 0),
    last_dispatched_at = now()
WHERE uqc.user_id IN (
    SELECT DISTINCT b.created_by_user_id
    FROM generation_tasks t2
    JOIN generation_batches b ON b.id = t2.batch_id
    WHERE t2.status IN ('SUBMITTING', 'RUNNING')
      AND t2.locked_by IS NOT NULL
      AND t2.locked_until::timestamptz < (now() - INTERVAL '5 minutes')
);

-- Alert if stuck count exceeds threshold
SELECT COUNT(*) FROM generation_tasks
WHERE status IN ('SUBMITTING', 'RUNNING')
  AND locked_by IS NOT NULL
  AND locked_until::timestamptz < now() - INTERVAL '5 minutes';
```

### 3. Starvation Prevention (防止某些用户长期得不到机会)

**Scenario**: 活跃用户不断有新的任务提交，导致老用户被持续忽略

**Solution**:
- Base implementation: FIFO by `last_dispatched_at` 已足够（每完成一个任务就轮换）
- Enhanced (optional): 增加久候权重——**注意不能在 ORDER BY 中把时间戳与数值直接相加**（类型不同）

```sql
-- Optional bonus scoring for starvation prevention（应用层计算排序键）
-- 正确做法：ORDER BY 只接受单一表达式；加权需在应用层算出排序键，
-- 或把 bonus 折叠进时间轴（如"每空闲 N 分钟视为早 N 分钟"）：
SELECT user_id
FROM user_queue_cursors
WHERE running_tasks_count = 0
ORDER BY
    CASE
        WHEN last_dispatched_at < now() - INTERVAL '30 minutes' THEN now() - INTERVAL '60 minutes'
        WHEN last_dispatched_at < now() - INTERVAL '10 minutes' THEN last_dispatched_at
        ELSE last_dispatched_at
    END ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

### 4. Migration Safety (迁移过程中的双轨运行)

**Scenario**: 旧任务继续使用全局 FIFO，新任务走公平队列

**Solution**: `runtime_settings` 全局开关 + 时间窗口过渡
（修订要点：原版在表上加 `active_migration` 列是按行的"开关"，语义错误——迁移开关是运行时全局配置；主线已有 `runtime_settings` 单例表，沿用）

```sql
-- T25 迁移追加 runtime_settings 列（或既有字段）：
-- fair_queue_enabled BOOLEAN NOT NULL DEFAULT FALSE

-- 领取路径入口检查（应用层读开关，与 read_runtime_limits 同模式）：
SELECT fair_queue_enabled FROM runtime_settings WHERE id = 1;
-- FALSE → 走旧全局 FIFO；TRUE → 走 Pattern A

-- 灰度完成、观察无误后开启：
UPDATE runtime_settings SET fair_queue_enabled = TRUE WHERE id = 1;
```

### 5. Idempotency Guard (幂等性保证)

**Scenario**: 重试请求可能多次更新同一个 cursor

**Solution**: 不需要 version 乐观锁——领取是**单事务**（Pattern A），同一 cursor 行被 `FOR UPDATE` 持有到 COMMIT/ROLLBACK，事务外不可见中间态；重试发生在事务外，天然串行。原版的 version + RETRY 适用于**跨事务**的多步更新，此处不成立，删除。

```sql
-- 保留的幂等性保证（T23 既有机制）：
-- - 任务领取本身幂等：Pattern A 的 UPDATE ... WHERE id = (SELECT ... LIMIT 1)
--   每次只领取一个任务，不重复；
-- - 客户端重试由既有 customer_idempotency_envelopes / admin_write_idempotency
--   机制覆盖（与本表无关）。
```

---

## 状态机与计数不变量（T25 测试断言基准）

| 事件 | task.status 迁移 | running_tasks_count |
| --- | --- | --- |
| Pattern A 领取 | PENDING/QUEUED → SUBMITTING | +1（0→1） |
| 提交成功 | SUBMITTING → RUNNING | 不变（1） |
| Pattern B 完成归档 | RUNNING → ARCHIVING/SUCCEEDED | -1（1→0） |
| Pattern C 失败 | SUBMITTING/RUNNING → FAILED | -1（1→0） |
| 维护任务超时接管 | SUBMITTING/RUNNING → QUEUED | -1（1→0） |

**不变量**：
- 任何时刻 `running_tasks_count ∈ {0, 1}`（CHECK 约束强制）
- `running_tasks_count = 1 ⟺ 该用户恰有一个非终态（SUBMITTING/RUNNING）任务持有租约`
- FAILED/SUCCEEDED/ARCHIVING 后计数必为 0（终态不占名额）

---

## Test Matrix (T25 专项测试覆盖)

### Core Functionality Tests

| Test Name | Scenario | Expected Outcome | Assert |
| --- | --- | --- | --- |
| `test_fair_queue_single_user` | Single user, multiple tasks | Tasks processed sequentially | `running_tasks_count = 1` at peak |
| `test_fair_queue_multi_user_round_robin` | 4 users × 3 tasks each | Each user gets exactly 1 task before repeat | Round-robin order confirmed |
| `test_fair_queue_concurrent_workers` | 4 Workers × 10 tasks | No duplicate assignments | Unique task assignments across workers |
| `test_fair_queue_idle_user_cleanup` | User inactive for 7 days | Cursor removed from table | Count reduced after cleanup job |

### Edge Case Tests

| Test Name | Scenario | Expected Outcome | Assert |
| --- | --- | --- | --- |
| `test_fair_queue_worker_crash_recovery` | Worker dies mid-task | Cursor count auto-recovered | `running_tasks_count` resets after timeout |
| `test_fair_queue_new_user_onboarding` | New user activates code | Cursor created automatically | Record exists in table |
| `test_fair_queue_starvation_prevention` | Power user floods queue | Regular users still get opportunities | Response time variance < 2x |
| `test_fair_queue_migration_safety` | Mixed old/new task paths | Both work correctly | Zero data loss during transition |
| `test_fair_queue_failure_releases_slot` | Task fails | Cursor count returns to 0 | User schedulable again (修订 #6 回归) |

### Stress Tests (T27)

| Test Name | Scale | Target Metric | Pass Criteria |
| --- | --- | --- | --- |
| `test_fair_queue_10k_tasks` | 10,000 tasks, 100 users | p95 latency | < 100ms per task acquisition |
| `test_fair_queue_lock_contention` | 10 Workers competing | Lock wait time | < 5ms average, < 50ms p99 |
| `test_fair_queue_memory_leak` | 1 hour continuous run | Memory growth | < 100MB/hour increase |

---

## 安全与合规考虑

### Data Privacy
- `user_queue_cursors` 只含 users 外键（无 PII 直存）
- 访问控制经 FK + RBAC 层（T21 fencing）

### Audit Requirements
- 所有更新记录 `updated_at` 时间戳
- Append-only 审计表推荐：`user_queue_operations_audit`（T25 迁移定义，不引用不存在表）
- 迁移历史由 Alembic revisions 跟踪

### Rate Limiting Integration
- 既有 `security_rate_limits` schema（T15）不得与公平队列锁冲突
- 认证限流与队列协调分表

---

## 运行手册

### Monitoring Dashboards

1. **Real-time Metrics**
   - `user_queue_cursors` 行数按 `running_tasks_count` 值分组
   - 平均锁等待时间（pg_stat_activity）
   - 任务领取延迟直方图

2. **Alert Thresholds**
   - P1: `running_tasks_count > 1` 出现（约束违例）
   - P2: 平均锁等待 > 10ms
   - P3: 任一用户闲置 > 24 小时未被清理

### Rollback Plan

**If migration fails:**

1. Disable new cursor usage immediately（runtime_settings 开关置 FALSE，回到全局 FIFO）：
   ```sql
   UPDATE runtime_settings SET fair_queue_enabled = FALSE WHERE id = 1;
   ```
2. Restore from backup (if needed):
   ```bash
   pg_restore -d customer_v3_test backup_pre_migration.sql
   ```
3. Notify stakeholders and schedule maintenance window

---

*Document Created: 2026-08-25 by Architecture Team*
*T24 Owner: TBD | Reviewer: TBD*
*Dependencies: T21 (fencing) completed before implementation*
