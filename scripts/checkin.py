#!/usr/bin/env python3
"""
CodeBuddy / WorkBuddy 每日自动签到与积分领取脚本
探测本地所有 auth 文件（WSL 与 Windows 凭据目录），自动去重、刷新 token 并调用每日签到接口。
"""

import os
import sys
import json
import time
from pathlib import Path
import httpx

BACKEND = "https://copilot.tencent.com"
CHECKIN_URL = "https://www.codebuddy.cn/v2/billing/meter/daily-checkin"
DEFAULT_DOMAIN = "www.codebuddy.cn"
STATE_FILE = Path(__file__).resolve().parent.parent / "checkin_state.json"


def record_checkin_state(uid: str, nickname: str, ok: bool, msg: str):
    """把签到结果写入 checkin_state.json,供 converter /panel 面板展示。"""
    try:
        state = {}
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state[uid] = {
            "nickname": nickname,
            "date": time.strftime("%Y-%m-%d"),
            "time": time.strftime("%H:%M:%S"),
            "ok": ok,
            "msg": msg,
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"写入签到状态失败: {e}")

def get_auth_dirs() -> list[Path]:
    dirs = []
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        dirs.append(Path(env_dir))

    wsl_dir = Path.home() / ".local" / "share" / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    dirs.append(wsl_dir)

    # WSL 下探测 Windows 宿主机所有用户的凭据目录
    for p in Path("/mnt/c/Users").glob("*/AppData/Local/CodeBuddyExtension/Data/Public/auth"):
        dirs.append(p)

    return dirs

def refresh_token(auth_file: Path, session_data: dict) -> dict:
    auth = session_data.get("auth") or {}
    account = session_data.get("account") or {}
    ref_tok = auth.get("refreshToken", "")
    if not ref_tok:
        return session_data

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {auth.get('accessToken', '')}",
        "X-Refresh-Token": ref_tok,
        "X-Auth-Refresh-Source": "plugin",
        "X-User-Id": account.get("uid", ""),
        "X-Enterprise-Id": account.get("enterpriseId", ""),
        "X-Tenant-Id": account.get("enterpriseId", ""),
        "X-Domain": auth.get("domain", DEFAULT_DOMAIN),
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2"
    }

    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(f"{BACKEND}/v2/plugin/auth/token/refresh", headers=headers, json={})
            data = r.json()
        if data.get("code") == 0 and data.get("data"):
            new_auth = data["data"]
            new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
            now_ms = int(time.time() * 1000)
            new_auth["lastRefreshTime"] = now_ms
            if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
                new_auth["expiresAt"] = now_ms + new_auth["expiresIn"] * 1000
            session_data["auth"] = new_auth
            tmp = auth_file.with_suffix(auth_file.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(session_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, auth_file)
            print(f"[{auth_file.name}] Token 自动刷新成功")
    except Exception as e:
        print(f"[{auth_file.name}] Token 刷新跳过: {e}")

    return session_data

def do_checkin(auth_file: Path, processed_uids: set):
    try:
        with open(auth_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[{auth_file.name}] 读取凭据失败: {e}")
        return

    account = data.get("account") or {}
    uid = account.get("uid", "")
    if uid in processed_uids:
        return
    if uid:
        processed_uids.add(uid)

    auth = data.get("auth") or {}
    expires_at = auth.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)
    if now_ms >= (expires_at - 60_000):
        data = refresh_token(auth_file, data)
        auth = data.get("auth") or {}
        account = data.get("account") or {}

    token = auth.get("accessToken", "")
    ent_id = account.get("enterpriseId", "")
    nickname = account.get("nickname") or uid or "未知用户"

    if not token:
        print(f"[{auth_file.name}] 无有效 accessToken")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-User-Id": uid,
        "X-Domain": auth.get("domain", DEFAULT_DOMAIN),
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2"
    }
    if ent_id:
        headers["X-Enterprise-Id"] = ent_id
        headers["X-Tenant-Id"] = ent_id

    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(CHECKIN_URL, headers=headers, json={})
            res = r.json()
        code = res.get("code")
        msg = res.get("msg", "")
        if code == 0:
            print(f"🎉 [{nickname}] 每日签到领积分成功！详情: {res.get('data', {})}")
            record_checkin_state(uid, nickname, True, "签到成功")
        elif "已签到" in msg or code == 10001:
            print(f"ℹ️ [{nickname}] 今日已领过积分（状态正常）。")
            record_checkin_state(uid, nickname, True, "今日已签到")
        else:
            print(f"⚠️ [{nickname}] 签到返回: {msg} (code={code})")
            record_checkin_state(uid, nickname, False, f"{msg} (code={code})")
    except Exception as e:
        print(f"❌ [{nickname}] 签到请求异常: {e}")
        record_checkin_state(uid, nickname, False, f"请求异常: {e}")

def main():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 正在执行 CodeBuddy 每日签到与积分领取...")
    processed_uids = set()
    found_any = False
    for d in get_auth_dirs():
        if not d.is_dir():
            continue
        for f in d.glob("*.info"):
            found_any = True
            do_checkin(f, processed_uids)

    if not found_any:
        print("未发现任何已保存的凭证文件（*.info），请先运行 ./login.sh 登录账号。")

if __name__ == "__main__":
    main()
