"""123网盘 OpenAPI 客户端（培训素材域唯一存储车道）。

协议基线见 ``docs/培训素材123网盘存储方案-2026-11-12.md`` §2（r1-r4 评审对照
官方语雀文档与 123pan-link 官方签名示例核定）。要点：

- **鉴权** ``POST /api/v1/access_token``：clientID/clientSecret → accessToken
  （30 天）。官方硬限制：同一 client_id 同时最多 3 个 token 在用、多次颁发会
  踢旧 token 下线——多进程部署下各自颁发必然互踢，因此 token 经
  ``provider_runtime_state`` 表全局共享（advisory lock 单飞刷新），见
  ``SharedTokenStore``。
- **上传** ``GET /upload/v2/file/domain`` → ``POST /upload/v2/file/create``
  （etag = 文件整体 md5；``reuse=true`` 即秒传，直接返回 fileID）→
  ``POST {server}/upload/v2/file/slice``（multipart：preuploadID/sliceNo/
  sliceMD5/slice）→ ``POST /upload/v2/file/upload_complete``（``completed=
  false`` 时轮询同一接口，1s 间隔有界）。
- **直链** ``POST /api/v1/direct-link/enable``（目标 folder 一次性开通，幂等）
  → ``GET /api/v1/direct-link/url?fileID=`` → 服务端追加防盗链 ``auth_key``：
  ``md5(解码后pathname-expire-rand-uid-sign_secret)``，参数值
  ``{expire}-{rand}-{uid}-{md5}``（官方 sign 示例口径）。直链为「持有即访问」
  的公开 URL，进程内按 (config_version, file_id) 缓存到到期前 5 分钟。
- ``GET /api/v1/file/download_info`` 仅用于连接测试探针与诊断（自用流量每日
  1GB 上限，且不承担防盗链时效承诺），**不作为客户下发通道**，不做静默回退。
- **封号红线**：只调 OpenAPI 列表内接口，任何网页端抓包接口一律不接。
- **HTTP 纪律**：全部经 ``recorded_urlopen``（provider="pan123"），凭据字段
  命中共享脱敏清单；``opener`` 参数保留测试注入缝隙。
"""

from __future__ import annotations

import json
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request
from uuid import uuid4

from cryptography.fernet import InvalidToken

from app.db_portable import BusinessConnection
from app.external_calls import external_call_context, recorded_urlopen
from app.settings import fernet_from_environment

PAN123_PROVIDER = "pan123"
PAN123_BASE_URL = "https://open-api.123pan.com"
PAN123_PLATFORM_HEADER = "open_platform"
# upload_complete 的 completed=false 表示服务端仍在落盘：1s 间隔轮询，60 次封顶
# （官方建议间隔 1s；有界失败优于无限等待）。
_UPLOAD_POLL_INTERVAL_SECONDS = 1.0
_UPLOAD_POLL_MAX_ATTEMPTS = 60
# 直链缓存提前失效余量：到期前 5 分钟一律重签，避免边界时钟漂移放出死链。
_DIRECT_LINK_REFRESH_MARGIN_SECONDS = 300
_REMOTE_NAME_MAX_CHARS = 255
# 123 网盘禁用字符（官方上传接口规定）；出现即替换为下划线。
_REMOTE_NAME_FORBIDDEN = re.compile(r'["\\/:*?|><]')


class Pan123ConfigError(RuntimeError):
    """配置缺失或字段非法（路由层映射 503，提示管理员补配置）。"""


class Pan123AuthError(RuntimeError):
    """鉴权失败（重取一次后仍 401）：凭据大概率已失效或被平台侧拒绝。"""


class Pan123Unavailable(RuntimeError):
    """平台侧不可用：网络、限流（429）或未分类业务错误。"""


class Pan123FileMissing(RuntimeError):
    """远端文件不存在（官方 code 5066）：路由层映射 404。"""


@dataclass(frozen=True)
class Pan123File:
    file_id: int
    size: int


@dataclass(frozen=True)
class Pan123TokenState:
    client_id: str
    access_token: str
    expires_at: int


def sanitize_remote_name(name: str) -> str:
    """把本地文件名清洗成 123 网盘接受的上传名（官方 ≤255 字符、禁字符集）。

    路径剥离必须跨平台一致：``Path().name`` 的分隔符语义随 OS 变化（Windows
    剥 ``/`` 与 ``\\``，POSIX 只剥 ``/``），会让同一文件名在开发机与 Linux
    服务器产出不同的远端名——显式按两种分隔符切分。
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _REMOTE_NAME_FORBIDDEN.sub("_", base).strip()
    if not cleaned:
        # 官方要求非全空格；清洗后为空（纯禁字符/纯空白）兜底为固定名。
        cleaned = "material"
    return cleaned[:_REMOTE_NAME_MAX_CHARS]


def signed_direct_link(
    url: str,
    uid: str,
    sign_secret: str,
    *,
    ttl_seconds: int,
    rand: str,
    now: Callable[[], int] = lambda: int(time.time()),
) -> tuple[str, int]:
    """给直链追加防盗链 auth_key（官方 sign 示例口径）。

    摘要输入用 **URL 解码后的 pathname**（官方示例对路径先解码再参与拼接）；
    保留直链既有查询参数并剥离其中旧 auth_key 项后，以查询参数追加新签名。
    返回 (签名直链, 到期 epoch)。
    """
    expire = now() + ttl_seconds
    parts = urlsplit(url)
    path = unquote(parts.path)
    digest = md5(  # noqa: S324 - 供应商防盗链签名协议，非安全摘要用途
        f"{path}-{expire}-{rand}-{uid}-{sign_secret}".encode(),
        usedforsecurity=False,
    ).hexdigest()
    auth_key = f"{expire}-{rand}-{uid}-{digest}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "auth_key"
    ]
    query.append(("auth_key", auth_key))
    signed = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    return signed, expire


class SharedTokenStore:
    """accessToken 的跨进程共享存储（``provider_runtime_state`` 表）。

    值存 Fernet 加密的 ``{client_id, access_token, expires_at}``——token 与颁发
    它的 client_id 绑定存证：切换账号后旧 token 即使未过期也因 client_id 不匹配
    而被弃用（r2-P1-1）。进程内缓存是同一校验的内存副本；单飞刷新对
    ``pan123:token`` 事务级 advisory lock 串行，保证全局同时只活跃一个 token。
    """

    def __init__(self, fernet: Any = None, *, connection: BusinessConnection | None = None) -> None:
        self._connection = connection
        self._fernet = fernet or fernet_from_environment()
        self._memory: Pan123TokenState | None = None

    # -- 进程内存（无 DB 开销的快路径） ------------------------------------

    def cached_for(
        self, client_id: str, *, now: Callable[[], int] = lambda: int(time.time())
    ) -> Pan123TokenState | None:
        state = self._memory
        if state is None or state.client_id != client_id:
            return None
        if state.expires_at <= now() + 60:
            return None
        return state

    def cache(self, state: Pan123TokenState) -> None:
        self._memory = state

    def invalidate(self) -> None:
        self._memory = None

    # -- DB 共享 -----------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[BusinessConnection]:
        # 配置保存已持有 token 行锁，复用同一事务，不能再借连接等待自己。
        if self._connection is not None:
            yield self._connection
            return
        from app.db_pg import pg_transaction

        with pg_transaction() as pg:
            yield BusinessConnection.postgres(pg)

    def read_shared(self, client_id: str) -> Pan123TokenState | None:
        with self._transaction() as business:
            row = business.execute(
                "SELECT encrypted_value FROM provider_runtime_state "
                "WHERE provider = %s AND state_key = 'token'",
                (PAN123_PROVIDER,),
            ).fetchone()
        return self._decode_row(row, client_id)

    def clear_shared(self) -> None:
        """配置切换时原子清除共享 token（与护栏同事务调用，r2-P1-1）。"""
        with self._transaction() as business:
            business.execute(
                "DELETE FROM provider_runtime_state WHERE provider = %s AND state_key = 'token'",
                (PAN123_PROVIDER,),
            )
        self.invalidate()

    def mint_single_flight(
        self,
        client_id: str,
        mint: Callable[[], Pan123TokenState],
        *,
        rejected_token: str | None = None,
    ) -> Pan123TokenState:
        """单飞颁发：advisory lock 事务串行，锁内复查共享行，避免并发多颁。

        HTTP 颁发调用发生在持锁事务内——竞争者等待的正是颁发本身，锁随事务
        释放；同一事务内读写共享行，不另开池连接。
        """
        now = int(time.time())
        with self._transaction() as business:
            business.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{PAN123_PROVIDER}:token",)
            )
            row = business.execute(
                "SELECT encrypted_value FROM provider_runtime_state "
                "WHERE provider = %s AND state_key = 'token'",
                (PAN123_PROVIDER,),
            ).fetchone()
            shared = self._decode_row(row, client_id)
            if (
                shared is not None
                and shared.expires_at > now + 60
                and shared.access_token != rejected_token
            ):
                self.cache(shared)
                return shared
            minted = mint()
            encrypted = self._fernet.encrypt(
                json.dumps(
                    {
                        "client_id": minted.client_id,
                        "access_token": minted.access_token,
                        "expires_at": minted.expires_at,
                    }
                ).encode("utf-8")
            )
            business.execute(
                "INSERT INTO provider_runtime_state (provider, state_key, encrypted_value) "
                "VALUES (%s, 'token', %s) "
                "ON CONFLICT (provider, state_key) DO UPDATE SET "
                "encrypted_value = EXCLUDED.encrypted_value, updated_at = now()",
                (PAN123_PROVIDER, encrypted.decode("ascii")),
            )
            self.cache(minted)
            return minted

    def _decode_row(self, row: Any, client_id: str) -> Pan123TokenState | None:
        if row is None:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(str(row["encrypted_value"])))
            if not isinstance(payload, dict) or payload.get("client_id") != client_id:
                return None
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                return None
            return Pan123TokenState(
                client_id=client_id,
                access_token=token,
                expires_at=int(payload.get("expires_at") or 0),
            )
        except (InvalidToken, ValueError, TypeError):
            # 解不开的历史行视同不存在：单飞会重新颁发并覆盖。
            return None


class Pan123Client:
    """面向培训素材域的最小 123 网盘客户端（上传/直链/删除/探针）。"""

    def __init__(
        self,
        config: Mapping[str, str],
        *,
        config_version: str = "",
        shared_token: SharedTokenStore | None = None,
        opener: Any = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client_id = str(config.get("client_id", "")).strip()
        self._client_secret = str(config.get("client_secret", "")).strip()
        self._uid = str(config.get("uid", "")).strip()
        self._sign_secret = str(config.get("sign_secret", "")).strip()
        folder_raw = str(config.get("folder_id", "")).strip()
        if not (self._client_id and self._client_secret and self._uid and self._sign_secret):
            raise Pan123ConfigError("素材存储通道配置不完整。")
        self._folder_id = int(folder_raw) if folder_raw else 0
        self._config_version = config_version
        self._tokens = shared_token or SharedTokenStore()
        self._opener = opener
        self._timeout = timeout_seconds
        # 直链缓存：file_id -> (签名直链, 到期, config_version)；配置版本变化即整体失效。
        self._link_cache: dict[int, tuple[str, int, str]] = {}

    @property
    def config_version(self) -> str:
        return self._config_version

    # -- 传输与鉴权 ---------------------------------------------------------

    def _headers(self, token: str, *, content_type: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Platform": PAN123_PLATFORM_HEADER,
            "Content-Type": content_type,
        }

    def _call_json(
        self,
        request: Any,
        *,
        endpoint: str,
        read_limit: int | None = 64 * 1024,
    ) -> dict[str, Any]:
        try:
            with external_call_context("PAN123", endpoint):
                body, _headers, _status = recorded_urlopen(
                    request,
                    timeout=self._timeout,
                    provider=PAN123_PROVIDER,
                    endpoint=endpoint,
                    opener=self._opener,
                    read_limit=read_limit,
                    expected_json=True,
                )
        except HTTPError as exc:
            raise self._from_http_status(exc.code, endpoint) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise Pan123Unavailable(f"素材存储通道服务不可用（{endpoint}）。") from exc
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise Pan123Unavailable(f"素材存储通道响应不是有效 JSON（{endpoint}）。") from exc
        if not isinstance(payload, dict):
            raise Pan123Unavailable(f"素材存储通道响应形状异常（{endpoint}）。")
        code = int(payload.get("code") or 0)
        if code == 0:
            return payload
        if code == 401:
            raise Pan123AuthError("素材存储通道访问令牌无效。")
        if code == 429:
            raise Pan123Unavailable("素材存储通道请求过于频繁，请稍后重试。")
        if code == 5066:
            raise Pan123FileMissing("素材存储通道文件不存在。")
        raise Pan123Unavailable(f"素材存储通道接口失败（错误码 {code}）。")

    def _from_http_status(self, status: int, endpoint: str) -> RuntimeError:
        if status == 401:
            return Pan123AuthError("素材存储通道访问令牌无效。")
        if status == 429:
            return Pan123Unavailable("素材存储通道请求过于频繁，请稍后重试。")
        return Pan123Unavailable(f"素材存储通道服务不可用（{endpoint}，HTTP {status}）。")

    def _token(self, *, rejected_token: str | None = None) -> str:
        client_id = self._client_id
        if rejected_token is None:
            cached = self._tokens.cached_for(client_id)
            if cached is not None:
                return cached.access_token
            shared = self._tokens.read_shared(client_id)
            if shared is not None and shared.expires_at > int(time.time()) + 60:
                self._tokens.cache(shared)
                return shared.access_token
        # 单飞颁发在 store 内完成（advisory lock 事务 + 锁内复查共享行）；
        # client 不直接触 DB，测试注入 fake store 即可离线运行。
        minted = self._tokens.mint_single_flight(
            client_id, self._mint_token, rejected_token=rejected_token
        )
        self._tokens.cache(minted)
        return minted.access_token

    def _mint_token(self) -> Pan123TokenState:
        request_body = json.dumps(
            {"clientID": self._client_id, "clientSecret": self._client_secret}
        ).encode("utf-8")
        request = Request(
            f"{PAN123_BASE_URL}/api/v1/access_token",
            data=request_body,
            headers={"Content-Type": "application/json", "Platform": PAN123_PLATFORM_HEADER},
            method="POST",
        )
        payload = self._call_json(request, endpoint="api/v1/access_token")
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("accessToken"):
            raise Pan123AuthError("素材存储通道未返回访问令牌。")
        expiry = data.get("expiredAt")
        try:
            if isinstance(expiry, str) and not expiry.isdecimal():
                parsed = datetime.fromisoformat(expiry)
                if parsed.tzinfo is None:
                    raise ValueError("missing timezone")
                expires_at = int(parsed.timestamp())
            else:
                expires_at = int(int(time.time()) + 30 * 24 * 3600 if expiry is None else expiry)
        except (TypeError, ValueError, OverflowError) as exc:
            raise Pan123AuthError("素材存储通道未返回有效的令牌到期时间。") from exc
        return Pan123TokenState(
            client_id=self._client_id,
            access_token=str(data["accessToken"]),
            expires_at=expires_at,
        )

    def _authorized_request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        content_type: str = "application/json",
        endpoint: str,
        read_limit: int | None = 64 * 1024,
    ) -> dict[str, Any]:
        """带 token 的 API 调用；401 清缓存重取一次后重放（受限单次）。"""
        token = self._token()
        request = Request(
            url, data=data, headers=self._headers(token, content_type=content_type), method=method
        )
        try:
            return self._call_json(request, endpoint=endpoint, read_limit=read_limit)
        except Pan123AuthError:
            self._tokens.invalidate()
            token = self._token(rejected_token=token)
            retry = Request(
                url,
                data=data,
                headers=self._headers(token, content_type=content_type),
                method=method,
            )
            return self._call_json(retry, endpoint=endpoint, read_limit=read_limit)

    # -- 上传 ---------------------------------------------------------------

    def upload_file(self, remote_name: str, path: Path, *, content_type: str) -> Pan123File:
        name = sanitize_remote_name(remote_name)
        size = path.stat().st_size
        file_md5 = _file_md5_hex(path)

        domain_data = self._authorized_request(
            f"{PAN123_BASE_URL}/upload/v2/file/domain", endpoint="upload/v2/file/domain"
        ).get("data")
        domain = ""
        if isinstance(domain_data, list) and domain_data:
            domain = str(domain_data[0]).rstrip("/")
        if not domain:
            raise Pan123Unavailable("素材存储通道未返回可用的上传域名。")

        create_payload = self._authorized_request(
            f"{PAN123_BASE_URL}/upload/v2/file/create",
            method="POST",
            data=json.dumps(
                {
                    "parentFileID": self._folder_id,
                    "filename": name,
                    "etag": file_md5,
                    "size": size,
                }
            ).encode("utf-8"),
            endpoint="upload/v2/file/create",
        ).get("data")
        if not isinstance(create_payload, dict):
            raise Pan123Unavailable("素材存储通道创建上传任务响应异常。")
        if create_payload.get("reuse"):
            file_id = int(create_payload.get("fileId") or create_payload.get("fileID") or 0)
            if not file_id:
                raise Pan123Unavailable("素材存储通道秒传响应缺少文件ID。")
            return Pan123File(file_id=file_id, size=size)

        preupload_id = str(create_payload.get("preuploadID") or "")
        slice_size = int(create_payload.get("sliceSize") or 0)
        servers = create_payload.get("servers")
        server = ""
        if isinstance(servers, list) and servers:
            server = str(servers[0]).rstrip("/")
        if not server:
            # 官方语义下 create 会带 servers，但 domain 端点是同一域名池的独立
            # 来源——缺失时回退，不因单字段缺席整体失败。
            server = domain
        if not preupload_id or slice_size <= 0 or not server:
            raise Pan123Unavailable("素材存储通道创建上传任务响应不完整。")

        with path.open("rb") as handle:
            slice_no = 0
            while True:
                chunk = handle.read(slice_size)
                if not chunk:
                    break
                slice_no += 1
                slice_md5 = md5(  # noqa: S324 - 官方分片校验协议要求，非安全摘要
                    chunk, usedforsecurity=False
                ).hexdigest()
                body, multipart_type = _multipart_slice(
                    preupload_id=preupload_id,
                    slice_no=slice_no,
                    slice_md5=slice_md5,
                    filename=name,
                    content=chunk,
                )
                request = Request(
                    f"{server}/upload/v2/file/slice",
                    data=body,
                    headers=self._headers(self._token(), content_type=multipart_type),
                    method="POST",
                )
                self._call_json(request, endpoint="upload/v2/file/slice", read_limit=16 * 1024)

        complete_data = self._poll_upload_complete(preupload_id)
        file_id = int(complete_data.get("fileID") or complete_data.get("fileId") or 0)
        if not file_id:
            raise Pan123Unavailable("素材存储通道上传完成响应缺少文件ID。")
        return Pan123File(file_id=file_id, size=size)

    def _poll_upload_complete(self, preupload_id: str) -> dict[str, Any]:
        for _attempt in range(_UPLOAD_POLL_MAX_ATTEMPTS):
            payload = self._authorized_request(
                f"{PAN123_BASE_URL}/upload/v2/file/upload_complete",
                method="POST",
                data=json.dumps({"preuploadID": preupload_id}).encode("utf-8"),
                endpoint="upload/v2/file/upload_complete",
            ).get("data")
            if isinstance(payload, dict) and payload.get("completed"):
                return payload
            time.sleep(_UPLOAD_POLL_INTERVAL_SECONDS)
        raise Pan123Unavailable("素材存储通道上传确认超时，请稍后重试。")

    # -- 直链与诊断 ---------------------------------------------------------

    def resolve_direct_link(self, file_id: int, *, ttl_seconds: int = 1800) -> tuple[str, int]:
        cached = self._link_cache.get(file_id)
        if cached is not None:
            url, expire, version = cached
            if (
                version == self._config_version
                and expire > int(time.time()) + _DIRECT_LINK_REFRESH_MARGIN_SECONDS
            ):
                return url, expire
        payload = self._authorized_request(
            f"{PAN123_BASE_URL}/api/v1/direct-link/url?fileID={quote(str(file_id))}",
            endpoint="api/v1/direct-link/url",
        ).get("data")
        if not isinstance(payload, dict) or not payload.get("url"):
            raise Pan123Unavailable("素材存储通道未返回直链。")
        signed, expire = signed_direct_link(
            str(payload["url"]),
            self._uid,
            self._sign_secret,
            ttl_seconds=ttl_seconds,
            rand=secrets.token_hex(8),
        )
        self._link_cache[file_id] = (signed, expire, self._config_version)
        return signed, expire

    def download_info_url(self, file_id: int) -> str:
        """诊断/探针用原始下载地址（自用流量上限 1GB/日，禁止作为下发通道）。"""
        payload = self._authorized_request(
            f"{PAN123_BASE_URL}/api/v1/file/download_info?fileId={quote(str(file_id))}",
            endpoint="api/v1/file/download_info",
        ).get("data")
        if not isinstance(payload, dict) or not payload.get("downloadUrl"):
            raise Pan123Unavailable("素材存储通道未返回下载地址。")
        return str(payload["downloadUrl"])

    def enable_direct_link_folder(self) -> None:
        """为目标 folder 开通直链空间（官方幂等；失败由调用方决定是否阻断）。"""
        self._authorized_request(
            f"{PAN123_BASE_URL}/api/v1/direct-link/enable",
            method="POST",
            data=json.dumps({"fileID": self._folder_id}).encode("utf-8"),
            endpoint="api/v1/direct-link/enable",
        )

    def delete_remote_file(self, file_id: int) -> None:
        self._authorized_request(
            f"{PAN123_BASE_URL}/api/v1/file/delete",
            method="POST",
            data=json.dumps({"fileID": file_id}).encode("utf-8"),
            endpoint="api/v1/file/delete",
        )

    def probe(self) -> None:
        """连接测试：1KB 探针文件全链路 + 防盗链负例必检。

        正例 = 正确 auth_key 可读取；负例 = 缺失/篡改/过期三种 auth_key 请求
        必须全部被拒，任一负例仍可读取说明防盗链未生效，判配置不合格
        （评审 r2-P2-2）。探针文件即传即删。
        """
        payload = ("pan123 probe " * 84)[:1024].encode("utf-8")
        tmp_path = Path(tempfile.gettempdir()) / f"pan123-probe-{uuid4().hex}.txt"
        tmp_path.write_bytes(payload)
        file_id: int | None = None
        try:
            uploaded = self.upload_file(
                f"probe-{uuid4().hex}.txt", tmp_path, content_type="text/plain"
            )
            file_id = uploaded.file_id
            url, _expire = self.resolve_direct_link(file_id, ttl_seconds=300)
            self._probe_link_read(url, expect_success=True, endpoint="probe-link-read")
            base = _strip_auth_key(url)
            self._probe_link_read(base, expect_success=False, endpoint="probe-negative-missing")
            self._probe_link_read(
                _tamper_auth_key(url), expect_success=False, endpoint="probe-negative-tampered"
            )
            self._probe_link_read(
                _expire_auth_key(url), expect_success=False, endpoint="probe-negative-expired"
            )
        finally:
            if file_id is not None:
                try:
                    self.delete_remote_file(file_id)
                except (Pan123Unavailable, Pan123AuthError):
                    # 探针清理失败不影响结论，残留由运维按调用日志人工清理（§7.5）。
                    pass

    def _probe_link_read(self, url: str, *, expect_success: bool, endpoint: str) -> None:
        request = Request(url, headers={"Range": "bytes=0-15"}, method="GET")
        try:
            with external_call_context("PAN123", endpoint):
                recorded_urlopen(
                    request,
                    timeout=self._timeout,
                    provider=PAN123_PROVIDER,
                    endpoint=endpoint,
                    opener=self._opener,
                    read_limit=16,
                )
        except (HTTPError, URLError, TimeoutError, OSError):
            if expect_success:
                raise Pan123Unavailable("素材存储通道直链读取失败（防盗链签名可能不匹配）。")
            return
        if not expect_success:
            raise Pan123Unavailable(
                "素材存储通道防盗链未生效：非法签名仍可读取，请核对直链鉴权配置。"
            )


class Pan123Storage:
    """培训路由使用的薄包装：对齐路由对 storage 的最小使用面。"""

    provider = "pan123"

    def __init__(self, client: Pan123Client) -> None:
        self._client = client

    @property
    def config_version(self) -> str:
        return self._client.config_version

    def put_file(self, remote_name: str, path: Path, *, content_type: str) -> Pan123File:
        return self._client.upload_file(remote_name, path, content_type=content_type)

    def resolve_direct_link(self, file_id: int, *, ttl_seconds: int = 1800) -> tuple[str, int]:
        return self._client.resolve_direct_link(file_id, ttl_seconds=ttl_seconds)

    def delete_remote_file(self, file_id: int) -> None:
        self._client.delete_remote_file(file_id)

    def probe(self) -> None:
        self._client.probe()

    def enable_direct_link_folder(self) -> None:
        self._client.enable_direct_link_folder()


# -- 模块级工具 -------------------------------------------------------------


def _file_md5_hex(path: Path) -> str:
    digest = md5(usedforsecurity=False)  # noqa: S324 - 官方 etag 协议要求
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _multipart_slice(
    *,
    preupload_id: str,
    slice_no: int,
    slice_md5: str,
    filename: str,
    content: bytes,
) -> tuple[bytes, str]:
    """官方 slice 接口的 multipart/form-data 体（分片 ≤ sliceSize，内存有界）。"""
    boundary = f"----video-replica-{uuid4().hex}"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    add_field("preuploadID", preupload_id)
    add_field("sliceNo", str(slice_no))
    add_field("sliceMD5", slice_md5)
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="slice"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    body.extend(content)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _strip_auth_key(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "auth_key"
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _tamper_auth_key(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, f"{value[:-4]}dead" if key == "auth_key" else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _expire_auth_key(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (
            key,
            f"1-{uuid4().hex[:8]}-{uuid4().hex[:8]}-0000000000000000000000000000000"
            if key == "auth_key"
            else value,
        )
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class Pan123ProviderTester:
    """连接测试：1KB 探针全链路（上传→直链签名读取→防盗链负例→删除）。

    负例必检是本测试的核心价值：签名读取「成功」也可能是防盗链未开启的
    公开直链，只有非法签名被拒才证明访问边界成立（方案 §3.4 / 评审 r2-P2-2）。
    """

    REQUIRED_FIELDS = ("client_id", "client_secret", "uid", "sign_secret", "folder_id")

    def __init__(self, fallback: Any) -> None:
        # 公开字段与其他 ProviderTester 一致（链组合测试逐层断言 .fallback）。
        self.fallback = fallback

    def connection_test(self, provider: str, config: dict[str, str]) -> Any:
        from fastapi import HTTPException

        if provider != PAN123_PROVIDER:
            return self.fallback.connection_test(provider, config)
        missing = [
            field for field in self.REQUIRED_FIELDS if not str(config.get(field, "")).strip()
        ]
        if missing:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PAN123_SETTINGS_INCOMPLETE",
                    "message": "素材存储通道配置不完整，请补齐全部字段后再测试。",
                },
            )
        client = Pan123Client(config, config_version="connection-test")
        try:
            client.probe()
        except (Pan123AuthError, Pan123Unavailable, Pan123ConfigError) as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PAN123_CONNECTION_TEST_FAILED",
                    "message": "素材存储通道连接测试未通过，请检查配置。",
                },
            ) from exc
        from app.settings import ProviderTestResult

        return ProviderTestResult(status="ok", provider=provider, test_kind="connection")

    def paid_test(self, provider: str, config: dict[str, str]) -> Any:
        return self.fallback.paid_test(provider, config)
