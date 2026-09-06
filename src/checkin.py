"""VPS8 (vps8.zz.cd) 签到主流程。

环境变量：
    VPS8_EMAIL    (必填) 登录邮箱
    VPS8_PASSWORD (必填) 登录密码
    TELEGRAM_BOT_TOKEN (可选)
    TELEGRAM_CHAT_ID   (可选)
    GITHUB_RUN_URL     (可选，由 workflow 注入)

退出码：
    0 - 签到成功，或本日已签到
    1 - 重试 3 次后仍失败
    2 - 配置错误（邮箱/密码缺失）
"""

from __future__ import annotations

import os
import sys
import time
import traceback

from . import browser, notifier
from .env import load_local_env

BASE_URL = "https://vps8.zz.cd"
LOGIN_URL = f"{BASE_URL}/login"
CHECKIN_URL = f"{BASE_URL}/points/signin"

MAX_ATTEMPTS = 3
RETRY_INTERVAL_SECONDS = 30
SUCCESS_SNAPSHOT_DELAY_SECONDS = 3

CHECKED_TEXT_MARKERS = (
    "今日已签到",
    "今天已签到",
    "签到成功",
    "已签到",
    "明天再来",
    "已经签到",
)
UNCHECKED_TEXT_MARKERS = (
    "立即签到",
    "点击签到",
    "今日签到",
    "签到领取",
)
VERIFICATION_FAILED_MARKERS = (
    "验证失败",
    "Turnstile验证失败",
    "Turnstile 验证失败",
)


class LoginFailed(Exception):
    """登录后未能跳出登录页。"""


class CheckinElementsNotFound(Exception):
    """页面上找不到关键元素（按钮/输入框等）。"""


class TurnstileTimeout(Exception):
    """Turnstile 验证超时未通过。"""


class CheckinNotConfirmed(Exception):
    """点击签到后未观察到「签到成功」状态。"""


def _get_env_or_die(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"[fatal] 环境变量 {name} 未设置")
        sys.exit(2)
    return val


def _visible_page_text(page) -> str:
    try:
        text = page.run_js("return document.body ? document.body.innerText : '';")
        return (text or "").replace(" ", " ")
    except Exception as exc:
        print(f"[checkin] 获取页面可见文本失败: {exc}")
        return ""


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _fill_email_and_password(page, email: str, password: str) -> None:
    """根据 vps8 登录页结构填入邮箱和密码。"""
    email_input = (
        page.ele("@@tag()=input@@type=email", timeout=2)
        or page.ele("@@tag()=input@@name=email", timeout=1)
        or page.ele("@@tag()=input@@placeholder:邮箱", timeout=1)
        or page.ele("@@tag()=input@@id=email", timeout=1)
    )
    if not email_input:
        browser.screenshot(page, "01-login-no-email-input")
        raise CheckinElementsNotFound("找不到邮箱输入框")

    pass_input = (
        page.ele("@@tag()=input@@type=password", timeout=2)
        or page.ele("@@tag()=input@@name=password", timeout=1)
        or page.ele("@@tag()=input@@placeholder:密码", timeout=1)
        or page.ele("@@tag()=input@@id=password", timeout=1)
    )
    if not pass_input:
        browser.screenshot(page, "01-login-no-pass-input")
        raise CheckinElementsNotFound("找不到密码输入框")

    email_input.click()
    email_input.clear()
    email_input.input(email)
    time.sleep(0.3)

    pass_input.click()
    pass_input.clear()
    pass_input.input(password)
    time.sleep(0.3)


def _click_login_button(page) -> None:
    """点击登录按钮，避开第三方登录按钮（GitHub/Google/Nodeloc）。"""
    js = r"""
    const isVisible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && rect.width > 0
        && rect.height > 0;
    };
    const blacklist = ['github', 'google', 'nodeloc', 'telegram', '注册', '忘记'];
    const candidates = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
    const target = candidates.find((el) => {
      if (!isVisible(el)) return false;
      const text = (el.innerText || el.textContent || el.value || '').trim();
      if (!text) return false;
      const lower = text.toLowerCase();
      if (blacklist.some((b) => lower.includes(b))) return false;
      return text === '登录' || text === '登 录' || text.includes('登录');
    });
    if (!target) return false;
    target.scrollIntoView({block: 'center', inline: 'center'});
    target.click();
    return true;
    """
    try:
        clicked = bool(page.run_js(js))
    except Exception as exc:
        print(f"[checkin] JS 点击登录按钮失败: {exc}")
        clicked = False

    if not clicked:
        # 兜底：submit 按钮
        submit_btn = page.ele("tag:button@type=submit", timeout=2)
        if submit_btn:
            submit_btn.click()
            return
        browser.screenshot(page, "01-login-no-button")
        raise CheckinElementsNotFound("找不到登录按钮")

    print("[checkin] 已点击登录")


def _wait_until_logged_in(page, timeout: int = 30) -> bool:
    """等待登录后页面跳出 /login。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "/login" not in page.url:
            print(f"[checkin] 登录后跳转到: {page.url}")
            time.sleep(1)
            return True
        time.sleep(0.5)
    return False


def _login(page, email: str, password: str) -> None:
    print(f"[checkin] 访问登录页: {LOGIN_URL}")
    page.get(LOGIN_URL)

    # 等密码框出现，确认表单渲染完毕
    pass_input = page.ele("tag:input@type=password", timeout=20)
    if not pass_input:
        browser.screenshot(page, "01-login-no-form")
        raise CheckinElementsNotFound("登录表单 20s 内未渲染")

    browser.screenshot(page, "01-login-page")

    _fill_email_and_password(page, email, password)

    # 先处理 Cloudflare Turnstile 复选框（在点击登录之前）
    print("[checkin] 尝试处理 Cloudflare Turnstile")
    turnstile_ok = browser.solve_turnstile(page, timeout=60)
    if not turnstile_ok:
        print("[checkin] Turnstile 未确认通过，继续尝试登录")
    else:
        # 即便 solve_turnstile 已返回 True，再多留一拍，保证服务端能收到 token
        time.sleep(1)

    browser.screenshot(page, "01a-after-turnstile")

    _click_login_button(page)

    if not _wait_until_logged_in(page, timeout=30):
        browser.screenshot(page, "01b-login-stuck")
        raise LoginFailed(f"登录后仍停留在 {page.url}")

    browser.screenshot(page, "02-after-login")


def _go_to_checkin_page(page) -> None:
    """点击顶部导航栏的「签到」链接，失败时直接 GET。"""
    js = r"""
    const isVisible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && rect.width > 0
        && rect.height > 0;
    };
    const candidates = Array.from(document.querySelectorAll('a, button, [role="button"]'));
    const target = candidates.find((el) => {
      if (!isVisible(el)) return false;
      const text = (el.innerText || el.textContent || '').trim();
      return text === '签到' || text === '签 到';
    });
    if (!target) return false;
    target.scrollIntoView({block: 'center', inline: 'center'});
    target.click();
    return true;
    """
    clicked = False
    try:
        clicked = bool(page.run_js(js))
    except Exception as exc:
        print(f"[checkin] JS 点击签到入口失败: {exc}")

    if clicked:
        print("[checkin] 已点击顶部「签到」")
        time.sleep(2)
    else:
        print(f"[checkin] 未找到导航「签到」入口，直接访问 {CHECKIN_URL}")
        page.get(CHECKIN_URL)
        time.sleep(2)

    browser.screenshot(page, "03-checkin-page")


def _click_checkin_action(page) -> bool:
    """在签到页面尝试点击「立即签到」按钮。返回是否点击到。

    必须用 button / role=button，避免点到顶部导航的「签到」<a> 链接。
    """
    js = r"""
    const isVisible = (el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none'
        && style.visibility !== 'hidden'
        && rect.width > 0
        && rect.height > 0
        && !el.disabled;
    };
    const keywords = ['立即签到', '点击签到', '签到领取', '今日签到'];
    // 只选按钮类元素，明确排除 <a>（顶部导航的「签到」是 <a>）
    const candidates = Array.from(document.querySelectorAll(
      'button, [role="button"], input[type="button"], input[type="submit"]'
    ));
    const target = candidates.find((el) => {
      if (!isVisible(el)) return false;
      const text = (el.innerText || el.textContent || el.value || '').trim();
      if (!text) return false;
      if (text.includes('已签到') || text.includes('明天再来')) return false;
      return keywords.some((k) => text.includes(k));
    });
    if (!target) return false;
    target.scrollIntoView({block: 'center', inline: 'center'});
    target.click();
    return true;
    """
    try:
        return bool(page.run_js(js))
    except Exception as exc:
        print(f"[checkin] JS 点击签到按钮失败: {exc}")
        return False


def _confirm_checkin_success(page, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _contains_any(_visible_page_text(page), CHECKED_TEXT_MARKERS):
            return True
        time.sleep(1)
    return False


def do_checkin(page, email: str, password: str) -> str:
    _login(page, email, password)
    _go_to_checkin_page(page)

    # 等签到页内容渲染（积分签到页是 SPA，进入后需要时间出"今日签到状态"等文字）
    time.sleep(2)
    page_text = _visible_page_text(page)

    if "今日签到状态" in page_text and (
        "已签到" in page_text and "未签到" not in page_text
    ):
        print("[checkin] 今日签到状态显示已签到，无需操作")
        time.sleep(SUCCESS_SNAPSHOT_DELAY_SECONDS)
        browser.screenshot(page, "05-success")
        return "本日已签到"

    if _contains_any(page_text, CHECKED_TEXT_MARKERS) and not _contains_any(
        page_text, UNCHECKED_TEXT_MARKERS
    ):
        print("[checkin] 检测到「已签到」状态，无需操作")
        time.sleep(SUCCESS_SNAPSHOT_DELAY_SECONDS)
        browser.screenshot(page, "05-success")
        return "本日已签到"

    # 签到页要求：先过 Cloudflare Turnstile，再点「立即签到」
    print("[checkin] 签到页：先处理 Cloudflare Turnstile")
    if not browser.solve_turnstile(page, timeout=60):
        browser.screenshot(page, "03c-checkin-turnstile-fail")
        raise TurnstileTimeout("签到页 Turnstile 未通过")

    # 留一点缓冲，确保 token 已注入隐藏表单
    time.sleep(1.5)
    browser.screenshot(page, "03d-checkin-after-turnstile")

    if not _click_checkin_action(page):
        if _contains_any(_visible_page_text(page), CHECKED_TEXT_MARKERS):
            print("[checkin] 未找到签到按钮但已显示签到状态")
            browser.screenshot(page, "05-success")
            return "本日已签到"
        browser.screenshot(page, "03b-no-checkin-button")
        raise CheckinElementsNotFound("签到页未找到签到按钮且未识别到已签到状态")

    print("[checkin] 已点击签到按钮")
    time.sleep(2)
    browser.screenshot(page, "04-after-click-checkin")

    if not _confirm_checkin_success(page, timeout=30):
        browser.screenshot(page, "04b-checkin-not-confirmed")
        raise CheckinNotConfirmed("点击签到后未确认到签到成功状态")

    time.sleep(SUCCESS_SNAPSHOT_DELAY_SECONDS)
    browser.screenshot(page, "05-success")
    print("[checkin] 签到成功")
    return "签到成功"


def _send_result_snapshot(page, status: str, filename: str) -> None:
    result_screenshot = browser.screenshot(page, filename)
    if result_screenshot:
        notifier.send_result_photo(status, result_screenshot)


def main() -> int:
    loaded_env = load_local_env()
    if loaded_env:
        print(f"[env] 已从本地 env 文件加载: {', '.join(loaded_env)}")

    email = _get_env_or_die("VPS8_EMAIL")
    password = _get_env_or_die("VPS8_PASSWORD")
    browser.clean_screenshots()

    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n========== 尝试 {attempt}/{MAX_ATTEMPTS} ==========")
        page = None
        try:
            page = browser.create_page()
            status = do_checkin(page, email, password)
            _send_result_snapshot(page, status, "06-result")
            print("[main] 任务完成（已签到或本次签到成功）")
            return 0
        except Exception as exc:
            last_error = exc
            print(f"[main] 第 {attempt} 次失败: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            if page is not None:
                browser.screenshot(page, f"failure-attempt-{attempt}")
        finally:
            browser.safe_close(page)

        if attempt < MAX_ATTEMPTS:
            print(f"[main] {RETRY_INTERVAL_SECONDS}s 后重试...")
            time.sleep(RETRY_INTERVAL_SECONDS)

    summary = f"{type(last_error).__name__}: {last_error}" if last_error else "未知错误"
    print(f"\n[main] {MAX_ATTEMPTS} 次尝试均失败: {summary}")
    notifier.send_failure(summary, MAX_ATTEMPTS)
    failure_screenshot = browser.SCREENSHOT_DIR / f"failure-attempt-{MAX_ATTEMPTS}.png"
    if failure_screenshot.exists():
        notifier.send_result_photo(f"签到失败: {summary}", failure_screenshot)
    return 1


if __name__ == "__main__":
    sys.exit(main())
