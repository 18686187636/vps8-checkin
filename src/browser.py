"""DrissionPage 浏览器封装：反检测启动 + GitHub OAuth 登录 + 通用验证码 + 截图。

验证码方案：
1. reCAPTCHA 优先音频识别（免费）
2. 失败回退 2captcha
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
    """执行一轮 GitHub OAuth。返回是否成功。"""
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
# 验证码检测（更宽松）
# ---------------------------------------------------------------------------

CAPTCHA_DETECT_JS = r"""
(() => {
  // 1. Google reCAPTCHA - 多种检测方式
  const grecaptchaEls = document.querySelectorAll('.g-recaptcha');
  if (grecaptchaEls.length > 0) {
    const el = grecaptchaEls[0];
    return JSON.stringify({
      type: 'recaptcha',
      sitekey: el.getAttribute('data-sitekey') || '',
      source: 'g-recaptcha-div',
    });
  }
  const recaptchaIframe = document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="google.com/recaptcha"], iframe[title*="recaptcha"]'
  );
  if (recaptchaIframe) {
    return JSON.stringify({
      type: 'recaptcha',
      sitekey: '',
      source: 'recaptcha-iframe',
    });
  }
  const anySitekey = document.querySelector('[data-sitekey]');
  if (anySitekey) {
    const sk = anySitekey.getAttribute('data-sitekey') || '';
    // 判断 sitekey 属于哪个服务
    const cls = (anySitekey.className || '').toLowerCase();
    if (cls.includes('h-captcha') || sk.startsWith('1')) {
      return JSON.stringify({type: 'hcaptcha', sitekey: sk, source: 'data-sitekey'});
    }
    if (cls.includes('cf-turnstile') || sk.startsWith('0x')) {
      return JSON.stringify({type: 'turnstile', sitekey: sk, source: 'data-sitekey'});
    }
    // 默认当作 reCAPTCHA
    return JSON.stringify({type: 'recaptcha', sitekey: sk, source: 'data-sitekey'});
  }
  // 2. hCaptcha
  if (document.querySelector('.h-captcha, iframe[src*="hcaptcha"]')) {
    const el = document.querySelector('.h-captcha');
    return JSON.stringify({
      type: 'hcaptcha',
      sitekey: el ? (el.getAttribute('data-sitekey') || '') : '',
      source: 'h-captcha',
    });
  }
  // 3. Turnstile
  if (document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]')) {
    const el = document.querySelector('.cf-turnstile, [data-sitekey]');
    return JSON.stringify({
      type: 'turnstile',
      sitekey: el ? (el.getAttribute('data-sitekey') || '') : '',
      source: 'cf-turnstile',
    });
  }
  return JSON.stringify({type: 'none', sitekey: '', source: 'not-found'});
})()
"""

CAPTCHA_TOKEN_JS = r"""
(() => {
  const ta1 = document.querySelector('textarea[name="g-recaptcha-response"]');
  if (ta1 && ta1.value) return ta1.value;
  const ta2 = document.querySelector('textarea[name="h-captcha-response"]');
  if (ta2 && ta2.value) return ta2.value;
  const ta3 = document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
  if (ta3 && ta3.value) return ta3.value;
  return '';
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


def get_captcha_token(page: ChromiumPage) -> str:
    try:
        token = page.run_js(CAPTCHA_TOKEN_JS)
        return token or ""
    except Exception:
        return ""


def wait_captcha_widget(page: ChromiumPage, timeout: int = 120) -> dict:
    """等待 widget 渲染，每 10 秒 dump 一次页面状态方便排查。"""
    deadline = time.time() + timeout
    last_report = 0

    while time.time() < deadline:
        info = detect_captcha(page)
        if info.get("type") != "none":
            print(f"[captcha] ✅ 检测到 {info['type']}（source={info.get('source')}, sitekey={info.get('sitekey', '')[:16]}）")
            return info

        now = time.time()
        if now - last_report >= 10:
            remaining = int(deadline - now)
            try:
                page_text_len = page.run_js("return (document.body && document.body.innerText || '').length;")
            except Exception:
                page_text_len = 0
            try:
                div_count = page.run_js("return document.querySelectorAll('div, iframe, form').length;")
            except Exception:
                div_count = 0
            try:
                has_recaptcha_script = page.run_js(
                    "return Array.from(document.querySelectorAll('script')).some(s => (s.src||'').includes('recaptcha'));"
                )
            except Exception:
                has_recaptcha_script = False
            print(
                f"[captcha] 等待 widget 渲染中... 剩余 {remaining}s "
                f"(bodyTextLen={page_text_len}, elements={div_count}, hasRecaptchaScript={has_recaptcha_script})"
            )
            last_report = now

        time.sleep(2)

    print(f"[captcha] ⚠️ {timeout}s 内未检测到 widget")
    return {"type": "none", "sitekey": "", "source": "timeout"}


# ---------------------------------------------------------------------------
# reCAPTCHA 音频识别
# ---------------------------------------------------------------------------

def _find_recaptcha_frame(page: ChromiumPage, keyword: str):
    try:
        for frame in page.get_frames():
            try:
                u = (frame.url or "").lower()
            except Exception:
                continue
            if "recaptcha" in u and keyword in u:
                return frame
    except Exception as exc:
        print(f"[recaptcha] 遍历 frame 失败: {exc}")
    return None


def _is_recaptcha_solved(page: ChromiumPage) -> bool:
    try:
        for frame in page.get_frames():
            try:
                token = frame.run_js(r"""
                (() => {
                  try {
                    const els = document.querySelectorAll(
                      'textarea[name^="g-recaptcha-response"], textarea[name="g-recaptcha-response"]');
                    for (const el of els) {
                      if (el && el.value && el.value.length > 30) return el.value;
                    }
                    return '';
                  } catch(e) { return ''; }
                })()
                """)
                if token and len(token) > 30:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    try:
        token = get_captcha_token(page)
        if token and len(token) > 30:
            return True
    except Exception:
        pass

    anchor = _find_recaptcha_frame(page, "anchor")
    if anchor:
        try:
            checked = anchor.run_js(r"""
                (() => {
                  try {
                    const el = document.querySelector('#recaptcha-anchor');
                    return el ? (el.getAttribute('aria-checked') === 'true') : false;
                  } catch(e) { return false; }
                })()
            """)
            if checked:
                return True
        except Exception:
            pass
    return False


def _click_recaptcha_checkbox(page: ChromiumPage) -> bool:
    for _ in range(60):
        anchor = _find_recaptcha_frame(page, "anchor")
        if anchor:
            break
        time.sleep(1)
    else:
        print("[recaptcha] 未找到 anchor iframe")
        return False

    try:
        checkbox = anchor.ele("#recaptcha-anchor", timeout=5)
        if not checkbox:
            print("[recaptcha] 未找到 checkbox 元素")
            return False
        try:
            checkbox.click()
        except Exception:
            try:
                checkbox.click(by_js=True)
            except Exception as exc:
                print(f"[recaptcha] checkbox.click 失败: {exc}")
                return False
        time.sleep(3)
        return True
    except Exception as exc:
        print(f"[recaptcha] 点击复选框失败: {exc}")
        return False


def _switch_to_audio(page: ChromiumPage) -> bool:
    bframe = _find_recaptcha_frame(page, "bframe")
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
                input_box = bframe.ele("#audio-response", timeout=2)
                if input_box and input_box.states.is_displayed:
                    return True
        except Exception:
            pass

    try:
        bframe.run_js(r"""
            (() => {
              const btn = document.querySelector('#recaptcha-audio-button');
              if (btn) btn.click();
            })()
        """)
        time.sleep(3)
        input_box = bframe.ele("#audio-response", timeout=2)
        if input_box and input_box.states.is_displayed:
            return True
    except Exception:
        pass
    return False


def _get_recaptcha_audio_url(page: ChromiumPage) -> str:
    bframe = _find_recaptcha_frame(page, "bframe")
    if not bframe:
        return ""
    for _ in range(10):
        for sel in (
            ".rc-audiochallenge-tdownload-link",
            ".rc-audiochallenge-ndownload-link",
            "#audio-source",
        ):
            try:
                el = bframe.ele(sel, timeout=1)
                if el:
                    attr = "src" if "audio-source" in sel else "href"
                    url = el.attr(attr)
                    if url and len(url) > 10:
                        return html.unescape(url)
            except Exception:
                pass
        time.sleep(1)
    return ""


def _download_recaptcha_audio(url: str) -> Optional[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:155.0) Gecko/20100101 Firefox/155.0",
        "Referer": "https://www.google.com/",
    }
    urls = [url]
    if "recaptcha.net" in url:
        urls.append(url.replace("recaptcha.net", "www.google.com"))
    elif "www.google.com" in url:
        urls.append(url.replace("www.google.com", "recaptcha.net"))

    for u in urls:
        try:
            r = requests.get(u, headers=headers, timeout=30)
            r.raise_for_status()
            if len(r.content) < 1000:
                continue
            path = tempfile.mktemp(suffix=".mp3")
            with open(path, "wb") as f:
                f.write(r.content)
            return path
        except Exception as exc:
            print(f"[recaptcha] 下载失败: {exc}")
            continue
    return None


def _recognize_recaptcha_audio(mp3_path: str) -> str:
    try:
        import speech_recognition as sr
        from pydub import AudioSegment
    except ImportError as exc:
        print(f"[recaptcha] 缺少依赖: {exc}")
        return ""

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
            print(f"[recaptcha] 识别结果: {text!r}")
            return text
    except Exception as exc:
        print(f"[recaptcha] 识别失败: {exc}")
    return ""


def _fill_recaptcha_audio(page: ChromiumPage, text: str) -> bool:
    bframe = _find_recaptcha_frame(page, "bframe")
    if not bframe:
        return False
    try:
        input_box = bframe.ele("#audio-response", timeout=2)
        if not input_box:
            return False
        try:
            input_box.click()
        except Exception:
            pass
        input_box.clear()
        input_box.input(text)
    except Exception as exc:
        print(f"[recaptcha] 填写答案失败: {exc}")
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


def solve_recaptcha_via_audio(page: ChromiumPage, timeout: int = 120) -> bool:
    print("[recaptcha] 开始音频挑战求解...")
    start = time.time()

    wait_deadline = time.time() + 30
    while time.time() < wait_deadline:
        if _find_recaptcha_frame(page, "anchor"):
            break
        time.sleep(2)

    while time.time() - start < timeout:
        if _is_recaptcha_solved(page):
            print("[recaptcha] ✅ 已通过")
            return True

        try:
            _click_recaptcha_checkbox(page)
        except Exception as exc:
            print(f"[recaptcha] 点击复选框失败: {exc}")
            time.sleep(2)
            continue

        time.sleep(2)
        if _is_recaptcha_solved(page):
            print("[recaptcha] ✅ 点击后直接通过")
            return True

        if not _switch_to_audio(page):
            time.sleep(2)
            if not _switch_to_audio(page):
                print("[recaptcha] 无法切换到音频模式")
                time.sleep(random.uniform(2, 4))
                continue

        time.sleep(random.uniform(2, 4))
        audio_url = _get_recaptcha_audio_url(page)
        if not audio_url:
            print("[recaptcha] 未找到音频 URL")
            time.sleep(random.uniform(3, 6))
            continue

        print(f"[recaptcha] 音频 URL: {audio_url[:80]}...")
        mp3_path = _download_recaptcha_audio(audio_url)
        if not mp3_path:
            print("[recaptcha] 音频下载失败")
            time.sleep(random.uniform(3, 6))
            continue

        text = _recognize_recaptcha_audio(mp3_path)
        try:
            os.remove(mp3_path)
        except Exception:
            pass

        if not text:
            print("[recaptcha] 识别失败，重试")
            time.sleep(random.uniform(3, 6))
            continue

        print(f"[recaptcha] 填入答案: {text}")
        _fill_recaptcha_audio(page, text)
        time.sleep(5)

        if _is_recaptcha_solved(page):
            print("[recaptcha] ✅ 音频验证通过")
            return True
        print("[recaptcha] 验证未通过，重试")

    print(f"[recaptcha] ❌ {timeout}s 超时")
    screenshot(page, "03c-recaptcha-timeout")
    return False


# ---------------------------------------------------------------------------
# 2captcha 兜底
# ---------------------------------------------------------------------------

def solve_captcha_via_2captcha(
    page: ChromiumPage,
    api_key: str,
    captcha_info: dict,
    timeout: int = 240,
) -> Optional[str]:
    ctype = captcha_info.get("type", "none")
    sitekey = captcha_info.get("sitekey", "")

    if ctype == "none" or not sitekey:
        print(f"[2captcha] 无效的验证码信息: {captcha_info}")
        return None

    existing = get_captcha_token(page)
    if existing:
        print(f"[2captcha] 页面已有 token（{len(existing)} 字符）")
        return existing

    page_url = page.url
    print(f"[2captcha] 类型={ctype}, sitekey={sitekey[:16]}..., pageurl={page_url}")

    if ctype == "recaptcha":
        data = {"key": api_key, "method": "userrecaptcha",
                "googlekey": sitekey, "pageurl": page_url, "json": 1}
    elif ctype == "hcaptcha":
        data = {"key": api_key, "method": "hcaptcha",
                "sitekey": sitekey, "pageurl": page_url, "json": 1}
    elif ctype == "turnstile":
        data = {"key": api_key, "method": "turnstile",
                "sitekey": sitekey, "pageurl": page_url, "json": 1}
    else:
        print(f"[2captcha] 不支持的类型: {ctype}")
        return None

    try:
        r = requests.post("https://2captcha.com/in.php", data=data, timeout=30).json()
    except Exception as exc:
        print(f"[2captcha] 提交异常: {exc}")
        return None

    if r.get("status") != 1:
        print(f"[2captcha] 提交失败: {r}")
        return None

    task_id = r["request"]
    print(f"[2captcha] 任务已提交，task_id={task_id}")

    deadline = time.time() + timeout
    last_report = 0
    while time.time() < deadline:
        time.sleep(5)
        try:
            r = requests.get(
                "https://2captcha.com/res.php",
                params={"key": api_key, "action": "get", "id": task_id, "json": 1},
                timeout=30,
            ).json()
        except Exception as exc:
            print(f"[2captcha] 轮询异常: {exc}")
            continue

        if r.get("status") == 1:
            token = r["request"]
            print(f"[2captcha] ✅ 已解决（token 长度 {len(token)}）")
            return token
        if r.get("request") == "CAPCHA_NOT_READY":
            now = time.time()
            if now - last_report >= 20:
                print(f"[2captcha] 等待中... 剩余 {int(deadline - now)}s")
                last_report = now
            continue
        print(f"[2captcha] 错误: {r}")
        return None

    print(f"[2captcha] ❌ {timeout}s 超时")
    return None


CAPTCHA_INJECT_JS = r"""
(() => {
  const token = __TOKEN__;
  const setValue = (el) => {
    if (!el) return false;
    el.value = token;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  };

  let count = 0;

  const ta1 = document.querySelector('textarea[name="g-recaptcha-response"]');
  if (ta1) { setValue(ta1); count++; }
  else {
    const form = document.getElementById('points-signin-form') || document.querySelector('form');
    if (form) {
      const hidden = document.createElement('textarea');
      hidden.name = 'g-recaptcha-response';
      hidden.style.display = 'none';
      hidden.value = token;
      form.appendChild(hidden);
      count++;
    }
  }

  const ta2 = document.querySelector('textarea[name="h-captcha-response"]');
  if (ta2) { setValue(ta2); count++; }

  const ta3 = document.querySelector('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]');
  if (ta3) { setValue(ta3); count++; }

  return count;
})()
"""


def inject_captcha_token(page: ChromiumPage, token: str) -> bool:
    js = CAPTCHA_INJECT_JS.replace("__TOKEN__", json.dumps(token))
    try:
        count = page.run_js(js)
        print(f"[captcha] token 已注入到 {count} 个字段")
        return bool(count)
    except Exception as exc:
        print(f"[captcha] 注入失败: {exc}")
        return False


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
