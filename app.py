#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, requests, subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone
from seleniumbase import SB

LOCAL_TZ_OFFSET_HOURS = 8
LOCAL_TZ = timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))

def get_local_now() -> datetime:
    return datetime.now(LOCAL_TZ)

def format_local_time(dt: datetime = None) -> str:
    if dt is None:
        dt = get_local_now()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    else:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# 环境变量配置
EMAIL           = os.environ.get("EMAIL") or ""           
SESSION_TOKEN   = os.environ.get("SESSION_TOKEN") or ""   
DISCORD_TOKEN   = os.environ.get("DISCORD_TOKEN") or ""   
GH_TOKEN        = os.environ.get("GH_TOKEN") or ""        
TG_CHAT_ID      = os.environ.get("TG_CHAT_ID") or ""      
TG_BOT_TOKEN    = os.environ.get("TG_BOT_TOKEN") or ""    
CRONJOB_API_KEY = os.environ.get("CRONJOB_API_KEY") or "" 
CRONJOB_ID      = os.environ.get("CRONJOB_ID") or ""     

IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"

# 解析 DISCORD_TOKEN
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN,脚本终止。")
    sys.exit(1)

# 构造cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

_LOGIN_METHOD = "SESSION_TOKEN"

def get_cookie_info(sb, name):
    cookies = sb.get_cookies()
    for c in cookies:
        if c.get('name') == name:
            value = c.get('value')
            expiry_ts = c.get('expiry')
            expiry_dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc) if expiry_ts else None
            return value, expiry_dt
    return None, None

def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3):
    if new_value is None:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
    return False

def update_github_secret(secret_name, new_value):
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    masked = new_value[:4] + "..." + new_value[-4:] if len(new_value) > 8 else "***"
    print(f"🔄 更新 Secret: {secret_name} (新值: {masked})")
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
            return True
        else:
            print(f"❌ 更新失败: {proc.stderr.strip()}")
            return False
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        print(f"❌ 异常: {type(e).__name__}: {e}")
        return False

# ==========================================
# 更新 CRON-JOB 调度时间 (返回: 是否成功, 调试信息, 下次运行的本地时间字符串)
# ==========================================
def update_cronjob_schedule(countdown_str: str) -> tuple[bool, str, str]:
    if not CRONJOB_API_KEY or not CRONJOB_ID:
        return False, "未配置 API KEY", ""
        
    print(f"🔄 准备将下一次执行时间写回 cron-job.org (依据倒计时 {countdown_str})...")
    try:
        if not countdown_str or ":" not in countdown_str:
            delta = timedelta(hours=1)
        else:
            parts = countdown_str.split(':')
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2]) if len(parts) > 2 else 0
            # 加 5 分钟作为缓冲时间，确保任务执行时按钮已可点击
            delta = timedelta(hours=hours, minutes=minutes + 5, seconds=seconds)
            
        # 计算下一次运行的 UTC 时间，用于提交给 cron-job.org
        next_run_utc = datetime.now(timezone.utc) + delta
        
        # 将算出的时间转换为本地时区字符串，用于 TG 通知展示
        display_time = format_local_time(next_run_utc)

        api_url = f"https://api.cron-job.org/jobs/{CRONJOB_ID}"
        headers = {
            "Authorization": f"Bearer {CRONJOB_API_KEY}",
            "Content-Type": "application/json"
        }
        
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return False, "获取 job 失败", ""
            
        job_data = resp.json().get("jobDetails", {})
        job_data["schedule"] = {
            "timezone": "UTC",
            "expiresAt": 0,
            "hours": [next_run_utc.hour],
            "mdays": [next_run_utc.day],
            "minutes": [next_run_utc.minute],
            "months": [next_run_utc.month],
            "wdays": [-1]
        }
        
        patch_resp = requests.patch(api_url, headers=headers, json={"job": job_data}, timeout=10)
        if patch_resp.status_code == 200:
            print(f"✅ 已成功修改下一次自动唤醒时间为: {display_time} (UTC+8)")
            return True, "成功", display_time
        else:
            print(f"❌ 更新调度失败: HTTP {patch_resp.status_code}")
            return False, "API 更新失败", ""
            
    except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as e:
        print(f"❌ 更新调度异常: {type(e).__name__}: {e}")
        return False, "发生异常", ""

def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except requests.exceptions.RequestException as e:
        print(f"❌ Telegram 发送失败: {type(e).__name__}: {e}")

# ==========================================
# 统一通知格式（直接在此处追加下次运行时间）
# ==========================================
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "", next_run: str = "") -> str:
    now = format_local_time()
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
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
    if next_run:
        lines.append(f"⏰ 下次唤醒: {next_run}")
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 运行时间: {now}")
    return "\n".join(lines)

def wait_for_turnstile_pass(sb, timeout=30):
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in cf_indicators):
            print("✅ Turnstile 验证已通过")
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False
    
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    try:
        response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        response.raise_for_status()
        return response.text.strip()
    except requests.exceptions.RequestException:
        return "Unknown"

def format_countdown(countdown_str: str) -> str:
    try:
        if not isinstance(countdown_str, str):
            return str(countdown_str)
        parts = countdown_str.split(':')
        if len(parts) < 2:
            return countdown_str
        h, m = int(parts[0]), int(parts[1])
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except (ValueError, IndexError, AttributeError, TypeError):
        return str(countdown_str) if countdown_str else ""

def extract_expiry_date(page_source: str) -> str:
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
    return None

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
    print("🔎 获取 Discord OAuth state...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=4)
    time.sleep(2)

    url = sb.get_current_url()
    if "discord.com" not in url:
        return ""
    m = STATE_RE.search(url)
    if not m:
        return ""
    return urllib.parse.unquote(m.group(1))

def discord_authorize(state: str) -> str:
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
    if IS_PROXY:
        proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER}

    try:
        resp = requests.post(authorize_url, headers=headers, data=body, proxies=proxies, timeout=20)
        if resp.status_code != 200:
            return ""
        resp_data = resp.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError):
        return ""

    location = resp_data.get("location", "")
    return location

def do_discord_login(sb) -> bool:
    print("\n🔑 通过 Discord Token 登录...")
    state = capture_discord_state(sb)
    if not state:
        return False
    location = discord_authorize(state)
    if not location:
        return False

    print("↩️ 携带授权码打开回调链接...")
    sb.uc_open_with_reconnect(location, reconnect_time=4)
    time.sleep(3)

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    return False

def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true"

    sb_kwargs = {"uc": True, "headless": HEADLESS}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    login_method = _LOGIN_METHOD

    with SB(**sb_kwargs) as sb:
        ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
        print(f"📍 当前出口IP: {ip}")

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
                print(f"❌ SESSION_TOKEN 登录失败")

        if not login_ok and DC_TOKEN:
            login_method = "Discord Token"
            print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord OAuth 登录...")
            if do_discord_login(sb):
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                if "a/billings" in sb.get_current_url():
                    login_ok = True
                    print("✅ Discord OAuth 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Discord OAuth 登录后仍未到达账单页")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            error_msg = "Cookie 已失效或页面异常"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

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
                    match = re.search(r"Renew in (\d+:\d{2}:\d{2})", button_text)
                    if match:
                        countdown_text = match.group(1)
                        break
                    elif "Renew" in button_text and "in" not in button_text.lower():
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except (AttributeError, RuntimeError, TypeError):
                pass

        # 核心通知变量，推迟到获取完 Cron-job 后统一发送
        notify_status = ""
        notify_extra = ""
        notify_error = ""
        notify_expiry = current_expiry
        final_countdown_text = ""

        if outer_renew_selector:
            print("🔄 点击外部续期按钮，等待验证窗口...")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                sb.sleep(15)  
            except Exception as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                print(f"❌ 点击外部按钮失败: {type(e).__name__}: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            print("🔒 检测弹窗中的 Turnstile 验证...")
            turnstile_passed = False
            for attempt in range(1, 4):
                try:
                    sb.uc_gui_click_captcha()
                    time.sleep(12)
                except Exception as e:
                    if isinstance(e, (KeyboardInterrupt, SystemExit)):
                        raise
                    pass

                if wait_for_turnstile_pass(sb, timeout=20):
                    turnstile_passed = True
                    break

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
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                print(f"⚠️ 点击续期按钮时出错: {type(e).__name__}")

            print("⏳ 等待新的过期时间...")
            sb.sleep(6)

            new_page_text = sb.get_page_source()
            new_expiry = extract_expiry_date(new_page_text)
            new_match = re.search(r"Renew in (\d+:\d{2}:\d{2})", new_page_text)
            
            if new_match:
                new_countdown = new_match.group(1)
                final_countdown_text = new_countdown
                notify_status = "✅ 续期成功"
                notify_extra = f"⏱️ 可续期时间: {format_countdown(new_countdown)}后"
                notify_expiry = new_expiry or current_expiry
                print(f"✅ 续期成功！新的倒计时: {new_countdown}")
            else:
                if new_expiry and new_expiry != current_expiry:
                    notify_status = "✅ 续期成功"
                    notify_extra = "到期日期已更新"
                    notify_expiry = new_expiry
                    print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
                else:
                    notify_status = "⚠️ 续期可能未成功"
                    notify_extra = "请登录后台检查"
                    final_countdown_text = "01:00:00"  # 1小时后重试
                    print("⚠️ 续期结果未知，到期日期未变化")

        else:
            if countdown_text:
                final_countdown_text = countdown_text
                friendly = format_countdown(countdown_text)
                notify_status = "⏳ 未到续期时间"
                notify_extra = f"⏱️ 可续期时间: {friendly}后"
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
            else:
                notify_status = "ℹ️ 无需续期/状态未知"
                notify_extra = "当前状态未知，请手动检查"
                final_countdown_text = "01:00:00"
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")

        # ==========================================
        # 1. 尝试写回下一次自动唤醒的时间到 cron-job.org
        # ==========================================
        next_run_display = ""
        if CRONJOB_API_KEY and CRONJOB_ID:
            print("🔄 准备向 CRON-JOB 写回下一次的计划时间...")
            success, msg, next_run_display = update_cronjob_schedule(final_countdown_text)
            if not success:
                notify_error = f"定时唤醒更新失败: {msg}"
        else:
            print("⚠️ 未配置 CRONJOB_API_KEY 或 CRONJOB_ID，不执行时间更新。")

        # ==========================================
        # 2. 发送合并后的最终 TG 通知
        # ==========================================
        send_telegram_message(
            format_notification(
                status=notify_status,
                extra=notify_extra,
                error=notify_error,
                expiry_date=notify_expiry,
                next_run=next_run_display
            )
        )

        # ==========================================
        # 3. 检查并按需更新 GitHub Secrets
        # ==========================================
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        old_token = SESSION_TOKEN
        
        if new_token and should_update_cookie(new_token, old_token, token_expiry):
            print("🔄 发现 Token 变更或即将过期，需要更新 GitHub Secrets")
            if GH_TOKEN:
                if update_github_secret("SESSION_TOKEN", new_token):
                    print("✅ SESSION_TOKEN 更新成功 (GitHub Secrets)")
            else:
                print("⚠️ 未设置 GH_TOKEN，跳过更新 GitHub Secrets")
        else:
            print("✅ SESSION_TOKEN 状态良好，无需更新 GitHub Secrets")
        
        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
