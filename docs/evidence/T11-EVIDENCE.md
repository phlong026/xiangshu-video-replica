# T11 — Activation Code Generation, Versioned Digests and AEAD Export (ACT-02 + ACT-03)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T11 / ACT-02 + ACT-03 |
| **Owner** | Backend/Security (Agent) |
| **Reviewer** | session-internal code review (Codex-style; 0 P1 / 2 P2 / 5 P3, all substantively fixed) |
| **Branch / Base SHA** | `feat/customer-v3-t11-activation-code-service` / base `570cd42` (PR #41 squash) |
| **Date** | 2026-08-22 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (real PostgreSQL 16 fixture; red → green) |

## Exit-Gate Verification

Task exit gate: *熵测试、日志扫描、导出下载审计通过* — delivered as `server/app/activation_code_service.py` (application layer over the revision-027 catalog) plus 21 fail-first tests (`server/tests/test_activation_code_service.py`, 12 unit + 9 PG integration):

- **Entropy** — `XS04-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX`: prefix + 4 groups × 7 Crockford-base32 characters (no I/L/O/U) = 28 × log2(32) = **140 bits ≥ 128** (acceptance spec §2.1), minted by `secrets.SystemRandom().choice`; the entropy test locks the bit floor, full-alphabet coverage across 500 codes and confusable-free output, plus 500-code uniqueness.
- **Log scanning** — the one-time-download test runs caplog at DEBUG across the package's whole life (create + first download + refused second download) and asserts no plaintext code ever appears in any log record; the DB-side twin asserts plaintext never matches `masked_code`/`code_digest` columns.
- **Export download audit** — `fetch_export_package` is the single download path: `SELECT … FOR UPDATE` + conditional `UPDATE … WHERE downloaded_at IS NULL` (rowcount==1 or fail-closed), expiry checked against real ISO timestamps, the download actor persisted on the export row; second download ("already downloaded") and expired packages are refused with `downloaded_at` still NULL.

## 1. Code Shape, Normalization, Masking (ACT-02)

| Aspect | Delivered |
| --- | --- |
| Generation | `generate_activation_code()` — CSPRNG (`secrets.SystemRandom`), injectable `rng` for deterministic fixtures only |
| Human input | `normalize_activation_code()` — upper-case, strip separators, O/I/L confusable mapping (`O→0`, `I→1`, `L→1`), prefix/alphabet/length validation, fail-closed on any malformed shape; error messages never echo input |
| Masking | `mask_activation_code()` — same length, prefix + first 4 and last 4 random characters visible, middle 20 always `*` |
| Digest | `compute_code_digest()` — HMAC-SHA256 (hex, 64 chars) of the normalized code; the database stores only digest + `digest_key_version` |

## 2. Versioned Keys and Rotation Window (ACT-02, T09 precedent)

- `VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY_V{N}` (raw string ≥ 32 bytes; V1 also accepts the un-suffixed name) — exactly the T09 `admin_hmac_key` discovery pattern.
- `VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY_V{N}` (urlsafe-base64, ≥ 32 decoded bytes; invalid base64 → `ActivationKeyError`).
- `iter_code_digests()` scans configured versions V1..V64 and yields `(digest, key_version)` highest-first — the rotation window: while an old key stays configured its codes remain verifiable; dropping the variable retires it. New codes always digest with the highest configured version.
- Missing version is an explicit error, never a silent fallback; too-short keys fail closed at resolution time.

## 3. Six-State Transition Matrix (acceptance spec §2.1)

`ALLOWED_CODE_TRANSITIONS` + `assert_code_transition()` for T12/T13 reuse: GENERATED→{ISSUED, EXPIRED, REVOKED}; ISSUED→{ACTIVE, SUSPENDED, EXPIRED, REVOKED}; ACTIVE→{SUSPENDED, REVOKED}; SUSPENDED→{ACTIVE, EXPIRED, REVOKED}; REVOKED/EXPIRED terminal. The full 6×6 matrix is tested, including unknown/empty states.

## 4. AEAD Export Envelope (ACT-03)

| Aspect | Delivered |
| --- | --- |
| Seal | `encrypt_code_package()` — AES-256-GCM, 12-byte random nonce, AAD `activation-code-export:{batch_id}` binds the envelope to its batch; returns `(urlsafe_b64(nonce‖ct+tag), sha256_hex)`; plaintext never appears in either |
| Open | `decrypt_code_package()` — tampering, wrong key or wrong batch context → `ActivationExportError("ciphertext verification failed")`; malformed payloads fail closed |
| Persist | `create_batch_export()` — exports row (ciphertext + SHA-256 + key version + short TTL) + one EXPORTED event per code; cross-batch code lists are refused before anything is written |
| Download | `fetch_export_package()` — one-time under row lock, expiry-checked, actor persisted, versioned AEAD key map lookup |

## 5. Persistence Invariants (PG integration)

- `generate_batch_codes()` lands every code as GENERATED with digest + key version + masked form only (plaintext solely in returned records), one GENERATED event each, and enforces the batch `quantity` snapshot as the frozen issuance budget (overrun refused before minting).
- Cross-batch digest uniqueness over 60 + 60 codes; unknown batches refused; export events recorded with the requesting actor.

## Files Changed

| File | Change |
| --- | --- |
| `server/app/activation_code_service.py` | new (frozen name): normalization/generation/masking, versioned HMAC/AEAD key resolution, rotation-window digests, six-state matrix, AES-GCM envelope, batch generation + one-time audited export persistence |
| `server/tests/test_activation_code_service.py` | new: 21 fail-first cases (12 unit, 9 PG on a dedicated migrated fixture database) |
| `deploy/customer.env.example` | T11 key family registered (HMAC raw-string key + AEAD urlsafe-base64 key, generation commands, `_V2` rotation comments, retention windows) |

## Pre-PR Review Fixes

Session-internal review (0 P1) surfaced 2 P2 + 5 P3; all were substantively fixed with the tests that lock them:

1. **P2 — dead `key_version` parameter**: `compute_code_digest` took a `key_version` it never used (the digest value is version-independent). Removed; the key↔version pairing is now the caller's documented contract (`activation_code_hmac_key` resolves one from the other). All call sites updated.
2. **P2 — env example gap**: `deploy/customer.env.example` shipped T09's key family only; the two T11 families (`VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY`, `VIDEO_REPLICA_ACTIVATION_EXPORT_AEAD_KEY`) are now registered with generation commands, `_V2` rotation lines and retention windows, following the T09 format.
3. **P3 — deterministic test injection**: `rng` is an explicit `random.Random | None` parameter on `generate_activation_code` / `generate_batch_codes` (production paths always use `secrets.SystemRandom`), so fixtures can be deterministic without touching the CSPRNG default.
4. **P3 — issuance budget unchecked**: `generate_batch_codes` minted without consulting the batch `quantity` snapshot, so repeated calls could oversell a batch. Now `already_generated + quantity > batch.quantity` fails closed ("budget exceeded"); a red test fills a quantity-3 batch, requests a 4th and asserts nothing extra landed.
5. **P3 — cross-batch export unchecked**: `create_batch_export` trusted the caller's `batch_id`, so batch-a codes could be sealed under batch-b (poisoning the AAD binding and the EXPORTED trail). A stray-code check now refuses mixed batches before the envelope or any row is written; red test locks it.
6. **P3 — non-deterministic confusable test**: the normalize test's `replace("A","U",…)`-style setup silently no-oped when the character was absent. Now deterministically seeds `0`/`1` at fixed positions before rebuilding and swapping to `O`/`I`/`L`, so the confusable targets provably exist.
7. **P3 — incomplete log-scan coverage**: the red-line caplog scan covered only `create_batch_export`. It now spans the package's whole life — create plus the first download and the refused second download.

Fix 4 additionally required a mypy type-narrowing rework (`count_row is None` guard) — the full quality gate re-ran green after all fixes.

## Regression

```
# PostgreSQL fixture up (scripts/pg-fixture.sh start; docker customer-v3-pg-test, PG16 :5433)
$ uv run python -m pytest tests/test_activation_code_service.py -q
21 passed                       # 12 unit + 9 PG integration (incl. all review locks)
$ uv run python -m pytest tests -q
709 passed, 2 warnings          # full suite on the PG fixture (zero regressions)
$ uv run ruff check . && uv run ruff format --check . && uv run mypy app
all green
# No client/e2e/Tauri changes in this task; npm run check gates re-verified in CI
```

## Section 14 Ledger Record

```text
任务/工作包：T11 / ACT-02 + ACT-03
Owner / Reviewer：后端/安全（Agent 执行）/ 会话内代码评审（0 P1、2 P2 + 5 P3 全部实质修复）
分支 / 基线 SHA：feat/customer-v3-t11-activation-code-service / 基线 570cd42（PR #41 squash）
上游规格段落：客户版任务清单 V3 §3 T11、§12.2 ACT-02/ACT-03；代码开发清单 V3（activation_code_service.py 冻结名）；激活码开发文档 §5/§12.1；测试与验收规格 §2.1
改动文件：server/app/activation_code_service.py（新增，517 行：规范化/CSPRNG 生成/掩码、版本化 HMAC/AEAD 密钥解析、轮换窗摘要、六态转移矩阵、AES-GCM 信封、批次生成+一次性审计导出持久化）、server/tests/test_activation_code_service.py（新增 21 用例：12 单元 + 9 PG 集成，专用迁移 fixture 库）、deploy/customer.env.example（登记 T11 双密钥族+生成命令+_V2 轮换注释）、docs/evidence/T11-EVIDENCE.md、任务与证据账本
失败测试或回归锁定：先红后绿——熵结构（140 bit ≥128、字母表全覆盖、无 I/L/O/U、500 码唯一）、人性化变体规范化（大小写/分隔符/混淆字符，确定性种子）、非法格式拒收、掩码稳定且仅露首尾 4 字符、摘要确定性/keyed/64hex、HMAC 密钥环境解析（V2/V1 无后缀/短钥拒/缺失显式报错）、密钥轮换验证窗（旧版本可验证、最高版本优先）、6×6 状态转移矩阵全量、AEAD roundtrip 且密文无明文、篡改+错批次拒收、AEAD 密钥解析（非法 base64/短钥）、生成落 GENERATED+事件+目录无明文、未知批次拒、超发拒（预算检查）、跨批次摘要唯一（60+60）、一次性下载审计（FOR UPDATE+条件 UPDATE+caplog 全生命周期明文扫描）、过期拒（downloaded_at 保持 NULL）、EXPORTED 事件、跨批次导出拒、未知导出拒
实现结果：XS04 140-bit Crockford-base32 码（CSPRNG）+ HMAC-SHA256 版本化摘要（仅摘要+key version+掩码入库）+ AES-256-GCM 批次绑定导出信封（AAD 批次绑定/SHA-256/短时 TTL/一次性审计下载）+ 六态转移矩阵供 T12/T13 复用；批次 quantity 快照作为冻结发放预算强制执行；明文仅存在于返回值与内存
验证命令与通过数：test_activation_code_service 21 passed；全量 709 passed（PG fixture）；ruff/format/mypy 全绿；CI 三门禁见 PR
证据层级：AUTOMATED_VERIFIED
安全与可观测性：无可预测码（CSPRNG+熵下限锁定）、无可逆数据库字段（仅摘要+掩码）、日志/事件/列全链路无明文（caplog 断言）、导出仅 AEAD 密文+SHA256、下载 actor 落审计行、密钥 ≥32 字节+版本化轮换+旧版本验证窗
迁移与回滚：无新迁移（复用已发布 027 目录 schema）；SQLite 车道零变化（PG-only 目录表）
外部授权记录：无；未调用真实 ZPay/COS/付费 Provider/发码/灰度/公网发布
未测试项：管理端 API 路由（T12）；首次激活原子事务（T13）；AEAD 幂等恢复（T14）；共享限流与防枚举（T15）；私有 COS 实际对象存储投递（T36 后真实链路）；STAGING/REAL_CHAIN/PRODUCTION
Lore 提交 SHA：见 PR squash 合并 SHA
```
