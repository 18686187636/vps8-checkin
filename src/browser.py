"""DrissionPage 浏览器封装：反检测启动 + GitHub OAuth 登录 + NoneCap 扩展 + 截图。

登录沿用旧版：同步 JS click 点 GitHub 按钮和 Authorize 按钮。
优化：
- 只有带 client_id 参数的授权页才点 Authorize
- 同一个 URL 只点一次，避免死循环
- 卡在授权页超过 N 秒自动重试整个流程
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from DrissionPage import ChromiumOptions, ChromiumPage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

GITHUB_SESSION_ENV = "VPS8_GITHUB_SESSION_B64"
PROXY_ENV = "VPS8_PROXY"
NONECAP_EXT_ENV = "NONECAP_EXT_PATH"

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

HAS_HCAPTCHA_JS = r"""
return !!document.querySelector(
  '.h-captcha, iframe[src*="hcaptcha.com"], iframe[src*="hcaptcha-assets"]'
);
"""

HCAPTCHA_RESPONSE_JS = r"""
const ta = document.querySelector(
  'textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"]'
);
if (!ta) return '';
return ta.value || '';
"""

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

    ext_path = os.environ.get(NONECAP_EXT_ENV, "").strip()
    if ext_path and os.path.isdir(ext_path):
        print(f"[browser] 加载 NoneCap 扩展: {ext_path}")
        co.set_argument("--enable-extensions")
        co.set_argument("--disable-features=DisableLoadExtensionCommandLineSwitch")
        try:
            co.add_extension(ext_path)
            print("[browser] add_extension 调用完成")
        except Exception as exc:
            print(f"[browser] 加载扩展失败: {exc}")
    else:
        print("[browser] 未配置 NoneCap 扩展路径，跳过")

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


def login_via_github(page: ChromiumPage, timeout: int = 120) -> None:
    print("[browser] 先检查 vps8 登录态...")
    if is_logged_in(page):
        print("[browser] ✅ 已经登录 vps8，无需 OAuth")
        return

    print("[browser] 未登录，走 GitHub OAuth 流程")

    if not _try_click_github_login(page):
        screenshot(page, "10-no-github-button")
        raise RuntimeError("找不到 GitHub 登录按钮")

    print("[browser] 已点击 GitHub 登录，等待跳转...")

    deadline = time.time() + timeout
    last_url = ""
    authorize_clicked_for_url = ""
    authorize_clicked_at = 0.0
    stuck_at_authorize_since = 0.0
    stuck_at_login_since = 0.0
    retry_count = 0
    MAX_RETRIES = 3

    while time.time() < deadline:
        try:
            url = page.url
        except Exception:
            url = ""
        if url and url != last_url:
            print(f"[browser] URL: {url}")
            last_url = url
            stuck_at_authorize_since = 0.0

        if "access_denied" in url:
            screenshot(page, "10-access-denied")
            raise RuntimeError(f"GitHub 拒绝授权: {url}")

        if "vps8.zz.cd" in url and "/login" not in url and "/github" not in url:
            print(f"[browser] OAuth 完成，当前 URL: {url}")
            time.sleep(2.5)
            return

        if "github.com" in url and "/oauth/authorize" in url:
            if "client_id=" in url:
                # 参数化授权页：点一次 Authorize
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
                        continue
                    else:
                        print("[browser] 本轮未点到 Authorize，3 秒后重试")
                        time.sleep(3)
                else:
                    # 同一个 URL 已点过，等 N 秒看是否跳走
                    elapsed = time.time() - authorize_clicked_at
                    if elapsed > 20:
                        retry_count += 1
                        print(f"[browser] 授权页卡住 {elapsed:.0f}s，重试整个流程（第 {retry_count} 次）")
                        if retry_count >= MAX_RETRIES:
                            screenshot(page, "10-authorize-stuck")
                            raise RuntimeError(f"授权页反复卡住，URL: {url}")
                        if not _try_click_github_login(page):
                            raise RuntimeError("重试时找不到 GitHub 按钮")
                        authorize_clicked_for_url = ""
                        stuck_at_authorize_since = 0.0
                        continue
                    time.sleep(1)
                continue
            else:
                # 无参数授权页：等待自动跳转
                if stuck_at_authorize_since == 0:
                    stuck_at_authorize_since = time.time()
                    print(f"[browser] 授权页无参数（{url}），等待自动跳转")
                elif time.time() - stuck_at_authorize_since > 30:
                    retry_count += 1
                    print(f"[browser] 无参数页等待超 30s，重试整个流程（第 {retry_count} 次）")
                    if retry_count >= MAX_RETRIES:
                        screenshot(page, "10-authorize-stuck")
                        raise RuntimeError(f"无参数授权页反复卡住，URL: {url}")
                    if not _try_click_github_login(page):
                        raise RuntimeError("重试时找不到 GitHub 按钮")
                    authorize_clicked_for_url = ""
                    stuck_at_authorize_since = 0.0
                    continue
                time.sleep(1)
                continue

        if "vps8.zz.cd" in url and "/login" in url:
            if stuck_at_login_since == 0:
                stuck_at_login_since = time.time()
            elif time.time() - stuck_at_login_since > 15:
                retry_count += 1
                print(f"[browser] 卡在 vps8 登录页超 15s，重试（第 {retry_count} 次）")
                if retry_count >= MAX_RETRIES:
                    raise RuntimeError("反复卡在 vps8 登录页")
                if not _try_click_github_login(page):
                    raise RuntimeError("重试时找不到 GitHub 按钮")
                authorize_clicked_for_url = ""
                stuck_at_login_since = 0.0
                continue
        else:
            stuck_at_login_since = 0.0

        time.sleep(1)

    screenshot(page, "10-oauth-timeout")
    raise RuntimeError(f"GitHub OAuth 超时，当前 URL: {page.url}")


# ---------------------------------------------------------------------------
# hCaptcha
# ---------------------------------------------------------------------------

def has_hcaptcha_widget(page: ChromiumPage) -> bool:
    try:
        return bool(page.run_js(HAS_HCAPTCHA_JS))
    except Exception:
        return False


def wait_hcaptcha_widget(page: ChromiumPage, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if has_hcaptcha_widget(page):
            return True
        time.sleep(1)
    return False


def _hcaptcha_response(page: ChromiumPage) -> str:
    try:
        token = page.run_js(HCAPTCHA_RESPONSE_JS)
        return token or ""
    except Exception:
        return ""


def wait_hcaptcha_solved(page: ChromiumPage, timeout: int = 180) -> bool:
    if not has_hcaptcha_widget(page):
        print("[hcaptcha] 未检测到 hCaptcha widget")
        return False

    print("[hcaptcha] 检测到 hCaptcha widget，等待 NoneCap 扩展自动解决...")

    if _hcaptcha_response(page):
        print("[hcaptcha] 已存在 response token")
        return True

    deadline = time.time() + timeout
    last_report = 0

    while time.time() < deadline:
        if _hcaptcha_response(page):
            elapsed = int(timeout - (deadline - time.time()))
            print(f"[hcaptcha] ✅ NoneCap 已解决（耗时约 {elapsed}s）")
            time.sleep(1)
            return True

        now = time.time()
        if now - last_report >= 15:
            remaining = int(deadline - now)
            print(f"[hcaptcha] 仍在等待... 剩余 {remaining}s")
            last_report = now

        time.sleep(1)

    print(f"[hcaptcha] ❌ {timeout}s 内未解决")
    screenshot(page, "03c-hcaptcha-timeout")
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
