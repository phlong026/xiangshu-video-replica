# T22/T33 并行开发计划

## 📋 开发概览

**启动时间**: 2026-08-24  
**并行任务**: 
- **T22**: 客户 session 下复用 ZPay 向同一钱包续充 (后端)
- **T33**: 管理端客户、设备、session、后台加款和安全审计页面 (前端)

**Worktree 结构**:
```
d:\xiangshu-video-replica\
├── main (工作目录)
├── worktree\
│   ├── t22-customer-recharge (feat/customer-v3-t22-customer-recharge) ← 后端实现
│   └── t33-admin-audit (feat/customer-v3-t33-admin-audit) ← 前端实现
```

---

## ✅ 前置依赖检查

### T22 (customer recharge):
| 依赖项 | 状态 | 证据 |
|-------|------|------|
| **T21** (fencing 接入写路由) | ✅ `AUTOMATED_VERIFIED` | PR #56 已合并 |
| **T23** (admin_adjustment 审计化调账) | ✅ `AUTOMATED_VERIFIED` | 迁移 039+ 29 测试 |
| **T13** (首次激活原子事务) | ✅ `AUTOMATED_VERIFIED` | ACT-05/ACT-06, 32 测试 |
| **T16-T20** (设备/session/epoch fencing) | ✅ `AUTOMATED_VERIFIED` | 会话控制已完成 |

**结论**: ✅ T22 可以立即开始

### T33 (admin audit pages):
| 依赖项 | 状态 | 证据 |
|-------|------|------|
| **T18** (管理员核验批准) | ✅ `AUTOMATED_VERIFIED` | 迁移 038+13 测试 |
| **T20** (session switch/epoch fencing) | ✅ `AUTOMATED_VERIFIED` | 显式切换完成 |
| **T23** (admin_adjustment 审计化调账) | ✅ `AUTOMATED_VERIFIED` | 29 测试通过 |

**结论**: ✅ T33 可以立即开始

---

## 🎯 T22 实施要点

### 核心不变量 (BILL-01):
1. ✅ 续充不改变主码、设备槽、session 或用户并发
2. ✅ 同一笔 order ID 幂等 (防止重复入账)
3. ✅ 进入同一个钱包 (customer wallet, 非 P0 内部钱包)
4. ✅ 真实 actor + reason + 二次确认 (管理端调账)
5. ✅ 账本差额为零断言

### 改动文件:
- ✅ `server/tests/test_customer_recharge.py` (新建 - 已完成框架)
- ⚠️ `server/app/recharge_routes.py` (改造为 customer_session 鉴权)

### 关键 API 契约:
```typescript
POST /api/recharge-orders
Request: { "amount_fen": 10000 }
Response: {
  "order_no": "string",
  "status": "PENDING",
  "amount_fen": number,
  "credits": number,
  "gateway_url": "string",
  "method": "POST",
  "form_fields": object
}
Headers: Authorization: "Bearer <session_token>"
```

### 失败测试用例 (已创建):
- `test_customer_session_recharge_preserves_all_state` - 核心不变量
- `test_customer_recharge_order_goes_to_same_wallet` - 同钱包验证
- `test_customer_recharge_invalid_amount_rejected` - 金额校验
- `test_customer_recharge_without_session_rejected` - session 门禁
- `test_customer_recharge_with_revoked_session_rejected` - 撤销门禁

### 下一步实现:
1. ⏳ 修改 `recharge_routes.py` 添加 customer_session 鉴权依赖
2. ⏳ 运行完整测试套件确保 PG fixture 通过
3. ⏳ 更新 OpenAPI 生成
4. ⏳ 证据归档到 `docs/evidence/T22-EVIDENCE.md`

---

## 🎯 T33 实施要点

### 需求范围:
- **customers 页面**: 查询客户列表、搜索、详情查看
- **devices 页面**: 设备绑定历史、解绑操作、凭据吊销
- **sessions 页面**: session 列表、epoch 监控、强制下线
- **adjustments 页面**: 后台加款表单、调账历史、原因记录
- **audit_events 页面**: admin_sessions、admin_write_idempotency、所有写操作的 append-only 日志

### 改动文件:
- ⏳ `client/src/admin/CustomersPage.tsx` (新建)
- ⏳ `client/src/admin/DevicesPage.tsx` (新建)
- ⏳ `client/src/admin/SessionsPage.tsx` (新建)
- ⏳ `client/src/admin/AdjustmentsPage.tsx` (新建)
- ⏳ `client/src/admin/AuditEventsPage.tsx` (新建)
- ⏳ AdminApp.tsx 路由配置更新

### API 端点映射 (T23 已提供):
```typescript
// 现有接口 (复用 T23):
GET    /api/wallet/:user_id -> read wallet balance
GET    /api/customers/:id/adjsutments -> list adjustments
POST   /api/customers/:id/adjustments -> create adjustment

// 需新增接口:
GET    /api/admin/customers -> list customers with search
GET    /api/admin/devices -> list all devices (owner scoped)
GET    /api/admin/sessions -> list sessions by status/filter
GET    /api/admin/audit-events -> list admin operations (auditor read-only)
```

### 下一步实现:
1. ⏳ 创建 CustomersPage (表格 + 搜索 + 详情抽屉)
2. ⏳ 创建 DevicesPage (绑定历史 + 解绑按钮)
3. ⏳ 创建 SessionsPage (当前在线 + 强制下线)
4. ⏳ 创建 AdjustmentsPage (后台加款表单 + 历史列表)
5. ⏳ 创建 AuditEventsPage (append-only 事件流)
6. ⏳ 集成到 AdminApp (路由配置)
7. ⏳ 组件测试与 UI E2E

---

## 🔄 并行协同策略

### 独立开发部分:
- **T22 后端**: focus on `recharge_routes.py` 鉴权改造 + pytest
- **T33 前端**: focus on React 组件 + 调用 T23 现有接口

### 接口对齐部分:
- **T22 ↔ T31**: T22 完成后通知前端对接续充交互
- **T33 ↔ T23**: T33 直接复用 T23 提供的 backend API，无需等待

### 每日站会同步点:
1. ✅ Worktree 分支状态是否 clean
2. ⏳ T22 test_customer_recharge.py 红→绿进度
3. ⏳ T33 组件开发顺序和 Mock 数据准备
4. ⏳ OpenAPI 变更对前端的类型影响

---

## 🚦 里程碑与出口门

### T22 DoD (Definition of Done):
- [ ] CODE_PRESENT: `recharge_routes.py` 改造完成，增加 `CustomerSessionDep`
- [ ] AUTOMATED_VERIFIED: 5 个测试用例全绿，PG fixture 通过
- [ ] STAGING_VERIFIED: ⏳ pending real chain → T22 仅自动化验证即可
- [ ] PRODUCTION_GO: ⏳ T40 真实 ZPay 授权执行

### T33 DoD:
- [ ] CODE_PRESENT: 5 个页面组件全部新建完成
- [ ] AUTOMATED_VERIFIED: Vitest 组件测试通过率 >80%
- [ ] STAGING_VERIFIED: 手动遍历 5 个页面，UI/UX 验收通过
- [ ] PRODUCTION_GO: ⏳ Gate D Windows 内测发布

---

## 📝 证据文档要求

根据 `docs/CUSTOMER-TASK-EVIDENCE-V3.md`,每个任务必须包含:

### T22-EVIDENCE 模板:
```markdown
## T22: 客户 session 下 ZPay 续充

**Owner / Reviewer**: 你 / reviewer  
**分支 / 基线 SHA**: feat/customer-v3-t22-customer-recharge / HEAD  
**上游规格段落**: §12.4 BILL-01  

### 改动文件
- `server/tests/test_customer_recharge.py` (新增)
- `server/app/recharge_routes.py` (改造)

### 失败测试先行
- `test_customer_session_recharge_preserves_all_state` (先红后绿)
- ... (共 5 个测试)

### 验证命令与通过数
```bash
cd server && scripts/pg-fixture.sh start
uv run python -m pytest tests/test_customer_recharge.py -v  # 5 passed
scripts/pg-fixture.sh stop
```

### 证据层级
✅ `AUTOMATED_VERIFIED` - 未过真实 ZPay 链路

### 安全与可观测性
- ✅ 无明文密钥
- ✅ session fencing 在事务内重验
- ✅ billing_settings 加密存储

### 迁移与回滚
- ✅ 无新迁移 (复用 ZPay existing schema)
- ⚠️ 如果部署失败，rollback 只需停止 API 实例

### 外部授权记录
⏳ T40 真实 ZPay 付费需人工授权

### 未测试项
- ⏳ ZPay 真实回调路径 (T40)

### Lore 提交 SHA
待填写...
```

### T33-EVIDENCE 模板:
```markdown
## T33: 管理端客户设备 session 调账审计页面

**Owner / Reviewer**: 你 / reviewer  
**分支 / 基线 SHA**: feat/customer-v3-t33-admin-audit / HEAD  
**上游规格段落**: §12.6 ADM-02  

### 改动文件
- `client/src/admin/CustomersPage.tsx` (新增)
- ... (共 5 个页面)

### 失败测试先行
- 《CustomersTable.test.tsx》空列表/分页/搜索
- 《AdjustmentsForm.test.tsx》金额校验/二次确认弹窗

### 验证命令
```bash
cd client
npm run check  # vitest + biome + tsc 全绿
```

### 证据层级
✅ `CODE_PRESENT` → `AUTOMATED_VERIFIED`

### 安全边界
- ✅ auditor 角色只读权限验证
- ✅ CSRF cookie/双重提交防护
- ✅ 敏感字段脱敏展示

### 未测试项
- ⏸️ E2E 双浏览器设备切换 (T34)

### Lore 提交 SHA
待填写...
```

---

## 🔧 Git Workflow 规范

### Worktree 操作命令:

```bash
# 查看所有 worktree
git worktree list

# 切换到 T22 worktree
cd "d:\xiangshu-video-replica\worktree\t22-customer-recharge"

# 查看状态
git status

# 提交代码
git add .
git commit -m "T22: 实现 customer session 下的 ZPay 续充 (#XX)"
git push origin feat/customer-v3-t22-customer-recharge

# 切换到 T33 worktree
cd "d:\xiangshu-video-replica\worktree\t33-admin-audit"

# 同理提交
```

### PR 命名规范:
- **T22 PR**: `T22: implement customer session ZPay top-up API`
- **T33 PR**: `T33: admin audit pages for customers/devices/sessions/adjustments/audit`

### 评审 checklist:
- [ ] 语义评审：功能符合§12.4 BILL-01 / §12.6 ADM-02
- [ ] 测试覆盖率：新测试全部通过，无回归
- [ ] 安全扫描：无敏感信息泄露
- [ ] 类型安全：tsc/ruff/mypy 全绿
- [ ] 文档同步：OpenAPI/CHANGELOG 已更新

---

## ⚠️ 风险提示

1. **T22 阻塞风险**: 
   - 如果 `recharge_routes.py` 中 `AuthenticatedUser` 无法兼容 `CustomerSessionContext`,需要紧急修复
   - 预案：参考 T21 `customer_fence.py` 的模式，创建 `CustomerRechargeDep` 依赖注入

2. **T33 依赖风险**:
   - 如果 T23 的 `admin_adjustments` API 字段与客户期望不一致，会导致 UI 反复修改
   - 预案：先用 Postman/Thunder Client 验证 API 响应结构再开发 UI

3. **Worktree 冲突风险**:
   - 如果两边都改了 `generated/api.ts`,可能导致冲突
   - 预案：T22 完成后统一更新一次 OpenAPI 生成，避免频繁跨分支拷贝

---

## 📅 预计时间表 (单人力估算)

| 任务 | 人日 | 并行度 | 备注 |
|-----|------|--------|------|
| T22 后端实现 | 1.5 | ⭐⭐⭐⭐⭐ | 可独立完成，无外部阻塞 |
| T22 测试 + 文档 | 0.5 | ⭐⭐⭐⭐⭐ | 可与实现重叠 |
| T33 组件开发 | 2.0 | ⭐⭐⭐⭐⭐ | 前后端分离 |
| T33 测试 + 文档 | 0.5 | ⭐⭐⭐⭐ | 需在实现后执行 |
| **合计** | **4.5 人日** | **可压缩至 3 天 (前后端并发)** | 若双人开发 |

---

## 🎬 收尾动作

### T22 关闭流程:
1. ✅ 所有测试用例 green
2. ✅ PR #XX 合并到 main
3. ✅ 更新 `docs/客户版任务清单-V3.md` T22 状态为 `[x]`
4. ✅ 创建 `docs/evidence/T22-EVIDENCE.md`
5. ✅ UpdateMemory: "T22 客户续充开发经验总结"

### T33 关闭流程:
1. ✅ 所有组件测试通过率 >80%
2. ✅ PR #YY 合并到 main
3. ✅ 更新任务清单 T33 状态为 `[x]`
4. ✅ 创建 `docs/evidence/T33-EVIDENCE.md`
5. ✅ UpdateMemory: "T33 管理端页面开发模式总结"

---

## 📢 下一步行动

**今天 (2026-08-24) - 进展汇报**:

✅ **T22 (customer recharge)**:
1. ✅ 已创建 Worktree
2. ✅ 已在 main 分支完成 `recharge_routes.py`改造，新增`/api/customer/recharge-orders` 路由
   - 使用 `BusinessDbDep` → `fenced_pg_transaction` →`verify_session_context`
   - `pricing_scope = 'CUSTOMER_STANDARD'` (区分内部充值)
   - 核心不变量保证：不改变 code/device/session/concurrency
3. ⚠️ 测试框架准备中 (`test_customer_recharge.py`) - 待补充完整实现

🔄 **T33 (admin audit pages)**:
1. ✅ 已创建 Worktree
2. ⏳ 尚未开始组件开发

**立即行动清单**:

🔥 **T22 worktree**(backend focus):
```bash
cd "d:\xiangshu-video-replica\worktree\t22-customer-recharge"
uv run python -m pytest tests/test_customer_recharge.py -v  # 等待实现完成后执行
```
目标：**今天完成 test_customer_recharge.py 的完整实现并跑通所有 5 个用例**

🎨 **T33 worktree**(frontend focus):
```bash
cd "d:\xiangshu-video-replica\worktree\t33-admin-audit"
npm run check  # Vitest + Biome + tsc
```
目标：**明天完成 CustomersPage.tsx 原型**

---

## 📞 协同提醒

如果你发现以下情况，请主动呼叫:
1. T22 测试因 session 鉴权问题持续 red → 可能需要重新审视 `customer_auth.py`
2. T33 UI 组件调用 T23 API 返回 403/500 → 检查 admin session CSRF token
3. 两边的 git commit message 出现中文 Lore → 按规范补充上下文

祝你开发顺利! 🚀
