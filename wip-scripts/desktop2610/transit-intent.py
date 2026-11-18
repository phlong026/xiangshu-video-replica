"""创建桌面 2.6.10 发行包的 COS 中转上传意图（presigned PUT）。

在 api 容器内运行：只读取 COS 配置并生成 45 分钟有效的 PUT intent，
不接触任何私钥；输出 JSON 供签名机解密后直传。
"""

import json
import sys
from datetime import timedelta

from app.db_pg import close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.settings import SettingsRepository, validate_provider_config
from app.storage import cloud_storage_config_from_settings, create_storage_adapter

KEY = 'desktop-releases/transit/desktop-2.6.10-0c089606.tar.gz'

try:
    try:
        with pg_transaction() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            cfg = SettingsRepository(BusinessConnection.postgres(conn)).load_provider_config('cos')
            validate_provider_config('cos', cfg)
    finally:
        close_pg_pool()
    storage = create_storage_adapter(cloud_storage_config_from_settings('cos', cfg))
    put = storage.create_upload_intent(KEY, content_type='application/gzip', expires_in=timedelta(minutes=45))
    if put.key != KEY or put.method != 'PUT':
        raise ValueError('transit upload intent did not match')
    print(json.dumps({'key': KEY, 'method': put.method, 'url': put.url, 'headers': put.headers}))
except Exception as exc:  # noqa: BLE001 — 边界统一脱敏
    print('COS_INTENT_ERROR ' + type(exc).__name__, file=sys.stderr)
    sys.exit(1)
