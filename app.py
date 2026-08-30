#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import json
import requests
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from seleniumbase import SB

# ==================== 环境变量配置 ====================
EMAIL           = os.environ.get("EMAIL") or ""           # 邮箱,只用于通知使用，可随意填写
SESSION_TOKEN   = os.environ.get("SESSION_TOKEN") or ""   # session token，默认登录方式,非必须
DISCORD_TOKEN   = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token 备用登录方式, 失败时才使用,必须填写
GH_TOKEN        = os.environ.get("GH_TOKEN") or ""        # GitHub PAT token,用于自动更新session token,可选
TG_CHAT_ID      = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN    = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 
CRONJOB_API_KEY = os.environ.get("CRONJOB_API_KEY") or "" # cron-job.org API Key
CRONJOB_ID      = os.environ.get("CRONJOB_ID") or ""      # cron-job.org 任务 ID

# 解析 DISCORD_TOKEN
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 Discord Token,脚本终止。")
    sys.exit(1)

# 构造基础 Cookie 字典
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 记录本次登录方式（用于通知展示）
_LOGIN_METHOD = "SESSION_TOKEN"


# ==================== 核心辅助函数 ====================

def get_cookie_info(sb, name):
    """获取指定 Cookie 的值及其过期时间"""
    try:
        cookies = sb.get_cookies()
        for c in cookies:
            if c.get('name') == name:
                value = c.get('value')
                expiry_ts = c.get('expiry')
                expiry_dt = datetime.fromtimestamp(expiry_ts) if expiry_ts else None
                return value, expiry_dt
    except Exception as e:
        print(f"⚠️ 获取 Cookie 信息异常: {e}")
    return None, None


def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3):
    """判断 Cookie 是否需要更新"""
    if new_value is None:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now()).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
    return False


def update_github_secret(secret_name, new_value):
    """通过 GitHub CLI 更新仓库 Secret"""
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    masked = new_value[:4] + "..." + new_value[-4:] if len(new_value) > 8 else "***"
    print(f"🔄 更新 GitHub Secret: {secret_name} (新值: {masked})")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
            capture_output=True, text=True, timeout=30, check=False,
            env=env
        )
        if proc.returncode == 0:
            print("✅ GitHub Secret 更新成功")
            return True
        else:
            print(f"❌ GitHub Secret 更新失败: {proc.stderr.strip()}")
            return False
    except Exception as e:
        print(f"❌ 更新 GitHub Secret 发生异常: {e}")
        return False


def update_cronjob_org(new_token: str) -> tuple[bool, str]:
    """写回 cron-job.org 并获取下一次执行时间"""
    if not CRONJOB_API_KEY or not CRONJOB_ID:
        print("⚠️ 未配置 CRONJOB_API_KEY 或 CRONJOB_ID，跳过更新 cron-job.org")
        return False, ""
        
    print("🔄 尝试将新 Token 写回 cron-job.org...")
    api_url = f"https://api.cron-job.org/jobs/{CRONJOB_ID}"
    headers = {
        "Authorization": f"Bearer {CRONJOB_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"❌ 获取 cron-job 失败: HTTP {resp.status_code} - {resp.text}")
            return False, ""
            
        job_wrapper = resp.json()
        job_data = job_wrapper.get("jobDetails", {})
        
        # 提取下一次执行时间戳并转换为本地时间格式
        next_exec_ts = job_data.get("nextExecution")
        next_run_str = "未知"
        if next_exec_ts:
            try:
                dt = datetime.fromtimestamp(next_exec_ts)
                next_run_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        
        updated = False
        if "extendedData" in job_data and "headers" in job_data["extendedData"]:
            header_keys = [k for k in job_data["extendedData"]["headers"].keys()]
            for k in header_keys:
                if k.upper() == "SESSION_TOKEN":
                    job_data["extendedData"]["headers"][k] = new_token
                    updated = True
            if not updated:
                job_data["extendedData"]["headers"]["SESSION_TOKEN"] = new_token
                updated = True
                
        if not updated:
            url = job_data.get("url", "")
            if "SESSION_TOKEN=" in url.upper():
                 job_data["url"] = re.sub(r'(?i)session_token=[^&]+', f'SESSION_TOKEN={new_token}', url)
                 updated = True
        
        payload = {"job": job_data}
        patch_resp = requests.patch(api_url, headers=headers, json=payload, timeout=10)
        
        if patch_resp.status_code == 200:
            masked = new_token[:4] + "..." + new_token[-4:] if len(new_token) > 8 else "***"
            print(f"✅ 成功将新的 SESSION_TOKEN ({masked}) 写回 cron-job.org (ID: {CRONJOB_ID})")
            return True, next_run_str
        else:
            print(f"❌ 更新 cron-job 失败: HTTP {patch_resp.status_code} - {patch_resp.text}")
            return False, ""
            
    except Exception as e:
        print(f"❌ 更新 cron-job.org 发生异常: {e}")
        return False, ""


def send_telegram_message(message: str):
    """发送 Telegram 消息通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        if resp.status_code == 200:
            print("✅ Telegram 通知已发送")
        else:
            print(f"❌ Telegram 发送失败: HTTP {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"❌ Telegram 发送异常: {e}")


def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    """格式化通知模板"""
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' if EMAIL else "User"
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 运行时间: {now}")
    return "\n".join(lines)


def wait_for_turnstile_pass(sb, timeout=30):
    """等待 Cloudflare Turnstile 验证通过"""
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        try:
            page_lower = sb.get_page_source().lower()
            if not any(x in page_lower for x in cf_indicators):
                print("✅ Turnstile 验证已通过")
                return True
        except Exception:
            pass
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False
    


def get_current_ip(proxy_server: str = "") -> str:
    """获取当前出口 IP"""
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()


def format_countdown(countdown_str: str) -> str:
    """优化倒计时格式显示"""
    try:
        parts = countdown_str.split(':')
        h = int(parts[0])
        m = int(parts[1])
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except Exception:
        return countdown_str


def extract_expiry_date(page_source: str) -> str:
    """从页面源码提取到期日期"""
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",   
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",   
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",        
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",        
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew manually to extend for 4 days", 
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            date_str = match.group(1)
            if len(date_str.split('/')[-1]) == 4:  
                parts = date_str.split('/')
                if len(parts[0]) == 2:  
                    return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return date_str
    return ""


# ==================== Discord OAuth 登录相关 ====================
DISCORD_CLIENT_ID   = "884382422530158623"
OAUTH_REDIRECT_URI  = "https://bot-hosting.net/login"
OAUTH_SCOPE         = "identify email guilds"
DISCORD_API         = "https://discord.com/api/v9/oauth2/authorize"
DISCORD_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
STATE_RE = re.compile(r"[?&]state=([^&]+)")


def capture_discord_state(sb) -> str:
    """捕获 Discord 登录跳转的 state 参数"""
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    time.sleep(2)
    url = sb.get_current_url()
    if "discord.com" not in url:
        print(f"⚠️ 未跳转到 Discord 相关页面，当前 URL：{url}")
        return ""
    m = STATE_RE.search(url)
    if not m:
        print(f"❌ 未能从 URL 中解析出 state，当前 URL：{url}")
        return ""
    state = urllib.parse.unquote(m.group(1))
    print(f"✅ 已捕获 state（当前落地页：{urllib.parse.urlparse(url).path}）")
    return state


def discord_authorize(state: str) -> str:
    """向 Discord API 发送授权请求并拿回回调链接"""
    query = urllib.parse.urlencode({
        "client_id":     DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "scope":         OAUTH_SCOPE,
        "state":         state,
    })
    authorize_url = f"{DISCORD_API}?{query}"
    referer = (
        "https://discord.com/oauth2/authorize?" +
        urllib.parse.urlencode({
            "client_id":     DISCORD_CLIENT_ID,
            "redirect_uri":  OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope":         OAUTH_SCOPE,
            "state":         state,
        })
    )
    headers = {
        "accept":           "*/*",
        "authorization":    DC_TOKEN,
        "content-type":     "application/json",
        "origin":           "https://discord.com",
        "referer":          referer,
        "user-agent":       DISCORD_UA,
        "x-discord-locale": "zh-CN",
    }
    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {
            "guild_id": "10000",
            "channel_id": "10000",
            "channel_type": 10000,
        },
    })
    proxies = None
    _is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    _proxy_server = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    if _is_proxy:
        proxies = {"http": _proxy_server, "https": _proxy_server}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            print(f"❌ Discord OAuth2 授权失败: HTTP {resp.status_code} - {resp.text[:300]}")
            return ""
        resp_data = resp.json()
    except Exception as e:
        print(f"❌ Discord OAuth2 授权异常: {e}")
        return ""

    location = resp_data.get("location", "")
    if not location:
        print(f"❌ 授权响应中未找到 location 字段: {resp_data}")
        return ""

    masked = re.sub(r"code=[^&]+", "code=***", location)
    print(f"✅ 拿到回调 URL: {masked}")
    return location


def do_discord_login(sb) -> bool:
    """执行完整的 Discord Token 登录流程（含全套截图兜底）"""
    print("\n🔑 通过 Discord Token 登录...")
    state = capture_discord_state(sb)
    if not state:
        sb.save_screenshot("login_no_state.png")
        return False

    location = discord_authorize(state)
    if not location:
        sb.save_screenshot("login_no_location.png")
        return False

    print("↩️ 携带授权码打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    time.sleep(3)
    url = sb.get_current_url()

    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        sb.save_screenshot("login_banned.png")
        return False

    if "bot-hosting.net" not in url:
        print(f"❌ 回调后未跳转至 bot-hosting.net，当前 URL：{url}")
        sb.save_screenshot("login_no_redirect.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是 IP 被拦截")
        sb.save_screenshot("login_fraud.png")
        return False

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    sb.save_screenshot("login_timeout.png")
    return False


# ==================== 主流程控制 ====================
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    global _LOGIN_METHOD

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        if SESSION_TOKEN:
            print("🚀 启动浏览器...")
            sb.open("https://bot-hosting.net/")
            sb.wait_for_ready_state_complete()
            sb.sleep(2)

            print("📝 注入 Cookie...")
            for name, value in COOKIES.items():
                if value:
                    sb.add_cookie({"name": name, "value": value, "domain": "bot-hosting.net"})

            print("🌐 访问 https://bot-hosting.net/a/billings ...")
            sb.open("https://bot-hosting.net/a/billings")
            sb.wait_for_ready_state_complete()
            sb.sleep(3)
            current_url = sb.get_current_url()
            current_title = sb.get_title()
            print(f"📝 当前URL: {current_url}, Title: {current_title}")

            if "/a/billings" in current_url and "/login" not in current_url and "error=" not in current_url:
                login_ok = True
                print("✅ SESSION_TOKEN 登录成功, 当前已到达账单页")
            else:
                print(f"❌ SESSION_TOKEN 登录失败，当前URL: {current_url}")

        if not login_ok and DC_TOKEN:
            _LOGIN_METHOD = "Discord Token"
            print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                print("🌐 访问 https://bot-hosting.net/a/billings ...")
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if "a/billings" in current_url:
                    login_ok = True
                    print("✅ Discord OAuth 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Discord OAuth 登录后仍未到达账单页，当前URL: {current_url}")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            error_msg = "Cookie 已失效或页面异常"
            if not SESSION_TOKEN and DC_TOKEN:
                error_msg = "Discord OAuth 登录失败"
            elif SESSION_TOKEN and DC_TOKEN:
                error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

        if _LOGIN_METHOD == "Discord Token":
            print("ℹ️ 本次使用 Discord OAuth 登录，新的 SESSION_TOKEN 将自动更新并写回")

        sb.sleep(2)
        page_source = sb.get_page_source()
        current_expiry = extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        outer_renew_selector = None
        countdown_text = None
        possible_selectors = [
            'button:contains("Renew")',
            'button:contains("Renew free plan")',
            'a:contains("Renew")',
            '[class*="renew"]',
            '[class*="Renew"]',
        ]

        for selector in possible_selectors:
            try:
                if sb.is_element_visible(selector):
                    button_text = sb.get_text(selector)
                    if "Renew in" in button_text:
                        match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", button_text)
                        if match:
                            countdown_text = match.group(1)
                        break
                    elif "Renew" in button_text and "in" not in button_text.lower():
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except Exception:
                pass

        if outer_renew_selector:
            print("🔄 点击外部续期按钮，等待验证窗口...")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                sb.sleep(15)  
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            print("🔒 检测弹窗中的 Turnstile 验证...")
            turnstile_passed = False
            for attempt in range(1, 4):
                try:
                    sb.uc_gui_click_captcha()
                    time.sleep(12)
                except Exception as e:
                    print(f"⚠️ 点击 Turnstile 出错: {e}")

                if wait_for_turnstile_pass(sb, timeout=20):
                    turnstile_passed = True
                    break
                else:
                    print(f"⏳ 第 {attempt} 次未通过，重试点击...")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过"))
                return

            print("⏳ 等待续期按钮可用并点击...")
            time.sleep(5) 
            try:
                sb.click('button:contains("Renew for 4 days")', timeout=8)
                print("✅ 已点击续期按钮")
            except Exception as e:
                print(f"续期按钮点击失败: {e}")

            print("⏳ 等待新的过期时间...")
            sb.sleep(6)

            new_page_text = sb.get_page_source()
            new_expiry = extract_expiry_date(new_page_text)
            new_match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", new_page_text)
            
            if new_match:
                new_countdown = new_match.group(1)
                print(f"✅ 续期成功！新的倒计时: {new_countdown}")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )
            else:
                if new_expiry and new_expiry != current_expiry:
                    print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
                    send_telegram_message(format_notification("✅ 续期成功", extra="到期日期已更新", expiry_date=new_expiry))
                else:
                    print("⚠️ 续期结果未知，到期日期未变化，请手动检查")
                    send_telegram_message(format_notification("⚠️ 续期可能未成功", extra="请登录后台检查", expiry_date=current_expiry or "（未获取到）"))
        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                send_telegram_message(format_notification("⏳ 未到续期时间", extra=f"⏱️ 可续期时间: {friendly}后", expiry_date=current_expiry or "（未获取到）"))
            else:
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                send_telegram_message(format_notification("ℹ️ 无需续期", extra="当前状态未知，请手动检查", expiry_date=current_expiry or "（未获取到）"))

        # ========== Token 更新及 TG 通知逻辑（含写回的 cron 时间）==========
        print("🔄 检查 SESSION_TOKEN 是否需要更新")
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        old_token = SESSION_TOKEN

        if should_update_cookie(new_token, old_token, token_expiry):
            print("🔄 SESSION_TOKEN 需要更新")
            
            token_sync_status = []
            cron_next_time = ""
            
            if GH_TOKEN:
                if update_github_secret("SESSION_TOKEN", new_token):
                    print("✅ SESSION_TOKEN 更新成功 (GitHub Secrets)")
                    token_sync_status.append("✅ GitHub Secrets: 更新成功")
                else:
                    print("⚠️ 更新 GitHub Secret 失败，请检查 GH_TOKEN 权限")
                    token_sync_status.append("❌ GitHub Secrets: 更新失败")
            else:
                print("⚠️ 未设置 GH_TOKEN，跳过更新 GitHub Secrets")
            
            if CRONJOB_API_KEY and CRONJOB_ID:
                success, next_run = update_cronjob_org(new_token)
                if success:
                    token_sync_status.append("✅ Cron-job.org: 写入成功")
                    cron_next_time = next_run
                else:
                    token_sync_status.append("❌ Cron-job.org: 写入失败")
            else:
                 print("⚠️ 未设置 CRONJOB_API_KEY 或 CRONJOB_ID，跳过更新 cron-job.org")

            if token_sync_status:
                sync_msg = "\n".join(token_sync_status)
                masked_token = new_token[:4] + "..." + new_token[-4:] if len(new_token) > 8 else "***"
                
                extra_content = f"🔑 新 Token: {masked_token}\n"
                if cron_next_time:
                    extra_content += f"⏰ Cron 下次执行: {cron_next_time}\n"
                extra_content += f"\n同步状态:\n{sync_msg}"

                send_telegram_message(
                    format_notification(
                        "🔄 SESSION_TOKEN 已刷新",
                        extra=extra_content
                    )
                )
        else:
            print("✅ SESSION_TOKEN 无需更新")
        
        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
