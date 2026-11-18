#!/usr/bin/env bash
# 2.6.10 桌面发行包发布：从 COS 中转拉回 → 校验 → 摘要 → 切 stable → 外部 200 → 删中转。
# 在服务器宿主机运行；依赖 0c089606 的 release checkout（内含 customer-desktop-release.sh）。
set -Eeuo pipefail
umask 077
expected=06dca508c052689d429374d8c40207de10df1c53ebb423674a1608112835448b
pub=/opt/video-replica-candidate/releases/0c089606422a8dc7e45ff293c927fe27419b5cd0/deploy/customer-desktop-release.sh
channel=/opt/video-replica/app/desktop-releases/customer-cloud
work=/tmp/desktop2610-publish
rm -rf "$work"; mkdir -p "$work"
trap 'rm -rf "$work"' EXIT

cat > "$work/helper.py" <<'PY'
import hashlib
import sys
from pathlib import Path

from app.db_pg import close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.settings import SettingsRepository, validate_provider_config
from app.storage import cloud_storage_config_from_settings, create_storage_adapter

KEY = 'desktop-releases/transit/desktop-2.6.10-0c089606.tar.gz'
TARGET = Path('/tmp/desktop2610-transit.tar.gz')


def main() -> None:
    mode = sys.argv[1]
    try:
        try:
            with pg_transaction() as conn:
                conn.execute('SET TRANSACTION READ ONLY')
                cfg = SettingsRepository(BusinessConnection.postgres(conn)).load_provider_config('cos')
                validate_provider_config('cos', cfg)
        finally:
            close_pg_pool()
        storage = create_storage_adapter(cloud_storage_config_from_settings('cos', cfg))
        if mode == 'download':
            digest = hashlib.sha256()
            size = 0
            with TARGET.open('wb') as fh:
                for chunk in storage.iter_object(KEY, chunk_size=1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    fh.write(chunk)
            print('OBJECT_SHA256', digest.hexdigest())
            print('OBJECT_SIZE', size)
        elif mode == 'delete':
            from app.content_store import delete_object_outside_content_namespace
            if not delete_object_outside_content_namespace(storage, KEY):
                raise RuntimeError('transit deletion refused')
            print('DELETED', KEY)
            TARGET.unlink(missing_ok=True)
        else:
            raise ValueError('bad mode')
    except Exception as exc:  # noqa: BLE001 — 边界统一脱敏
        print('TRANSIT_ERROR ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
PY

docker exec -i -w /opt/video-replica/server video-replica-candidate-api-1 python - download < "$work/helper.py" > "$work/download.log"
sha=$(awk '$1=="OBJECT_SHA256"{print $2}' "$work/download.log")
test "$sha" = "$expected"
docker cp video-replica-candidate-api-1:/tmp/desktop2610-transit.tar.gz "$work/bundle.tar.gz"
test "$(sha256sum "$work/bundle.tar.gz" | cut -d' ' -f1)" = "$expected"
mkdir -p "$work/2.6.10"
tar -xzf "$work/bundle.tar.gz" -C "$work/2.6.10"
(cd "$work/2.6.10" && sha256sum -c SHA256SUMS.txt)
bash "$pub" --channel-root "$channel" --release-dir "$work/2.6.10"
curl -fsS --max-time 20 https://video.zszhj.cn/downloads/customer-cloud/stable.json | diff - "$channel/2.6.10/stable.json"
url=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["platforms"]["windows-x86_64"]["url"])' "$channel/2.6.10/stable.json")
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 60 "$url")
test "$code" = 200
printf 'EXTERNAL_ASSET_200 %s\n' "$url"
docker exec -i -w /opt/video-replica/server video-replica-candidate-api-1 python - delete < "$work/helper.py"
printf 'PUBLISH_DONE\n'
