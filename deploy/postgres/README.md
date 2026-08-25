# PostgreSQL 16 staging/production contract

Customer V3 uses PostgreSQL as its only data source. The staging environment
must use PostgreSQL 16 with HA rather than a local developer container. A
managed HA service is preferred; a self-managed cluster is acceptable only
when it exposes one stable primary/read-write endpoint and has automatic
failover owned by the database layer.

## Required boundary

- Keep every database node and the primary endpoint on a private network. Do
  not expose PostgreSQL to the 公网.
- Require TLS on the application DSN, restrict the security group/firewall to
  the API/Worker hosts, and use a dedicated least-privilege application role.
  Customer production rejects a missing `sslmode` and the libpq downgrade-capable
  `disable`, `allow` and `prefer` modes. Deploy the provider/private CA as a
  read-only file and use `sslmode=verify-full&sslrootcert=...`, so both the
  certificate chain and the HA endpoint hostname are verified.
- Point every API and Worker at the same read/write primary endpoint. This
  application does not route reads to replicas.
- Budget connections for two API processes, four Workers, maintenance and one
  migration session. Tune the per-process `VIDEO_REPLICA_PG_POOL_MAX` so the
  total remains below the service connection limit with failover headroom.
- Run `deploy/postgres/migrate.sh` from one designated migration host before
  rolling application processes. Its `flock` is host-local and is not a
  distributed migration lock. The script runs the customer PostgreSQL/TLS
  validator before Alembic can touch the target.
- After the complete migration, a truly empty database may use the guarded
  `app.bootstrap provision-empty-customer` one-shot command to atomically seed
  its first administrator and encrypted COS settings. The command refuses any
  target with users, provider settings, audit history, or a previously updated
  runtime-settings row; never use it as an operator or settings update path.
- Never place a real DSN, password, snapshot URL or database dump in Git or CI
  logs.

Backup restore evidence, RPO/RTO measurement and point-in-time recovery belong
to T38. Until that drill is complete, the database may satisfy T36 staging HA
topology but must not be described as recovery-verified or `PRODUCTION_GO`.
