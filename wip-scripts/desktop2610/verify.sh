#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=/opt/video-replica-candidate
SHA=0d7f20fa8313218443fd6af4769e17ce7aea9b01
echo "== release asset byte-compare (fixed paths) =="
B=$(grep -o 'assets/index-[^"]*\.js' /www/wwwroot/video.zszhj.cn/index.html | head -1)
A=$(grep -o 'assets/admin-[^"]*\.js' /www/wwwroot/video.zszhj.cn-admin/index.html | head -1)
sha256sum "/www/wwwroot/video.zszhj.cn/$B" "$ROOT/releases/$SHA/client/dist/$B"
sha256sum "/www/wwwroot/video.zszhj.cn-admin/$A" "$ROOT/releases/$SHA/client/dist-admin/$A"
echo "== health =="
curl -fsS --max-time 15 https://video.zszhj.cn/health; echo
echo "== api version =="
curl -fsS --max-time 15 https://video.zszhj.cn/openapi.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])'
echo "== migration head + wallet after =="
docker exec video-replica-candidate-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version"'
docker exec video-replica-candidate-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT COALESCE(SUM(available_credits),0),COALESCE(SUM(reserved_credits),0),COUNT(*) FROM wallets"'
echo "== reconciliation =="
docker exec -i -w /opt/video-replica/server video-replica-candidate-api-1 python - <<'PY'
import json, os, psycopg
dsn = next(v for v in os.environ.values() if v.startswith('postgres'))
dsn = dsn.replace('postgresql+psycopg:', 'postgresql:')
with psycopg.connect(dsn, options='-c default_transaction_read_only=on') as conn:
    with conn.cursor() as c:
        for name, sql in [
            ('RECON1', "SELECT count(*) FROM wallets w LEFT JOIN (SELECT user_id, SUM(available_delta) a, SUM(reserved_delta) r FROM wallet_transactions GROUP BY user_id) l ON l.user_id=w.user_id WHERE w.available_credits<>COALESCE(l.a,0) OR w.reserved_credits<>COALESCE(l.r,0)"),
            ('RECON2', "SELECT count(*) FROM recharge_orders o WHERE o.status='PAID' AND NOT EXISTS (SELECT 1 FROM wallet_transactions wt WHERE wt.recharge_order_id=o.id AND wt.type='CHARGE')"),
            ('RECON3', "SELECT count(*) FROM wallet_transactions wt WHERE wt.type='CHARGE' AND NOT EXISTS (SELECT 1 FROM recharge_orders o WHERE o.id=wt.recharge_order_id AND o.status='PAID')"),
            ('KS_CHECK', "SELECT count(*) FROM information_schema.check_constraints WHERE check_clause LIKE '%kuaishou%'"),
        ]:
            c.execute(sql)
            print(name, json.dumps(c.fetchall(), default=str))
print('PY_DONE')
PY
echo VERIFY_FIX_DONE
