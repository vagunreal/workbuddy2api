#!/usr/bin/env python3
"""
腾讯 CodeBuddy / WorkBuddy 命令行免客户端 OAuth 登录工具
直接向 copilot.tencent.com 发起设备登录流，并在本地自动生成/更新 .info 凭证文件。
"""

import os
import sys
import time
import json
from pathlib import Path
import httpx

BACKEND = "https://copilot.tencent.com"
ORIGIN = "https://www.codebuddy.cn"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": ORIGIN,
    "Referer": f"{ORIGIN}/",
    "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2"
}

def get_auth_dir() -> Path:
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        p = Path(env_dir)
    else:
        p = Path.home() / ".local" / "share" / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    p.mkdir(parents=True, exist_ok=True)
    return p

def main():
    print("=" * 60)
    print(" 腾讯 CodeBuddy / WorkBuddy 账号登录工具 (OAuth)")
    print("=" * 60)

    client = httpx.Client(timeout=30)
    try:
        r = client.post(f"{BACKEND}/v2/plugin/auth/state?platform=CLI", headers=HEADERS, json={})
        res = r.json()
    except Exception as e:
        print(f"获取授权链接失败: {e}")
        sys.exit(1)

    if res.get("code") != 0 or not res.get("data"):
        print(f"服务端返回错误: {res}")
        sys.exit(1)

    state = res["data"]["state"]
    auth_url = res["data"]["authUrl"]

    print("\n请在浏览器中打开以下链接完成登录（QQ/微信/手机号）：\n")
    print(f"  \033[36m{auth_url}\033[0m\n")

    input("👉 在浏览器完成登录后，请按回车继续...")

    print("正在获取 Token 与用户信息...")
    try:
        tok_res = client.get(f"{BACKEND}/v2/plugin/auth/token?state={state}", headers=HEADERS).json()
    except Exception as e:
        print(f"请求 token 失败: {e}")
        sys.exit(1)

    if tok_res.get("code") != 0 or not tok_res.get("data"):
        print(f"登录尚未完成或已过期: {tok_res.get('msg', tok_res)}")
        print("请重新运行并在网页授权成功后再按回车。")
        sys.exit(1)

    tok_data = tok_res["data"]
    access_token = tok_data.get("accessToken", "")
    refresh_token = tok_data.get("refreshToken", "")
    expires_in = tok_data.get("expiresIn", 7200)
    domain = tok_data.get("domain", "www.codebuddy.cn")

    # 获取账号信息
    acct_headers = dict(HEADERS)
    acct_headers["Authorization"] = f"Bearer {access_token}"
    acct_data = {}
    try:
        acct_res = client.get(f"{BACKEND}/v2/plugin/login/account?state={state}", headers=acct_headers).json()
        if acct_res.get("code") == 0 and acct_res.get("data"):
            acct_data = acct_res["data"]
    except Exception as e:
        print(f"获取账号信息警告: {e}")

    uid = acct_data.get("uid") or "default_user"
    nickname = acct_data.get("nickname") or "CodeBuddyUser"
    ent_id = acct_data.get("enterpriseId") or ""
    ent_name = acct_data.get("enterpriseName") or ""

    now_ms = int(time.time() * 1000)
    expires_at = now_ms + expires_in * 1000

    # 执行每日签到
    try:
        checkin_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-User-Id": uid,
            "X-Domain": domain,
        }
        if ent_id:
            checkin_headers["X-Enterprise-Id"] = ent_id
            checkin_headers["X-Tenant-Id"] = ent_id
        c_res = client.post("https://www.codebuddy.cn/v2/billing/meter/daily-checkin", headers=checkin_headers, json={})
        c_data = c_res.json()
        if c_data.get("code") == 0:
            print("🎁 自动每日签到成功！")
        else:
            print(f"每日签到: {c_data.get('msg', 'ok')}")
    except Exception:
        pass

    auth_info = {
        "account": {
            "uid": uid,
            "nickname": nickname,
            "enterpriseId": ent_id,
            "enterpriseName": ent_name
        },
        "auth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": expires_at,
            "domain": domain,
            "lastRefreshTime": now_ms
        }
    }

    auth_dir = get_auth_dir()
    auth_file = auth_dir / f"{uid}.info"
    with open(auth_file, "w", encoding="utf-8") as f:
        json.dump(auth_info, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("✅ 登录成功！凭证已保存。")
    print(f"   用户: {nickname} (UID: {uid})")
    print(f"   凭据路径: {auth_file}")
    print(f"   过期时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_at / 1000))}")
    print("=" * 60 + "\n")

if __name__ == "__main__":
    main()
