# 文案工坊后端：隔离探针复现命令与执行证据

执行日期：2026-09-07。主结论见 [后端专项备忘](./backend-generation-notes.md)。

以下命令是本轮已经执行的两段 Python 探针，覆盖三类问题。第一段只对 `TemporaryDirectory` 内的新 SQLite 文件运行当前迁移与任务状态函数；第二段把 `urlopen` 替换为内存字节流。均没有调用真实 Provider，没有使用共享 PostgreSQL fixture、生产数据库或 COS，也没有修改业务源码和现有测试。本文件补录现有证据，补录时未重复执行这些探针。

## 1. 同项目活跃任务约束与过期旧租约写回

### 可复制完整命令

在本项目 `server` 目录执行；完整命令已包括目录切换。

```bash
cd '/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server'
PYTHONDONTWRITEBYTECODE=1 uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import json
from app.db import initialize_database
from app.db_portable import BusinessConnection
from app.script_from_audio import acquire_script_from_audio_task, complete_script_from_audio_task
from app.asr import TranscriptResult

with TemporaryDirectory(prefix='copy-audit-') as d:
    with initialize_database(Path(d) / 'audit.db') as raw:
        conn = BusinessConnection.sqlite(raw)
        conn.execute("INSERT INTO users (id, username, display_name, role) VALUES ('audit_user','audit_user','Audit','employee')")
        conn.execute("INSERT INTO projects (id, owner_user_id, name) VALUES ('audit_project','audit_user','Audit')")
        for task in ('first', 'second'):
            conn.execute("INSERT INTO script_from_audio_tasks (id,project_id,source_asset_id,created_by_user_id,idempotency_key,request_hash,request_json,status) VALUES (%s,'audit_project','audit_asset','audit_user',%s,%s,%s,'PENDING')", (task,task,task,json.dumps({'source_asset_id':'audit_asset'})))
        count = conn.execute("SELECT COUNT(*) AS n FROM script_from_audio_tasks WHERE project_id='audit_project' AND status='PENDING'").fetchone()['n']
        print('active_tasks_same_project_accepted_by_schema=', count)
        conn.execute("DELETE FROM script_from_audio_tasks WHERE id='second'")
        lease = acquire_script_from_audio_task(conn, worker_id='old-worker')
        conn.execute("UPDATE script_from_audio_tasks SET provider_started_at='2000-01-01 00:00:00', locked_until='2000-01-01 00:00:00' WHERE id='first'")
        conn.commit()
        replacement = acquire_script_from_audio_task(conn, worker_id='replacement-worker')
        state_before = conn.execute("SELECT status FROM script_from_audio_tasks WHERE id='first'").fetchone()['status']
        complete_script_from_audio_task(conn, lease=lease, result=TranscriptResult('late result',12.0,'zh'))
        state_after = conn.execute("SELECT status FROM script_from_audio_tasks WHERE id='first'").fetchone()['status']
        print('expired_lease_state_before_late_completion=',state_before)
        print('expired_lease_state_after_late_completion=',state_after)
        print('real_provider_calls=0')
PY
```

### 本轮实际输出

退出码 `0`。运行前半部分打印了当前 SQLite Alembic 迁移日志（从 `001_core` 到 `076_studio_notification_preferences`）；下面完整保留探针自身的实际输出，不重复列迁移进度日志。

```text
active_tasks_same_project_accepted_by_schema= 2
expired_lease_state_before_late_completion= SUBMISSION_UNCERTAIN
expired_lease_state_after_late_completion= SUCCEEDED
real_provider_calls=0
```

### 证据含义与边界

1. **同项目 PENDING 约束**：两个不同幂等键的 PENDING 行均能插入同一项目，证明当前迁移没有数据库级活跃项目互斥。该探针刻意直接构造可并发产生的行状态，不冒充真实 HTTP/PG 并发压测；API 先查后插的并发窗口仍须补专门回归测试。
2. **旧 lease 写回**：调用真实领取函数取得旧租约；仅在临时库中把其锁时间设置为过去并标记已提交；再次领取触发真实过期回收逻辑，状态变为 SUBMISSION_UNCERTAIN；随后真实成功写回函数接受旧租约，将状态覆盖为 SUCCEEDED。这里执行了真实领域状态函数，未调用存储、ffmpeg 或 ASR。
3. `audit_asset` 是任务行中的假来源标识，探针不执行 `prepare`/`perform`，因而不需要也不会读取原视频。临时数据库随 `TemporaryDirectory` 退出删除。

修复后预期：活跃项目约束应拒绝第二条活跃任务（或由 API 原子地归并相同请求/拒绝不同请求）；旧 lease 完成必须被拒绝，已回收状态不得被覆盖。修复后原探针可能在第二次 INSERT 处提前抛约束异常，因此开发回归测试应将两项拆成独立测试。

## 2. LLM null 与截断结果接受

### 可复制完整命令

```bash
cd '/Users/honor.pei/Documents/订单项目/乡墅爆款短视频复刻/server'
PYTHONDONTWRITEBYTECODE=1 uv run python - <<'PY'
import io
import json
from unittest.mock import patch
from app import script_rewrite

for content, finish_reason in [(None, 'stop'), ('这是一段未写完的文案', 'length')]:
    body = {'choices':[{'message':{'content':content},'finish_reason':finish_reason}]}
    with patch.object(script_rewrite, 'urlopen', return_value=io.BytesIO(json.dumps(body).encode())):
        result = script_rewrite._request_deepseek(base_url='https://invalid.example', api_key='fake', model='fake', source_text='需要改写的原文')
        print(json.dumps({'provider_content':content,'finish_reason':finish_reason,'accepted_as_result':result},ensure_ascii=False))
print('real_provider_calls=0')
PY
```

### 本轮实际输出

退出码 `0`。

```text
{"provider_content": null, "finish_reason": "stop", "accepted_as_result": "None"}
{"provider_content": "这是一段未写完的文案", "finish_reason": "length", "accepted_as_result": "这是一段未写完的文案"}
real_provider_calls=0
```

### 证据含义与边界

- 第一条模拟 JSON `content: null`。当前 `_request_deepseek` 把它转换成字符串 `None`，没有拒绝。
- 第二条模拟 `finish_reason: length`。当前函数直接返回正文，没有识别“达到输出上限”的截断标记。
- `urlopen` 在调用期间完全替换为 `io.BytesIO`，`https://invalid.example` 没有被访问；没有读取真实 API Key。`real_provider_calls=0` 是对这条确定的 mock 执行路径的声明，并非供应商后台账单查询结果。
- 探针验证的是本地响应处理，不评判真实模型的文案质量。后续需分别补内容类型/非空校验、截断处理以及业务所需的长度/事实/口播时长验收。

修复后预期：null 应返回可识别的结果格式错误；length 应提示截断并进入明确的重生成/分段处理流程，不能直接当完整稿成功交付。
