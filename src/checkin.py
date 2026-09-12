"""VPS8 (vps8.zz.cd) 签到主流程。

流程：
    1. GitHub OAuth 静默登录 vps8
    2. 进入签到页，检查是否是真正的签到页
    3. 等待验证码 widget（最长 120 秒）
    4. reCAPTCHA → 音频识别（失败回退 2captcha）
    5. 提交签到表单
"""

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


def _dump_page_state(page, tag: str = "") -> None:
    """dump 页面状态，方便定位问题。"""
    prefix = f"[checkin-dump{(' ' + tag) if tag else ''}]"
    try:
        info = page.run_js(r"""
        (() => {
          const bodyText = (document.body && document.body.innerText) || '';
          return JSON.stringify({
            url: location.href,
            title: document.title,
            readyState: document.readyState,
            bodyTextLen: bodyText.length,
            bodyTextHead: bodyText.slice(0, 800),
            hasSigninForm: !!document.querySelector('#points-signin-form'),
            hasSigninBtn: !!document.querySelector('#points-signin-submit'),
            hasRecaptchaDiv: !!document.querySelector('.g-recaptcha'),
            hasRecaptchaIframe: !!document.querySelector('iframe[src*="recaptcha"]'),
            hasLoginForm: !!document.querySelector('form[action*="login"], input[name="email"], input[name="password"]'),
            h1Text: (document.querySelector('h1,h2,h3') || {}).innerText || '',
            iframeCount: document.querySelectorAll('iframe').length,
          });
        })()
        """)
        if not info:
            print(f"{prefix} 无数据")
            return
        import json as _json
        data = _json.loads(info) if isinstance(info, str) else info
        print(f"{prefix} URL: {data.get('url')}")
        print(f"{prefix} title: {data.get('title')!r}  h1: {data.get('h1Text')!r}")
        print(f"{prefix} readyState={data.get('readyState')}  bodyTextLen={data.get('bodyTextLen')}")
        print(f"{prefix} hasSigninForm={data.get('hasSigninForm')}  hasSigninBtn={data.get('hasSigninBtn')}")
        print(f"{prefix} hasRecaptchaDiv={data.get('hasRecaptchaDiv')}  hasRecaptchaIframe={data.get('hasRecaptchaIframe')}")
        print(f"{prefix} hasLoginForm={data.get('hasLoginForm')}  iframeCount={data.get('iframeCount')}")
        print(f"{prefix} bodyText 前 800 字：\n{data.get('bodyTextHead')}")
    except Exception as exc:
        print(f"{prefix} dump 失败: {exc}")


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


def _verify_on_checkin_page(page, timeout: int = 15) -> None:
    """确认当前确实是签到页，否则 dump 并报错。"""
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

        # 如果页面里出现「今日签到状态」字样也算签到页
        text = _visible_page_text(page)
        if "今日签到状态" in text or "积分签到" in text:
            print("[checkin] ✅ 已进入签到页（通过文本判断）")
            return

        time.sleep(1)

    # 没找到关键元素
    _dump_page_state(page, tag="verify-fail")
    browser.screenshot(page, "03-not-on-checkin-page")

    text = _visible_page_text(page)
    if "登录" in text and "密码" in text:
        raise LoginFailed(f"进入签到页后显示登录页（{page.url}），session 未生效")
    if "Just a moment" in text or "cloudflare" in text.lower():
        raise LoginFailed("进入签到页被 Cloudflare 拦截")
    if len(text) < 500:
        raise NotOnCheckinPage(f"签到页内容异常（仅 {len(text)} 字符），URL: {page.url}")

    raise NotOnCheckinPage(f"签到页缺少签到表单，URL: {page.url}")


def do_checkin(page, github_cookies: list[dict], captcha_api_key: str) -> str:
    # ===== 1. GitHub OAuth 登录 =====
    browser.inject_github_session(page, github_cookies)
    browser.login_via_github(page, timeout=120)

    # 登录后再访问一次 dashboard 确认
    if not browser.is_logged_in(page):
        _dump_page_state(page, tag="after-oauth")
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

    # ===== 2.5 确认进入真正的签到页 =====
    _verify_on_checkin_page(page, timeout=15)

    time.sleep(2)

    if _is_already_checked_in(page):
        print("[checkin] 今日已签到（服务端确认）")
        time.sleep(SUCCESS_SNAPSHOT_DELAY_SECONDS)
        browser.screenshot(page, "05-success")
        return "本日已签到"

    # ===== 3. 等验证码 widget 渲染 =====
    print("[checkin] 等待验证码 widget 渲染（最多 120 秒）...")
    captcha_info = browser.wait_captcha_widget(page, timeout=120)
    print(f"[checkin] 检测到的验证码: {captcha_info}")
    browser.screenshot(page, "03c-captcha-widget")

    captcha_solved = False
    ctype = captcha_info.get("type", "none")

    if ctype == "recaptcha":
        print("[checkin] 尝试音频识别求解 reCAPTCHA...")
        if browser.solve_recaptcha_via_audio(page, timeout=120):
            print("[checkin] ✅ 音频识别成功")
            captcha_solved = True
        else:
            print("[checkin] 音频识别失败，将回退 2captcha")
    elif ctype == "none":
        print("[checkin] ⚠️ 未检测到验证码 widget，刷新后再等一轮")
        page.refresh()
        time.sleep(5)
        browser.screenshot(page, "03c-refresh-retry")

        if _is_already_checked_in(page):
            print("[checkin] 刷新后已显示已签到")
            time.sleep(SUCCESS_SNAPSHOT_DELAY_SECONDS)
            browser.screenshot(page, "05-success")
            return "本日已签到"

        print("[checkin] 第二轮等待 widget（60 秒）...")
        captcha_info = browser.wait_captcha_widget(page, timeout=60)
        ctype = captcha_info.get("type", "none")
        print(f"[checkin] 第二轮检测: {captcha_info}")
        browser.screenshot(page, "03d-captcha-widget-2")

        if ctype == "recaptcha":
            if browser.solve_recaptcha_via_audio(page, timeout=120):
                captcha_solved = True
        elif ctype == "none":
            print("[checkin] 两轮都没检测到验证码，dump 页面并尝试直接提交")
            _dump_page_state(page, tag="captcha-none")
            captcha_solved = True
    else:
        print(f"[checkin] 验证码类型 {ctype}，需要用 2captcha")

    # ===== 4. 2captcha 兜底 =====
    if not captcha_solved and ctype != "none":
        if not captcha_api_key:
            browser.screenshot(page, "03e-no-apikey")
            raise CaptchaTimeout(
                f"{ctype} 音频识别失败且未配置 CAPTCHA_API_KEY"
            )
        print(f"[checkin] 用 2captcha 解决 {ctype}...")
        token = browser.solve_captcha_via_2captcha(
            page, captcha_api_key, captcha_info, timeout=240
        )
        if not token:
            browser.screenshot(page, "03e-2captcha-fail")
            raise CaptchaTimeout(f"2captcha 未能解决 {ctype}")
        browser.inject_captcha_token(page, token)
        captcha_solved = True
        time.sleep(2)
        browser.screenshot(page, "03f-token-injected")

    # ===== 5. 点击签到按钮 =====
    print("[checkin] 点击签到按钮")
    if not _click_checkin_action(page):
        if _is_already_checked_in(page):
            print("[checkin] 未找到按钮但已显示签到状态")
            browser.screenshot(page, "05-success")
            return "本日已签到"
        browser.screenshot(page, "03g-no-button")
        _dump_page_state(page, tag="no-button")
        raise CheckinElementsNotFound("未找到签到按钮")

    print("[checkin] 已点击签到按钮")
    time.sleep(4)
    browser.screenshot(page, "04-after-click")

    # ===== 6. 确认签到成功 =====
    if not _confirm_checkin_success(page, timeout=30):
        print("[checkin] 第一次确认失败，等页面 reload 后再试...")
        time.sleep(5)
        if not _confirm_checkin_success(page, timeout=20):
            browser.screenshot(page, "04b-not-confirmed")
            _dump_page_state(page, tag="not-confirmed")
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
