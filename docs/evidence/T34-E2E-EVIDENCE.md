# T34-E2E Evidence Report - 客户全链路 E2E（M4 出口门禁顺延 M5）

## Task Summary

**任务**: T34-E2E — 客户全链路竖切 E2E（激活 → 任务 → 第二设备 → 冲突 → 切换 → 续充）  
**状态**: `AUTOMATED_VERIFIED`  
**完成日期**: 2026-08-25  
**前置**: T21（会话 fencing 写路由）、T22（ZPay 续充）、T25（公平队列）、T26（崩溃恢复）、T27（压测）  
**DB**: `t34_chain_e2e`（专属 fixture 库，`alembic upgrade head` 到 revision-040，用完即删）

---

## §1 任务门禁与本报告证据对照

| T34-E2E 门禁（任务清单/CORE-05） | 证据 | 结果 |
|----------------------------------|------|------|
| 激活 → 项目 → 任务 → 归档 → 结算 主链闭环 | §3 链路 1：激活→登录→建项目→锁定 Prompt→批次 QUEUED→真实 worker 消费→SUCCEEDED/ARCHIVED→批次 SUCCEEDED→公平队列游标归零；§4 链路 2 续充闭环 | ✅ |
| 第二设备 → 冲突 → 切换 链路 | §4 链路 2：enroll 202→approve→同键 201（slot2）→登录 409 OTHER_DEVICE_ONLINE→显式 switch 201（epoch 顶替）→旧 token 写路由 401 SESSION_REPLACED | ✅ |
| 组件测试或单接口 smoke 不算主链闭环 | 全程 `TestClient` 走真实客户 API 路由（激活/登录/设备/会话/项目/Prompt/批次/续充/回调），worker 用真实 `run_next_generation_task`（fenced 每任务事务），仅分析/上传车道产物（shot_card 版本、首帧资产与确认链）直插建模 | ✅ |

---

## §2 测试设计

文件: `server/tests/test_customer_chain_e2e.py`（2 个测试）。

- **fixture 链**: module 级 `chain_dsn`（专属库 + alembic upgrade head，用完即删）→ 函数级 `route_state`（TRUNCATE 全套表 + 种子 admin_u + `runtime_settings`（`fair_queue_enabled=true`、batch 上限 100）+ ZPay `provider_settings`（Fernet 加密，merchant-123/merchant-secret））→ `customer_app`（14 个 router 真实装配，仅存储/Provider/抽取依赖用 `lambda: object()` 惰性替身——本链路 provider=fake_h3、worker 存储直接注入，业务路由从不触达）→ `client`。
- **会话 fencing 全程生效**: 所有客户写路由走 T21 的 `BusinessDbDep` → `fenced_pg_transaction`（行锁 + epoch/lease 全量重比对），E2E 断言的两条 401/409 语义（SESSION_REPLACED / OTHER_DEVICE_ONLINE）正是这条栅栏的真实输出。
- **worker 驱动**: `_drain_pg_worker` 循环 `pg_transaction`（每任务独立 fenced 事务）内 `BusinessConnection.postgres(raw)` + `run_next_generation_task(provider=FakeH3Provider(), storage=FakeStorageAdapter(...), first_frame_storage=...)`——与 `run_pg_worker_once` 同形。
- **直插边界（诚实声明）**: `versions`（shot_card 含 10s 时长与两镜头、无 `source_analysis_version_id` 即无 stale 检查）与 `assets`（首帧）以及 `first_frame_candidates`/`first_frame_selection` 确认链为直插——它们建模分析/上传/首帧确认车道的产物。除此之外的每一步（激活、登录、项目、脚本、编译、锁定、批次、续充、回调、冲突、切换）全部走真实 API 与真实业务函数。真实 COS 上传与视频拆解不在本门禁范围（CORE-02/03 后续任务）。
- **调研修正**: 初版测试暴露 compile 路由要求「首帧确认」（`FIRST_FRAME_CONFIRMATION_REQUIRED`）——`first_frame_candidates` + `first_frame_selection` 两版本链；以及批次读取路由为桌面身份认证（`VIDEO_REPLICA_DESKTOP_USER_ID`），不属客户链路，终态改由 DB 断言承载。两处均已按真实契约收敛。

---

## §3 链路 1:激活 → 任务 → 归档

测试: `test_customer_chain_activation_to_archived_task`

| 步骤 | API/操作 | 断言 |
|------|----------|------|
| 激活 | POST /api/customer/activate（code+fingerprint，Idempotency-Key） | 201，含 user_id/device_id/device_token/session_token |
| 登录 | POST /api/customer/sessions/login（device_token） | 201 session_token |
| 建项目 | POST /api/projects（session_token） | 201，`owner_user_id == user_id`（fenced 写路由） |
| 脚本 | POST /api/projects/{id}/scripts（shot_card 版本） | 200 script_version_id |
| 编译 | POST /api/projects/{id}/prompts/compile（首帧确认链已直插） | 200 prompt_version_id |
| 锁定 | POST /api/projects/{id}/prompts/{id}/lock | 200，payload.status == LOCKED |
| 批次 | POST /api/projects/{id}/generation-batches（quantity 1，provider fake_h3） | 200，QUEUED + tasks[0] PENDING + prompt_snapshot LOCKED |
| Worker | `_drain_pg_worker` 真实消费 | 恰处理 1 个任务 |
| 终态 | DB 断言 | task SUCCEEDED+ARCHIVED、result_asset_id 非空、locked_by 已清空；批次 SUCCEEDED；`user_queue_cursors.running_tasks_count == 0`（公平队列槽位归还） |

关键语义：任务经 T26 的每任务 fenced 事务归档并落 result asset；归档完成后公平队列游标归零，下一个任务可立即被领取——T25/T26/T27 的机制在真实 API 链路上贯通验证。

---

## §4 链路 2:第二设备 → 冲突 → 切换 → 续充

测试: `test_customer_chain_second_device_conflict_switch_recharge`

| 步骤 | API/操作 | 断言 |
|------|----------|------|
| 激活 | POST /api/customer/activate | 201（session epoch 1） |
| 登录 | POST /api/customer/sessions/login | 201（epoch 2） |
| 配对 | POST /api/customer/devices/enroll（第二设备，Idempotency-Key） | 202 pairing_request_id |
| 批准 | POST /api/customer/device-pairings/{id}/approve（首设备 device_token） | 200 |
| 消费 | POST enroll 同键重放 | 201，slot_no == 2 + 新 device_token（T17 两阶段语义） |
| 冲突 | 第二设备 POST /api/customer/sessions/login | 409，detail.code == OTHER_DEVICE_ONLINE，online_slot_no == 1（不踢人） |
| 切换 | 第二设备 POST /api/customer/sessions/switch | 201，session_epoch == 3（激活 1→登录 2→切换 3），device_id 为新设备 |
| 旧会话顶替 | 旧 session_token POST /api/projects（写路由） | 401，detail.code == SESSION_REPLACED（fencing 栅栏逐出） |
| 续充 | 新 session POST /api/customer/recharge-orders {amount_fen:10000} | 201 PENDING，credits == 10 |
| 回调 | GET /api/payments/zpay/notify（MD5 签名参数） | 200 "success" |
| 结算 | DB 断言 | 订单 PAID + provider_trade_no + paid_at；钱包 available_credits ≥ 10；恰好 1 条 CHARGE（available_delta==10，idempotency_key LIKE 'zpay:charge:%'）；customer_session_state epoch 3 / device 新设备；SWITCH 事件存在（reason explicit_switch） |
| 重放 | GET notify 同参重复 | 200 "success"，CHARGE 仍恰好 1 条（幂等） |

关键语义：409 冲突与 201 切换并存（T20 显式 switch 契约）；切换后旧 token 在同一会话栅栏下立即失效；续充经真实签名回调落账且重放幂等——T22 契约在链路上贯通。

---

## §5 执行命令与文件变更

### 测试执行（PG fixture 端口 5433，专属库 `t34_chain_e2e` 用完即删）

```bash
$ cd server
$ uv --cache-dir ../.uv-cache run --project . --locked python -m pytest tests/test_customer_chain_e2e.py -q -p no:logging -s
2 passed in 3.37s
```

### 文件变更

| 文件 | 类型 | 说明 |
|------|------|------|
| `server/tests/test_customer_chain_e2e.py` | 新增 | 2 测试 + fixtures + helpers（约 540 行）：链路 1（激活→任务→归档）与链路 2（第二设备→冲突→切换→续充） |

### 代码质量门禁

| 门禁 | 状态 |
|------|------|
| `ruff check tests/test_customer_chain_e2e.py` | ✅ Pass |
| `ruff format --check tests/test_customer_chain_e2e.py` | ✅ Pass |
| `mypy tests/test_customer_chain_e2e.py` | ✅ 零错误 |

### 无业务代码改动

本轮**未修改** `server/app/` 下任何文件、无新迁移。E2E 开发中发现的两个契约细节（首帧确认链、批次读取路由桌面认证）均已在测试侧按真实行为收敛，未暴露业务缺陷。

---

## §6 结论与后续

- M4 出口门禁的客户竖切 E2E 证据齐备：激活、任务（真实 worker 消费至归档）、第二设备、冲突、切换（含 fencing 逐出）、续充（真实签名回调 + 幂等）在 PG 专属库上全链贯通。
- 本任务为纯新增测试 + 文档，无回归风险面。
- 全量回归（~1023 用例）按项目节奏在 PR 合并前统一执行一次。
- 仍未覆盖（明确不在本门禁范围，见 CORE-02/03/05 后续）：真实 COS 上传与视频拆解、真实浏览器双设备、真实 Provider/ZPay 小流量。

---

*报告生成: 2026-08-25*  
*状态: READY FOR PR REVIEW*
