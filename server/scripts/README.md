# server/scripts 使用说明

本目录是服务端离线维护工具，**不随业务部署**，也没有任何调度
（CI/cron/systemd 均不调用，仅被 `tests/test_cw057_cli_pg_entry.py` 的
CLI 入口名单断言覆盖）。

> 2026-11-01 T3 批 C-4：`sqlite_to_postgres.py` 与
> `reconcile_customer_billing.py`（一次性 SQLite→PG 割接双工具，依赖已删的
> `app.backup`/`app.db`）随 SQLite lane 退役删除——业务确认无存量桌面快照。

## 一次性工具（oneoff）

以下四个只做历史数据补齐或特权操作，跑完即弃，勿当作例行程式复用：

- `backfill_generation_thumbnails.py` — 历史生成记录缩略图回填
- `backfill_content_objects.py` — 内容对象表回填
- `reset_customer_password.py` — 客户密码重置（特权操作）
- `issue_admin_exchange_credential.py` — 签发管理端换登凭据（特权操作）

## 运维脚本

`purge_*.py`、`reconcile_*.py`、`close_expired_native_orders.py`、
`check_ops_alerts.py`、`pitr_recovery_facts.py` 为可重复执行的运维入口，
由运维按需手动运行。
