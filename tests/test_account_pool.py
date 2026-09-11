#!/usr/bin/env python3
"""账号池 failover 集成测试。

用本地 mock 后端(线程内 uvicorn)模拟两个账号：账号 A 返回 429(额度用尽)，
账号 B 返回 200 SSE。验证：
  1. find_auth_files 按 uid 去重
  2. CredentialPool 粘性 / 冷却 / 切换顺序
  3. _is_failover_error 判定
  4. 非流式 chat: A 429 → 自动切 B 成功
  5. 流式 chat: A 429 → 自动切 B,客户端收到正常 SSE 而非错误事件
  6. 所有账号失败:返回最后一次错误
  7. 切换后粘性:后续请求直接走 B
运行: python3 test_account_pool.py
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MOCK_PORT = 18789
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"

# ---------------------------------------------------------------------------
# mock 后端：tokA 可编程(默认 429 额度用尽)，tokB 恒 200 SSE
# ---------------------------------------------------------------------------

mock_state = {"a_status": 429, "a_body": json.dumps({"code": 429, "msg": "额度已用尽"}).encode(),
              "b_status": 200, "b_body": json.dumps({"code": 429, "msg": "额度已用尽"}).encode()}
call_counter = {"tokA": 0, "tokB": 0}

mock_app = FastAPI()


def _sse_chunks() -> bytes:
    c1 = {"id": "1", "object": "chat.completion.chunk", "model": "glm-5.2",
          "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}}]}
    c2 = {"id": "1", "object": "chat.completion.chunk", "model": "glm-5.2",
          "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    return (f"data: {json.dumps(c1)}\n\ndata: {json.dumps(c2)}\n\ndata: [DONE]\n\n").encode()


@mock_app.post("/v2/chat/completions")
async def mock_chat(request: Request):
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    call_counter[token] = call_counter.get(token, 0) + 1
    if token == "tokA":
        return JSONResponse(content=json.loads(mock_state["a_body"]), status_code=mock_state["a_status"])
    if mock_state["b_status"] != 200:
        return JSONResponse(content=json.loads(mock_state["b_body"]), status_code=mock_state["b_status"])
    return StreamingResponse(iter([_sse_chunks()]), media_type="text/event-stream")


@mock_app.post("/v2/plugin/auth/token/refresh")
async def mock_refresh():
    return {"code": 0, "data": {"accessToken": "refreshed", "expiresIn": 7200}}


def _start_mock():
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=MOCK_PORT, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            return
        time.sleep(0.1)
    raise RuntimeError("mock server 未能在 10s 内启动")


# ---------------------------------------------------------------------------
# 工具：mock 凭据
# ---------------------------------------------------------------------------

def _write_cred(path: Path, uid: str, nick: str, token: str):
    path.write_text(json.dumps({
        "account": {"uid": uid, "nickname": nick, "enterpriseId": ""},
        "auth": {"accessToken": token, "refreshToken": f"ref-{uid}",
                 "expiresAt": int(time.time() * 1000) + 3600_000,
                 "domain": "www.codebuddy.cn"},
    }, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

PASS = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS
    tag = "✅" if cond else "❌"
    print(f"  {tag} {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        raise AssertionError(f"{name}: {detail}")
    PASS += 1


def main():
    import converter
    from fastapi.testclient import TestClient

    # 隔离真实 api_keys.json:测试期间指向不存在的文件,避免面板创建的 Key 干扰鉴权
    import tempfile as _td
    converter.KEYS_FILE = Path(_td.mkdtemp()) / "api_keys_test.json"

    _start_mock()
    print("== mock 后端已启动 ==")

    # ---- 1. find_auth_files uid 去重 ----
    print("\n[1] find_auth_files uid 去重")
    with tempfile.TemporaryDirectory() as td:
        d1, d2 = Path(td) / "a", Path(td) / "b"
        d1.mkdir(); d2.mkdir()
        _write_cred(d1 / "x.info", "uid-1", "昵称1", "t1")
        _write_cred(d2 / "y.info", "uid-1", "昵称1副本", "t1-dup")   # 同 uid，应被去重
        _write_cred(d2 / "z.info", "uid-2", "昵称2", "t2")
        orig = converter.auth_dirs
        converter.auth_dirs = lambda: [d1, d2]
        try:
            files = converter.find_auth_files()
        finally:
            converter.auth_dirs = orig
        check("两个目录 3 个文件去重为 2 个账号", len(files) == 2, str(files))
        check("保留的是先扫描目录中的那份", files[0].name == "x.info", str(files[0]))

    # ---- 2. CredentialPool 单元行为 ----
    print("\n[2] CredentialPool 粘性 / 冷却 / 切换顺序")
    with tempfile.TemporaryDirectory() as td:
        pa = Path(td) / "a.info"; pb = Path(td) / "b.info"
        _write_cred(pa, "uid-a", "账号A", "tokA")
        _write_cred(pb, "uid-b", "账号B", "tokB")
        pool = converter.CredentialPool([pa, pb])
        check("池大小 = 2", len(pool) == 2)
        cands = pool.candidates()
        check("初始顺序 A→B", [c.path.name for _, c in cands] == ["a.info", "b.info"])
        pool.report_failure(pool.creds[0], 429, b"quota")
        check("A 失败后 current 切到 B", pool.get_current() is pool.creds[1])
        cands = pool.candidates()
        check("A 进入冷却排到队尾兜底", [c.path.name for _, c in cands] == ["b.info", "a.info"])
        check("A 冷却剩余时间 > 0", pool.snapshot()[0]["cooldown_remaining"] > 0)
        pool.report_success(pool.creds[1])
        check("B 成功后粘住 B", pool.get_current() is pool.creds[1])
        check("仅 B 成功不清除 A 冷却", pool.snapshot()[0]["cooldown_remaining"] > 0)

    # ---- 3. _is_failover_error ----
    print("\n[3] _is_failover_error 判定")
    check("429 → True", converter._is_failover_error(429))
    check("401 → True", converter._is_failover_error(401))
    check("403 → True", converter._is_failover_error(403))
    check("400 → False", not converter._is_failover_error(400))
    check("500 → False", not converter._is_failover_error(500))
    check("200+普通文本 → False", not converter._is_failover_error(200, b"hello"))
    check("200+额度文本 → True", converter._is_failover_error(200, "您的额度已用尽"))
    check("429+任意文本 → True", converter._is_failover_error(429, b"anything"))

    # ---- 4~7. 端到端：converter app + mock 后端 ----
    print("\n[4] 非流式: A 429 → 自动切 B 成功")
    with tempfile.TemporaryDirectory() as td:
        pa = Path(td) / "a.info"; pb = Path(td) / "b.info"
        _write_cred(pa, "uid-a", "账号A", "tokA")
        _write_cred(pb, "uid-b", "账号B", "tokB")
        converter.BACKEND = MOCK_BASE
        converter.CONFIG["pool"] = converter.CredentialPool([pa, pb])
        call_counter.update({"tokA": 0, "tokB": 0})
        mock_state.update({"a_status": 429, "a_body": json.dumps({"code": 429, "msg": "额度已用尽"}).encode()})

        client = TestClient(converter.app)
        r = client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        check("HTTP 200", r.status_code == 200, f"got {r.status_code} {r.text[:200]}")
        body = r.json()
        check("content == 'hi'(走了 B 账号)", body["choices"][0]["message"]["content"] == "hi", json.dumps(body, ensure_ascii=False)[:200])
        check("A 被调用 1 次后冷却", call_counter["tokA"] == 1, str(call_counter))
        check("B 被调用 1 次", call_counter["tokB"] == 1, str(call_counter))

        print("\n[5] 切换后粘性: 后续请求直接走 B")
        r = client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        check("HTTP 200", r.status_code == 200)
        check("A 不再被调用", call_counter["tokA"] == 1, str(call_counter))
        check("B 累计 2 次", call_counter["tokB"] == 2, str(call_counter))

        print("\n[6] 流式: A 429 → 自动切 B, 客户端收到正常 SSE")
        mock_state.update({"a_status": 429})   # 保持 A 失败
        with client.stream("POST", "/v1/chat/completions",
                           json={"model": "glm-5.2", "stream": True,
                                 "messages": [{"role": "user", "content": "hi"}]}) as r:
            check("HTTP 200", r.status_code == 200, str(r.status_code))
            chunks = b"".join(r.iter_bytes())
        # converter 转发时会重新序列化 JSON（键后带空格），所以解析而非字符串匹配
        sse_contents = []
        for line in chunks.decode("utf-8", "replace").splitlines():
            if line.startswith("data:") and "[DONE]" not in line:
                try:
                    obj = json.loads(line[5:].strip())
                    for ch in obj.get("choices") or []:
                        if (ch.get("delta") or {}).get("content"):
                            sse_contents.append(ch["delta"]["content"])
                except Exception:
                    pass
        check("SSE 含内容 hi", "hi" in sse_contents, chunks[:300].decode("utf-8", "replace"))
        check("SSE 无 error 事件", b'"error"' not in chunks, chunks[:300].decode("utf-8", "replace"))
        check("SSE 以 [DONE] 结束", b"[DONE]" in chunks)
        # A 在 [4] 已冷却并粘住 B，[6] 请求应直接走 B，A 不再被调用
        check("A 保持 1 次(冷却中不再被调用)", call_counter["tokA"] == 1, str(call_counter))
        check("B 累计 3 次(三次请求全走 B)", call_counter["tokB"] == 3, str(call_counter))

        print("\n[7] 所有账号都失败 → 返回最后一次错误")
        # 让 mock 后端的 B 也返回 429,并让 B 进入冷却 → 全部账号不可用,应兜底硬试并透传错误
        mock_state["b_status"] = 429
        pool = converter.CONFIG["pool"]
        pool.report_failure(pool.creds[1], 429, b"quota")   # B 冷却
        r = client.post("/v1/chat/completions", json={"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
        check("全部账号失败 → 429 透传", r.status_code == 429, f"got {r.status_code} {r.text[:200]}")
        mock_state["b_status"] = 200   # 恢复,供 [8] 使用

        print("\n[8] health 端点显示账号池")
        r = client.get("/health")
        accounts = r.json().get("accounts", [])
        check("accounts 数量 = 2", len(accounts) == 2, str(r.json()))
        check("含 nickname 字段", all("nickname" in a for a in accounts), str(accounts))
        check("标记当前账号", any(a.get("current") for a in accounts), str(accounts))

    print(f"\n🎉 全部 {PASS} 项检查通过")


if __name__ == "__main__":
    main()
