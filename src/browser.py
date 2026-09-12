"""DrissionPage 浏览器封装：反检测启动 + 登录态注入 + Turnstile 处理 + 截图。

约定：
- 不开 --headless，Github Actions 上靠 xvfb-run 提供虚拟显示
- 截图统一保存到项目根目录的 screenshots/ 下
- Turnstile 处理实现多策略回退，提高过盾稳定性
- 登录态通过 VPS8_STORAGE_STATE_B64 注入，不再走邮箱密码登录
"""

from __future__ import annotations

import base64
import json
import os
import platform
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from DrissionPage import ChromiumOptions, ChromiumPage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

STORAGE_STATE_ENV = "VPS8_STORAGE_STATE_B64"
TARGET_ORIGIN = "https://vps8.zz.cd"

_CHROME_CANDIDATES = {
    "Darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
    "Linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ],
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
}

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

HAS_TURNSTILE_IFRAME_JS = r"""
return Array.from(document.querySelectorAll('iframe')).some((frame) => {
  const src = frame.getAttribute('src') || '';
  const title = frame.getAttribute('title') || '';
  return src.includes('challenges.cloudflare.com')
    || title.toLowerCase().includes('cloudflare')
    || title.toLowerCase().includes('challenge');
});
"""

# 找 Turnstile widget 可点击的元素：优先用 widget 内部的 iframe（位置最准），
# 退到 .cf-turnstile / [data-sitekey] 容器（可能被父布局拉伸）。
LOCATE_TURNSTILE_WIDGET_JS = r"""
const isVisible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return false;
  const style = window.getComputedStyle(el);
  return style.display !== 'none' && style.visibility !== 'hidden';
};

const isTurnstileFrame = (frame) => {
  const src = frame.getAttribute('src') || '';
  const title = frame.getAttribute('title') || '';
  return src.includes('challenges.cloudflare.com')
    || title.toLowerCase().includes('cloudflare')
    || title.toLowerCase().includes('challenge');
};

let target = null;
let source = '';

const containerIframes = document.querySelectorAll(
  '.cf-turnstile iframe, [data-sitekey] iframe'
);
for (const f of containerIframes) {
  if (isVisible(f)) { target = f; source = 'container-iframe'; break; }
}
if (!target) {
  const frames = Array.from(document.querySelectorAll('iframe'));
  const cf = frames.find((f) => isTurnstileFrame(f) && isVisible(f));
  if (cf) { target = cf; source = 'cloudflare-iframe'; }
}
if (!target) {
  const containers = document.querySelectorAll('.cf-turnstile, [data-sitekey]');
  for (const c of containers) {
    if (isVisible(c)) { target = c; source = 'container'; break; }
  }
}
if (!target) return null;

const rect = target.getBoundingClientRect();
return {
  source,
  tag: target.tagName.toLowerCase(),
  cls: target.className ? String(target.className).slice(0, 80) : '',
  sitekey: (target.closest('[data-sitekey]') || target).getAttribute('data-sitekey') || '',
  x: rect.x,
  y: rect.y,
  width: rect.width,
  height: rect.height,
};
"""


# ---------------------------------------------------------------------------
# 登录态 cookies
# ---------------------------------------------------------------------------

def _normalize_cookies(raw: list[dict]) -> list[dict]:
    """把浏览器扩展导出 / Playwright storage_state 的 cookie 数组
    归一化为 DrissionPage page.set.cookies() 接受的格式。
    """
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        if not (name and value and domain):
            continue

        # DrissionPage 不认 domain 前导的点
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


def load_cookies_from_env() -> list[dict]:
    """从 VPS8_STORAGE_STATE_B64 读取并解码 cookies。

    兼容两种输入：
    - 浏览器扩展（Cookie-Editor）导出的 JSON 数组
    - Playwright storage_state（{"cookies": [...], "origins": [...]}）
    """
    raw = os.environ.get(STORAGE_STATE_ENV, "").strip()
    if not raw:
        raise RuntimeError(f"环境变量 {STORAGE_STATE_ENV} 未设置")

    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"{STORAGE_STATE_ENV} base64 解码失败: {exc}") from exc

    try:
        parsed = json.loads(decoded)
    except Exception as exc:
        raise RuntimeError(f"{STORAGE_STATE_ENV} JSON 解析失败: {exc}") from exc

    if isinstance(parsed, dict):
        parsed = parsed.get("cookies", [])
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError(f"{STORAGE_STATE_ENV} 内容为空或格式不正确")

    cookies = _normalize_cookies(parsed)
    if not cookies:
        raise RuntimeError(f"{STORAGE_STATE_ENV} 中没有有效的 cookie")

    print(f"[browser] 已从环境变量加载 {len(cookies)} 条 cookies:")
    for c in cookies:
        exp = c.get("expires", -1)
        exp_str = "session" if exp == -1 else time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(exp)
        )
        print(f"[browser]   - {c['name']} (domain={c['domain']}, expires={exp_str})")

    return cookies


# ---------------------------------------------------------------------------
# Chrome 路径 / UA
# ---------------------------------------------------------------------------

def _detect_chrome_path() -> Optional[str]:
    env_path = os.environ.get("CHROME_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = _CHROME_CANDIDATES.get(platform.system(), [])
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _build_user_agent(chrome_path: Optional[str]) -> Optional[str]:
    if not chrome_path:
        return None

    try:
        result = subprocess.run(
            [chrome_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        print(f"[browser] 获取 Chrome 版本失败: {exc}")
        return None

    version_text = (result.stdout or result.stderr or "").strip()
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", version_text)
    if not match:
        print(f"[browser] 未能解析 Chrome 版本: {version_text}")
        return None

    platform_part = _PLATFORM_UA_PARTS.get(platform.system())
    if not platform_part:
        return None

    version = match.group(1)
    return (
        f"Mozilla/5.0 ({platform_part}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _resolve_user_agent(chrome_path: Optional[str]) -> Optional[str]:
    env_user_agent = os.environ.get(USER_AGENT_ENV, "").strip()
    if env_user_agent:
        return env_user_agent
    return _build_user_agent(chrome_path)


# ---------------------------------------------------------------------------
# 创建页面
# ---------------------------------------------------------------------------

def create_page(cookies: Optional[list[dict]] = None) -> ChromiumPage:
    co = ChromiumOptions()

    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-gpu")
    co.set_argument("--disable-infobars")
    co.set_argument("--disable-extensions")
    co.set_argument("--lang=zh-CN,zh;q=0.9,en;q=0.8")
    co.set_argument("--window-size=1280,800")
    co.set_argument("--force-device-scale-factor=1")

    co.set_pref("devtools.preferences.currentDockState", '"undocked"')
    co.set_pref("credentials_enable_service", False)
    co.set_pref("profile.password_manager_enabled", False)

    co.auto_port()

    chrome_path = _detect_chrome_path()
    if chrome_path:
        co.set_browser_path(chrome_path)
        print(f"[browser] 使用 Chrome: {chrome_path}")
    else:
        print("[browser] 未找到 Chrome 二进制，将使用 DrissionPage 默认查找逻辑")

    user_agent = _resolve_user_agent(chrome_path)
    if user_agent:
        co.set_user_agent(user_agent)
        print(f"[browser] 使用 User-Agent: {user_agent}")

    page = ChromiumPage(co)

    if cookies:
        _inject_cookies(page, cookies)

    return page


def _inject_cookies(page: ChromiumPage, cookies: list[dict]) -> None:
    """注入 cookies。

    DrissionPage 的 set.cookies 需要在目标域下调用才稳妥，
    所以先访问一次目标站点，再写入 cookie，最后重载 dashboard 校验。
    """
    print(f"[browser] 准备注入 {len(cookies)} 条 cookies，先访问 {TARGET_ORIGIN}")
    try:
        page.get(TARGET_ORIGIN)
        time.sleep(1.0)
    except Exception as exc:
        print(f"[browser] 访问目标域失败: {exc}")

    try:
        page.set.cookies(cookies)
        print("[browser] cookies 注入完成")
    except Exception as exc:
        print(f"[browser] cookies 注入失败: {exc}")
        raise

    # 重新加载一次，让服务器看到 cookie
    try:
        page.get(TARGET_ORIGIN + "/dashboard")
        time.sleep(1.5)
        print(f"[browser] cookie 生效校验，当前 URL: {page.url}")
    except Exception as exc:
        print(f"[browser] 校验 cookie 时访问 dashboard 失败: {exc}")


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------

def clean_screenshots() -> int:
    removed = 0
    for path in SCREENSHOT_DIR.glob("*.png"):
        if not path.is_file():
            continue
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


# ---------------------------------------------------------------------------
# Turnstile
# ---------------------------------------------------------------------------

def wait_turnstile_iframe(page: ChromiumPage, timeout: int = 30) -> bool:
    """等待 Cloudflare Turnstile iframe 加载出来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_turnstile_iframe(page):
            return True
        time.sleep(0.5)
    return False


def _has_turnstile_widget(page: ChromiumPage) -> bool:
    """检测页面是否有 Turnstile widget（容器、sitekey 或 iframe）。"""
    try:
        return bool(page.run_js("return !!document.querySelector('.cf-turnstile, [data-sitekey]');")) \
            or bool(page.run_js(HAS_TURNSTILE_IFRAME_JS))
    except Exception:
        return False


def wait_turnstile_widget(page: ChromiumPage, timeout: int = 30) -> bool:
    """等待 Turnstile widget 出现（容器或 iframe 任一即可）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_turnstile_widget(page):
            return True
        time.sleep(0.4)
    return False


# 向后兼容旧名字
wait_turnstile_iframe = wait_turnstile_widget


def solve_turnstile(
    page: ChromiumPage,
    timeout: int = 30,
    poll_interval: float = 1.5,
    require_iframe: bool = True,
) -> bool:
    """处理 Cloudflare Turnstile 复选框验证。

    策略：等 widget 容器出现 → 根据容器视口位置点击复选框区域 →
    检查 cf-turnstile-response token 是否生成。
    """
    if require_iframe and not wait_turnstile_widget(page, timeout=min(timeout, 20)):
        print("[turnstile] 未检测到 Turnstile widget（页面可能没有盾）")
        return False

    try:
        info = page.run_js(LOCATE_TURNSTILE_WIDGET_JS)
        if info:
            print(
                "[turnstile] 定位到 widget: "
                f"source={info.get('source')}, tag={info.get('tag')}, "
                f"cls={info.get('cls')!r}, "
                f"sitekey={(info.get('sitekey') or '')[:12]}..., "
                f"pos=({info.get('x'):.0f},{info.get('y'):.0f}), "
                f"size=({info.get('width'):.0f}x{info.get('height'):.0f})"
            )
        else:
            print("[turnstile] 未能定位 widget 元素")
    except Exception as exc:
        print(f"[turnstile] 读取 widget 信息失败: {exc}")

    if _turnstile_token(page):
        print("[turnstile] 已存在 response token，盾已通过")
        return True

    deadline = time.time() + timeout
    attempts = 0

    while time.time() < deadline:
        attempts += 1

        clicked = _click_turnstile_checkbox(page)
        if clicked:
            print(f"[turnstile] 第 {attempts} 次尝试点击完成，等盾通过")
        else:
            print(f"[turnstile] 第 {attempts} 次尝试未能点击")

        if _wait_for_pass(page, timeout=8):
            # token 出现后再等一会儿，给前端回调留时间
            time.sleep(1.5)
            print(f"[turnstile] 盾通过 (尝试 {attempts} 次)")
            return True

        time.sleep(poll_interval)

    print(f"[turnstile] {timeout}s 内未能完成验证 (共尝试 {attempts} 次)")
    return False


def _click_turnstile_checkbox(page: ChromiumPage) -> bool:
    """根据 widget 容器的视口位置，用 CDP 鼠标事件点击复选框区域。

    Turnstile widget 复选框始终在容器左侧约 28px 处、垂直居中。
    """
    try:
        info = page.run_js(LOCATE_TURNSTILE_WIDGET_JS)
    except Exception as exc:
        print(f"[turnstile] 获取 widget 坐标失败: {exc}")
        return False

    if not info:
        print("[turnstile] widget 未找到")
        return False

    width = float(info.get("width") or 0)
    height = float(info.get("height") or 0)
    if width < 30 or height < 20:
        print(f"[turnstile] widget 尺寸异常: {width}x{height}")
        return False

    # 先把容器滚动到视口内，避免 y 是负数或超过窗口
    try:
        page.run_js(
            "const t = document.querySelector('.cf-turnstile, [data-sitekey]'); "
            "if (t) t.scrollIntoView({block: 'center', inline: 'center'});"
        )
        time.sleep(0.3)
        info = page.run_js(LOCATE_TURNSTILE_WIDGET_JS) or info
    except Exception:
        pass

    base_x = float(info["x"]) + 28
    base_y = float(info["y"]) + float(info["height"]) / 2
    x = int(base_x + random.randint(-3, 3))
    y = int(base_y + random.randint(-3, 3))

    try:
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseMoved",
            x=x - 15,
            y=y - 5,
        )
        time.sleep(random.uniform(0.15, 0.3))
        page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        time.sleep(random.uniform(0.2, 0.4))
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=x,
            y=y,
            button="left",
            clickCount=1,
        )
        time.sleep(random.uniform(0.08, 0.18))
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=x,
            y=y,
            button="left",
            clickCount=1,
        )
        print(f"[turnstile] 已在 ({x},{y}) 模拟点击")
        return True
    except Exception as exc:
        print(f"[turnstile] CDP 点击失败: {exc}")
        return False


def _has_turnstile_iframe(page: ChromiumPage) -> bool:
    try:
        return bool(page.run_js(HAS_TURNSTILE_IFRAME_JS))
    except Exception:
        return False


def _try_click_via_iframe(page: ChromiumPage, by_title: bool) -> bool:
    try:
        if by_title:
            iframe = page.get_frame("@title:Cloudflare", timeout=2)
        else:
            iframe = page.get_frame(
                "@@tag()=iframe@@src:challenges.cloudflare.com",
                timeout=2,
            )
        if not iframe:
            return False

        checkbox = iframe.ele("xpath://input[@type='checkbox']", timeout=3)
        if not checkbox:
            return False
        checkbox.click()
        return True
    except Exception as exc:
        print(f"[turnstile] iframe 点击失败 ({'title' if by_title else 'src'}): {exc}")
        return False


def _try_click_iframe_element(page: ChromiumPage) -> bool:
    try:
        iframe_ele = page.ele(
            "@@tag()=iframe@@src:challenges.cloudflare.com",
            timeout=2,
        )
        if not iframe_ele:
            return False
        iframe_ele.click.at(30, iframe_ele.rect.size[1] // 2)
        return True
    except Exception as exc:
        print(f"[turnstile] iframe 偏移点击失败: {exc}")
        return False


def _try_click_visible_iframe(page: ChromiumPage) -> bool:
    try:
        iframes = page.eles("tag:iframe", timeout=2)
        for iframe_ele in iframes:
            width, height = iframe_ele.rect.size
            if width < 100 or height < 40:
                continue
            iframe_ele.click.at(30, height // 2)
            return True
        return False
    except Exception as exc:
        print(f"[turnstile] 可见 iframe 点击失败: {exc}")
        return False


def _try_click_checkbox_by_viewport(page: ChromiumPage) -> bool:
    try:
        viewport = page.run_js(
            "return {width: window.innerWidth, height: window.innerHeight};"
        )
        width = int(viewport["width"])
        height = int(viewport["height"])
        x = width // 2 - 132 + random.randint(-3, 3)
        y = height // 2 - 13 + random.randint(-3, 3)
        page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x - 20, y=y - 8)
        time.sleep(random.uniform(0.15, 0.35))
        page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        time.sleep(random.uniform(0.2, 0.45))
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=x,
            y=y,
            button="left",
            clickCount=1,
        )
        time.sleep(random.uniform(0.08, 0.18))
        page.run_cdp(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=x,
            y=y,
            button="left",
            clickCount=1,
        )
        return True
    except Exception as exc:
        print(f"[turnstile] 视口坐标点击失败: {exc}")
        return False


def _turnstile_token(page: ChromiumPage) -> str:
    """读取 Cloudflare Turnstile 验证通过后的 response token。"""
    try:
        token = page.run_js(
            r"""
            const inputs = document.querySelectorAll(
              'input[name="cf-turnstile-response"], input[name^="cf-chl-widget"]'
            );
            for (const el of inputs) {
              if (el.value) return el.value;
            }
            return '';
            """
        )
        return token or ""
    except Exception:
        return ""


def _wait_for_pass(page: ChromiumPage, timeout: int = 20) -> bool:
    """点击复选框后等盾通过。

    通过信号只看 cf-turnstile-response token —— iframe 在 widget 重渲染
    过程中可能短暂消失，不能作为通过依据。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _turnstile_token(page):
            return True
        time.sleep(0.5)
    return False


def safe_close(page: Optional[ChromiumPage]) -> None:
    if page is None:
        return
    try:
        page.quit()
    except Exception as exc:
        print(f"[browser] 关闭浏览器异常: {exc}")
