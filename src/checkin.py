"""VPS8 (vps8.zz.cd) 签到主流程（cookie 登录态方案）。

环境变量：
    VPS8_STORAGE_STATE_B64 (必填) Base64 编码的登录态 cookies
    TELEGRAM_BOT_TOKEN     (可选)
    TELEGRAM_CHAT_ID       (可选)
    GITHUB_RUN_URL         (可选，由 workflow 注入)
    VPS8_USER_AGENT        (可选)

退出码：
    0 - 签到成功，或本日已签到，或本地状态显示今日已签到
    1 - 重试 3 次后仍失败
    2 - 配置错误（VPS8_STORAGE_STATE_B64 缺失或解码失败）
"""

from __future__ import annotations

import time
import traceback

from . import browser, notifier, state
from .env import load_local_env

BASE_URL = "https://vps8.zz.cd"
LOGIN_URL = f"{BASE_URL}/login"
DASHBOARD_URL = f"{BASE_URL}/dashboard"
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


class LoginFailed(Exception):
    """登录态无效，被踢回登录页。"""


class CheckinElementsNotFound(Exception):
    """页面上找不到关键元素（按钮/输入框等）。"""


class TurnstileTimeout(Exception):
    """Turnstile 验证超时未通过。"""


class CheckinNotConfirmed(Exception):
    """点击签到后未观察到「签到成功」状态。"""


def _visible_page_text(page) -> str:
    try:
        text = page.run_js("return document.body ? document.body.innerText : '';")
        return (text or "").replace("\u00a0", " ")
    except Exception as exc:
        print(f"[checkin] 获取页面可见文本失败: {exc}")
        return ""


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _verify_session(page) -> None:
    """进入 dashboard 检查登录态是否有效。"""
    print(f"[checkin] 校验登录态: {DASHBOARD_URL}")
    page.get(DASHBOARD_URL)
    time.sleep(2)

    if "/login" in page.url:
        browser.screenshot(page, "00-session-expired")
        raise LoginFailed(
            "登录态已失效，请重新导出 cookies 并更新 VPS8_STORAGE_STATE_B64"
        )

    print(f"[checkin] 登录态有效，当前 URL: {page.url}")
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
    """在签到页面尝试点击「立即签到」按钮。"""
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


def do_checkin(page) -> str:
    _verify_session(page)
    _go_to_checkin_page(page)

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

    print("[checkin] 签到页：先处理 Cloudflare Turnstile")
    if not browser.solve_turnstile(page, timeout=60):
        browser.screenshot(page, "03c-checkin-turnstile-fail")
        raise TurnstileTimeout("签到页 Turnstile 未通过")

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

    # 当天已签到 → 直接跳过（不需要 VPS8_STORAGE_STATE_B64）
    if state.already_checked_in_today():
        print("[main] 本地状态显示今日已签到，跳过本次运行")
        return 0

    try:
        cookies = browser.load_cookies_from_env()
    except Exception as exc:
        print(f"[fatal] 加载登录态失败: {exc}")
        return 2

    browser.clean_screenshots()
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n========== 尝试 {attempt}/{MAX_ATTEMPTS} ==========")
        page = None
        try:
            page = browser.create_page(cookies=cookies)
            status = do_checkin(page)
            # 只要服务端确认已签到/签到成功，就写状态
            state.mark_success()
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
    import sys
    sys.exit(main())
