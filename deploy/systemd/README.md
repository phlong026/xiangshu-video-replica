# deploy/systemd 单元说明

本目录的 systemd 单元分两代，**并存是有意的**，对应两种部署形态；删除
任一代之前必须先由运维确认线上实际使用的形态（2026-10-31 T5 决策：
保留两代 + 本说明）。

## 单实例代（单机部署）

- `video-replica-api.service` —— 单 API 进程
- `video-replica-worker.service` —— 生成 Worker（`Requires=`/`After=`
  `video-replica-api.service`，与单实例 API 绑定）
- `video-replica-publish-worker.service` —— 发布 Worker（同样依赖单实例
  `api.service`）

单机部署时三者一起启用；worker 对 api 的依赖锚点写在单元里，改用多实例
形态时需同步迁移该依赖关系。

## @ 模板代（多实例 / HA 部署）

- `video-replica-api@.service` —— 模板实例化（如 `api@1`、`api@2`）
- `video-replica-worker@.service` —— 模板实例化（如 `worker@1..4`）

多实例契约由 `server/tests/test_customer_ha_smoke.py`
（`test_systemd_contract_has_two_api_ports_and_four_independent_workers`）
锁定：两个 API 端口 + 四个相互独立的 worker。宿主端口/副本参数见
`deploy/customer/compose.yaml` 的 api-1/api-2、worker-1..4 段（同一契约的
容器化形态）。

## 两代共用的定时单元

`video-replica-maintenance(.timer)`、`video-replica-ops-alerts(.timer)`、
`video-replica-pitr-backup(.timer)` 与部署形态无关：维护巡检、告警与
pg_basebackup+WAL 的 PITR 备份（客户侧唯一备份通道，见
`deploy/postgres/`）。
