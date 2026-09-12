name: VPS8 Checkin

on:
  schedule:
    # 每 8 小时一次（北京时间 9:23 / 17:23 / 1:23）
    - cron: "23 1,9,17 * * *"
  workflow_dispatch:

jobs:
  checkin:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Debug - source check
        run: |
          echo "===== 目录结构 ====="
          ls -la
          echo ""
          echo "===== src/ 内容 ====="
          ls -la src/ 2>/dev/null || echo "src/ 目录不存在"
          echo ""
          echo "===== browser.py create_page 签名 ====="
          grep -n "def create_page" src/browser.py || echo "找不到 create_page"
          echo ""
          echo "===== browser.py _inject_cookies 是否存在 ====="
          grep -n "_inject_cookies" src/browser.py || echo "找不到 _inject_cookies"
          echo ""
          echo "===== checkin.py 调用处 ====="
          grep -n "create_page" src/checkin.py || echo "找不到 create_page 调用"

      - name: Compute Beijing date
        id: bjdate
        run: echo "date=$(TZ=Asia/Shanghai date +%Y-%m-%d)" >> "$GITHUB_OUTPUT"

      - name: Restore checkin state
        uses: actions/cache@v4
        id: checkin-state
        with:
          path: .checkin_state
          key: vps8-checkin-state-${{ steps.bjdate.outputs.date }}

      - name: Install Python deps
        run: pip install -r requirements.txt

      - name: Install Chrome
        run: |
          python - <<'PY'
          from DrissionPage import ChromiumOptions
          ChromiumOptions().set_browser_path("chrome").save()
          PY
          sudo apt-get update
          sudo apt-get install -y chromium-browser || sudo apt-get install -y chromium

      - name: Install Xvfb
        run: sudo apt-get install -y xvfb

      - name: Debug - cookie probe
        env:
          VPS8_STORAGE_STATE_B64: ${{ secrets.VPS8_STORAGE_STATE_B64 }}
        run: |
          python3 - <<'PY'
          import base64, json, os
          raw = os.environ.get("VPS8_STORAGE_STATE_B64", "")
          print(f"VPS8_STORAGE_STATE_B64 长度: {len(raw)}")
          if not raw:
              print("::error::Secret 为空")
              raise SystemExit(1)
          try:
              cookies = json.loads(base64.b64decode(raw).decode())
          except Exception as e:
              print(f"::error::解码失败: {e}")
              raise SystemExit(1)
          if isinstance(cookies, dict):
              cookies = cookies.get("cookies", [])
          php = next((c for c in cookies if c["name"] == "PHPSESSID"), None)
          if not php:
              print("::error::找不到 PHPSESSID")
              raise SystemExit(1)
          with open("/tmp/php.txt", "w") as f:
              f.write(php["value"])
          print(f"PHPSESSID = {php['value']}")
          print(f"cookies 数量: {len(cookies)}")
          PY

          PHP=$(cat /tmp/php.txt)
          echo "===== curl /dashboard ====="
          curl -s -i -o /tmp/resp.txt \
            -H "Cookie: PHPSESSID=$PHP" \
            -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36" \
            "https://vps8.zz.cd/dashboard"

          echo "HTTP 状态行:"
          head -1 /tmp/resp.txt
          echo ""
          echo "===== 响应前 30 行 ====="
          head -30 /tmp/resp.txt
          echo ""
          if grep -q "/login" /tmp/resp.txt; then
            echo "::warning::cookie 可能已失效（响应里出现 /login）"
          else
            echo "cookie 看起来有效（响应里没有 /login）"
          fi

      - name: Run checkin
        env:
          VPS8_STORAGE_STATE_B64: ${{ secrets.VPS8_STORAGE_STATE_B64 }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GITHUB_RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: xvfb-run -a python -m src.checkin

      - name: Show checkin state
        if: always()
        run: |
          if [ -f .checkin_state/last_success.txt ]; then
            echo "last_success = $(cat .checkin_state/last_success.txt)"
          else
            echo "no state file"
          fi

      - name: Upload failure screenshots
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: failure-screenshots
          path: screenshots/
          if-no-files-found: ignore
