# T35 Evidence Report — 客户版专项安全审查

## Task Summary

- **任务**：T35 — 认证、资金、并发、IDOR、CSRF、密钥专项审查
- **状态**：`AUTOMATED_VERIFIED (SEC-01/SEC-02)`；父任务保持 `[~]`
- **完成日期**：2026-08-26
- **基线**：`main@d15b3f9`（M4/M5 关闭复核 PR #69 合并后）
- **分支**：`feat/customer-v3-t35-security-review`
- **独立复审结论**：`0 Critical / 0 High / 0 Medium`；PR connector 1 个 P1 已修复

父任务没有标记完成：T35 的 DoD 同时引用 BILL-01/BILL-02 真实 ZPay 联合对账。该动作需要人工授权，并按用户要求与 COS/Provider 真实账号统一留到 T40 最后确认。本证据只把 SEC-01、SEC-02 提升到 `AUTOMATED_VERIFIED`，不声称 `STAGING_VERIFIED`、`REAL_CHAIN_VERIFIED` 或 `PRODUCTION_GO`。

## §1 审查范围与结论

审查覆盖认证与会话、管理端 cookie/CSRF/RBAC、owner/IDOR、账务原子性与幂等、并发 fencing、Host/CORS/转发地址信任、生产密钥、日志/凭据泄漏、激活码/签名 URL、CSV 注入、依赖漏洞与常见静态安全规则。

独立 reviewer 共执行三轮：

1. 首轮发现 1 个 Medium：客户 IP 限流直接依赖未验证的代理地址语义。
2. 修复后复审发现 2 个 Medium：Uvicorn 误开 proxy-header parsing 时仍可能先改写 ASGI peer；`PUBLIC_BASE_URL` 可能与浏览器公网 origin 分叉。
3. 第二轮修复后第三次复审确认上述问题全部实质关闭，当前 diff 无剩余 Critical、High 或 Medium。
4. PR connector 随后发现 1 个 P1 可用性缺口：仓库启动命令没有真正传入 `--no-proxy-headers`，同机 Nginx 下合法请求会在应用自检前被改写并 403。四条产品启动路径及浏览器 E2E harness 已全部显式关闭代理头解析，并加入合同测试；修复后独立复审仍为 0C/0H/0M。

未发现需要改变业务状态机的认证绕过、IDOR、CSRF/RBAC、SQL 注入、命令注入、Provider URL 任意 scheme、明文凭据或 CSV 公式注入缺陷。

## §2 已实施的安全修复

### 2.1 客户生产入口信任边界

- 客户生产不再沿用内部版 loopback-only 入口；只接受原始 ASGI peer 落在 `VIDEO_REPLICA_TRUSTED_PROXY_CIDRS` 最小网段内的请求。
- `Host` 必须精确匹配 `VIDEO_REPLICA_PUBLIC_ORIGIN`，`X-Forwarded-Proto` 必须是唯一的 `https`，`X-Forwarded-For` 必须是唯一且无逗号链的合法 IP。
- 路由限流统一读取入口中间件验证后写入的 `request.state.client_ip`；激活、登录/切换、设备 enroll、管理端 session exchange 不再各自直接信任 `request.client.host`。
- 若外层 Uvicorn `ProxyHeadersMiddleware` 已把 `request.client` 改写成单一转发地址，应用通过 `forwarded_ip == peer_ip` 识别原始 last-hop peer 已丢失，并以 `503 PROXY_HEADER_REWRITE_DETECTED` fail-closed。集成测试使用真实 Uvicorn middleware 复现误配。
- systemd、npm 开发命令、Tauri POSIX 与 Windows sidecar 启动器四条产品 API 启动路径，以及浏览器 E2E harness 都显式传入 `--no-proxy-headers`；T36 新增多实例 service 模板必须沿用该合同。
- 内部 P0 路径保持 loopback-only 行为，不扩大既有暴露面。

### 2.2 生产启动门与密钥

客户生产启动现在同时校验：

- 唯一 canonical HTTPS origin；`PUBLIC_BASE_URL` 必须存在并与 `VIDEO_REPLICA_PUBLIC_ORIGIN` 逐字相同，使浏览器入口、资源签名链接与 ZPay notify/return 回调同源；
- canonical trusted-proxy CIDR，拒绝空项、非 canonical 网段和 IPv4/IPv6 `/0`；
- 激活码 HMAC、设备凭据 HMAC、管理会话 HMAC 均至少 32 bytes，并支持版本化换钥；
- 激活码导出与客户幂等 AEAD 均须为 urlsafe-base64 且解码后恰 32 bytes；
- `VIDEO_REPLICA_SETTINGS_KEY` 必须是合法 Fernet key；客户多实例禁止依赖单机 OS keystore 作为共享根密钥；
- 原有 SQLite、开发身份、本地持久资产、legacy control identity 等 fail-closed 红线继续保留。

### 2.3 Secret 与凭据扫描

`scripts/verify_no_secrets.sh` 现在扫描 tracked 与 untracked 的运行时合同面，并新增：

- code-shaped 激活码；
- S3/COS 类签名 URL 参数；
- 客户前端测试文件的精确排除（测试夹具不进入运行时 bundle）；
- 只允许唯一的全 X 无熵占位符，且同一行混入真实形状内容仍会失败；
- 扫描命令异常返回非零，不能以缺工具或读取失败冒充通过。

## §3 失败测试与回归锁定

先写失败测试后实现：

- 生产启动门新增缺失/弱 HMAC、错误长度 AEAD、无效 Fernet、缺失/非法公网 origin、缺失/非法/过宽代理 CIDR、缺失/分叉 `PUBLIC_BASE_URL` 等回归锁；
- 客户入口新增可信代理正常路径、非可信 peer 伪造头、缺失/非法/逗号链 XFF、错误 Host、非 HTTPS、CORS 精确 origin，以及真实 Uvicorn `ProxyHeadersMiddleware` 改写 peer 后必须 503 的集成锁；
- 既有 owner/IDOR、cookie/CSRF、管理员与 auditor RBAC、fencing、账务幂等/对账、恶意 CSV 用例在全量回归中继续通过。

专项结果：

- `tests/test_admin_auth.py`：69 passed；
- `tests/test_customer_security.py`：34 passed；
- 先前受影响路由专项：activation 24 passed、sessions 60 passed、devices 67 passed、health 6 passed；
- CSV/表格注入专项：7 passed（6 deselected）。
- PR P1 启动合同回归：`test_internal_deployment.py` + `test_build_contracts.py` 19 passed（使用 Git Bash，未跳过 POSIX launcher 测试）。

## §4 自动化与扫描证据

全仓质量门的每个组成部分均通过。首次根命令在 Windows 将 `bash` 错误解析为未安装的 WSL，未进入检查；改用已安装的 Git Bash 后完成 secret scan 与前端 513 tests。随后一个被忽略的本地 Playwright `run.json` 格式文件使门禁在服务端测试前中止；格式化该本地运行状态后，从中断点顺序完成剩余门禁，未重复全量 pytest。

| 门禁 | 结果 |
| --- | --- |
| Secret / runtime credential scan | Pass：`No hardcoded secrets detected in runtime contract surface.` |
| Client Biome + TypeScript + Vitest | Pass：47 files / 513 tests |
| E2E Biome | Pass：14 files |
| Tauri format + cargo check | Pass |
| Ruff | Pass |
| Ruff format | Pass：180 files |
| mypy | Pass：71 source files |
| Server full pytest | Pass：1188 passed / 3 skipped / 16 warnings，1194.01s；skip/警告均为既有环境或未登记 marker，不是失败 |
| `pip-audit` | Pass：No known vulnerabilities found |
| `npm audit --audit-level=high` | Pass：0 vulnerabilities |
| `npm audit --omit=dev --audit-level=moderate` | Pass：0 vulnerabilities |

Bandit `-r app -ll -ii`：0 High；metrics 记录 44 Medium，输出结果为 B608 固定 SQL skeleton 39 处与 B310 URL open 4 处。逐项复核结果：

- B608 的动态片段为固定表名、固定列集合或内部构造的 placeholder skeleton，业务值继续参数化；未发现用户输入拼接进 SQL 标识符/语句。
- B310 的 4 条路径均位于现有外部 Provider/内部 gate 调用，调用前由 HTTPS/public allowlist 或受控 URL 合同限制；未开放 `file:` 或自定义 scheme。
- 因属于已人工分类的静态规则噪声，本任务没有通过添加 `# nosec` 隐藏报告；完整结果仍保留为后续审计可见项。

## §5 变更文件

- `server/app/bootstrap.py`
- `server/app/main.py`
- `server/app/security_rate_limit.py`
- `server/app/activation_code_routes.py`
- `server/app/customer_session_routes.py`
- `server/app/customer_device_routes.py`
- `server/app/admin_auth_routes.py`
- `server/tests/test_admin_auth.py`
- `server/tests/test_customer_security.py`
- `deploy/customer.env.example`
- `deploy/systemd/video-replica-api.service`
- `package.json`
- `client/src-tauri/resources/start-backend.sh`
- `client/src-tauri/resources/start-backend.bat`
- `e2e/customer/setup-backend.mjs`
- `scripts/verify_no_secrets.sh`
- `server/tests/test_internal_deployment.py`
- `server/tests/test_build_contracts.py`
- `docs/evidence/T35-EVIDENCE.md`
- `docs/CUSTOMER-TASK-EVIDENCE-V3.md`
- `docs/客户版任务清单-V3.md`

## §6 迁移、回滚与外部授权

- **数据库迁移**：无。
- **回滚**：代码与配置样例可整体 revert；回滚入口修复会重新暴露已关闭的 trusted-proxy Medium，不得在公网客户环境单独回退。
- **外部授权**：无。没有真实 ZPay 下单/支付、COS 权限变更、付费 Provider 调用、对外发码、灰度扩容或公网发布。
- **未测试**：T36 LB/双 API/四 Worker 的真实反代客户端 IP 与限流桶联测；T35 父任务所需 BILL-01/BILL-02 真实 ZPay 联合对账；T40 COS/Provider 真实链；T41/T42 灰度与生产决策。

## §14 任务记录

```text
任务/工作包：T35（部分）/ SEC-01 / SEC-02
Owner / Reviewer：安全/后端（Agent 执行）/ 独立 Security Reviewer（三轮；最终 0C/0H/0M）+ PR connector（1 P1，已实质修复并锁定）
分支 / 基线 SHA：feat/customer-v3-t35-security-review / main@d15b3f9
上游规格段落：客户版任务清单 V3 §7 T35、§12.7 SEC-01/SEC-02、§13–§15；代码开发清单 V3 §11.1/§13；测试与验收规格 V3 安全与证据分层
改动文件：bootstrap/main/security_rate_limit 与四个 IP 消费路由；四条产品 Uvicorn 启动路径及浏览器 E2E harness；admin/customer/deployment/build-contract tests；customer env；secret scan；T35 证据与账本
失败测试或回归锁定：生产密钥/origin/proxy 配置门 + 真实 ProxyHeadersMiddleware 改写 peer + Host/HTTPS/单一 XFF/CORS；先红后绿
实现结果：客户生产入口建立可验证 trusted-proxy 边界；所有限流 IP 统一使用已验证地址；公网 origin 与 ZPay/签名 URL base 绑定；生产 HMAC/AEAD/Fernet 全量 fail-closed；runtime secret scan 扩面
验证命令与通过数：admin auth 69；customer security 34；启动合同 19；全仓 client 513；server 1188 passed/3 skipped；Tauri/Ruff/mypy 全绿；pip/npm audit 0 已知漏洞；独立复审 0C/0H/0M；PR P1 已修复
证据层级：SEC-01/SEC-02 = AUTOMATED_VERIFIED；T35 父任务 = 部分完成，不高于 AUTOMATED_VERIFIED
安全与可观测性：中风险全部修复；Bandit 0 High，Medium 已逐项分类；扫描 tracked+untracked，日志/CSV/credential 回归继续通过
迁移与回滚：无迁移；代码 revert 可回滚，但不得在公网单独撤销入口/密钥门
外部授权记录：无；真实账号与真实付费动作按用户要求留到 T40 最后确认
未测试项：T36 staging 拓扑；BILL-01/BILL-02 真实联合对账；T40–T42 真实链/灰度/Go
Lore 提交 SHA：见本任务 PR squash SHA
```
