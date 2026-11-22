"""客户 E2E 专用入口：保留邮件业务流程，仅将模板投递留在本地。"""

from __future__ import annotations

import os

import requests
from fastapi import FastAPI
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

from app.db_pg import DATABASE_URL_ENV
from app.db_portable import BusinessConnection
from app.email_delivery import PROVIDER, EmailSender, TemplateEmailSender
from app.settings import SettingsRepository


def _local_post(_url: str, **_kwargs: object) -> requests.Response:
    # 不保存请求体或凭据，浏览器仍通过测试库取得真实签发的验证码。
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"Response":{"MessageId":"customer-e2e","RequestId":"customer-e2e"}}'
    return response


def _local_email_sender(conn: BusinessConnection) -> EmailSender | None:
    config = SettingsRepository(conn).load_provider_config(PROVIDER)
    if not config:
        return None
    return TemplateEmailSender(config, post=_local_post)


def create_app() -> FastAPI:
    database_dsn = os.environ.get(DATABASE_URL_ENV, "")
    runner_dsn = os.environ.get("CUSTOMER_E2E_DATABASE_URL", "")
    guard_message = "客户 E2E 入口仅允许专用 customer_e2e 数据库，且必须与 runner DSN 一致。"
    if not database_dsn or not runner_dsn:
        raise RuntimeError(guard_message)
    try:
        database = conninfo_to_dict(database_dsn)
        runner_database = conninfo_to_dict(runner_dsn)
    except ProgrammingError:
        raise RuntimeError(guard_message) from None
    if database.get("dbname") != "customer_e2e" or database != runner_database:
        raise RuntimeError(guard_message)

    from app import customer_email_routes
    from app.main import app

    # 只在 runner 显式调用工厂后注入；普通生产入口及单纯导入本模块不受影响。
    setattr(customer_email_routes, "email_sender_from_settings", _local_email_sender)
    return app
