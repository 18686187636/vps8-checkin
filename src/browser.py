"""DrissionPage 浏览器封装：GitHub OAuth 登录 + reCAPTCHA 语音识别 + 截图。

reCAPTCHA 语音识别逻辑参考 HAX VPS 脚本：
    1. 遍历所有 frame 找 anchor/bframe（URL 里含 "recaptcha" + keyword）
    2. 鼠标移动到 checkbox → 点击
    3. 切到音频模式 → 拿音频 URL → 下载
    4. Google Speech 识别 → 填入 → 验证
"""

from __future__ import annotations

import base64
import html
import json
import os
import platform
import random
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

from DrissionPage import ChromiumOptions, ChromiumPage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

GITHUB_SESSION_ENV = "VPS8_GITHUB_SESSION_B64"
PROXY_ENV = "VPS8_PROXY"

VPS8_BASE = "https://vps8.zz.cd"
VPS8_LOGIN_URL = f"{VPS8_BASE}/login"
VPS8_DASHBOARD_URL = f"{VPS8_BASE}/dashboard"
GITHUB_BASE = "https://github.com/"

_PLATFORM_UA_PARTS = {
    "Darwin": "Macintosh; Intel Mac OS X 10_15_7",
    "Linux": "X11; Linux x86_64",
    "Windows": "Windows NT 10.0; Win64; x64",
}

USER_AGENT_ENV = "VPS8_USER_AGENT"

_SAMESITE_MAP = {
    "unspecified": "Lax",
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "": "Lax",
}

GITHUB_BTN_JS = r"""
const isVisible = (el) => {
  const s = window.getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return s.display !== 'none' && s.visibility !== 'hidden'
    && r.width > 0 && r.height > 0;
};
const candidates = Array.from(document.querySelectorAll('a, button, [role="button"]'));
const target = candidates.find((el) => {
  if (!isVisible(el)) return false;
  const href = (el.getAttribute('href') || '').toLowerCase();
  const text = (el.innerText || el.textContent || '').toLowerCase();
  return href.includes('/github/login') || text.includes('github');
});
if (!target) return false;
target.scrollIntoView({block: 'center', inline: 'center'});
target.click();
return true;
"""

AUTHORIZE_BTN_JS = r"""
const btn = document.querySelector('button[name="authorize"]')
  || Array.from(document.querySelectorAll('button')).find(b =>
       (b.innerText || '').toLowerCase().includes('authorize'));
if (btn) { btn.click(); return true; }
return false;
"""


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------

def _all_cookies(page: ChromiumPage) -> list[dict]:
    try:
        result = page.cookies(all_domains=True)
        if result:
            return result
    except TypeError:
        pass
    except Exception as exc:
        print(f"[browser] cookies(all_domains=True) 失败: {exc}")
    try:
        return page.cookies() or []
    except Exception as exc:
        print(f"[browser] cookies() 失败: {exc}")
        return []


def _normalize_cookies(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not (name and value and domain):
            continue
        domain = str(domain).lstrip(".")
        item = {
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": c.get("path", "/") or "/",
        }
        if c.get("secure"):
            item["secure"] = True
        if c.get("httpOnly"):
            item["httpOnly"] = True
        ss = str(c.get("sameSite", "")).lower()
        item["sameSite"] = _SAMESITE_MAP.get(ss, "Lax")
        if c.get("session"):
            item["expires"] = -1
        elif "expirationDate" in c:
            try:
                item["expires"] = float(c["expirationDate"])
            except (TypeError, ValueError):
                item["expires"] = -1
        elif "expires" in c:
            try:
                item["expires"] = float(c["expires"])
            except (TypeError, ValueError):
                item["expires"] = -1
        else:
            item["expires"] = -1
        out.append(item)
    return out


def load_github_session_from_env() -> list[dict]:
    raw = os.environ.get(GITHUB_SESSION_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"环境变量 {GITHUB_SESSION_ENV} 未设置")
    try:
        parsed = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{GITHUB_SESSION_ENV} 解码失败: {exc}") from exc
    if isinstance(parsed, dict):
        cookies_raw = parsed.get("cookies", [])
    else:
        cookies_raw = parsed
    if not cookies_raw:
        raise RuntimeError(f"{GITHUB_SESSION_ENV} 中没有 cookie")
    cookies = _normalize_cookies(cookies_raw)
    if not cookies:
        raise RuntimeError(f"{GITHUB_SESSION_ENV} 中没有有效的 cookie")
    print(f"[browser] 已加载 {len(cookies)} 条 GitHub cookies:")
    for c in cookies:
        print(f"[browser]   - {c['name']} (domain={c['domain']})")
    return cookies


# ---------------------------------------------------------------------------
# Chrome 路径 / UA
# ---------------------------------------------------------------------------

def _detect_chrome_path() -> Optional[str]:
    env_path = os.environ.get("CHROME_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    for p in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(p):
            return p
    return None


def _build_user_agent(chrome_path: Optional[str]) -> Optional[str]:
    if not chrome_path:
        return None
    try:
        result = subprocess.run(
            [chrome_path, "--version"], check=False,
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    version_text = (result.stdout or result.stderr or "").strip()
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", version_text)
    if not match:
        return None
    platform_part = _PLATFORM_UA_PARTS.get(platform.system())
    if not platform_part:
        return None
    return (
        f"Mozilla/5.0 ({platform_part}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{match.group(1)} Safari/537.36"
    )


def _resolve_user_agent(chrome_path: Optional[str]) -> Optional[str]:
    env_user_agent = os.environ.get(USER_AGENT_ENV, "").strip()
    if env_user_agent:
        return env_user_agent
    return _build_user_agent(chrome_path)


# ---------------------------------------------------------------------------
# 创建页面
# ---------------------------------------------------------------------------

def create_page() -> ChromiumPage:
    co = ChromiumOptions()
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-infobars")
    co.set_argument("--lang=zh-CN,zh;q=0.9,en;q=0.8")
    co.set_argument("--window-size=1280,800")
    co.set_argument("--force-device-scale-factor=1")
    co.set_pref("devtools.preferences.currentDockState", '"undocked"')
    co.set_pref("credentials_enable_service", False)
    co.set_pref("profile.password_manager_enabled", False)

    proxy = os.environ.get(PROXY_ENV, "").strip()
    if proxy:
        print(f"[browser] 使用代理: {proxy}")
        try:
            co.set_proxy(proxy)
        except Exception as exc:
            print(f"[browser] 设置代理失败: {exc}")
    else:
        print("[browser] 未配置代理，直连")

    co.auto_port()

    chrome_path = _detect_chrome_path()
    if chrome_path:
        co.set_browser_path(chrome_path)
        print(f"[browser] 使用 Chrome: {chrome_path}")

    user_agent = _resolve_user_agent(chrome_path)
    if user_agent:
        co.set_user_agent(user_agent)
        print(f"[browser] 使用 User-Agent: {user_agent}")

    return ChromiumPage(co)


# ---------------------------------------------------------------------------
# GitHub OAuth 登录
# ---------------------------------------------------------------------------

def _dump_github_cookies(page: ChromiumPage) -> None:
    actual = _all_cookies(page)
    gh = [c for c in actual if "github.com" in str(c.get("domain") or "").lower()]
    print(f"[browser] 浏览器中 GitHub cookies: {len(gh)} 条")
    for c in gh:
        print(f"[browser]   {c.get('name')} = {str(c.get('value'))[:12]}...")


def inject_github_session(page: ChromiumPage, cookies: list[dict]) -> None:
    print(f"[browser] 访问 {GITHUB_BASE} 以准备注入 GitHub cookies")
    try:
        page.get(GITHUB_BASE)
        time.sleep(2)
    except Exception as exc:
        print(f"[browser] 访问 GitHub 失败: {exc}")
    try:
        page.set.cookies(cookies, set_domain=True)
    except TypeError:
        page.set.cookies(cookies)
    print(f"[browser] 已注入 {len(cookies)} 条 GitHub cookies")
    _dump_github_cookies(page)


def is_logged_in(page: ChromiumPage) -> bool:
    try:
        page.get(VPS8_DASHBOARD_URL)
        time.sleep(2)
        url = page.url or ""
        if "/login" in url:
            return False
        try:
            text = page.run_js(
                "return document.body ? document.body.innerText : '';"
            ) or ""
        except Exception:
            text = ""
        if "退出" in text or "控制台" in text or "控制面板" in text:
            return True
        return "vps8.zz.cd" in url and "/login" not in url and "/github" not in url
    except Exception as exc:
        print(f"[browser] 检查登录态失败: {exc}")
        return False


def _try_click_github_login(page: ChromiumPage) -> bool:
    page.get(VPS8_LOGIN_URL)
    time.sleep(3)
    for i in range(3):
        try:
            if page.run_js(GITHUB_BTN_JS):
                print("[browser] 已点击 GitHub 登录按钮")
                return True
        except Exception as exc:
            print(f"[browser] 点击 GitHub 按钮异常: {exc}")
        print(f"[browser] 第 {i+1} 次未找到 GitHub 按钮")
        time.sleep(2)

    try:
        ele = page.ele("xpath://a[contains(@href, '/github/login')]", timeout=5)
        if ele:
            ele.click()
            print("[browser] ele.click() 兜底点击 GitHub 按钮成功")
            return True
    except Exception as exc:
        print(f"[browser] ele 兜底异常: {exc}")
    return False


def _do_one_oauth_round(page: ChromiumPage, timeout: int) -> bool:
    if not _try_click_github_login(page):
        return False

    print("[browser] 已点击 GitHub 登录，等待跳转...")

    deadline = time.time() + timeout
    last_url = ""
    authorize_clicked_for_url = ""
    authorize_clicked_at = 0.0

    while time.time() < deadline:
        try:
            url = page.url
        except Exception:
            url = ""
        if url and url != last_url:
            print(f"[browser] URL: {url}")
            last_url = url

        if "access_denied" in url:
            print(f"[browser] ⚠️ access_denied: {url[:120]}")
            return False

        if "vps8.zz.cd" in url and "/login" not in url and "/github" not in url:
            print(f"[browser] OAuth 完成，当前 URL: {url}")
            time.sleep(2.5)
            return True

        if "github.com" in url and "/oauth/authorize" in url and "client_id=" in url:
            if authorize_clicked_for_url != url:
                time.sleep(3)
                clicked = False
                try:
                    if page.run_js(AUTHORIZE_BTN_JS):
                        clicked = True
                except Exception as exc:
                    print(f"[browser] 点击 Authorize 异常: {exc}")
                if not clicked:
                    try:
                        ele = page.ele("xpath://button[@name='authorize']", timeout=3)
                        if ele:
                            ele.click()
                            clicked = True
                    except Exception as exc:
                        print(f"[browser] ele 点击 Authorize 异常: {exc}")
                if clicked:
                    print("[browser] 已点击 GitHub Authorize")
                    authorize_clicked_for_url = url
                    authorize_clicked_at = time.time()
                    time.sleep(4)
                else:
                    print("[browser] 本轮未点到 Authorize，3 秒后重试")
                    time.sleep(3)
            else:
                elapsed = time.time() - authorize_clicked_at
                if elapsed > 25:
                    print(f"[browser] 授权页卡住 {elapsed:.0f}s，本轮结束")
                    return False
                time.sleep(1)
            continue

        time.sleep(1)

    return False


def login_via_github(page: ChromiumPage, timeout: int = 120) -> None:
    print("[browser] 先检查 vps8 登录态...")
    if is_logged_in(page):
        print("[browser] ✅ 已经登录 vps8，无需 OAuth")
        return

    print("[browser] 未登录，走 GitHub OAuth 流程")

    max_oauth_rounds = 3
    for round_no in range(1, max_oauth_rounds + 1):
        print(f"\n[browser] === OAuth 第 {round_no}/{max_oauth_rounds} 轮 ===")
        ok = _do_one_oauth_round(page, timeout=timeout)
        if ok:
            return
        if round_no < max_oauth_rounds:
            delay = 30 + round_no * 15
            print(f"[browser] OAuth 未完成，等 {delay} 秒后重试...")
            time.sleep(delay)

    screenshot(page, "10-oauth-timeout")
    raise RuntimeError(f"GitHub OAuth {max_oauth_rounds} 轮均失败，当前 URL: {page.url}")


# ---------------------------------------------------------------------------
# 验证码检测（从 .g-recaptcha[data-sitekey] 读 sitekey）
# ---------------------------------------------------------------------------

CAPTCHA_DETECT_JS = r"""
(() => {
  const result = {type: 'none', sitekey: '', source: ''};

  const recaptchaDiv = document.querySelector('.g-recaptcha[data-sitekey]');
  if (recaptchaDiv) {
    result.type = 'recaptcha';
    result.sitekey = recaptchaDiv.getAttribute('data-sitekey') || '';
    result.source = 'g-recaptcha-div';
    return JSON.stringify(result);
  }
  const hcaptchaDiv = document.querySelector('.h-captcha[data-sitekey]');
  if (hcaptchaDiv) {
    result.type = 'hcaptcha';
    result.sitekey = hcaptchaDiv.getAttribute('data-sitekey') || '';
    result.source = 'h-captcha-div';
    return JSON.stringify(result);
  }
  const turnstileDiv = document.querySelector('.cf-turnstile[data-sitekey]');
  if (turnstileDiv) {
    result.type = 'turnstile';
    result.sitekey = turnstileDiv.getAttribute('data-sitekey') || '';
    result.source = 'cf-turnstile-div';
    return JSON.stringify(result);
  }
  const anySitekey = document.querySelector('[data-sitekey]');
  if (anySitekey) {
    const sk = anySitekey.getAttribute('data-sitekey') || '';
    const cls = (anySitekey.className || '').toLowerCase();
    if (cls.includes('h-captcha') || sk.startsWith('1')) result.type = 'hcaptcha';
    else if (cls.includes('cf-turnstile') || sk.startsWith('0x')) result.type = 'turnstile';
    else result.type = 'recaptcha';
    result.sitekey = sk;
    result.source = 'any-data-sitekey';
    return JSON.stringify(result);
  }
  return JSON.stringify(result);
})()
"""


def detect_captcha(page: ChromiumPage) -> dict:
    try:
        raw = page.run_js(CAPTCHA_DETECT_JS)
        if raw:
            return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        print(f"[captcha] 检测失败: {exc}")
    return {"type": "none", "sitekey": "", "source": "error"}


def wait_captcha_widget(page: ChromiumPage, timeout: int = 60) -> dict:
    deadline = time.time() + timeout
    last_report = 0
    while time.time() < deadline:
        info = detect_captcha(page)
        if info.get("type") != "none" and info.get("sitekey"):
            print(f"[captcha] ✅ 检测到 {info['type']}（sitekey={info.get('sitekey', '')[:16]}...）")
            return info
        now = time.time()
        if now - last_report >= 10:
            print(f"[captcha] 等待 sitekey 中... 剩余 {int(deadline - now)}s")
            last_report = now
        time.sleep(2)
    return {"type": "none", "sitekey": "", "source": "timeout"}


# ---------------------------------------------------------------------------
# reCAPTCHA 音频识别（移植 HAX 脚本逻辑）
# ---------------------------------------------------------------------------

def find_frame(page: ChromiumPage, keyword: str):
    """遍历所有 frame，找 URL 里同时含 'recaptcha' 和 keyword 的。"""
    try:
        frames = page.get_frames()
    except Exception as exc:
        print(f"  [frame] get_frames 失败: {exc}")
        return None

    for frame in frames:
        try:
            u = (getattr(frame, "url", "") or "").lower()
        except Exception:
            continue
        if "recaptcha" in u and keyword in u:
            return frame

    # 更宽松：只匹配 keyword
    for frame in frames:
        try:
            u = (getattr(frame, "url", "") or "").lower()
        except Exception:
            continue
        if keyword in u and ("google.com" in u or "recaptcha.net" in u):
            return frame

    return None


def is_recaptcha_solved(page: ChromiumPage) -> bool:
    # 1. 遍历所有 frame 找 token
    try:
        for frame in page.get_frames():
            try:
                token = frame.run_js(
                    "(() => { try { const els = document.querySelectorAll('textarea[name^=g-recaptcha-response]');"
                    " for (const el of els) { if (el && el.value && el.value.length > 30) return el.value; }"
                    " return ''; } catch(e) { return ''; } })()")
                if token and len(token) > 30:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # 2. 主文档 textarea
    try:
        token = page.run_js(
            "(() => { const el = document.querySelector('textarea[name=\"g-recaptcha-response\"]');"
            " return el && el.value ? el.value : ''; })()")
        if token and len(token) > 30:
            return True
    except Exception:
        pass

    # 3. anchor 的 aria-checked
    anchor = find_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js(
                "(() => { try { const el = document.querySelector('#recaptcha-anchor');"
                " return el ? (el.getAttribute('aria-checked') === 'true') : false; } catch(e) { return false; } })()")
            if checked:
                return True
        except Exception:
            pass

    return False


def click_recaptcha_checkbox(page: ChromiumPage) -> bool:
    anchor = find_frame(page, "anchor")
    if not anchor:
        for _ in range(60):
            anchor = find_frame(page, "anchor")
            if anchor:
                break
            time.sleep(1)
        if not anchor:
            raise RuntimeError("reCAPTCHA anchor iframe 未找到")

    checkbox = anchor.ele("#recaptcha-anchor", timeout=3)
    if not checkbox:
        raise RuntimeError("reCAPTCHA checkbox 未找到")

    try:
        page.actions.move_to(checkbox, duration=random.uniform(0.4, 1.0))
        time.sleep(random.uniform(0.2, 0.5))
    except Exception as exc:
        print(f"  [reCAPTCHA] 鼠标移动失败: {exc}")

    try:
        checkbox.click()
    except Exception:
        try:
            checkbox.click(by_js=True)
        except Exception as exc:
            print(f"  [reCAPTCHA] checkbox 点击失败: {exc}")
            return False
    time.sleep(3)
    return True


def switch_to_audio(page: ChromiumPage) -> bool:
    bframe = find_frame(page, "bframe")
    if not bframe:
        return False

    try:
        input_box = bframe.ele("#audio-response", timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass

    for _ in range(3):
        try:
            audio_btn = bframe.ele("#recaptcha-audio-button", timeout=3)
            if audio_btn:
                try:
                    audio_btn.click()
                except Exception:
                    audio_btn.click(by_js=True)
                time.sleep(3)
                input_box = bframe.ele("#audio-response", timeout=1)
                if input_box and input_box.states.is_displayed:
                    return True
        except Exception:
            pass

    try:
        bframe.run_js(
            "(() => { const btn = document.querySelector('#recaptcha-audio-button'); if (btn) btn.click(); })()")
        time.sleep(3)
        input_box = bframe.ele("#audio-response", timeout=1)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    return False


def get_audio_url(page: ChromiumPage) -> Optional[str]:
    bframe = find_frame(page, "bframe")
    if not bframe:
        return None
    for _ in range(10):
        try:
            link = bframe.ele(".rc-audiochallenge-tdownload-link", timeout=1)
            if link:
                href = link.attr("href")
                if href and len(href) > 10:
                    return html.unescape(href)
            link = bframe.ele(".rc-audiochallenge-ndownload-link", timeout=1)
            if link:
                href = link.attr("href")
                if href and len(href) > 10:
                    return html.unescape(href)
            audio = bframe.ele("#audio-source", timeout=1)
            if audio:
                src = audio.attr("src")
                if src and len(src) > 10:
                    return html.unescape(src)
        except Exception:
            pass
        time.sleep(1)
    return None


def download_audio(url: str) -> Optional[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0",
        "Referer": "https://www.google.com/",
    }
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))

    for audio_url in urls:
        try:
            r = requests.get(audio_url, headers=headers, timeout=30)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except Exception as exc:
            print(f"  [STT] 下载失败: {exc}")
            continue
    return None


def recognize_audio(mp3_path: str) -> Optional[str]:
    try:
        import speech_recognition as sr
        from pydub import AudioSegment
    except ImportError as exc:
        print(f"  [STT] 缺少依赖: {exc}")
        return None

    try:
        wav_path = mp3_path.replace(".mp3", ".wav")
        AudioSegment.from_mp3(mp3_path).export(wav_path, format="wav")
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            text = recognizer.recognize_google(audio_data, language="en-US")
        try:
            os.remove(wav_path)
        except Exception:
            pass
        if text:
            print(f"  [STT] Google 识别: {text}")
            return text
    except Exception as e:
        print(f"  [STT] Google 失败: {e}")
    return None


def fill_and_verify(page: ChromiumPage, text: str) -> bool:
    bframe = find_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele("#audio-response", timeout=2)
        if not input_box:
            return False
        input_box.click()
        input_box.clear()
        input_box.input(text)
    except Exception as exc:
        print(f"  [reCAPTCHA] 填入答案失败: {exc}")
        return False

    time.sleep(random.uniform(0.5, 1.5))
    try:
        verify_btn = bframe.ele("#recaptcha-verify-button", timeout=2)
        if verify_btn:
            try:
                verify_btn.click()
            except Exception:
                verify_btn.click(by_js=True)
    except Exception:
        pass
    return True


def solve_recaptcha(page: ChromiumPage, timeout: int = 120) -> bool:
    """完整的 reCAPTCHA 音频求解，参考 HAX 脚本。"""
    print("  [reCAPTCHA] 开始求解...")
    start_time = time.time()

    # 等 anchor iframe（最多 30 秒）
    wait_anchor_deadline = time.time() + 30
    has_anchor = False
    while time.time() < wait_anchor_deadline:
        if find_frame(page, "anchor"):
            has_anchor = True
            break
        time.sleep(2)

    if not has_anchor:
        print("  [reCAPTCHA] ❌ 30 秒内 anchor iframe 未出现")
        # 列出所有 frame URL 便于排查
        try:
            for f in page.get_frames():
                u = (getattr(f, "url", "") or "")[:120]
                print(f"  [reCAPTCHA]   frame: {u}")
        except Exception:
            pass
        return False

    print("  [reCAPTCHA] anchor iframe 已找到")

    while time.time() - start_time < timeout:
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] ✅ 已通过")
            return True

        try:
            click_recaptcha_checkbox(page)
        except Exception as e:
            print(f"  [reCAPTCHA] 点击复选框失败: {e}")
            time.sleep(2)
            continue

        time.sleep(2)
        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] ✅ 点击后直接通过")
            return True

        if not switch_to_audio(page):
            time.sleep(2)
            if not switch_to_audio(page):
                print("  [reCAPTCHA] 无法切换到音频模式")
                time.sleep(random.uniform(2, 4))
                continue

        time.sleep(random.uniform(2, 4))
        audio_url = get_audio_url(page)
        if not audio_url:
            print("  [reCAPTCHA] 未找到音频 URL，重试...")
            time.sleep(random.uniform(3, 6))
            continue

        print(f"  [reCAPTCHA] 音频 URL: {audio_url[:80]}...")
        mp3_path = download_audio(audio_url)
        if not mp3_path:
            print("  [reCAPTCHA] 音频下载失败，重试...")
            time.sleep(random.uniform(3, 6))
            continue

        text = recognize_audio(mp3_path)
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        if not text:
            print("  [reCAPTCHA] 无法识别语音，重试...")
            time.sleep(random.uniform(3, 6))
            continue

        print(f"  [reCAPTCHA] 识别结果: [{text}]")
        fill_and_verify(page, text)
        time.sleep(5)

        if is_recaptcha_solved(page):
            print("  [reCAPTCHA] ✅ 语音验证通过！")
            return True
        else:
            print("  [reCAPTCHA] 验证未通过，重新获取音频...")
            time.sleep(random.uniform(2, 4))

    print(f"  [reCAPTCHA] ❌ {timeout} 秒超时")
    screenshot(page, "03c-recaptcha-timeout")
    return False


# ---------------------------------------------------------------------------
# 页面 dump
# ---------------------------------------------------------------------------

def dump_page_snapshot(page: ChromiumPage, tag: str = "") -> None:
    prefix = f"[page-dump{(' ' + tag) if tag else ''}]"
    try:
        url = page.url
    except Exception:
        url = "?"
    try:
        title = page.title
    except Exception:
        title = "?"
    print(f"{prefix} URL: {url}")
    print(f"{prefix} title: {title!r}")

    try:
        html = page.html or ""
    except Exception as exc:
        print(f"{prefix} 获取 html 失败: {exc}")
        return
    print(f"{prefix} HTML 长度: {len(html)}")

    keys = {
        "signin_form": 'id="points-signin-form"',
        "signin_btn": 'id="points-signin-submit"',
        "g_recaptcha_div": 'class="g-recaptcha"',
        "recaptcha_script": 'recaptcha/api.js',
    }
    for name, needle in keys.items():
        print(f"{prefix}   {name}: {'✓' if needle in html else '✗'}")

    try:
        body_text = page.run_js(
            "return (document.body && document.body.innerText || '').slice(0, 500);"
        ) or ""
        print(f"{prefix} body 文本前 500 字：\n{body_text}")
    except Exception as exc:
        print(f"{prefix} 读取 body 文本失败: {exc}")


# ---------------------------------------------------------------------------
# 截图 / 清理
# ---------------------------------------------------------------------------

def clean_screenshots() -> int:
    removed = 0
    for path in SCREENSHOT_DIR.glob("*.png"):
        try:
            path.unlink()
            removed += 1
        except Exception as exc:
            print(f"[browser] 清理截图失败 ({path}): {exc}")
    if removed:
        print(f"[browser] 已清理旧截图: {removed} 个")
    return removed


def screenshot(page: ChromiumPage, name: str, full_page: bool = False) -> Optional[Path]:
    target = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.get_screenshot(path=str(target), full_page=full_page)
        print(f"[browser] 截图已保存: {target}")
        return target
    except Exception as exc:
        print(f"[browser] 截图失败 ({name}): {exc}")
        return None


def safe_close(page: Optional[ChromiumPage]) -> None:
    if page is None:
        return
    try:
        page.quit()
    except Exception as exc:
        print(f"[browser] 关闭浏览器异常: {exc}")
