# M0 评审修复 P1 批次证据（H1 + H3 + M1–M6 + LOW-1/3）

> 对应评审报告：`docs/客户版V3-M0评审报告-2026-08-21.md` §4（H1、H3）、§5（M1–M6）、§6（LOW-1、LOW-3）
>
> 分支：`fix/customer-v3-m0-review-p1`，基线：`8925d16`（main）

## H1（代码部分）— Worker PG 模式静默 exit 0 → 显式失败退出

`app/generation_worker.py`：PG 模式 ready check 后不再 `return`（exit 0 会被
systemd `Restart=on-failure` 视为正常退出，静默丢掉 worker）。现在先
`close_pg_pool()` 释放连接池，再 `raise SystemExit(带说明消息)`——非零退出码让
supervision 明确看到"PG 任务循环未随 T05 交付"，而不是伪装成健康空闲。

回归：`test_worker_main_ready_check_in_pg_mode` 改为断言 `SystemExit` 非 0 +
SQLite 任务循环未被调用 + 池释放后可重建。

> H1 的另一半（DB-03 出口门文案修订与状态纠偏）在 P2 文档批次处理。

## H3 — env.py/alembic.ini 支持 VIDEO_REPLICA_DATABASE_URL

`migrations/env.py` 新增 `resolve_migration_url()`：

- `VIDEO_REPLICA_DATABASE_URL`（与 `app.db_pg` 同一运行时模式判据）优先于
  alembic.ini 默认值——运维直接 `VIDEO_REPLICA_DATABASE_URL=postgresql://...
  alembic upgrade head`，无需改 ini
- 裸 `postgresql://` / `postgres://` 自动重写为 `postgresql+psycopg://`
  （裸 scheme 会落到未安装的 psycopg2 方言——T06 发现的第一个方言问题）
- 测试的 `config.set_main_option` 显式覆盖在未设环境变量时仍生效
- online/offline 两条路径统一走该解析

`alembic.ini` 头部注释固化 URL 解析顺序与 offline 限制（见 M7）。

回归（subprocess 真实 CLI 闭环）：

- `test_alembic_env_var_targets_sqlite_file`：env var 指向 tmp SQLite →
  `python -m alembic upgrade head` 成功且库落在 env var 路径（证明覆盖 ini
  默认的 data/app.db），head 断言动态读取（与迁移链演进解耦）
- `test_alembic_env_var_dsn_runs_migrations_on_pg`：env var 用裸
  `postgresql://`（重写逻辑是被测对象）→ 真实 CLI 对全新 PG 库升级到 head

## M1 — 连接池健康检查与生命周期

`get_pg_pool()` 现配置 `check=ConnectionPool.check_connection`（取出连接前
探活，失效连接被替换而不是发放给业务）+ `max_lifetime=3600` /
`max_idle=600` / `timeout=30`。回归：`test_pool_applies_hygiene_parameters`
断言池实例上的四个参数（`pool._check`——`pool.check` 是按需执行检查的方法，
配置回调存于私有属性）。

## M2 — close_pg_pool 接入生产退出路径

- `app/main.py`：新增 FastAPI `lifespan`，shutdown 时 `close_pg_pool()`
  （SQLite 车道从未开池，为 no-op）
- `app/bootstrap.py`：PG ready check 后 `close_pg_pool()`（短命引导进程退出前
  释放池）
- `app/generation_worker.py`：SystemExit 前释放（见 H1）

## M3 — isolation 白名单

`pg_transaction(isolation=...)` 参数收窄为
`Literal["READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"]`；运行时在
**获取池连接之前**校验白名单（fail-fast），杜绝把任意字符串拼进
`SET TRANSACTION ISOLATION LEVEL`。回归：
`test_pg_transaction_rejects_unknown_isolation_level`（注入式载荷被
ValueError 拒绝，无需 PG 即可运行）。

## M4 — datetime 直返与配置解析复用

- 新增 `_as_datetime()`：psycopg3 原生返回的 tz-aware datetime 直接
  `isinstance` 收窄，`str()` 往返仅作回退
- `check_pg_ready()` 内 `resolve_database_config()` 从 3 次降为 1 次显式调用
  （+`get_pg_pool()` 内部 1 次），DSN 复用同一结果

## M5 — asyncpg/pytest-asyncio 移入 dev group

两者均仅被测试使用，从 runtime `dependencies` 移入 `[dependency-groups].dev`；
`uv.lock` 同步重生成。CI 使用 `uv sync --group dev`，行为不变；冻结规格
"psycopg3 + psycopg_pool only" 的 runtime 约束恢复一致。

## M6 — pg-fixture.sh test 子命令路径修复

`tests/test_postgres_fixture.py`（不存在）→ `tests/test_postgres_migrations.py`。

## M7（文档部分）— offline 模式限制固化

`run_migrations_offline()` docstring 与 `alembic.ini` 注释明确：Alembic offline
硬编码 `alembic_version.version_num VARCHAR(32)`，本项目 revision id 最长 33+
字符，offline 产出的脚本不可执行——真实升级与 DBA 审阅一律走 online 路径
（配合 H3 的 env var）。代码不改动（Alembic 行为无法在 offline 分支内修复）。

## LOW-1 / LOW-3

- LOW-1：`pg_transaction` 的 `try: ... except Exception: raise` 空操作已删除
  （注释保留在 with 行上方）
- LOW-3：worktree venv 以 Python 3.12.11 重建（对齐 CI 的
  `--python 3.12`、`requires-python >=3.12`、mypy/ruff target 3.12），
  本批次全部验证在 3.12 下执行

## 全量回归（Python 3.12.11）

- `pytest -q`：**607 passed**（main 基线 603 + 4 新测试），PG fixture 就绪
- `ruff check .` / `ruff format --check .`：All checks passed / 117 files
- `mypy app`（strict）：Success, no issues in 53 source files

## 未测试项

- systemd 真实环境下的 worker 重启行为（exit code 语义由 Python/系统保证）
