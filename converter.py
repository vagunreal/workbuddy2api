#!/usr/bin/env python3
"""
codebuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import uvicorn

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from responses_projection import project_responses_chat_body
from anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy2openai/2.0"

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    """收集所有可能的凭据目录（多账号池支持：全部扫描，按 uid 去重）。

    优先级：CODEBUDDY_AUTH_DIR > 平台默认目录 > WSL2 下挂载的 Windows 宿主目录。
    """
    dirs: list[Path] = []
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        dirs.append(home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    elif plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs.append(local / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        dirs.append(xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth")
        # WSL2：探测 Windows 宿主机所有用户的凭据目录
        for p in Path("/mnt/c/Users").glob("*/AppData/Local/CodeBuddyExtension/Data/Public/auth"):
            dirs.append(p)
    # 去重目录路径本身，保持顺序
    seen: set[str] = set()
    uniq: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def _read_uid(path: Path) -> str | None:
    """读取凭据文件的 account.uid；文件不可读/格式坏返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("account") or {}).get("uid") or ""
    except Exception:
        return None


def find_auth_files() -> list[Path]:
    """收集所有账号凭据文件，按 uid 去重（同一账号在多个目录只保留第一份）。

    无法解析的文件跳过（启动预检会给出警告）。
    """
    seen_uids: set[str] = set()
    result: list[Path] = []
    for d in auth_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.info")):
            uid = _read_uid(f)
            if uid is None:
                sys.stderr.write(f"[warn] 跳过无法解析的凭据文件: {f}\n")
                continue
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            result.append(f)
    return result


def find_auth_file() -> Path | None:
    files = find_auth_files()
    return files[0] if files else None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 账号池：多账号粘性使用 + 额度/认证失败自动切换
# ---------------------------------------------------------------------------

# 触发切换的后端 HTTP 状态码：限流/额度用尽、token 失效、账号不可用
FAILOVER_STATUS_CODES = {401, 402, 403, 429}
# 账号失败后的冷却时间（秒）：期间排到候选队尾，仅当无健康账号时才硬试
FAIL_COOLDOWN_SECS = 1800


def _is_failover_error(status: int, raw: bytes | str = "") -> bool:
    """判断后端响应是否应当切换账号重试：状态码命中，或错误文本含额度/账号类关键词。"""
    if status in FAILOVER_STATUS_CODES:
        return True
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    text = (text or "").lower()
    quota_keywords = ("额度", "余额", "积分不足", "配额", "配額",
                      "quota", "insufficient", "exceeded", "限流", "频率")
    return any(k in text for k in quota_keywords)


class CredentialPool:
    """多账号凭据池。

    调度策略（粘性主账号 + 故障切换）：
      - 正常时一直使用当前账号（candidates() 把它排在最前）；
      - 某账号请求遇到额度/认证类错误（_is_failover_error）后进入冷却
        （FAIL_COOLDOWN_SECS），后续请求自动落到下一个健康账号；
      - 成功响应会"粘住"该账号，直到它再次失败；
      - 所有账号都在冷却时，仍会按顺序硬试（额度可能已恢复），失败则重新冷却。
    """

    def __init__(self, paths: list[Path]):
        self.creds: list[CredentialManager] = []
        for p in paths:
            try:
                self.creds.append(CredentialManager(p))
            except Exception as e:
                sys.stderr.write(f"[warn] 加载凭据失败 {p}: {e}\n")
        self._lock = threading.Lock()
        self._current = 0
        self._failed_until: dict[int, float] = {}   # index -> 失败冷却截止时间戳

    def __len__(self) -> int:
        return len(self.creds)

    def _in_cooldown(self, idx: int) -> bool:
        return time.time() < self._failed_until.get(idx, 0.0)

    def candidates(self) -> list[tuple[int, CredentialManager]]:
        """按切换顺序返回候选账号：当前(健康) → 其他健康 → 冷却中兜底。"""
        with self._lock:
            healthy = [i for i in range(len(self.creds)) if not self._in_cooldown(i)]
            cooling = [i for i in range(len(self.creds)) if self._in_cooldown(i)]
            if self._current in healthy:
                healthy.remove(self._current)
            order = [self._current] + healthy + cooling
            # 去重保序（current 已冷却时会同时出现在队首和 cooling 里）
            seen: set[int] = set()
            order = [i for i in order if not (i in seen or seen.add(i))]
            return [(i, self.creds[i]) for i in order]

    def get_current(self) -> CredentialManager | None:
        with self._lock:
            return self.creds[self._current] if self.creds else None

    def set_current(self, uid: str) -> bool:
        """手动切换当前账号（按 uid 或凭据文件名匹配）；清除其冷却并粘住。"""
        with self._lock:
            for i, c in enumerate(self.creds):
                try:
                    u = c.summary().get("uid")
                except Exception:
                    u = None
                if u == uid or c.path.name == uid:
                    self._current = i
                    self._failed_until.pop(i, None)
                    return True
            return False

    def report_success(self, cred: CredentialManager):
        """粘住成功账号；清除其冷却状态。"""
        with self._lock:
            for i, c in enumerate(self.creds):
                if c is cred:
                    self._current = i
                    self._failed_until.pop(i, None)
                    break

    def report_failure(self, cred: CredentialManager, status: int, raw: bytes | str = ""):
        """账号失败：进入冷却，并把当前账号移到下一个健康账号（若无则保持）。"""
        with self._lock:
            idx = next((i for i, c in enumerate(self.creds) if c is cred), None)
            if idx is None:
                return
            self._failed_until[idx] = time.time() + FAIL_COOLDOWN_SECS
            healthy = [i for i in range(len(self.creds)) if not self._in_cooldown(i)]
            if healthy:
                self._current = healthy[0]

    def snapshot(self) -> list[dict]:
        """所有账号状态（health 端点用）。"""
        out = []
        with self._lock:
            for i, c in enumerate(self.creds):
                try:
                    info = c.summary()
                except Exception as e:
                    info = {"error": str(e), "path": str(c.path)}
                info["current"] = (i == self._current)
                info["cooldown_remaining"] = max(0, int(self._failed_until.get(i, 0) - time.time()))
                out.append(info)
        return out


# ---------------------------------------------------------------------------
# 模型注册表：上游发现 + 内置 + 自定义 + 探测
# ---------------------------------------------------------------------------

UPSTREAM_MODELS_URL = "https://copilot.tencent.com/console/enterprises/personal/models"
MODELS_FILE = Path(__file__).parent / "models_registry.json"
_models_lock = threading.RLock()
_upstream_models_cache: dict = {"t": 0.0, "details": {}, "agent_only": []}  # 60s TTL
_probe_state: dict = {"running": False, "done": 0, "total": 0, "current": ""}


def _load_registry() -> dict:
    try:
        return json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"custom": [], "disabled": [], "probe": {}}


def _save_registry(reg: dict):
    """写盘;不加锁——调用方负责持有 _models_lock(RLock,可重入)。"""
    tmp = MODELS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, MODELS_FILE)


def _fetch_upstream_models(cred: CredentialManager) -> tuple[dict, list[str]]:
    """从上游拉取模型详情（60s TTL）。

    返回 (details, agent_only)：
      details    — {模型id: 官方详情}，来自 data.models（含 maxInputTokens/maxOutputTokens/
                   credits 倍率/中文描述/多模态/免费标签等）
      agent_only — 仅出现在 agents 配置里、无详情的模型 id（如内部辅助模型 lite）
    """
    with _models_lock:
        if time.time() - _upstream_models_cache["t"] < 60:
            return _upstream_models_cache["details"], _upstream_models_cache["agent_only"]
    details: dict = {}
    agent_models: list[str] = []
    try:
        headers = cred.get_headers()
        with httpx.Client(timeout=15) as c:
            r = c.get(UPSTREAM_MODELS_URL, headers=headers)
        data = r.json()
        if data.get("code") == 0:
            dd = data.get("data") or {}
            for m in dd.get("models") or []:
                mid = m.get("id")
                if mid:
                    details[mid] = m
            for agent in dd.get("agents") or []:
                for m in agent.get("models") or []:
                    if m not in agent_models:
                        agent_models.append(m)
    except Exception:
        pass
    agent_only = [m for m in agent_models if m not in details]
    with _models_lock:
        _upstream_models_cache.update({"t": time.time(), "details": details, "agent_only": agent_only})
    return details, agent_only


def _specs_from_upstream(info: dict) -> dict:
    """把上游模型详情转换成面板规格(用户编辑前的基础值)。"""
    inp = ["text"]
    if info.get("supportsImages") and not info.get("disabledMultimodal"):
        inp.append("image")
    return {
        "context_length": int(info.get("maxInputTokens") or info.get("maxAllowedSize") or 0) or 131072,
        "max_output_tokens": int(info.get("maxOutputTokens") or 0) or 8192,
        "input": inp,
        "output": ["text"],
    }


def _default_specs() -> dict:
    """客户端接入所需的模型规格默认值(面板可逐模型编辑)。"""
    return {"context_length": 131072, "max_output_tokens": 8192,
            "input": ["text"], "output": ["text"]}


def _model_specs(reg: dict, name: str) -> dict:
    merged = _default_specs()
    merged.update((reg.get("specs") or {}).get(name) or {})
    return merged


def _all_models(cred: CredentialManager | None) -> list[dict]:
    """合并上游/内置/自定义模型，标注来源与规格，过滤禁用项。顺序：上游 → 内置 → 自定义。"""
    reg = _load_registry()
    disabled = set(reg.get("disabled") or [])
    out: list[dict] = []
    seen: set[str] = set()

    def _merged(name: str, upstream_info: dict | None) -> dict:
        # 优先级:上游官方规格 < 用户面板编辑(registry.specs)
        base = _specs_from_upstream(upstream_info) if upstream_info else _default_specs()
        merged = {**base, **((reg.get("specs") or {}).get(name) or {})}
        meta = {}
        if upstream_info:
            meta = {
                "display_name": upstream_info.get("name"),
                "description": upstream_info.get("descriptionZh") or upstream_info.get("descriptionEn"),
                "credits": upstream_info.get("credits"),
                "tags": [t for t in (upstream_info.get("tags") or []) if str(t).startswith("badge:")]
                        or None,
                "supports_reasoning": upstream_info.get("supportsReasoning"),
                "supports_tool_call": upstream_info.get("supportsToolCall"),
            }
        return merged, meta

    details, agent_only = _fetch_upstream_models(cred) if cred is not None else ({}, [])
    for mid in details:
        if mid in disabled or mid in seen:
            continue
        seen.add(mid)
        specs, meta = _merged(mid, details[mid])
        out.append({"name": mid, "source": "上游", "specs": specs, "meta": meta})
    for m in agent_only:
        if m in disabled or m in seen:
            continue
        seen.add(m)
        specs, meta = _merged(m, None)
        out.append({"name": m, "source": "上游", "specs": specs, "meta": meta})
    for m in DEFAULT_MODELS:
        if m in disabled or m in seen:
            continue
        seen.add(m)
        specs, meta = _merged(m, details.get(m))
        out.append({"name": m, "source": "内置", "specs": specs, "meta": meta})
    for c in reg.get("custom") or []:
        if c["name"] in disabled or c["name"] in seen:
            continue
        seen.add(c["name"])
        specs, meta = _merged(c["name"], details.get(c["name"]))
        out.append({"name": c["name"], "source": "自定义", "specs": specs, "meta": meta})
    return out


def _resolve_model(raw: str) -> str:
    """客户端模型名 → 上游真实模型名：内置别名 → 自定义别名 → 原样透传。"""
    reg = _load_registry()
    for c in reg.get("custom") or []:
        if c.get("alias") and c["alias"] == raw:
            return c["name"]
    return MODEL_ALIASES.get(raw, raw)


def probe_model(cred: CredentialManager, model: str) -> dict:
    """对单个模型发一次最小真实请求，测可用性/首字延迟/总延迟。"""
    reg = _load_registry()
    headers = cred.get_headers()
    body = {"model": model, "messages": [{"role": "user", "content": "只回复两个字:正常"}],
            "stream": True, "max_tokens": 16}
    result: dict = {"ok": False, "error": None, "ttfb_ms": None, "total_ms": None,
                    "resp_model": None, "finish_reason": None, "reply": "", "tested_at": int(time.time())}
    t0 = time.time()
    try:
        with httpx.Client(timeout=90) as c:
            with c.stream("POST", f"{BACKEND}/v2/chat/completions", headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = r.read()
                    result["error"] = f"HTTP {r.status_code}: {_truncate(raw.decode('utf-8','replace'), 200)}"
                else:
                    got_first = False
                    finish = None
                    parts: list[str] = []
                    resp_model = None
                    for line in r.iter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except Exception:
                            continue
                        resp_model = obj.get("model") or resp_model
                        for ch in obj.get("choices") or []:
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
                            content = (ch.get("delta") or {}).get("content")
                            if content:
                                parts.append(content)
                                if not got_first:
                                    got_first = True
                                    result["ttfb_ms"] = int((time.time() - t0) * 1000)
                    result.update({"ok": True, "total_ms": int((time.time() - t0) * 1000),
                                   "resp_model": resp_model, "finish_reason": finish,
                                   "reply": "".join(parts)[:40]})
    except Exception as e:
        result["error"] = str(e)
    result["elapsed_hint"] = f"{result['ttfb_ms'] or '-'}ms 首字 / {result['total_ms'] or '-'}ms 总计" if result["ok"] else None
    with _models_lock:
        reg.setdefault("probe", {})[model] = result
        _save_registry(reg)
    return result


def _probe_all_worker():
    cred = (CONFIG["pool"] or CredentialPool([])).get_current()
    if cred is None:
        _probe_state.update({"running": False})
        return
    models = [m["name"] for m in _all_models(cred)]
    _probe_state.update({"running": True, "done": 0, "total": len(models), "current": ""})
    for m in models:
        if not _probe_state.get("running"):
            break
        _probe_state["current"] = m
        try:
            probe_model(cred, m)
        except Exception:
            pass
        _probe_state["done"] += 1
    _probe_state.update({"running": False, "current": ""})


def _api_info() -> dict:
    host = CONFIG.get("host") or "127.0.0.1"
    keys = [k for k in _load_keys() if not k.get("disabled")]
    master = CONFIG.get("api_key") or ""
    return {
        "base_url": f"http://{host}:{CONFIG.get('port', 8787)}/v1",
        "api_key": master or (keys[0]["key"] if keys else ""),
        "auth_enabled": bool(master) or bool(keys),
        "keys_count": len(keys),
    }


PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy 账号池 · 配额管理</title>
<style>
  :root { --green:#22c55e; --orange:#f59e0b; --red:#ef4444; --blue:#3b82f6;
          --bg:#f5f6f8; --card:#ffffff; --text:#1f2937; --muted:#6b7280; --line:#e5e7eb; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text);
         font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; padding:32px 24px; }
  .wrap { max-width:960px; margin:0 auto; }
  h1 { font-size:26px; font-weight:700; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .sub b { color:var(--green); }
  .stats { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:12px 18px; min-width:150px; }
  .stat .v { font-size:22px; font-weight:700; }
  .stat .k { font-size:12px; color:var(--muted); margin-top:2px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
          padding:20px 22px; margin-bottom:22px; box-shadow:0 1px 3px rgba(0,0,0,.04); }
  .head { display:flex; align-items:center; gap:10px; margin-bottom:6px; flex-wrap:wrap; }
  .badge { font-size:11px; font-weight:700; letter-spacing:.5px; padding:3px 8px;
           border-radius:6px; background:#eff6ff; color:var(--blue); }
  .badge.cur { background:#dcfce7; color:#16a34a; }
  .badge.cool { background:#fef3c7; color:#d97706; }
  .nick { font-size:18px; font-weight:600; }
  .meta { color:var(--muted); font-size:12px; margin-bottom:12px; word-break:break-all; }
  .meta code { background:#f3f4f6; padding:1px 6px; border-radius:4px; }
  .checkin { font-size:13px; margin-bottom:14px; }
  .checkin .ok { color:#16a34a; } .checkin .no { color:var(--red); }
  /* 资源包列表：整体一个卡片，固定露出 4 行，内部滚动，先结束的排前面 */
  .pkg-list { border:1px solid var(--line); border-radius:12px; background:#fafbfc;
              max-height:236px; overflow-y:auto; padding:5px 6px; }
  .pkg-list::-webkit-scrollbar { width:6px; }
  .pkg-list::-webkit-scrollbar-track { background:transparent; }
  .pkg-list::-webkit-scrollbar-thumb { background:#d1d5db; border-radius:3px; }
  .pkg-list::-webkit-scrollbar-thumb:hover { background:#9ca3af; }
  .pkg { display:grid; grid-template-columns:minmax(200px,1.1fr) minmax(140px,1.6fr) auto;
         gap:14px; align-items:center; height:56px; padding:6px 12px; border-radius:9px; }
  .pkg + .pkg { border-top:1px solid #eef0f2; }
  .pkg:hover { background:#f1f3f5; }
  .pkg .name { font-size:13px; font-weight:500; min-width:0; }
  .pkg .name .t { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .pkg .name .d { font-size:11px; color:var(--muted); font-weight:400; margin-top:2px; }
  .pkg .name .d.soon { color:var(--orange); font-weight:600; }
  .pkg .mid { display:flex; align-items:center; gap:10px; min-width:0; }
  .pkg .bar { flex:1; height:7px; background:#e5e7eb; border-radius:99px; overflow:hidden; }
  .pkg .bar i { display:block; height:100%; border-radius:99px; background:var(--green); transition:width .4s; }
  .pkg .bar i.mid { background:var(--orange); } .pkg .bar i.low { background:var(--red); }
  .pkg .pct { font-size:11px; color:var(--muted); width:34px; text-align:right;
              font-variant-numeric:tabular-nums; }
  .pkg .nums { font-size:13px; text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
  .pkg .nums b { font-size:15px; }
  .pkg .nums .u { color:var(--muted); font-size:11px; }
  .pkg .nums.low b { color:var(--red); }
  .err { color:var(--red); font-size:13px; }
  .toolbar { display:flex; justify-content:flex-end; margin-bottom:16px; align-items:center; gap:12px; }
  button { background:#111827; color:#fff; border:0; border-radius:10px; padding:10px 18px;
           font-size:14px; cursor:pointer; }
  button:active { transform:scale(.98); }
  .auto { font-size:12px; color:var(--muted); }
  .empty { text-align:center; color:var(--muted); padding:40px; }
  .sw { background:#f3f4f6; color:#374151; border:1px solid var(--line); border-radius:8px;
        padding:5px 12px; font-size:12px; cursor:pointer; margin-left:auto; }
  .sw:hover { background:#e5e7eb; }
  /* ---- 模型管理板块 ---- */
  .sec-h { font-size:19px; font-weight:700; margin:30px 0 4px; }
  .sec-sub { font-size:12px; color:var(--muted); margin-bottom:14px; }
  .kv { display:flex; align-items:center; gap:10px; margin-bottom:9px; font-size:13px; flex-wrap:wrap; }
  .kv > span:not(.note) { color:var(--muted); width:64px; flex-shrink:0; }
  .kv .note { color:var(--muted); font-size:12px; flex:1 1 260px; }
  .kv code { background:#f3f4f6; padding:5px 11px; border-radius:6px; word-break:break-all; }
  .kv .note { color:var(--muted); font-size:12px; }
  .mini { background:#f3f4f6; color:#374151; border:1px solid var(--line); border-radius:6px;
          padding:3px 10px; font-size:12px; cursor:pointer; }
  .mini:hover { background:#e5e7eb; }
  .mini.warn { color:#b91c1c; }
  .mini2 { background:#111827; color:#fff; border:0; border-radius:8px; padding:8px 13px;
           font-size:12px; cursor:pointer; }
  .toolbar input { border:1px solid var(--line); border-radius:8px; padding:8px 11px;
                   font-size:13px; width:220px; }
  .toolbar input.short { width:110px; }
  .model-list { }
  /* ---- 模型页:厂商卡片 ---- */
  .vcard { background:var(--card); border:1px solid var(--line); border-radius:14px;
           box-shadow:0 1px 3px rgba(0,0,0,.04); margin-bottom:16px; overflow:hidden; }
  .vhead { display:flex; align-items:center; gap:10px; padding:14px 18px 10px; }
  .vd { width:30px; height:30px; border-radius:9px; color:#fff; font-size:14px; font-weight:800;
        display:flex; align-items:center; justify-content:center; }
  .vname { font-size:15px; font-weight:700; }
  .vg-n { background:#f3f4f6; color:var(--muted); border-radius:99px; font-size:11px;
          padding:2px 9px; font-weight:600; }
  .vbody { padding:0 12px 8px; }
  .mrow { display:flex; align-items:center; gap:14px; padding:10px 6px; }
  .mrow + .mrow { border-top:1px solid #f3f4f6; }
  .mrow.off { opacity:.55; }
  .mmain { flex:1; min-width:0; }
  .mtitle { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
  .mtitle .t { font-size:13.5px; font-weight:600; }
  .mspec { font-size:11.5px; color:var(--muted); margin-top:3px; }
  .mrate { min-width:66px; text-align:center; border-radius:9px; padding:5px 10px;
           font-size:14px; font-weight:800; white-space:nowrap; }
  .mrate small { display:block; font-size:9.5px; font-weight:500; opacity:.75; }
  .rate-lo { background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0; }
  .rate-mid { background:#fffbeb; color:#b45309; border:1px solid #fde68a; }
  .rate-hi { background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; }
  .badge.src { background:#f0fdf4; color:#15803d; font-size:10px; padding:2px 7px; }
  .mtag { border:1px solid; border-radius:5px; padding:1px 6px; font-size:10px; font-weight:600; }
  .ps { font-size:11px; }
  .ops { display:flex; gap:6px; justify-content:flex-end; flex-shrink:0; }
  /* 接口页 */
  pre.curl { background:#0f172a; color:#e2e8f0; border-radius:10px; padding:14px 16px;
             font-size:12.5px; line-height:1.7; white-space:pre-wrap; word-break:break-all;
             overflow-x:auto; max-width:100%; box-sizing:border-box; margin:0;
             font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
  .chead { display:flex; align-items:center; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
  .ct { font-size:14.5px; font-weight:700; }
  .kv code.bigcode { background:#0f172a; color:#e2e8f0; padding:9px 15px;
                     border-radius:8px; font-size:13px; word-break:break-all; border:0; }
  .method { font-size:10.5px; font-weight:800; border-radius:5px; padding:3px 8px;
            letter-spacing:.5px; flex-shrink:0; }
  .method.get { background:#dcfce7; color:#15803d; }
  .method.post { background:#dbeafe; color:#1d4ed8; }
  .ep { display:flex; align-items:center; gap:12px; padding:9px 10px;
        border-bottom:1px solid #f3f4f6; }
  .ep:last-child { border-bottom:0; }
  .ep code { font-size:12.5px; background:#f8fafc; padding:4px 10px; border-radius:6px;
             border:1px solid #eef0f2; word-break:break-all; }
  .edesc { font-size:12px; color:var(--muted); }
  /* API Keys 列表 */
  .krow { display:flex; align-items:center; gap:12px; padding:9px 6px; }
  .krow + .krow { border-top:1px solid #f3f4f6; }
  .krow .kname { font-size:13px; font-weight:600; width:130px; flex-shrink:0;
                 overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .krow code { flex:1; background:#f3f4f6; padding:5px 10px; border-radius:6px;
               font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .krow .ku { font-size:11px; color:var(--muted); width:110px; flex-shrink:0; }
  /* 表单字段(Key 生成行 / 模型编辑展开) */
  .add-form { display:flex; align-items:flex-end; gap:14px; flex-wrap:wrap; }
  .fe { display:flex; flex-direction:column; gap:4px; min-width:0; }
  .fe > span { font-size:11px; color:var(--muted); }
  .fe input[type=number], .fe input[type=text], .fe > input { border:1px solid var(--line);
      border-radius:8px; padding:8px 11px; font-size:13px; width:150px; background:#fff; }
  .fe input#key-name { width:200px; }
  /* 编辑展开 */
  .fe { display:flex; flex-direction:column; gap:4px; }
  .fe > span { font-size:11px; color:var(--muted); }
  .fe input[type=number], .fe input[type=text] { border:1px solid var(--line);
      border-radius:8px; padding:8px 11px; font-size:13px; width:150px; }
  .mrow-edit { background:#f8fafc; border:1px dashed var(--line); border-radius:10px;
               padding:12px 14px; margin:6px 4px 10px; display:flex; gap:18px;
               flex-wrap:wrap; align-items:flex-end; }
  .chip { display:inline-flex; align-items:center; gap:4px; border:1px solid var(--line);
          background:#fff; border-radius:7px; padding:5px 9px; font-size:12px;
          cursor:pointer; user-select:none; }
  .chip:has(input:checked) { border-color:var(--green); background:#f0fdf4; }
  .chip input { accent-color:var(--green); margin:0; }
  .vgroup { margin-bottom:6px; }
  .vg-title { font-size:13px; font-weight:700; padding:8px 12px 6px; color:#374151;
              display:flex; align-items:center; gap:8px; }
  .vg-n { background:#e5e7eb; color:#6b7280; border-radius:99px; font-size:10px;
          padding:1px 8px; font-weight:600; }
</style>
</head>
<body>
<div class="wrap">
  <h1>WorkBuddy 控制台</h1>
  <div class="sub">多账号额度聚合 · 数据来自腾讯 CodeBuddy 后端 · <b id="refreshed"></b></div>
  <div class="tabs">
    <button class="tab act" data-p="home" onclick="showPage('home')">🖥️ 主页面</button>
    <button class="tab" data-p="models" onclick="showPage('models')">🧩 模型</button>
    <button class="tab" data-p="api" onclick="showPage('api')">🔌 接口</button>
  </div>
  <div id="page-home">
  <div class="toolbar">
    <span class="auto" id="auto">30s 自动刷新</span>
    <span style="flex:1"></span>
    <button onclick="load()">刷新全部账号</button>
  </div>
  <div class="stats" id="stats"></div>
  <div id="cards"><div class="empty">加载中…</div></div>
  </div>

  <div id="page-models" style="display:none">
  <div class="sec-sub" style="margin-top:4px;">自动发现上游可用模型 · 每个模型可编辑参数规格(客户端添加模型时照抄) · 支持自定义添加/禁用/删除</div>
  <div class="card">
    <div class="toolbar" style="justify-content:flex-end; margin:0 0 10px;">
      <span class="auto" id="models-updated"></span>
      <button class="mini" onclick="refreshModels()">↻ 刷新模型列表</button>
    </div>
    <div class="model-list" id="model-list"><div class="empty">加载中…</div></div>
  </div>
  </div>

  <div id="page-api" style="display:none">
  <div class="card">
    <div class="chead"><span class="vd" style="background:#4338ca">⚡</span><span class="ct">接入信息</span>
      <span class="note" id="api-note"></span></div>
    <div class="kv"><span>Base URL</span><code class="bigcode" id="api-url">—</code>
      <button class="mini" onclick="copyTxt('api-url')">复制</button></div>
    <div class="kv"><span>当前 Key</span><code class="bigcode" id="api-key">—</code>
      <button class="mini" onclick="copyTxt('api-key')">复制</button></div>
    <div class="chead" style="margin:18px 0 8px;"><span class="ct" style="font-size:13px; color:var(--muted);">调用端点</span>
      <span class="note">三类接口任选其一接入;模型名用「模型」页里的模型 ID,流式加 "stream": true</span></div>
    <div id="api-eps"></div>
  </div>
  <div class="card">
    <div class="chead"><span class="vd" style="background:#b45309">🔑</span><span class="ct">API Keys</span>
      <span class="note">创建立即生效,可同时存在多个;删除立即失效</span></div>
    <div class="add-form" style="margin-bottom:10px;">
      <div class="fe"><span>用途备注</span><input id="key-name" placeholder="如:我的电脑 / 手机" style="width:190px;"></div>
      <button class="mini2" onclick="createKey()" style="align-self:flex-end;">+ 生成新 Key</button>
      <span class="note" style="align-self:flex-end;" id="master-note"></span>
    </div>
    <div id="keys-list"><div class="empty">加载中…</div></div>
  </div>
  <div class="card">
    <div class="chead"><span class="vd" style="background:#0f172a">{ }</span><span class="ct">快速开始</span>
      <button class="mini" onclick="copyTxt('api-curl')">复制 curl</button></div>
    <pre class="curl" id="api-curl">—</pre>
    <div class="kv" style="margin-top:10px;"><span class="note">把 Base URL 和 Key 填进任何 OpenAI 兼容客户端(Cherry Studio / LobeChat / NextChat 等)即可使用</span></div>
  </div>
  </div>
  </div>
</div>
<script>
const fmt = n => (n ?? 0).toLocaleString('en-US');
function barHtml(pct, cls) {
  return `<div class="bar"><i class="${cls || ''}" style="width:${Math.max(pct,2)}%"></i></div>`;
}
function badge(a) {
  if (a.cooldown_remaining > 0) return `<span class="badge cool">冷却 ${Math.ceil(a.cooldown_remaining/60)} 分钟</span>`;
  if (a.current) return '<span class="badge cur">● 当前使用</span>';
  return '<span class="badge">备用</span>';
}
function render() {
  const d = latestAccounts;
  const accs = d.accounts || [];
  document.getElementById('refreshed').textContent =
    '更新于 ' + new Date(d.generated_at * 1000).toLocaleTimeString('zh-CN');
  if (!accs.length) { document.getElementById('cards').innerHTML = '<div class="empty">账号池为空</div>'; return; }
  const totalRemain = accs.reduce((s,a)=>s+(a.credits?.total_remain||0),0);
  const checked = accs.filter(a=>a.checkin?.today && a.checkin?.ok).length;
  document.getElementById('stats').innerHTML = `
    <div class="stat"><div class="v">${accs.length}</div><div class="k">账号总数</div></div>
    <div class="stat"><div class="v">${fmt(totalRemain)}</div><div class="k">总剩余 credits</div></div>
    <div class="stat"><div class="v">${checked}/${accs.length}</div><div class="k">今日已签到</div></div>`;
  document.getElementById('cards').innerHTML = accs.map(a => {
    const c = a.credits || {};
    const exp = a.token_expires_at ? new Date(a.token_expires_at).toLocaleString('zh-CN') : '?';
    const ck = a.checkin || {};
    const now = Date.now();
    const DAY = 86400000;
    // 后端已按 cycle_end 升序（先结束的在前），这里只做渲染
    const pkgs = (c.packages || []).map(p => {
      const pct = p.size ? Math.round(p.remain * 100 / p.size) : 0;
      const barCls = pct <= 20 ? 'low' : pct <= 50 ? 'mid' : '';
      const endTs = p.cycle_end ? new Date(p.cycle_end.replace(' ', 'T')).getTime() : 0;
      const daysLeft = endTs ? Math.ceil((endTs - now) / DAY) : null;
      const soon = daysLeft !== null && daysLeft <= 7;
      const dTxt = daysLeft === null ? '—' :
        daysLeft < 0 ? '已过期' : daysLeft === 0 ? '今天到期' :
        daysLeft === 1 ? '明天到期' : `${daysLeft} 天后到期`;
      return `<div class="pkg">
        <div class="name"><div class="t" title="${p.name}">${p.name}</div>
          <div class="d${soon ? ' soon' : ''}">${dTxt} · ${p.cycle_end || ''}</div></div>
        <div class="mid">${barHtml(pct, barCls)}<span class="pct">${pct}%</span></div>
        <div class="nums${pct <= 20 ? ' low' : ''}"><b>${fmt(p.remain)}</b> <span class="u">/ ${fmt(p.size)} ${p.unit}</span></div>
      </div>`;
    }).join('');
    const list = c.error ? `<div class="err">额度查询失败: ${c.error}</div>`
      : c.packages?.length ? `<div class="pkg-list">${pkgs}</div>` : '<div class="empty">无活跃资源包</div>';
    return `<div class="card">
      <div class="head"><span class="badge">WORKBUDDY</span>${badge(a)}
        <span class="nick">${a.nickname || '未知账号'}</span>
        ${a.current ? '' : `<button class="sw" onclick="switchTo('${a.uid}', this)">设为当前使用</button>`}</div>
      <div class="meta">UID <code>${(a.uid||'').slice(0,8)}…</code>
        · token 到期 ${exp}${a.token_expired ? ' <b style="color:#ef4444">(已过期,将自动刷新)</b>' : ''}
        · 合计 <b>${fmt(c.total_remain)}</b> / ${fmt(c.total_size)} credits</div>
      <div class="checkin">今日签到:
        ${ck.today ? (ck.ok ? `<span class="ok">✅ ${ck.msg || '已签到'}${ck.time ? ' ('+ck.time+')' : ''}</span>`
                            : `<span class="no">❌ ${ck.msg || '失败'}</span>`)
                  : '<span class="no">未记录(服务重启后首次签到前)</span>'}</div>
      ${list}
    </div>`;
  }).join('');
}
let latestAccounts = {accounts: []};
async function load() {
  try { latestAccounts = await (await fetch('/v1/account-status')).json(); }
  catch(e) { document.getElementById('cards').innerHTML = `<div class="empty">加载失败: ${e}</div>`; return; }
  render();
}
async function switchTo(uid, btn) {
  btn.disabled = true; btn.textContent = '切换中…';
  try {
    const r = await fetch('/v1/account-switch', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({uid})});
    if (!r.ok) throw new Error((await r.json()).error?.message || r.status);
  } catch(e) { alert('切换失败: ' + e.message); }
  load();
}

/* ---- 模型管理 ---- */
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
let editingName = null;   // 当前展开参数编辑的模型名(编辑期间冻结列表刷新)
const fmtK = n => !n ? '—' : n >= 1048576 ? (n/1048576).toFixed(n%1048576 ? 1 : 0) + 'M' : Math.round(n/1024) + 'K';
const IN_LAB = {text:'文本', image:'图片', video:'视频', pdf:'PDF'};
function specSummary(s) {
  if (!s) return '—';
  const inp = (s.input || []).map(x => IN_LAB[x] || x).join('/');
  const out = (s.output || []).map(x => IN_LAB[x] || x).join('/');
  return `<span class="spec"><span><b>${fmtK(s.context_length)}</b> 上下文</span>·<span><b>${fmtK(s.max_output_tokens)}</b> 最大输出</span>·<span>输入 <span class="tag">${inp || '—'}</span></span>·<span>输出 <span class="tag">${out || '—'}</span></span></span>`;
}
function editFormHtml(name, s) {
  const chk = (g, v, lab) =>
    `<label class="chip"><input type="checkbox" data-g="${g}" value="${v}" ${s[g]?.includes(v) ? 'checked' : ''}> ${lab}</label>`;
  return `<div class="mrow-edit">
    <div class="fe"><span>上下文窗口</span><input id="e-ctx" type="number" value="${s.context_length}"></div>
    <div class="fe"><span>最大输出 Token</span><input id="e-out" type="number" value="${s.max_output_tokens}"></div>
    <div class="fe"><span>输入类型</span><div style="display:flex; gap:6px;">${['text:文本','image:图片','video:视频','pdf:PDF'].map(x => { const [v,l] = x.split(':'); return chk('input', v, l); }).join('')}</div></div>
    <div class="fe"><span>输出类型</span><div style="display:flex; gap:6px;">${chk('output','text','文本')}</div></div>
    <div class="fe"><span>&nbsp;</span><div style="display:flex; gap:8px;">
      <button class="mini2" onclick="saveSpecs('${esc(name)}')">保存参数</button>
      <button class="mini" onclick="toggleEdit('${esc(name)}')">取消</button></div></div>
  </div>`;
}
async function loadModels() {
  let d;
  try { d = await (await fetch('/v1/models-info')).json(); }
  catch(e) { return; }
  const api = d.api || {};
  document.getElementById('api-url').textContent = api.base_url || '—';
  const k = document.getElementById('api-key');
  k.textContent = api.auth_enabled ? api.api_key : '(未启用鉴权,客户端留空即可)';
  document.getElementById('api-note').textContent = api.auth_enabled ? '' : '如需启用鉴权,启动时加 --api-key your-secret';
  if (editingName) return;   // 编辑展开期间冻结列表,避免输入丢失
  // 展示过滤:auto、无倍率(官方未上架计费)、探测失败(❌=不能用)的模型一律不显示;
  // 禁用模型保留以便恢复
  const visible = (d.models || []).filter(m =>
    m.disabled || (m.name !== 'auto' && m.meta?.credits && !(m.probe && !m.probe.ok)));
  const vendorOf = n => {
    n = n.toLowerCase();
    if (n.startsWith('deepseek')) return 'DeepSeek';
    if (n.startsWith('glm') || n.startsWith('glm5v')) return '智谱 GLM';
    if (n.startsWith('kimi')) return 'Kimi';
    if (n.startsWith('hy') || n.startsWith('hunyuan')) return '混元';
    if (n.startsWith('minimax')) return 'MiniMax';
    return '其他';
  };
  const VENDOR_META = {
    'DeepSeek': {color:'#4D6BFE', abbr:'D'},
    '智谱 GLM': {color:'#3859FF', abbr:'Z'},
    'Kimi': {color:'#1f2937', abbr:'K'},
    '混元': {color:'#0052D9', abbr:'混'},
    'MiniMax': {color:'#ef4444', abbr:'M'},
    '其他': {color:'#6b7280', abbr:'·'},
  };
  const ORDER = ['DeepSeek', '智谱 GLM', 'Kimi', '混元', 'MiniMax', '其他'];
  const rateNumOf = m => m.meta?.credits ? parseFloat(String(m.meta.credits).replace(/[^0-9.]/g, '')) : Infinity;
  const groups = {};
  for (const m of visible) { const v = vendorOf(m.name); (groups[v] = groups[v] || []).push(m); }
  for (const v in groups) groups[v].sort((a, b) => rateNumOf(a) - rateNumOf(b));
  ORDER.sort((a, b) => (groups[a] && groups[b] ? rateNumOf(groups[a][0]) - rateNumOf(groups[b][0])
                        : groups[a] ? -1 : groups[b] ? 1 : 0));
  const rowHtml = m => {
    const p = m.probe;
    const ps_badge = p && p.ok ? '<span class="ps" title="最近一次可用性检测通过">✅</span>' : '';
    const op = m.disabled
      ? `<button class="mini" onclick="toggleModel('${esc(m.name)}')">恢复</button>`
      : `<button class="mini" onclick="toggleEdit('${esc(m.name)}')">编辑</button>
         <button class="mini" onclick="probeOne('${esc(m.name)}', this)">测</button>
         <button class="mini warn" onclick="toggleModel('${esc(m.name)}')">禁</button>`;
    const src = m.source && m.source !== '上游' ? `<span class="badge src">${m.source}</span>` : '';
    const meta = m.meta || {};
    const tags = (meta.tags || []).map(t => {
      const parts = String(t).split(':');          // badge:标签:颜色
      const lab = parts[1] || '', col = parts[2] || '#d97706';
      return `<span class="mtag" style="color:${esc(col)}; border-color:${esc(col)}55;">${esc(lab)}</span>`;
    }).join('');
    const rateNum = meta.credits ? parseFloat(String(meta.credits).replace(/[^0-9.]/g, '')) : null;
    const rateCls = rateNum === null ? '' : rateNum <= 0.1 ? 'rate-lo' : rateNum <= 0.6 ? 'rate-mid' : 'rate-hi';
    const rate = rateNum !== null
      ? `<div class="mrate ${rateCls}">${rateNum}x<small>倍率</small></div>` : '';
    const desc = meta.description ? ` title="${esc(m.name)} · ${esc(meta.description)}${meta.credits ? ' · ' + esc(meta.credits) : ''}"` : '';
    const inp = (m.specs?.input || []).map(x => IN_LAB[x] || x).join(' / ');
    const out = (m.specs?.output || []).map(x => IN_LAB[x] || x).join(' / ');
    let row = `<div class="mrow${m.disabled ? ' off' : ''}" id="mrow-${esc(m.name)}">
      <div class="mmain">
        <div class="mtitle"><span class="t"${desc}>${m.name}</span>${src}${tags}${ps_badge}</div>
        <div class="mspec">${fmtK(m.specs?.context_length)} 上下文 · ${fmtK(m.specs?.max_output_tokens)} 最大输出 · 输入 ${inp || '—'} · 输出 ${out || '—'}</div>
      </div>
      ${rate}
      <div class="ops">${op}</div>
    </div>`;
    if (editingName === m.name && !m.disabled) row += editFormHtml(m.name, m.specs || {});
    return row;
  };
  const grouped = ORDER.filter(v => groups[v] && groups[v].length).map(v => {
    const vm = VENDOR_META[v] || VENDOR_META['其他'];
    const ms = groups[v];
    return `<div class="vcard">
      <div class="vhead">
        <span class="vd" style="background:${vm.color}">${vm.abbr}</span>
        <span class="vname">${v}</span>
        <span class="vg-n">${ms.length} 个模型</span>
      </div>
      <div class="vbody">${ms.map(rowHtml).join('')}</div>
    </div>`;
  }).join('');
  document.getElementById('model-list')
.innerHTML =
    grouped || '<div class="empty">所有模型均不可用或已被过滤</div>';
  window.latestModels = d.models || [];
  renderApi();
}

/* ---- v2 页签与接口页 ---- */
function showPage(p) {
  for (const id of ['home', 'models', 'api'])
    document.getElementById('page-' + id).style.display = (id === p ? '' : 'none');
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('act', t.dataset.p === p));
  localStorage.setItem('wb-page', p);
  if (p === 'api') { renderApi(); loadKeys(); }
}
function renderApi() {
  const pv = 'v1';
  const host = (document.getElementById('api-url').textContent || '').replace(/\\/v1$/, '');
  const p = (host || 'http://127.0.0.1:8787') + '/' + pv;
  const eps = [
    ['POST', '/chat/completions', 'OpenAI Chat — 通用对话/工具调用'],
    ['POST', '/responses', 'OpenAI Responses — Codex CLI'],
    ['POST', '/messages', 'Anthropic Messages — Claude Code'],
  ];
  document.getElementById('api-eps').innerHTML = eps.map(([m, path, desc]) =>
    `<div class="ep"><span class="method ${m.toLowerCase()}">${m}</span><code>${p}${esc(path)}</code><span class="edesc">${desc}</span></div>`).join('');
  const model = (window.latestModels || []).find(x => !x.disabled);
  const modelName = model ? model.name : 'glm-5.2';
  const key = (document.getElementById('api-key').textContent || '').trim();
  const keyHdr = key.startsWith('(') ? '' : `
  -H "Authorization: Bearer ${key}"`;
  document.getElementById('api-curl').textContent =
    `curl ${p}/chat/completions -H "Content-Type: application/json"${keyHdr} -d '{"model":"${modelName}","messages":[{"role":"user","content":"你好"}],"stream":true}'`;
}
(function initV2() {
  const pg = localStorage.getItem('wb-page');
  if (pg && pg !== 'home') showPage(pg);
  const pv = localStorage.getItem('wb-pv');
  if (pv) { const r = document.querySelector(`input[name="pv"][value="${pv}"]`); if (r) r.checked = true; }
})();
function toggleEdit(name) {
  editingName = (editingName === name) ? null : name;
  loadModels();
}
async function saveSpecs(name) {
  const get = g => [...document.querySelectorAll('#model-list .mrow-edit input[data-g="' + g + '"]:checked')].map(i => i.value);
  const specs = {
    context_length: parseInt(document.getElementById('e-ctx').value) || 131072,
    max_output_tokens: parseInt(document.getElementById('e-out').value) || 8192,
    input: get('input').length ? get('input') : ['text'],
    output: get('output').length ? get('output') : ['text'],
  };
  try {
    const r = await fetch('/v1/models/specs', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, specs})});
    if (!r.ok) throw new Error((await r.json()).error?.message || r.status);
  } catch(e) { alert('保存失败: ' + e.message); return; }
  editingName = null;
  loadModels();
}
async function probeOne(name, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await fetch('/v1/models/probe', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: name})});
  } catch(e) { alert('探测失败: ' + e.message); }
  loadModels();
}
async function refreshModels(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '刷新中…'; }
  try {
    const r = await fetch('/v1/models-refresh');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    document.getElementById('models-updated').textContent =
      '已同步 ' + d.count + ' 个模型 · ' + new Date().toLocaleTimeString('zh-CN');
  } catch(e) { alert('刷新失败: ' + e.message); }
  if (btn) { btn.disabled = false; btn.textContent = '↻ 刷新模型列表'; }
  loadModels();
}
async function toggleModel(name) {
  await fetch('/v1/models/delete', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
  loadModels();
}
function copyTxt(id) {
  navigator.clipboard.writeText(document.getElementById(id).textContent.trim());
}
/* ---- API Keys 管理 ---- */
async function loadKeys() {
  let d;
  try { d = await (await fetch('/v1/keys')).json(); }
  catch(e) { return; }
  const fmtT = ts => ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '从未';
  const rows = (d.keys || []).map(k => `<div class="krow">
    <div class="kname" title="${esc(k.name)}">${esc(k.name)}</div>
    <code title="点击复制" style="cursor:pointer;" onclick="copyKey('${esc(k.key)}')">${esc(k.key)}</code>
    <div class="ku">最近使用: ${fmtT(k.last_used)}</div>
    <button class="mini warn" onclick="delKey('${esc(k.key)}')">删除</button>
  </div>`).join('');
  document.getElementById('keys-list').innerHTML =
    rows || '<div class="empty" style="padding:18px;">还没有创建 Key — 当前所有客户端均可免鉴权调用</div>';
  document.getElementById('master-note').textContent =
    d.master_set ? '启动参数主密钥同时有效' : '';
}
function copyKey(k) { navigator.clipboard.writeText(k); }
async function createKey() {
  const name = document.getElementById('key-name').value.trim() || '未命名';
  try {
    const r = await fetch('/v1/keys', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    document.getElementById('key-name').value = '';
    await loadKeys(); await loadModels();
    alert('Key 已创建(已自动复制):' + String.fromCharCode(10, 10) + d.key);
    navigator.clipboard.writeText(d.key);
  } catch(e) { alert('创建失败: ' + e.message); }
}
async function delKey(key) {
  if (!confirm('删除后该 Key 立即失效,确定删除?')) return;
  try {
    const r = await fetch('/v1/keys/delete', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({key})});
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch(e) { alert('删除失败: ' + e.message); }
  loadKeys(); loadModels();
}
load();
loadModels();
setInterval(load, 30000);
</script>
</body>
</html>"""

DEFAULT_MODELS = [
    "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v3", "deepseek-r1",
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "minimax-m3", "minimax-m3-pay",
    "hy3", "hy3-preview-agent",
    "auto",
]

# 模型别名映射（允许客户端用简短名称或常见别名直接请求）
MODEL_ALIASES = {
    "v4.1": "deepseek-v4.1-flash",
    "v4.1-flash": "deepseek-v4.1-flash",
    "deepseek-v4.1": "deepseek-v4.1-flash",
    "v4": "deepseek-v4-pro",
    "v4-pro": "deepseek-v4-pro",
    "v4-flash": "deepseek-v4-flash",
    "r1": "deepseek-r1",
    "v3": "deepseek-v3",
}

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="codebuddy2openai", version="2.0")
# 允许 CLIProxyAPI 管理面板(8317)下的 workbuddy 板块页跨源调用本服务
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8317", "http://127.0.0.1:8317"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
CONFIG: dict = {"api_key": "", "pool": None, "log_path": None,
                "desensitize": False, "no_compact": False}  # pool: CredentialPool | None


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


# ---- 动态 API Key 管理(面板创建/删除,立即生效) ----
KEYS_FILE = Path(__file__).parent / "api_keys.json"
_keys_cache: dict = {"mtime": 0.0, "keys": []}
_keys_last_used: dict = {}   # key -> 最后使用时间(内存,随增删持久化)


def _load_keys() -> list[dict]:
    """读取动态 key 列表(mtime 缓存,避免每请求解析 JSON)。"""
    try:
        mt = KEYS_FILE.stat().st_mtime
    except OSError:
        return []
    if _keys_cache["mtime"] != mt:
        try:
            _keys_cache["keys"] = json.loads(KEYS_FILE.read_text(encoding="utf-8")).get("keys") or []
            _keys_cache["mtime"] = mt
        except Exception:
            pass
    return _keys_cache["keys"]


def _save_keys(keys: list[dict]):
    for k in keys:
        k["last_used"] = _keys_last_used.get(k["key"])
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"keys": keys}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, KEYS_FILE)
    _keys_cache["keys"] = keys
    _keys_cache["mtime"] = KEYS_FILE.stat().st_mtime


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    """鉴权:启动参数主密钥 或 面板创建的任一动态 key。

    主密钥未设置且不存在动态 key 时不鉴权(本机自用模式)。
    """
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    master = CONFIG.get("api_key") or ""
    keys = _load_keys()
    # 鉴权开关:设置了主密钥,或创建过 Key(api_keys.json 存在)即启用;全删后所有请求 401
    auth_on = bool(master) or KEYS_FILE.exists()
    if not auth_on:
        return
    if master and token == master:
        return
    for k in keys:
        if not k.get("disabled") and k.get("key") == token:
            if token:
                _keys_last_used[token] = int(time.time())
            return
    raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _pool() -> CredentialPool:
    if CONFIG["pool"] is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return CONFIG["pool"]


def _safe_headers(pool: CredentialPool, cred: CredentialManager, rid: str, model_name: str) -> dict | None:
    """取账号请求头；token 刷新失败视为该账号不可用（冷却并返回 None）。"""
    prefix = f"[{rid}] " if rid else ""
    try:
        return cred.get_headers()
    except Exception as e:
        nick = _safe_nickname(cred)
        _log(f"{prefix}✗ 账号[{nick}] 凭据不可用（{e}），冷却并尝试切换")
        pool.report_failure(cred, 401, str(e))
        return None


def _safe_nickname(cred: CredentialManager) -> str:
    try:
        return cred.summary().get("nickname") or cred.path.name
    except Exception:
        return cred.path.name


@app.get("/health")
def health():
    pool: CredentialPool = CONFIG["pool"]
    info: dict = {"status": "ok", "platform": sys.platform, "python": sys.version.split()[0],
                  "auth_dirs": [str(d) for d in auth_dirs() if d.is_dir()],
                  "mode": "direct-proxy (native function calling, multi-account pool)"}
    if pool is not None:
        info["accounts"] = pool.snapshot()
        info["accounts_total"] = len(pool)
    return info


# ---------------------------------------------------------------------------
# 账号池可视化面板（/panel + /v1/account-status）
# ---------------------------------------------------------------------------

CREDIT_URL = "https://www.codebuddy.cn/v2/billing/meter/get-user-resource"
CHECKIN_STATE_FILE = Path(__file__).parent / "checkin_state.json"
CREDIT_TTL_SECS = 30
_credit_cache: dict = {}          # uid -> {"t": epoch, "data": dict}
_credit_lock = threading.Lock()


def _fetch_credits(cred: CredentialManager) -> dict:
    """查询账号的 credits 资源包（腾讯 get-user-resource），30s TTL 缓存。

    返回 {"total_remain": int, "total_size": int, "packages": [...], "error": str|None}。
    """
    try:
        info = cred.summary()
        uid = info.get("uid") or cred.path.name
    except Exception:
        uid = cred.path.name
    with _credit_lock:
        hit = _credit_cache.get(uid)
        if hit and time.time() - hit["t"] < CREDIT_TTL_SECS:
            return hit["data"]
    try:
        headers = cred.get_headers()
        with httpx.Client(timeout=15) as c:
            r = c.post(CREDIT_URL, headers=headers, json={})
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(data.get("msg") or f"HTTP {r.status_code}")
        accounts = (((data.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        now = time.time()
        packages = []
        for a in accounts:
            # 只保留未到期的活跃资源包
            try:
                cycle_end_ts = time.mktime(time.strptime(a.get("CycleEndTime", ""), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                cycle_end_ts = None
            if cycle_end_ts is not None and cycle_end_ts < now:
                continue
            remain = int(a.get("CapacityRemain") or 0)
            if remain <= 0:
                continue   # 已耗尽的历史资源包不在面板展示
            packages.append({
                "name": a.get("PackageName") or "资源包",
                "remain": remain,
                "used": int(a.get("CapacityUsed") or 0),
                "size": int(a.get("CapacitySize") or 0),
                "unit": a.get("CapacityUnit") or "credits",
                "cycle_end": a.get("CycleEndTime", ""),
            })
        # 按周期截止时间升序：即将结束的资源包排在前面
        packages.sort(key=lambda p: p["cycle_end"] or "9999-12-31")
        result = {"total_remain": sum(p["remain"] for p in packages),
                  "total_size": sum(p["size"] for p in packages),
                  "packages": packages, "error": None}
    except Exception as e:
        result = {"total_remain": 0, "total_size": 0, "packages": [], "error": str(e)}
    with _credit_lock:
        _credit_cache[uid] = {"t": time.time(), "data": result}
    return result


def _load_checkin_state() -> dict:
    try:
        return json.loads(CHECKIN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


@app.get("/v1/account-status")
def account_status():
    """面板数据接口：账号池状态 + credits 额度 + 今日签到状态。"""
    pool: CredentialPool = CONFIG["pool"]
    today = time.strftime("%Y-%m-%d")
    state = _load_checkin_state()
    accounts = []
    if pool is not None:
        for i, cred in enumerate(pool.creds):
            s = pool.snapshot()[i] if i < len(pool.snapshot()) else {}
            credits = _fetch_credits(cred)
            cs = state.get(s.get("uid") or "", {})
            accounts.append({
                "nickname": s.get("nickname"),
                "uid": s.get("uid"),
                "file": cred.path.name,
                "current": s.get("current", False),
                "cooldown_remaining": s.get("cooldown_remaining", 0),
                "token_expired": s.get("token_expired", False),
                "token_expires_at": s.get("token_expires_at", 0),
                "checkin": {"today": cs.get("date") == today, "ok": cs.get("ok"),
                            "msg": cs.get("msg"), "time": cs.get("time")},
                "credits": credits,
            })
    return {"accounts": accounts, "generated_at": int(time.time())}


@app.post("/v1/account-switch")
async def account_switch(request: Request):
    """手动切换当前使用的账号。请求体: {"uid": "<账号uid或凭据文件名>"}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    uid = (payload or {}).get("uid", "")
    if not uid:
        raise HTTPException(status_code=400, detail={"error": {"message": "uid is required", "type": "invalid_request_error"}})
    pool = _pool()
    if not pool.set_current(uid):
        raise HTTPException(status_code=404, detail={"error": {"message": f"账号不存在: {uid}", "type": "not_found"}})
    cur = pool.get_current()
    _log(f"⇄ 手动切换当前账号 -> {_safe_nickname(cur)} ({uid})")
    return {"ok": True, "current_uid": uid, "accounts": pool.snapshot()}


# ---------------------------------------------------------------------------
# 模型管理端点：列表/探测/自定义增删/启停
# ---------------------------------------------------------------------------

def _cred_for_models() -> CredentialManager | None:
    pool: CredentialPool | None = CONFIG["pool"]
    return pool.get_current() if pool else None


@app.get("/v1/models-info")
def models_info():
    """面板模型管理数据：合并模型列表 + 探测缓存 + 探测进度 + API 接入信息。"""
    cred = _cred_for_models()
    reg = _load_registry()
    models = _all_models(cred)
    probes = reg.get("probe") or {}
    for m in models:
        m["probe"] = probes.get(m["name"])
        m["disabled"] = False
    for name in reg.get("disabled") or []:
        models.append({"name": name, "source": "", "disabled": True, "probe": probes.get(name)})
    return {"models": models, "probe_state": dict(_probe_state),
            "api": _api_info(), "custom": reg.get("custom") or []}


@app.post("/v1/models/custom")
async def models_add_custom(request: Request):
    """添加自定义模型。请求体: {"name": "模型名", "alias": "可选别名"}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    name = ((payload or {}).get("name") or "").strip()
    alias = ((payload or {}).get("alias") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"error": {"message": "name is required", "type": "invalid_request_error"}})
    reg = _load_registry()
    reg.setdefault("custom", [])
    if any(c["name"] == name for c in reg["custom"]):
        raise HTTPException(status_code=409, detail={"error": {"message": f"模型已存在: {name}", "type": "conflict"}})
    reg["custom"].append({"name": name, "alias": alias, "added_at": int(time.time())})
    specs = (payload or {}).get("specs")
    if isinstance(specs, dict) and specs:
        merged = _default_specs()
        merged.update(specs)
        reg.setdefault("specs", {})[name] = merged
    reg["disabled"] = [d for d in (reg.get("disabled") or []) if d != name]
    _save_registry(reg)
    _log(f"+ 添加自定义模型: {name}" + (f" (别名 {alias})" if alias else ""))
    return {"ok": True, "custom": reg["custom"]}


@app.post("/v1/models/delete")
async def models_delete(request: Request):
    """删除自定义模型；对内置/上游模型则是禁用⇄恢复开关。请求体: {"name": "..."}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    name = ((payload or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"error": {"message": "name is required", "type": "invalid_request_error"}})
    reg = _load_registry()
    before = len(reg.get("custom") or [])
    reg["custom"] = [c for c in (reg.get("custom") or []) if c["name"] != name]
    if len(reg["custom"]) < before:
        reg["probe"] = {k: v for k, v in (reg.get("probe") or {}).items() if k != name}
        (reg.get("specs") or {}).pop(name, None)
        _save_registry(reg)
        _log(f"- 删除自定义模型: {name}")
        return {"ok": True, "action": "deleted", "custom": reg["custom"]}
    disabled = reg.get("disabled") or []
    if name in disabled:
        reg["disabled"] = [d for d in disabled if d != name]
        action = "enabled"
    else:
        reg["disabled"] = disabled + [name]
        action = "disabled"
    _save_registry(reg)
    _log(f"⏻ 模型{action}: {name}")
    return {"ok": True, "action": action, "disabled": reg["disabled"]}


@app.post("/v1/models/specs")
async def models_specs(request: Request):
    """更新模型参数规格。请求体: {"name": "...", "specs": {"context_length":..., "max_output_tokens":..., "input":[...], "output":[...]}}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    name = ((payload or {}).get("name") or "").strip()
    specs = (payload or {}).get("specs")
    if not name or not isinstance(specs, dict):
        raise HTTPException(status_code=400, detail={"error": {"message": "name and specs are required", "type": "invalid_request_error"}})
    reg = _load_registry()
    merged = _default_specs()
    for k in ("context_length", "max_output_tokens"):
        try:
            merged[k] = int(specs.get(k) or merged[k])
        except (TypeError, ValueError):
            pass
    for k in ("input", "output"):
        v = specs.get(k)
        if isinstance(v, list) and v:
            merged[k] = [str(x) for x in v]
    reg.setdefault("specs", {})[name] = merged
    _save_registry(reg)
    return {"ok": True, "name": name, "specs": merged}


# ---------------------------------------------------------------------------
# API Key 管理(面板创建/删除,立即生效)
# ---------------------------------------------------------------------------

@app.get("/v1/keys")
def keys_list():
    keys = _load_keys()
    for k in keys:
        k["last_used"] = _keys_last_used.get(k["key"]) or k.get("last_used")
    master = CONFIG.get("api_key") or ""
    return {"keys": keys, "master_set": bool(master),
            "auth_enabled": bool(master) or bool(keys)}


@app.post("/v1/keys")
async def keys_create(request: Request):
    """创建新 API Key。请求体: {"name": "用途备注"}。返回完整 key(仅此一次展示原文)。"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    name = ((payload or {}).get("name") or "").strip() or "未命名"
    import secrets as _secrets
    key = "sk-wb-" + _secrets.token_hex(16)
    keys = _load_keys()
    while any(k["key"] == key for k in keys):   # 极小概率碰撞
        key = "sk-wb-" + _secrets.token_hex(16)
    keys.append({"key": key, "name": name, "created_at": int(time.time()),
                 "last_used": None, "disabled": False})
    _save_keys(keys)
    _log(f"+ 创建 API Key: {name} ({key[:12]}…)")
    return {"ok": True, "key": key, "name": name}


@app.post("/v1/keys/delete")
async def keys_delete(request: Request):
    """删除 API Key。请求体: {"key": "sk-wb-..."}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    key = ((payload or {}).get("key") or "").strip()
    keys = _load_keys()
    new_keys = [k for k in keys if k["key"] != key]
    if len(new_keys) == len(keys):
        raise HTTPException(status_code=404, detail={"error": {"message": "key 不存在", "type": "not_found"}})
    _save_keys(new_keys)
    _log(f"- 删除 API Key: {key[:12]}…")
    return {"ok": True, "keys": new_keys}


@app.get("/v1/models-refresh")
def models_refresh():
    """强制重新拉取上游模型目录(名称/规格/倍率/标签),不发聊天请求、零消耗。"""
    cred = _cred_for_models()
    with _models_lock:
        _upstream_models_cache["t"] = 0.0
    models = _all_models(cred)
    reg = _load_registry()
    for m in models:
        m["probe"] = (reg.get("probe") or {}).get(m["name"])
        m["disabled"] = False
    return {"ok": True, "refreshed": True, "models": models,
            "count": len(models)}


@app.post("/v1/models/probe")
async def models_probe(request: Request):
    """探测单个模型（发一次最小真实请求，消耗少量 credits）。请求体: {"model": "..."}。"""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    model = ((payload or {}).get("model") or "").strip()
    cred = _cred_for_models()
    if cred is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "账号池为空", "type": "auth_error"}})
    result = await asyncio.to_thread(probe_model, cred, model)
    return {"ok": True, "model": model, "result": result}


@app.post("/v1/models/probe-all")
async def models_probe_all():
    """后台顺序探测全部模型；进度通过 /v1/models-info 的 probe_state 轮询。"""
    if _probe_state.get("running"):
        return {"ok": True, "already_running": True, "state": dict(_probe_state)}
    cred = _cred_for_models()
    if cred is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "账号池为空", "type": "auth_error"}})
    threading.Thread(target=_probe_all_worker, daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/panel")
def panel():
    return HTMLResponse(PANEL_HTML)


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    models = _all_models(_cred_for_models())
    data = [{"id": m["name"], "object": "model", "created": 1700000000,
             "owned_by": "codebuddy"} for m in models]
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    pool = _pool()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    raw_model = body.get("model", "auto")
    body["model"] = _resolve_model(raw_model)
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system", "developer"),
                                desensitize_harness_user=True,
                                desensitize_tools=True,
                                compact_harness=not CONFIG.get("no_compact"),
                                strip_tool_metadata=True)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(pool, url, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应。
    # 账号池 failover：当前账号额度/认证类失败时自动切换下一个账号重试。
    collected: dict | None = None
    last_status, last_raw = 503, b'"no credentials available in pool"'
    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        try:
            async with httpx.AsyncClient(timeout=300) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        last_raw = await r.aread()
                        last_status = r.status_code
                        _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(last_raw.decode('utf-8','replace'),200)}")
                        _log(f"[{rid}] ── ERROR BODY ──\n{last_raw.decode('utf-8','replace')}")
                        if _is_failover_error(r.status_code, last_raw):
                            pool.report_failure(cred, r.status_code, last_raw)
                            continue
                        raise HTTPException(status_code=r.status_code, detail=_safe_err_raw(last_raw, r.status_code))
                    collected = await _collect_stream(r)
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
            raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
        pool.report_success(cred)
        break
    if collected is None:
        raise HTTPException(status_code=last_status, detail=_safe_err_raw(last_raw, last_status))
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(pool: CredentialPool, url: str, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    带账号池 failover：当前账号在拿到响应状态阶段遇额度/认证类错误（尚未向客户端
    输出任何字节）时，自动切换下一个账号重试；转发开始后不再切换。
    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    prefix = f"[{rid}] " if rid else ""

    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        finish_reason = None
        tool_names: list[str] = []
        usage: dict = {}
        saw_filter = False
        buf = b""
        raw_parts: list[bytes] = []   # 累积完整原始 SSE

        def _feed(chunk: bytes):
            nonlocal finish_reason, saw_filter, buf
            # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if obj.get("usage"):
                    usage.update(obj["usage"])
                for ch in obj.get("choices") or []:
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
                    for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                        nm = (tc.get("function") or {}).get("name")
                        if nm:
                            tool_names.append(nm)
                # 内容审核拦截常以 content-filter 或特殊中文文案返回
                try:
                    text_repr = data.decode("utf-8", "replace")
                except Exception:
                    text_repr = ""
                if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                    saw_filter = True

        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(err.decode('utf-8','replace'),200)}")
                        _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                        if _is_failover_error(r.status_code, err):
                            pool.report_failure(cred, r.status_code, err)
                            _log(f"{prefix}↻ 账号[{_safe_nickname(cred)}] 不可用（HTTP {r.status_code}），切换下一个账号重试")
                            continue
                        yield _err_event(err, r.status_code)
                        return
                    stream_buf = b""
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            raw_parts.append(chunk)
                            _feed(chunk)
                            stream_buf += chunk
                            while b"\n" in stream_buf:
                                line, stream_buf = stream_buf.split(b"\n", 1)
                                stripped = line.strip()
                                if not stripped.startswith(b"data:"):
                                    if stripped:
                                        yield line + b"\n"
                                    continue
                                data_bytes = stripped[5:].strip()
                                if data_bytes == b"[DONE]":
                                    yield b"data: [DONE]\n\n"
                                    continue
                                try:
                                    obj = json.loads(data_bytes)
                                    for ch in obj.get("choices") or []:
                                        delta = ch.get("delta") or {}
                                        for empty_key in ("reasoning_content", "refusal", "function_call", "extra_fields", "tool_calls"):
                                            if delta.get(empty_key) in ("", None, []):
                                                delta.pop(empty_key, None)
                                    out_line = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
                                    yield out_line
                                except Exception:
                                    yield line + b"\n"
        except httpx.HTTPError as e:
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            yield _err_event(str(e).encode(), 502)
            return

        pool.report_success(cred)
        # 流结束：输出完成日志
        elapsed = time.time() - t0 if t0 else 0
        tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
        _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
             + (f" | tool_calls={tool_names}" if tool_names else "")
             + f" | tokens={usage.get('total_tokens', '?')}")
        # 完整原始 SSE（后端返回的全部内容）
        _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")
        return


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_failover(pool: CredentialPool, url: str, body: dict,
                                      rid: str = "", model_name: str = "?") -> tuple[int, bytes, CredentialManager | None, dict]:
    """带账号池 failover 的后端请求。

    从当前账号开始依次尝试：额度/认证类失败（_is_failover_error）切换下一个账号；
    200 但检测到 content-filter 且处于 no_compact 脱敏模式时，按原逻辑用压缩
    harness 重试一次。返回 (status, raw, cred, final_body)；所有账号不可用时
    cred 为 None 且 status 为最后一次的错误状态。
    """
    prefix = f"[{rid}] " if rid else ""
    last: tuple[int, bytes, CredentialManager | None, dict] = (503, b'"no credentials available in pool"', None, body)
    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        status, raw = await _post_backend_once(url, headers, body)
        if status == 200:
            pool.report_success(cred)
            text = raw.decode("utf-8", "replace")
            if _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
                retry_body = _chat_body_desensitize(body, force_compact=True)
                _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
                _log(f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}")
                retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
                retry_text = retry_raw.decode("utf-8", "replace")
                if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
                    return retry_status, retry_raw, cred, retry_body
            return status, raw, cred, body
        _log(f"{prefix}✗ HTTP {status} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(raw.decode('utf-8','replace'),200)}")
        if not _is_failover_error(status, raw):
            return status, raw, cred, body
        pool.report_failure(cred, status, raw)
        _log(f"{prefix}↻ 账号[{_safe_nickname(cred)}] 不可用（HTTP {status}），切换下一个账号重试")
        last = (status, raw, cred, body)
    return last


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)
    pool = _pool()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    raw_model = chat_body.get("model", "auto")
    chat_body["model"] = _resolve_model(raw_model)
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    chat_body = _chat_body_desensitize(chat_body)

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}")
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log(f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(pool, url, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, cred, final_body = await _post_backend_with_failover(pool, url, chat_body, rid, model_name)
        if status_code != 200:
            _log(f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status_code, detail=_safe_err_raw(raw, status_code))
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log(f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    return JSONResponse(content=result)


async def _stream_responses(pool: CredentialPool, url: str, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流。"""
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        status_code, raw, cred, _ = await _post_backend_with_failover(pool, url, body, rid, model_name)
        if status_code != 200:
            _log(f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            error_evt = {"type": "error", "error": {"message": raw.decode('utf-8','replace')[:500], "code": status_code}}
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)
    pool = _pool()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    raw_model = chat_body.get("model", "auto")
    chat_body["model"] = _resolve_model(raw_model)
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(chat_body, roles=("system", "developer"),
                                     desensitize_harness_user=True,
                                     desensitize_tools=True,
                                     compact_harness=not CONFIG.get("no_compact"),
                                     strip_tool_metadata=True)

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    _log(f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    return StreamingResponse(
        _stream_anthropic(pool, url, chat_body, model_name, t0, rid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_anthropic(pool: CredentialPool, url: str, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。

    带账号池 failover：当前账号额度/认证类失败时自动切换下一个账号重试
    （仅在尚未向客户端输出任何事件前切换）。
    """
    prefix = f"[{rid}] " if rid else ""

    for _, cred in pool.candidates():
        headers = _safe_headers(pool, cred, rid, model_name)
        if headers is None:
            continue
        converter = AnthropicStreamConverter(model=model_name)
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", url, headers=headers, json=body) as r:
                    if r.status_code != 200:
                        err = await r.aread()
                        _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | 账号[{_safe_nickname(cred)}] | {_truncate(err.decode('utf-8','replace'),200)}")
                        if _is_failover_error(r.status_code, err):
                            pool.report_failure(cred, r.status_code, err)
                            _log(f"{prefix}↻ 账号[{_safe_nickname(cred)}] 不可用（HTTP {r.status_code}），切换下一个账号重试")
                            continue
                        error_evt = {"type": "error", "error": {"message": err.decode('utf-8','replace')[:500], "type": "api_error", "code": r.status_code}}
                        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                        return
                    async for line in r.aiter_lines():
                        events = converter.feed_line(line)
                        if events:
                            yield events.encode("utf-8")
        except httpx.HTTPError as e:
            _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
            error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
            yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return

        pool.report_success(cred)
        finish_events = converter.finish()
        if finish_events:
            yield finish_events.encode("utf-8")

        elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic token 计数端点（stub）。

    Claude Code 可能在发送消息前调用此端点。
    返回一个简单估算值，不做实际 token 计数。
    """
    _check_auth(authorization, x_api_key)
    return {"input_tokens": 0}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    files = find_auth_files()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if not files:
        sys.stderr.write("\n[警告] 未找到登录文件。请运行 ./login.sh 或在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        sys.stderr.write(f"账号池    : {len(files)} 个账号\n")
        for f in files:
            try:
                cm = CredentialManager(f)
                info = cm.summary()
                sys.stderr.write(f"  - {info.get('nickname')} / {info.get('enterpriseName') or '(个人)'}"
                                 f" | token过期: {'是(将自动刷新)' if info['token_expired'] else '否'} | {f.name}\n")
            except Exception as e:
                sys.stderr.write(f"[警告] 读取凭据失败 {f}：{e}\n")
                ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
                         "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
                         "但审核误拦风险略高于默认压缩模式。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    CONFIG["pool"] = CredentialPool(find_auth_files())
    CONFIG["port"] = args.port

    if not args.skip_check:
        preflight()

    pool: CredentialPool = CONFIG["pool"]
    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    if pool is not None and len(pool) > 0:
        sys.stderr.write(f"   账号池    : {len(pool)} 个账号（额度/认证失败自动切换下一个）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write("   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


# /v2 别名路由:兼容习惯使用 /v2 前缀的客户端(与 /v1 同一处理器)
app.add_api_route("/v2/chat/completions", chat_completions, methods=["POST"])
app.add_api_route("/v2/models", list_models, methods=["GET"])

if __name__ == "__main__":
    main()
