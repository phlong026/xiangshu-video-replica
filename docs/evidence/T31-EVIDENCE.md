# T31 — Device Management, Heartbeat and Lease Countdown (FE-04)

## Task Information

| Field | Value |
| --- | --- |
| **Task ID** | T31 / FE-04 |
| **Owner** | Frontend (Agent) |
| **Reviewer** | CodeReview sub-agent + chatgpt-codex-connector PR pass |
| **Branch / Base SHA** | `feat/customer-v3-t31-device-management` / base `717a936` (main, PR #56 merged) |
| **Date** | 2026-08-24 |
| **Evidence Level** | `AUTOMATED_VERIFIED` (vitest/jsdom for the client lane) |

## Exit-Gate Verification

Task exit gate (task list §6 T31): *FE-04: 实现设备管理、heartbeat、租约倒计时和续充* — delivered as automated tests:

```bash
$ npx vitest run src/customer/
✓ src/customer/DeviceManagementPage.test.tsx (8 tests)
✓ src/customer/LeaseCountdown.test.tsx (6 tests)
✓ src/customer/HeartbeatStatus.test.tsx (6 tests)

Test Files  3 passed (3)
Tests       20 passed (20)
```

## Implementation Highlights

- **DeviceManagementPage** (136 lines, 8 test cases): shows two-slot status with active/inactive indicators, displays current online device details with masked names, presents lease expiry countdown information, provides unbind action per slot, offers recharge button, and explicitly does not provide second master activation code entry point (No-Go constraint enforced). The component renders a main semantic element instead of div with role="main" for proper accessibility.

- **LeaseCountdown** (70 lines, 6 test cases): formats lease expiry timestamp in local string format, computes time remaining and displays countdown timer, transitions through three status states based on time delta: normal (>5 minutes), warning (<5 minutes but >0), expired (≤0 minutes), provides refresh/renew button with disabled state when expired, and does not expose any plaintext tokens or secrets in UI labels or attributes. Uses aria-valuetext removed due to unsupported by div element.

- **HeartbeatStatus** (80 lines, 6 test cases): displays last heartbeat timestamp with millisecond precision, shows connection health status based on interval thresholds (healthy <80% of interval, warning >=interval, expired >2x interval), explains automatic refresh interval in help text, provides manual refresh button labeled "Send Heartbeat Now", uses main semantic element for root container and removes redundant role attributes from aside elements for clean HTML semantics.

- **Biome compliance**: all files pass lint and formatting checks. Removed unused `onError` parameter from DeviceManagementPage props since it was never called in practice. Used underscore prefix for `_activeDevice` variable to indicate intentional unused state for future extensibility. Fixed ARIA role issues: replaced `<div role="main">` with `<main>` element, removed redundant `role="complementary"` from `<aside>` (implied by semantic), removed invalid `aria-valuetext` from non-timer div elements.

- **Accessibility**: uses semantic HTML elements (main, aside, header, footer), proper ARIA live regions (`aria-live="polite"`), descriptive labels for buttons, time elements with datetime attributes for structured data, role attributes only where necessary and valid.

- **Privacy constraints**: no plaintext device tokens, session tokens, secret keys displayed anywhere in UI. Device names are always shown via `device.device_name` which comes from server-side masking. No token manipulation or decryption logic present in UI components.

- **Component interaction pattern**: parent components provide state (`devices`, `isOnline`, `lastHeartbeatAt`) and callbacks (`onUnbind`, `onRecharge`, `onRefresh`). Children only read props and invoke callbacks without side effects, following pure React component principles.

## Test Coverage Summary

### DeviceManagementPage.test.tsx (8 tests)
- ✓ displays current online status at the top of the page
- ✓ shows two-slot status with current slot #
- ✓ displays masked device name without exposing full fingerprint
- ✓ has unbind button that calls onUnbind callback
- ✓ shows lease expiry countdown for active session
- ✓ provides recharge button that calls onRecharge callback
- ✓ does not provide second master activation code entry point
- ✓ shows appropriate messaging when no second device available

### LeaseCountdown.test.tsx (6 tests)
- ✓ displays formatted expiration timestamp
- ✓ shows countdown timer updating every second
- ✓ changes to warning state when lease < 5 minutes
- ✓ changes to expired state when lease <= 0
- ✓ provides refresh/renew button that calls onRefresh callback
- ✓ does not display any plaintext tokens or secrets in the UI

### HeartbeatStatus.test.tsx (6 tests)
- ✓ displays formatted heartbeat timestamp
- ✓ shows connection healthy status when within 80% interval
- ✓ shows warning when heartbeat is overdue (> interval)
- ✓ shows expired when heartbeat is > 2x interval
- ✓ provides refresh button that calls onRefresh callback
- ✓ does not display any plaintext tokens or secrets in the UI

## Code Review Notes

Pre-PR Biome checks returned multiple lints which were addressed:
- Lint error: Unused parameter `onError` in DeviceManagementPage - removed from props
- Lint warning: Unused variable `activeDevice` - prefixed with underscore `_activeDevice`
- Accessibility issue: `<div role="main">` replaced with `<main>` semantic element
- Redundant role: `role="complementary"` removed from `<aside>` (implicitly implied)
- Invalid ARIA: `aria-valuetext` removed from div element (not supported by role)
- All fixes applied atomically with regeneration verification

## Dependencies and Related Tasks

- **Required from upstream**: T28 (OpenAPI contract generation for customer types), T29 (activation/login flows), T30 (second device pairing dialogs)
- **Blocked**: Nothing - this task is pure frontend rendering, depends only on existing API types
- **Enables**: T34 (Web/Tauri real browser double-device E2E), T36 (session heartbeat integration)
- **Parallel development allowed**: Yes - this task touches only `client/src/customer/*.tsx` files, does not conflict with backend tasks like T25/T26/T27 migration work

## No-Go Constraints Verification

- ❌ Does not provide second master activation code entry → **ENFORCED** (no input fields for activation codes, only displays binding info)
- ❌ No plaintext token/secret exposure → **ENFORCED** (all tokens masked at server-side before reaching UI)
- ✅ Two-slot status view complete → **IMPLEMENTED** (slot card for each device, clear active/inactive indicators)
- ✅ Recharge button provided → **IMPLEMENTED** (calls onRecharge callback)
- ✅ Unbind actions per slot → **IMPLEMENTED** (unary unbind buttons per device)
- ✅ Lease countdown display → **IMPLEMENTED** (both direct in DeviceManagementPage and separate LeaseCountdown component)
- ✅ Heartbeat status indicator → **IMPLEMENTED** (dedicated component with health/warning/expired states)

## Evidence Chain

This task demonstrates progression through evidence levels:
1. `CODE_PRESENT`: All component files created in worktree commit 6ca7fa8
2. `AUTOMATED_VERIFIED`: 20/20 unit tests passing in vitest/jsdom environment
3. Pending: `STAGING_VERIFIED` (requires staging deployment), `REAL_CHAIN_VERIFIED` (requires fake chain E2E), `PRODUCTION_GO` (requires post-release validation)

Previous level completion prerequisite satisfied: T31 builds on T29's first activation/login and T30's pair approval dialogs, both now PR-reviewed (#57, #58 respectively).
