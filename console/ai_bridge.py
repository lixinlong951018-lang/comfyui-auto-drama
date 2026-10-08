"""Durable AI transport journal; business prompts/parsers remain in batch_console."""
from contextlib import contextmanager
import base64
import copy
import io
import json
import sqlite3
import threading
import time
import uuid

ROUTES = {
    "/api/generate_script": "生成剧本", "/api/rewrite_script": "改写剧本",
    "/api/parse_script_text": "转换剧本", "/api/expand_script": "扩写 / 润色分镜",
    "/api/asset_prompt": "修改图片提示词", "/api/story_prompt": "生成分镜图提示词",
    "/api/asset_gen": "生成参考资产", "/api/boogu_gen": "生成图片",
}
LOCAL = threading.local()
STOP = threading.Event()
RUNNING = set()
GUARD = threading.Lock()
DB = None
EXECUTE = None
FINISH = None


class Paused(BaseException):
    pass


def context():
    return getattr(LOCAL, "job", None)


@contextmanager
def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init(db, execute, finish):
    global DB, EXECUTE, FINISH
    DB, EXECUTE, FINISH = db, execute, finish
    STOP.clear()
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS ai_jobs (
          id TEXT PRIMARY KEY, path TEXT NOT NULL, body TEXT NOT NULL,
          project TEXT NOT NULL, config TEXT NOT NULL, status TEXT NOT NULL,
          code INTEGER, result TEXT, acknowledged INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_calls (
          id TEXT PRIMARY KEY, job TEXT NOT NULL, slot INTEGER NOT NULL,
          kind TEXT NOT NULL, url TEXT NOT NULL, payload TEXT NOT NULL,
          mode TEXT NOT NULL, response TEXT, error TEXT,
          UNIQUE(job, slot));
        """)
        ids = [r[0] for r in con.execute("SELECT id FROM ai_jobs WHERE status='running'")]
    for jid in ids:
        launch(jid)


def submit_job(path, body, project, config):
    jid = uuid.uuid4().hex
    with connect() as con:
        con.execute("INSERT INTO ai_jobs(id,path,body,project,config,status,created) VALUES(?,?,?,?,?,'running',?)",
                    (jid, path, json.dumps(body, ensure_ascii=False), json.dumps(project, ensure_ascii=False),
                     json.dumps(config, ensure_ascii=False), time.time()))
    launch(jid)
    return jid


def launch(jid):
    with GUARD:
        if jid in RUNNING:
            return
        RUNNING.add(jid)
    def work():
        try:
            with connect() as con:
                row = dict(con.execute("SELECT * FROM ai_jobs WHERE id=?", (jid,)).fetchone())
            job = {**row, "body": json.loads(row["body"]), "project": json.loads(row["project"]),
                   "config": json.loads(row["config"]), "slot": 0}
            LOCAL.job = job
            code, result = EXECUTE(job)
            if STOP.is_set():
                raise Paused()
            # Project update and completion marker share a transaction, so replay cannot re-apply a result.
            with connect() as con:
                FINISH(con, job, code, result)
                con.execute("UPDATE ai_jobs SET status='done',code=?,result=? WHERE id=?",
                            (code, json.dumps(result, ensure_ascii=False), jid))
        except Paused:
            pass  # Running journal is intentionally retained for startup recovery.
        except Exception as exc:
            with connect() as con:
                con.execute("UPDATE ai_jobs SET status='done',code=400,result=? WHERE id=?",
                            (json.dumps({"error": str(exc)}, ensure_ascii=False), jid))
        finally:
            LOCAL.job = None
            with GUARD:
                RUNNING.discard(jid)
    threading.Thread(target=work, daemon=True, name="ai-" + jid[:8]).start()


def public_job(row):
    return {"id": row["id"], "step": ROUTES[row["path"]], "path": row["path"],
            "project": json.loads(row["project"]).get("name", ""), "status": row["status"],
            "code": row["code"], "result": json.loads(row["result"]) if row["result"] else None,
            "context": json.loads(row["body"]).get("_ai_context", {})}


def get_job(jid):
    with connect() as con:
        row = con.execute("SELECT * FROM ai_jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise ValueError("AI 操作不存在")
    return public_job(row)


def copy_text(payload):
    # Keep provider-specific system, message order and every non-message parameter.
    messages = payload.get("messages") or (payload.get("input") or {}).get("messages") or []
    parts = []
    if payload.get("system"):
        parts.append("[system]\n" + str(payload["system"]))
    for index, message in enumerate(messages, 1):
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        parts.append(f"[{index}. {message.get('role', 'user')}]\n{content}")
    prompt = payload.get("prompt") or (payload.get("input") or {}).get("prompt")
    if prompt is not None:
        parts.append("[图片提示词]\n" + str(prompt))
    # Exact JSON is also included: no field or output constraint is silently omitted.
    parts.append("[完整请求 JSON]\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return "\n\n".join(parts)


def references(payload):
    found = []
    def visit(value):
        if isinstance(value, dict):
            ref = value.get("image_url")
            if isinstance(ref, dict) and ref.get("url"):
                found.append(ref["url"])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    return list(dict.fromkeys(found))


def list_work():
    with connect() as con:
        jobs = [public_job(r) for r in con.execute(
            "SELECT * FROM ai_jobs WHERE acknowledged=0 ORDER BY created")]
        calls = [dict(r) for r in con.execute(
            "SELECT c.*,j.path,j.project,j.body FROM ai_calls c JOIN ai_jobs j ON c.job=j.id "
            "WHERE c.mode='manual' AND c.response IS NULL AND c.error IS NULL AND j.status='running' ORDER BY j.created,c.slot")]
    pending = []
    for row in calls:
        payload = json.loads(row["payload"])
        ctx = json.loads(row["body"]).get("_ai_context") or {}
        purpose = {"role": "角色图", "scene": "场景图", "story": "分镜图"}.get(ctx.get("kind"), "")
        key = str(ctx.get("key", ""))
        if purpose and key:
            purpose += " · " + (str(int(key) + 1) if ctx.get("kind") == "story" and key.isdigit() else key)
        if row["kind"] == "vision":
            purpose = "图片质检 · " + purpose
        pending.append({"purpose": purpose, "id": row["id"], "job": row["job"], "kind": row["kind"],
                        "step": ROUTES[row["path"]], "project": json.loads(row["project"]).get("name", ""),
                        "request": payload, "text": copy_text(payload), "references": references(payload)})
    return {"jobs": jobs, "pending": pending}


def acknowledge(jid):
    with connect() as con:
        con.execute("UPDATE ai_jobs SET acknowledged=1 WHERE id=? AND status='done'", (jid,))


def manual_response(kind, url, text, image):
    if kind == "image_gen":
        raw = base64.b64decode(image, validate=True)
        if not raw or len(raw) > 16 * 1024 * 1024:
            raise ValueError("图片为空或超过 16MB")
        if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff")
                or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")):
            raise ValueError("请上传 PNG、JPEG 或 WebP 图片")
        if "multimodal-generation" in url:
            return {"output": {"task_id": "manual", "task_status": "SUCCEEDED",
                               "results": [{"url": "data:image/png;base64," + image}]}}
        return {"data": [{"b64_json": image}]}
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请粘贴 AI 返回内容")
    if url.endswith("/messages"):
        return {"content": [{"type": "text", "text": text}]}
    choices = [{"message": {"role": "assistant", "content": text}}]
    return {"output": {"choices": choices}} if "text-generation" in url else {"choices": choices}


def submit_result(cid, jid, text="", image=""):
    with connect() as con:
        row = con.execute("SELECT * FROM ai_calls WHERE id=? AND job=?", (cid, jid)).fetchone()
        if not row or row["mode"] != "manual":
            raise ValueError("请求与当前步骤不匹配")
        response = json.dumps(manual_response(row["kind"], row["url"], text, image), ensure_ascii=False)
        if row["response"]:
            if row["response"] != response:
                raise ValueError("该请求已提交，不能覆盖另一个结果")
            return  # Same double click is idempotent.
        changed = con.execute("UPDATE ai_calls SET response=? WHERE id=? AND response IS NULL", (response, cid))
        if changed.rowcount != 1:
            raise ValueError("请求已被处理")


def open_request(req, kind, timeout, opener, config):
    job = context()
    mode = config.get("llm" if kind == "vision" else kind, {}).get("invocation_mode", "api")
    if not job:
        if mode == "manual":
            raise RuntimeError("手动 AI 必须通过控制台持久化操作入口调用")
        return opener.open(req, timeout=timeout)
    slot = job["slot"]
    job["slot"] += 1
    payload = req.data.decode("utf-8")
    with connect() as con:
        row = con.execute("SELECT * FROM ai_calls WHERE job=? AND slot=?", (job["id"], slot)).fetchone()
        if not row:
            cid = uuid.uuid4().hex
            con.execute("INSERT INTO ai_calls(id,job,slot,kind,url,payload,mode) VALUES(?,?,?,?,?,?,?)",
                        (cid, job["id"], slot, kind, req.full_url, payload, mode))
            row = con.execute("SELECT * FROM ai_calls WHERE id=?", (cid,)).fetchone()
    if row["kind"] != kind:
        raise RuntimeError("恢复请求的类型与原记录不一致，已停止自动处理")
    if row["error"]:
        raise RuntimeError(row["error"])
    if row["response"]:
        return io.BytesIO(row["response"].encode("utf-8"))
    if row["mode"] == "manual":
        while not STOP.wait(0.25):
            with connect() as con:
                saved = con.execute("SELECT response FROM ai_calls WHERE id=?", (row["id"],)).fetchone()[0]
            if saved:
                return io.BytesIO(saved.encode("utf-8"))
        raise Paused()
    # Replay sends the original assembled payload, never a newly reconstructed prompt.
    req.data = row["payload"].encode("utf-8")
    req.full_url = row["url"]
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        json.loads(raw)
        with connect() as con:
            con.execute("UPDATE ai_calls SET response=? WHERE id=?", (raw, row["id"]))
        return io.BytesIO(raw.encode("utf-8"))
    except Exception as exc:
        with connect() as con:
            con.execute("UPDATE ai_calls SET error=? WHERE id=?", (str(exc), row["id"]))
        raise
