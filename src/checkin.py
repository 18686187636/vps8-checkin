"""VPS8 (vps8.zz.cd) 签到主流程。"""

from __future__ import annotations

import os
import time
import traceback

from . import browser, notifier, state
from .env import load_local_env

BASE_URL = "https://vps8.zz.cd"
DASHBOARD_URL = f"{BASE_URL}/dashboard"
CHECKIN_URL = f"{BASE_URL}/points/signin"

MAX_ATTEMPTS = 3
RETRY_INTERVAL_SECONDS = 90
SUCCESS_SNAPSHOT_DELAY_SECONDS = 3

CHECKED_TEXT_MARKERS = (
    "今日已签到", "今天已签到", "签到成功",
    "已签到", "明天再来", "已经签到",
)
UNCHECKED_TEXT_MARKERS = (
    "立即签到", "点击签到", "今日签到", "签到领取",
)


class LoginFailed(Exception):
    pass


class CheckinElementsNotFound(Exception):
    pass


class CaptchaTimeout(Exception):
    pass


class CheckinNotConfirmed(Exception):
    pass


class NotOnCheckinPage(Exception):
    pass


def _visible_page_text(page) -> str:
    try:
        text = page.run_js("return document.body ? document.body.innerText : '';")
        return (text or "").replace("\u00a0", " ")
    except Exception as exc:
        print(f"[checkin] 获取页面文本失败: {exc}")
        return ""


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def _is_already_checked_in(page) -> bool:
    page_text = _visible_page_text(page)
    if "今日签到状态" in page_text and (
        "已签到" in page_text and "未签到" not in page_text
    ):
        return True
    if _contains_any(page_text, CHECKED_TEXT_MARKERS) and not _contains_any(
        page_text, UNCHECKED_TEXT_MARKERS
    ):
        return True
    return False


def _verify_on_checkin_page(page, timeout: int = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ok = page.run_js(r"""
                return !!(
                    document.querySelector('#points-signin-form')
                    || document.querySelector('#points-signin-submit')
                    || document.querySelector('.g-recaptcha')
                    || document.querySelector('.h-captcha')
                    || document.querySelector('.cf-turnstile')
                );
            """)
        except Exception:
            ok = False
        if ok:
            print("[checkin] ✅ 已进入真正的签到页")
            return
        text = _visible_page_text(page)
        if "今日签到状态" in text or "积分签到" in text:
            print("[checkin] ✅ 已进入签到页（通过文本判断）")
            return
        time.sleep(1)

    browser.dump_page_snapshot(page, tag="verify-fail")
    browser.screenshot(page, "03-not-on-checkin-page")
    text = _visible_page_text(page)
    if "登录" in text and "密码" in text:
        raise LoginFailed(f"进入签到页后显示登录页（{page.url}），session 未生效")
    if "Just a moment" in text or "cloudflare" in text.lower():
        raise LoginFailed("进入签到页被 Cloudflare 拦截")
    raise NotOnCheckinPage(f"签到页缺少签到表单，URL: {page.url}")


def _click_checkin_action(page) -> bool:
    js = r"""
    const isVisible = (el) => {
      const s = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return s.display !== 'none' && s.visibility !== 'hidden'
        && r.width > 0 && r.height > 0 && !el.disabled;
    };
    const exact = document.querySelector('#points-signin-submit');
    if (exact && isVisible(exact) && !exact.disabled) {
      exact.scrollIntoView({block: 'center', inline: 'center'});
      exact.click();
      return true;
    }
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


def _confirm_checkin_success(page, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _contains_any(_visible_page_text(page), CHECKED_TEXT_MARKERS):
            return True
        time.sleep(1)
    return False


def do_checkin(page, github_cookies: list[dict], captcha_api_key: str) -> str:
    # ===== 1. GitHub OAuth 登录 =====
    browser.inject_github_session(page, github_cookies)
    browser.login_via_github(page, timeout=120)

    if not browser.is_logged_in(page):
        browser.dump_page_snapshot(page, tag="after-oauth")
        raise LoginFailed("OAuth 完成但无法确认登录状态")

    # ===== 2. 进入签到页 =====
    print(f"[checkin] 访问签到页: {CHECKIN_URL}")
    page.get(CHECKIN_URL)
    time.sleep(5)

    if "/login" in (page.url or ""):
        browser.screenshot(page, "00-session-expired")
        raise LoginFailed(f"被踢回登录页（{page.url}）")

    print(f"[checkin] 签到页 URL: {page.url}")
    browser.screenshot(page, "03-checkin-page")

    _verify_on_checkin_page(page, timeout=15)
    time.sleep(2)

    if _is_already_checked_in(page):
        print("[checkin] 今日已签到（服务端确认）")
        time.sleep(SUCCESS_SNAPSHOT_DELAY_SECONDS)
        browser.screenshot(page, "05-success")
        return "本日已签到"

    # ===== 3. 先尝试用音频识别解 reCAPTCHA =====
    print("[checkin] 尝试音频识别解 reCAPTCHA...")
    audio_solved = browser.solve_recaptcha(page, timeout=120)

    if audio_solved:
        print("[checkin] ✅ 音频识别成功")
    else:
        # 音频失败 → dump 页面 + 尝试 2captcha
        print("[checkin] ❌ 音频识别失败，dump 页面状态：")
        browser.dump_page_snapshot(page, tag="audio-failed")
        browser.screenshot(page, "03c-audio-failed")

        captcha_info = browser.detect_captcha(page)
        print(f"[checkin] 检测到的验证码: {captcha_info}")

        ctype = captcha_info.get("type", "none")
        sitekey = captcha_info.get("sitekey", "")

        if ctype != "none" and sitekey and captcha_api_key:
            print(f"[checkin] 尝试 2captcha 解决 {ctype}...")
            token = browser.solve_captcha_via_2captcha(
                page, captcha_api_key, captcha_info, timeout=240
            )
            if token:
                browser.inject_captcha_token(page, token)
                time.sleep(2)
            else:
                print("[checkin] 2captcha 也失败，继续尝试直接提交")
        else:
            print(f"[checkin] 无法用 2captcha（type={ctype}, sitekey={'有' if sitekey else '无'}, api_key={'有' if captcha_api_key else '无'}），直接提交")

    # ===== 4. 点击签到按钮 =====
    print("[checkin] 点击签到按钮")
    if not _click_checkin_action(page):
        if _is_already_checked_in(page):
            print("[checkin] 未找到按钮但已显示签到状态")
            browser.screenshot(page, "05-success")
            return "本日已签到"
        browser.screenshot(page, "03g-no-button")
        browser.dump_page_snapshot(page, tag="no-button")
        raise CheckinElementsNotFound("未找到签到按钮")

    print("[checkin] 已点击签到按钮")
    time.sleep(4)
    browser.screenshot(page, "04-after-click")

    # ===== 5. 确认签到成功 =====
    if not _confirm_checkin_success(page, timeout=30):
        print("[checkin] 第一次确认失败，等页面 reload 后再试...")
        time.sleep(5)
        if not _confirm_checkin_success(page, timeout=20):
            browser.screenshot(page, "04b-not-confirmed")
            browser.dump_page_snapshot(page, tag="not-confirmed")
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

    if state.already_checked_in_today():
        print("[main] 今日已签到，跳过")
        return 0

    try:
        github_cookies = browser.load_github_session_from_env()
    except Exception as exc:
        print(f"[fatal] 加载 GitHub session 失败: {exc}")
        return 2

    captcha_api_key = os.environ.get("CAPTCHA_API_KEY", "").strip()
    if captcha_api_key:
        print(f"[env] CAPTCHA_API_KEY: {captcha_api_key[:8]}...（已加载）")
    else:
        print("[env] 未配置 CAPTCHA_API_KEY，仅使用音频识别")

    browser.clean_screenshots()
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\n========== 尝试 {attempt}/{MAX_ATTEMPTS} ==========")
        page = None
        try:
            page = browser.create_page()
            status = do_checkin(page, github_cookies, captcha_api_key)
            state.mark_success()
            _send_result_snapshot(page, status, "06-result")
            print("[main] 任务完成")
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
