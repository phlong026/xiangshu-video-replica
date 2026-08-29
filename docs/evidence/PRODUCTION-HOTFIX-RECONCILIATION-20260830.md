# Production hotfix reconciliation — 2026-08-30

## Outcome

This change makes the server source used by the successful PR #78 production
deployment reproducible from the repository. It closes the release risk where a
future source-only build could silently discard production hotfixes that were
previously preserved only inside the active image.

| Field | Value |
| --- | --- |
| Repository base | `a689cb18cb0c4b5ea454ef0ce4343bd472510358` |
| Production image | `video-replica-rehearsal-app:a689cb18-admin-hotfix-merged` |
| Production database head | `045_async_analysis_tasks` |
| Reconciled server archive SHA-256 | `a61e414267d51bdc6c72f0ac7083b071c14e4a32b3739c0beb427b38eedbc967` |
| Production URL | `https://video.zszhj.cn/admin` |
| Evidence level | `STAGING_VERIFIED` pending this PR's PG16 CI |

## Source boundary

The active production image was exported and reconciled against the PR #78
repository tree. This branch contains the resulting 21 server modules and the
append-only migration `045_async_analysis_tasks`.

SHA-256 comparison confirms that 20 of those modules are byte-for-byte equal
to the active production snapshot. The only intentional source difference is
`analysis_routes.py`, where the analysis-task lease is extended from five to
ten minutes as documented below.

The reconciled behavior covers:

- activation-code recovery, self-service code rotation, device dismissal, and
  the frozen two-device/single-online-session rules;
- persistent reference-video analysis tasks, duration-rounding compatibility,
  redacted provider failures, and worker execution outside API transactions;
- durable generation submission, polling, archive recovery, first-frame
  storage, and Worker crash recovery;
- customer profile, recharge-order closure, server-side ZPay payment codes,
  acceptance-payment scoping, and customer unit prices;
- COS-backed media/character cache fixes and project character-selection
  recovery.

The unreviewed local migration `046_async_image_tasks` and its first-frame and
character-sheet task endpoints are explicitly excluded. They remain a separate
future release and must not be inferred from this reconciliation.

## Additional risk closure

During reconciliation, the analysis-task lease was found to be shorter than the
maximum paid provider path: one 240-second primary request plus one 240-second
repair request. The lease is now 10 minutes, preventing another Worker from
marking a still-running request as interrupted after five minutes.

Migration tests now verify more than the Alembic version string. Both SQLite and
PostgreSQL rehearsals assert the `analysis_tasks` table, required queue columns,
and the partial unique index that permits only one `PENDING` or `RUNNING` task
per project asset.

## Verification

- Ruff check and format check: passed for application, migrations, and tests.
- mypy: passed for all 72 application source files.
- Final repository gate: secret scan, Biome, TypeScript, all 527 frontend tests,
  E2E source checks, Tauri formatting/build, Ruff, formatting, mypy, and the
  complete default server suite all passed. The server result was 870 passed,
  490 PostgreSQL-dependent tests skipped, and zero failures.
- The session-expiry regression was repeated 10 times after its React 19 async
  assertion was hardened; all 10 runs passed.
- Isolated PostgreSQL 15 compatibility run: 526 passed and one explicit PG16
  version assertion was intentionally deselected. The migration subset passed
  14/14 with that same version-only test deselected.
- PostgreSQL 16 remains the authoritative dialect/version check and is required
  to pass in the Linux CI job before merge.

The original production deployment already verified two API replicas, four
workers, authenticated live-data access, logout, browser console health, and a
390 × 844 responsive admin flow. Its deployment report remains the source for
those production observations.

## Operational notes

- The reconciliation changes repository source only; it does not rerun or
  rewrite migration 045 on production, whose database is already at that head.
- No credentials, activation-code plaintext, device/session token, provider
  secret, or database dump is included in this evidence.
- Do not deploy a source-only backend from `main` again until this PR is merged
  and the PG16 CI job is green.
