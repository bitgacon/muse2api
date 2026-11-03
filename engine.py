"""Muse 生成引擎：用真实网页会话驱动 muse.ai 完成生图/生视频。

已验证流程：
  注入 cookie -> 打开 https://muse.ai/ -> 定位 textarea(placeholder=消息)
  -> Input.insertText 填入 -> 点「发送」
  -> 等待新的附件容器 [data-testid^=hatch-chat-attachment-presentation-]
  -> 从该容器内的 img/video 取 blob 字节 -> 落盘
"""
from __future__ import annotations

import base64
import mimetypes
import urllib.request
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

from cdp import CDP, http_json

log = logging.getLogger("muse2api")

ATT_SEL = '[data-testid^="hatch-chat-attachment-presentation-"]'

# 决定账号生死的核心 cookie（缺失或过期 = 会话失效）
ESSENTIAL_COOKIES = ("hatch_sess",)


class MuseAuthError(RuntimeError):
    pass


class MuseGenerationError(RuntimeError):
    pass


class MuseModelRefusalError(MuseGenerationError):
    """Muse explicitly refused the requested media."""


class MuseEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.browser: CDP | None = None
        self._local = threading.local()
        self._start_lock = threading.Lock()
        self._active_contexts: set[str] = set()
        self._contexts_lock = threading.Lock()
        self._last_http_renew: dict[str, float] = {}
        self._log = None
        os.makedirs(cfg.profile_dir, exist_ok=True)

    def _ensure_local(self):
        local = getattr(self, "_local", None)
        if local is None:
            local = threading.local()
            self._local = local
        return local

    @property
    def page(self) -> CDP | None:
        return getattr(self._ensure_local(), "page", None)

    @page.setter
    def page(self, val: CDP | None):
        self._ensure_local().page = val

    @property
    def current_acc_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_acc_id", None)

    @current_acc_id.setter
    def current_acc_id(self, val: str | None):
        self._ensure_local().current_acc_id = val

    @property
    def current_context_id(self) -> str | None:
        return getattr(self._ensure_local(), "current_context_id", None)

    @current_context_id.setter
    def current_context_id(self, val: str | None):
        self._ensure_local().current_context_id = val

    # ---------------- 浏览器生命周期 ----------------
    def _debug_url(self):
        return f"http://127.0.0.1:{self.cfg.cdp_port}/json/version"

    def start(self):
        # All startup, including process creation, must be serialized across accounts.
        with self._start_lock:
            return self._start_locked()

    def _start_locked(self):
        if self.browser and (self.proc is None or self.proc.poll() is None):
            return
        env = dict(os.environ)
        env.setdefault("HOME", self.cfg.home_dir)
        env["PATH"] = (self.cfg.extra_path + os.pathsep + env.get("PATH", "")) if self.cfg.extra_path else env.get("PATH", "")
        args = [
            self.cfg.chromium,
            "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1440,2400",
            f"--remote-debugging-port={self.cfg.cdp_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.cfg.profile_dir}",
            "about:blank",
        ]
        # 尝试复用已有健康 CDP
        if not self.proc:
            try:
                v = http_json(self._debug_url(), timeout=1)
                if v and "webSocketDebuggerUrl" in v:
                    self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                    return
            except Exception:
                pass

        # 清理残留锁
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lp = os.path.join(self.cfg.profile_dir, lock_name)
            if os.path.exists(lp) or os.path.islink(lp):
                try:
                    os.unlink(lp)
                except Exception:
                    pass

        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self._log = open(os.path.join(self.cfg.data_dir, "chromium.log"), "ab", buffering=0)
        cwd_dir = self.cfg.home_dir if (self.cfg.home_dir and os.path.isdir(self.cfg.home_dir)) else None
        self.proc = subprocess.Popen(args, stdout=self._log, stderr=subprocess.STDOUT,
                                     env=env, cwd=cwd_dir)
        last = None
        for _ in range(90):
            try:
                v = http_json(self._debug_url(), timeout=2)
                self.browser = CDP(v["webSocketDebuggerUrl"], timeout=180)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1)
        raise MuseGenerationError(f"Chromium 启动失败: {last}")

    def stop(self):
        with self._start_lock:
            self._close_page()
            if self.browser:
                with self._contexts_lock:
                    for ctx_id in list(self._active_contexts):
                        try:
                            self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
                        except Exception:
                            pass
                    self._active_contexts.clear()
                try:
                    self.browser.close()
                except Exception:
                    pass
                self.browser = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
            self.proc = None

    # ---------------- 页面 ----------------
    def _close_page(self):
        """关闭当前线程专属的页面并销毁隔离的 BrowserContext，彻底释放内存与 Session。"""
        p = self.page
        ctx_id = self.current_context_id
        if p:
            try:
                p.close()
            except Exception:
                pass
            self.page = None
        if ctx_id and self.browser:
            try:
                self.browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
            except Exception:
                pass
            with self._contexts_lock:
                self._active_contexts.discard(ctx_id)
            self.current_context_id = None
        self.current_acc_id = None

    def _open_page(self):
        """为当前工作线程创建独立的页面。
        若浏览器主连接存在，则创建独立的 Incognito BrowserContext，实现多账号 Cookies 与会话 100% 隔离。"""
        if self.browser:
            try:
                res = self.browser.send("Target.createBrowserContext")
                ctx_id = res.get("result", {}).get("browserContextId")
                if ctx_id:
                    self.current_context_id = ctx_id
                    with self._contexts_lock:
                        self._active_contexts.add(ctx_id)
                    t_res = self.browser.send("Target.createTarget", {
                        "url": "about:blank",
                        "browserContextId": ctx_id
                    })
                    target_id = t_res.get("result", {}).get("targetId")
                    if target_id:
                        ws_url = f"ws://127.0.0.1:{self.cfg.cdp_port}/devtools/page/{target_id}"
                        page = CDP(ws_url, timeout=180)
                        page.send("Network.enable")
                        page.send("Page.enable")
                        page.send("Runtime.enable")
                        page.send("Browser.setDownloadBehavior",
                                  {"behavior": "allow", "downloadPath": self.cfg.download_dir,
                                   "browserContextId": ctx_id})
                        return page
            except Exception as exc:
                self._close_page()
                raise MuseGenerationError('Không tạo được phiên trình duyệt riêng cho tài khoản; đã dừng để tránh dùng chung cookie') from exc
        self._close_page()
        raise MuseGenerationError('Chưa tạo được phiên trình duyệt riêng; hãy chạy lại dịch vụ')

    @staticmethod
    def renew_session_http(cookies: dict, expires: dict | None = None,
                           wake_vm: bool = True) -> dict:
        """直接调用 muse.ai/api/session 续签 hatch_vml (+48h) / hatch_sess (+30d) / hatch_gw (+1y)，
        并按需调用 /api/hatch/vm/wake 唤醒云端工作区 VM。"""
        import requests
        cur_cookies = dict(cookies or {})
        cur_exp = dict(expires or {})
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "Origin": "https://muse.ai",
            "Referer": "https://muse.ai/thread/new",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Cookie": "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v),
        }
        try:
            r = requests.get("https://muse.ai/api/session", headers=headers,
                             timeout=12, allow_redirects=False)
        except requests.RequestException as exc:
            # 不回显请求内容：异常可能包含带凭据的代理 URL。
            raise MuseGenerationError(
                f"/api/session 网络请求失败 ({type(exc).__name__})；请检查服务器网络/代理后重试") from None
        if r.status_code == 401:
            raise MuseAuthError("会话认证失败 (/api/session HTTP 401)，请在官网确认登录后重新导入 cookie")
        if r.status_code != 200:
            hint = ("访问被拒绝，请检查服务器出口/地区/访问限制；不能据此判定 Cookie 失效"
                    if r.status_code == 403 else "上游请求未成功，请稍后重试并检查服务器网络")
            raise MuseGenerationError(f"/api/session HTTP {r.status_code}：{hint}")
        try:
            sj = r.json()
        except ValueError:
            raise MuseGenerationError("/api/session HTTP 200 返回非 JSON；会话状态未确认") from None
        if not isinstance(sj, dict) or sj.get("status") != "assigned":
            raise MuseGenerationError("/api/session HTTP 200 未返回 assigned 会话；请在官网检查账号/工作区状态")
        for c in r.cookies:
            if c.value:
                cur_cookies[c.name] = c.value
            if c.expires:
                cur_exp[c.name] = int(float(c.expires))
        vm_id = sj.get("vm_id")
        vm_state = sj.get("vm_state")
        wake_ok = False
        wake_requested = False
        if wake_vm and vm_id and vm_state != "DISABLED":
            try:
                headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cur_cookies.items() if v)
                rw = requests.post("https://muse.ai/api/hatch/vm/wake", headers=headers, json={
                    "vm_id": vm_id,
                    "retry_count": 0,
                    "connect_attempt_id": str(uuid.uuid4()),
                }, timeout=8)
                wake_requested = rw.status_code == 200
                if wake_requested:
                    verify = requests.get("https://muse.ai/api/session", headers=headers,
                                          timeout=12, allow_redirects=False)
                    if verify.status_code == 200:
                        verified = verify.json()
                        if isinstance(verified, dict) and verified.get("status") == "assigned":
                            vm_state = verified.get("vm_state") or vm_state
                            wake_ok = vm_state == "RUNNING"
            except Exception:
                pass
        return {
            "ok": sj.get("status") == "assigned",
            "status": sj.get("status"),
            "vm_id": vm_id,
            "vm_state": vm_state,
            "wake_ok": wake_ok,
            "wake_requested": wake_requested,
            "cookies": cur_cookies,
            "cookies_exp": cur_exp,
        }

    def _apply_cookies(self, page: CDP, cookies: dict, expires: dict | None = None):
        """注入 cookie。彻底清空旧账号 cookie 保证隔离，且绝不传入过去时间的 expires 防止 Chromium 丢弃 hatch_vml。"""
        try:
            page.send("Network.clearBrowserCookies")
        except Exception:
            pass
        now = time.time()
        for name, value in cookies.items():
            if not value:
                continue
            exp = (expires or {}).get(name)
            try:
                exp_val = float(exp) if exp and float(exp) > now + 3600 else (now + 7 * 86400)
            except (TypeError, ValueError):
                exp_val = now + 7 * 86400
            for dom in (".muse.ai", "muse.ai"):
                params = {
                    "name": name,
                    "value": value,
                    "domain": dom,
                    "path": "/",
                    "secure": True,
                    "expires": exp_val,
                }
                try:
                    page.send("Network.setCookie", params)
                except Exception:  # noqa: BLE001
                    pass

    def read_cookies(self) -> dict[str, dict]:
        """从当前页面读回 cookie（**包含 httpOnly**，这是网页 JS 做不到的）。

        返回 {name: {"value":..., "expires": unix秒 或 -1}}。
        用途：muse.ai 在访问时会续期部分 cookie，生成完读回来写进账号池，
        账号就不容易过期。
        """
        if not self.page:
            return {}
        try:
            msg = self.page.send("Network.getCookies",
                                 {"urls": [self.cfg.site_url]}, timeout=20)
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict] = {}
        for c in (msg.get("result", {}).get("cookies") or []):
            name = c.get("name")
            if not name:
                continue
            try:
                exp = int(float(c.get("expires", -1)))
            except (TypeError, ValueError):
                exp = -1
            out[name] = {"value": c.get("value", ""), "expires": exp}
        return out

    def _wait_ws_ready(self, page: CDP, timeout: float = 15.0) -> bool:
        """等待 muse.ai 页面完成 React hydration 且不再处于 Connecting... 状态。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = page.js("""(function(){
                    if (document.readyState !== 'complete') return 'loading';
                    if (!document.querySelector('textarea')) return 'no-ta';
                    var h = document.querySelector('[data-hatch-shell-hydration-state]');
                    if (h && h.getAttribute('data-hatch-shell-hydration-state') !== 'hydrated') return 'hydrating';
                    var b = document.body ? (document.body.innerText || '') : '';
                    if (b.indexOf('Connecting...') !== -1) return 'connecting';
                    // Empty composers may not render Send until text has been entered.
                    if (document.querySelector('textarea').disabled) return 'input-disabled';
                    return 'ready';
                })()""")
                if st == "ready":
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def reset_thread(self, for_chat: bool = False):
        """关闭残留弹窗并确保处于干净会话且 WebSocket 已就绪。
        对于纯文本对话（for_chat=True），若当前热页面无附件、无卡死且气泡数较少，直接复用现有热连接以实现 2s 级秒回。"""
        if not self.page:
            return
        try:
            needs_nav = self.page.js("""(function(forChat){
                var d = document.querySelector('[role="dialog"]');
                if (d) {
                    var b = d.querySelector('button[aria-label*="close" i], button');
                    if (b) b.click();
                }
                var scope = document.querySelector('main,[class*="chat-scroll"],[class*="hatch-chat-scroll"]') || document.body;
                var bubbleCount = scope ? scope.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length : 0;
                var hasAtts = document.querySelectorAll('[data-testid^="hatch-chat-attachment-presentation-"]').length > 0;
                var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                    || document.querySelector('button[aria-label*="Stop" i]')
                    || document.querySelector('button[aria-label*="停止"]'));
                var bodyTxt = document.body ? (document.body.innerText || '') : '';
                var hasStuck = bodyTxt.indexOf('Still sending') !== -1 || bodyTxt.indexOf('Connecting...') !== -1;
                if (hasStop || hasStuck || hasAtts) return true;
                if (forChat) {
                    return bubbleCount >= 24;
                }
                return (window.location.pathname !== '/thread/new') || bubbleCount > 0;
            })(%s)""" % ("true" if for_chat else "false"))
            if needs_nav:
                self.page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
                t_end = time.time() + 10.0
                while time.time() < t_end:
                    time.sleep(0.08)
                    ready = self.page.js("""(function(){
                        return document.readyState === 'complete'
                            && !!document.querySelector('textarea')
                            && document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]').length === 0;
                    })()""")
                    if ready:
                        break
                self._wait_ws_ready(self.page, timeout=12.0)
        except Exception:
            pass

    def ensure_page(self, cookies: dict, expires: dict | None = None, account_id: str | None = None):
        if self.page is not None and (account_id is None or getattr(self, "current_acc_id", None) == account_id):
            try:
                if self.page.js("!!document.querySelector('textarea')") and self._wait_ws_ready(self.page, timeout=8.0):
                    return self.page
            except Exception:
                pass
        self._close_page()
        # 仅当距离上次 HTTP 续签超过 10 分钟时才在主链路调用 /api/session，避免每次切号重复阻塞
        last_map = getattr(self, "_last_http_renew", None)
        if last_map is None:
            last_map = {}
            self._last_http_renew = last_map
        now_ts = time.time()
        if not account_id or (now_ts - last_map.get(account_id, 0) > 600):
            try:
                renewed = self.renew_session_http(cookies, expires, wake_vm=True)
                if renewed.get("cookies"):
                    cookies = renewed["cookies"]
                if renewed.get("cookies_exp"):
                    expires = renewed["cookies_exp"]
                if account_id:
                    last_map[account_id] = now_ts
            except MuseAuthError:
                raise
            except Exception as e:
                log.warning("预续签 /api/session 失败（继续尝试浏览器加载）: %s", e)

        page = self._open_page()
        self.page = page  # Own the page even when hydration/connection fails.
        self._apply_cookies(page, cookies, expires)
        page.send("Page.navigate", {"url": "https://muse.ai/thread/new"})
        if self._wait_ws_ready(page, timeout=self.cfg.login_wait):
            self.current_acc_id = account_id
            return page
        try:
            body = (page.js("document.body ? document.body.innerText.slice(0,1200) : ''") or "").lower()
        except Exception:
            body = ""
        self._close_page()
        if "connecting..." in body:
            raise MuseGenerationError("Muse chưa kết nối được workspace (Connecting...). Kiểm tra Muse trên trình duyệt và nhập lại cookie nếu cần.")
        if re.search(r"log in|sign in|create an account|登录|use another account", body):
            raise MuseAuthError("会话已被 muse.ai 登出（可能被其它登录挤掉或触发风控），"
                                "请用浏览器扩展重新导入 cookie")
        raise MuseGenerationError("muse.ai 页面加载超时（未出现聊天输入框），请检查服务器网络后重试；未确认会话失效")

    def close_current_page(self):
        """显式释放当前线程所持有的页面和隔离上下文。"""
        self._close_page()

    def refresh(self, cookies: dict, expires: dict | None = None):
        self._close_page()
        return self.ensure_page(cookies, expires)

    # ---------------- 额度查询（Settings 面板） ----------------
    # muse.ai 的额度在底部 Settings 菜单 → Settings 项 → 设置面板的
    # General → Usage 区块里，形如：
    #   Free plan
    #   Weekly limit resets on Sep 30
    #   1% used
    #   Additional tokens / Never expires / 0% used (2B tokens left)
    def _click_point(self, x: int, y: int):
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": x, "y": y,
                            "button": "left", "clickCount": 1})

    _CLICK_JS = (
        "(function(){var sel=%s;"
        "var b=[...document.querySelectorAll(sel)]"
        ".filter(function(x){return x.offsetParent!==null;})[0];"
        "if(!b)return null;var r=b.getBoundingClientRect();"
        "return JSON.stringify({x:Math.round(r.x+r.width/2),"
        "y:Math.round(r.y+r.height/2)});})()")

    def quota(self, cookies: dict, expires: dict | None = None) -> dict:
        """打开 Settings 面板读额度。返回结构化 dict；读不到时 raise。"""
        self.ensure_page(cookies, expires)
        p = self.page
        time.sleep(1)

        # 1) 点左下角 Settings 按钮（aria-label=Settings）
        raw = p.js(self._CLICK_JS % json.dumps('button[aria-label="Settings"]'))
        if not raw:
            raise MuseGenerationError("找不到 Settings 按钮")
        pt = json.loads(raw)
        self._click_point(pt["x"], pt["y"])
        time.sleep(1.6)

        # 2) 点弹出的菜单里文本为 Settings 的项
        raw = p.js(
            "(function(){"
            "var els=[...document.querySelectorAll('div,span,li,[role=menuitem],button')]"
            ".filter(function(e){return e.offsetParent!==null"
            "&&(e.textContent||'').trim()==='Settings'"
            "&&e.getAttribute('aria-label')!=='Settings'"
            "&&e.children.length<=3;});"
            "if(!els.length)return null;"
            "var el=els[els.length-1];var r=el.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.x+r.width/2),"
            "y:Math.round(r.y+r.height/2)});})()")
        if not raw:
            raise MuseGenerationError("Settings 菜单未弹出")
        pt = json.loads(raw)
        self._click_point(pt["x"], pt["y"])
        time.sleep(3.0)

        # 3) 读设置面板文本
        txt = ""
        for _ in range(6):
            txt = p.js(
                "(function(){var d=document.querySelector('[role=dialog],[aria-modal=true]');"
                "return d?(d.innerText||''):'';})()") or ""
            if "Usage" in txt or "used" in txt:
                break
            time.sleep(1.2)

        # 4) 关闭面板（Escape）
        for t in ("keyDown", "keyUp"):
            p.send("Input.dispatchKeyEvent",
                   {"type": t, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
        time.sleep(0.5)

        return self._parse_quota(txt)

    @staticmethod
    def _parse_quota(txt: str) -> dict:
        """从设置面板文本解析额度字段。"""
        lines = [ln.strip() for ln in (txt or "").split("\n") if ln.strip()]
        out: dict = {"raw": "\n".join(lines[:40])}
        # 计划名：Free plan / xxx plan
        for ln in lines:
            m = re.match(r"^(.+?)\s*plan$", ln, re.I)
            if m:
                out["plan"] = ln
                break
        # Weekly limit resets on Sep 30
        m = re.search(r"Weekly limit resets? on (.+)", txt or "")
        if m:
            out["weekly_reset"] = m.group(1).strip()
        # 周用量：第一个 "N% used"（出现在 plan 行之后）
        m = re.search(r"(\d+)%\s*used", txt or "")
        if m:
            out["weekly_used_pct"] = int(m.group(1))
        # 额外代币："0% used (2B tokens left)"
        m = re.search(r"(\d+)%\s*used\s*\(([^)]+)\)", txt or "")
        if m:
            out["extra_used_pct"] = int(m.group(1))
            out["extra_left"] = m.group(2).strip()
        if "Never expires" in (txt or ""):
            out["extra_expires"] = "never"
        out["found"] = bool(out.get("plan") or "weekly_used_pct" in out)
        return out

    # ---------------- 附件（生成结果） ----------------
    _ATT_JS = (
        "(function(){"
        "var list = []; var seen = new Set();"
        "function addEl(el, tid){"
        "  if(!el || seen.has(el)) return;"
        "  seen.add(el);"
        "  if(el.closest('form, [class*=chat-user-bubble], [class*=\"group/msg\"]')) return;"
        "  var v = el.querySelector('video') || (el.tagName === 'VIDEO' ? el : null);"
        "  var img = el.querySelector('img') || (el.tagName === 'IMG' ? el : null);"
        "  var isVid = (tid || '').includes('video') || !!v;"
        "  var primary = isVid ? (v || img) : (img || v);"
        "  var src = primary ? (primary.currentSrc || primary.src || '') : '';"
        "  if(src && !seen.has(src)){"
        "    seen.add(src);"
        "    list.push({"
        "      tid: tid || el.getAttribute('data-testid') || (isVid ? 'video' : 'image'),"
        "      hasVideo: !!v,"
        "      hasImg: !!img,"
        "      src: src,"
        "      vSrc: v ? (v.currentSrc || v.src || '') : '',"
        "      iSrc: img ? (img.currentSrc || img.src || '') : '',"
        "      w: primary ? (primary.videoWidth || primary.naturalWidth || 0) : 0,"
        "      h: primary ? (primary.videoHeight || primary.naturalHeight || 0) : 0"
        "    });"
        "  }"
        "}"
        "document.querySelectorAll('[data-testid^=\"hatch-chat-attachment-presentation-\"]').forEach(function(a){ addEl(a, a.getAttribute('data-testid')); });"
        "document.querySelectorAll('div[class*=\"hatch-agent-bubble-bg\"] img, div[class*=\"hatch-agent-bubble-bg\"] video').forEach(function(m){"
        "  var s = m.currentSrc || m.src || '';"
        "  if(s && !s.includes('avatar') && !s.includes('emoji')) addEl(m.parentElement || m, 'agent-media');"
        "});"
        "return JSON.stringify(list);"
        "})()"
    )

    def attachments(self) -> list[dict]:
        try:
            raw = self.page.js(self._ATT_JS)
            return json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []

    # ---------------- 发送 ----------------
    # 检查「文字真的进了输入框 + Send 按钮真的被渲染出来」。
    # 两个条件缺一不可：Send 按钮只有 React state 里有文字才会渲染 ——
    # 它在，就说明 React 真的收到了输入（不是 DOM value 被改了而已）。
    _SEND_STATE_JS = (
        "(function(){var ta=document.querySelector('textarea');"
        "var b=[...document.querySelectorAll('button,[role=button]')]"
        ".find(function(x){return /send/i.test(x.getAttribute('aria-label')||'');});"
        "return JSON.stringify({v:ta?ta.value:'',btn:b?(b.disabled?2:1):0});})()"
    )

    def _send(self, prompt: str):
        if not self._wait_ws_ready(self.page, timeout=self.cfg.login_wait):
            raise MuseGenerationError("Muse chưa sẵn sàng nhận tin nhắn (Connecting / đang tải workspace). Prompt chưa được gửi.")
        # 1. 确保 textarea 滚动到视口中央并获得真实焦点
        try:
            self.page.js("""(function(){
                var ta = document.querySelector('textarea');
                if (ta) {
                    ta.scrollIntoView({block: 'center', inline: 'nearest'});
                    ta.focus();
                }
            })()""")
        except Exception:
            pass
        time.sleep(0.1)

        rect = self.page.js(
            "(function(){var t=document.querySelector('textarea');if(!t)return null;"
            "var r=t.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.left+r.width/2),"
            "y:Math.round(r.top+r.height/2)});})()")
        if not rect:
            raise MuseGenerationError("找不到聊天输入框")
        c = json.loads(rect)
        for t in ("mousePressed", "mouseReleased"):
            self.page.send("Input.dispatchMouseEvent",
                           {"type": t, "x": c["x"], "y": c["y"],
                            "button": "left", "clickCount": 1})
        time.sleep(0.1)

        # 触发 React 18 原型 setter 以及 input/change 事件以同步发送按钮状态
        # （对于 DeepSeek/Codex 等 100KB+ 超长上下文，直接走原型 setter 仅需 <1s，避免 Input.insertText 逐字注入卡死）
        _SETTER_JS = (
            "(function(t){var ta=document.querySelector('textarea');"
            "if(!ta) return 0;"
            "ta.focus();"
            "var s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;"
            "s.call(ta,t);"
            "ta.dispatchEvent(new Event('input',{bubbles:true}));"
            "ta.dispatchEvent(new Event('change',{bubbles:true}));"
            "return (ta.value||'').length;})(%s)")
        val_len = self.page.js(_SETTER_JS % json.dumps(prompt)) or 0
        if not val_len and len(prompt) < 500:
            self.page.send("Input.insertText", {"text": prompt})
            self.page.js(_SETTER_JS % json.dumps(prompt))

        # 等待发送按钮就绪并点击
        clicked = "no-button"
        t_deadline = time.time() + 3.0
        while time.time() < t_deadline:
            res = self.page.js(
                "(function(){var b=[...document.querySelectorAll('button,[role=button]')]"
                ".filter(function(x){return x.offsetParent!==null;})"
                ".find(function(x){return /发送|send/i.test(x.getAttribute('aria-label')||'')"
                "||/发送|send/i.test(x.getAttribute('data-testid')||'')"
                "||/send/i.test(x.innerText||'');});"
                "if(!b)return 'no-button';"
                "if(b.disabled)return 'disabled';"
                "b.click();return 'clicked';})()")
            if res == "clicked":
                clicked = "clicked"
                break
            time.sleep(0.08)

        if clicked != "clicked":
            # 兜底：Ctrl+Enter 或普通 Enter
            for combo in ({"modifiers": 1}, {}):
                for t in ("keyDown", "char", "keyUp"):
                    params = {"type": t, "key": "Enter", "code": "Enter",
                              "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                              "modifiers": combo.get("modifiers", 0)}
                    if t == "char":
                        params["text"] = "\r"
                        params["unmodifiedText"] = "\r"
                    self.page.send("Input.dispatchKeyEvent", params)
                time.sleep(1.0)
                try:
                    if self.page.js("!(document.querySelector('textarea')||{value:''}).value"):
                        clicked = "enter-sent"
                        break
                except Exception:
                    pass
        time.sleep(0.3)
        return clicked

    # ---------------- 等待生成 ----------------
    def _last_attachment(self) -> dict | None:
        atts = self.attachments()
        return atts[-1] if atts else None

    def _scroll_bottom(self):
        """滚到聊天底部。muse.ai 的聊天滚动容器是内层 div（不是 document），
        虚拟列表按滚动位置渲染节点 —— 不滚到底，新消息根本不在 DOM 里。"""
        try:
            self.page.js(
                "(function(){"
                "var els=[...document.querySelectorAll('*')].filter(function(e){"
                "var s=getComputedStyle(e);"
                "return (s.overflowY==='auto'||s.overflowY==='scroll')"
                "&&e.scrollHeight>e.clientHeight+100;});"
                "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
                "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
                "var s=document.scrollingElement||document.body;"
                "s.scrollTop=s.scrollHeight;"
                "var el=document.querySelector('textarea');"
                "if(el)el.scrollIntoView({block:'end'});return 1;})()")
        except Exception:  # noqa: BLE001
            pass

    def _wait_attachment(self, baseline_src: str, timeout: int, expect: str,
                         on_progress=None, base_agent_cnt: int = 0,
                         base_att_cnt: int = 0, stop_event=None, baseline_sources=None) -> dict | None:
        baseline_sources = set(baseline_sources or ()) | {baseline_src}
        deadline = time.time() + timeout
        t_start = time.time()
        stable_src, stable_n = "", 0
        last_txt, txt_stable = "", 0
        refusal_last, refusal_stable = "", 0
        seen_stop = False
        previous_state = None
        last_activity = t_start
        progress_sent = 0
        while time.time() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("客户端已断开连接，终止生成任务")
            time.sleep(0.6)
            self._scroll_bottom()
            atts = self.attachments()
            # 严格按期望类型挑选附件，绝不接受跨模态兜底（如将视频请求降级为图片封面）
            matching_atts = []
            for a in atts:
                tid = (a.get("tid") or "").lower()
                has_video = bool(a.get("hasVideo"))
                src = a.get("src") or ""
                v_src = a.get("vSrc") or ""
                if expect == "video":
                    # tid 含 "video" 不足以命中：muse 会先把 data-testid 标成
                    # ...-presentation-video，此时 <video> 可能尚未挂上，_ATT_JS 的
                    # primary = v || img 会退回封面 img，src/w/h 全是静帧。
                    # 只认真正的视频源（vSrc / hasVideo / 视频扩展名）。
                    is_match = (
                        has_video
                        or bool(v_src)
                        or src.endswith((".mp4", ".webm", ".mov"))
                    )
                else:
                    is_match = ("image" in tid) or (not has_video and not src.endswith((".mp4", ".webm", ".mov")))
                if is_match:
                    matching_atts.append(a)

            att = matching_atts[-1] if matching_atts else None
            if att:
                src = att.get("src") or ""
                v_src = att.get("vSrc") or ""
                w = att.get("w", 0) or 0
                h = att.get("h", 0) or 0
                # 视频任务禁止用封面 src 冒充成品；无 vSrc 时仅接受显式视频后缀的 src
                if expect == "video":
                    check_src = v_src if v_src else (
                        src if src.endswith((".mp4", ".webm", ".mov")) else ""
                    )
                else:
                    check_src = src
                if check_src and check_src not in baseline_sources:
                    if w > 0 and h > 0:
                        return att
                    if check_src == stable_src:
                        stable_n += 1
                    else:
                        stable_src, stable_n = check_src, 0
                    if stable_n >= 1:
                        return att
            elapsed = time.time() - t_start
            cur_cnt = base_agent_cnt
            cur_txt = ""
            has_stop = False
            tail = ""
            try:
                st_raw = self.page.js("""(function(){
                    var bs=[].slice.call(document.querySelectorAll('div[class*="hatch-chat-groupable-bubble"]'))
                        .filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});
                    var lastTxt = bs.length ? (bs[bs.length-1].innerText||'').trim() : '';
                    var hasStop = !!(document.querySelector('[data-testid="hatch-composer-stop-button"]')
                        || document.querySelector('button[aria-label*="Stop" i]')
                        || document.querySelector('button[aria-label*="停止"]'));
                    var tail = document.body ? (document.body.innerText||'').slice(-1200) : '';
                    return JSON.stringify({cnt: bs.length, txt: lastTxt, stop: hasStop, tail: tail,
                        path: location.pathname || '', href: location.href || ''});
                })()""")
                st = json.loads(st_raw) if st_raw else {}
            except Exception:
                st = {}
            tail = st.get("tail") or ""
            cur_cnt = int(st.get("cnt") or 0)
            cur_txt = st.get("txt") or ""
            has_stop = bool(st.get("stop"))
            path = st.get("path") or ""
            seen_stop = seen_stop or has_stop
            state = (cur_cnt, cur_txt, len(atts), has_stop, path)
            if previous_state is None or state != previous_state:
                last_activity = time.time()
                previous_state = state

            if on_progress:
                milestone = 25 if seen_stop else 20
                if cur_cnt > base_agent_cnt:
                    milestone = 40
                if milestone > progress_sent:
                    try:
                        on_progress(milestone)
                        progress_sent = milestone
                    except Exception:
                        pass

            if re.search(r"额度不足|积分不足|out of credits|达到上限|token limit", tail, re.I):
                raise MuseGenerationError("账号额度不足")
            # A Stop button is also shown for messages never acknowledged by the gateway.
            if elapsed > 30.0 and cur_cnt == base_agent_cnt and re.search(
                    r"Still sending|Delivery not confirmed|正在发送|未确认送达", tail, re.I):
                raise MuseGenerationError(
                    "提示词未成功送达后端（Still sending / Delivery not confirmed），终止等待并重试")

            refusal_source = cur_txt if cur_cnt > base_agent_cnt else (
                tail if seen_stop and not has_stop and elapsed > 20.0 else "")
            refusal = self._media_refusal_text(refusal_source, expect)
            if refusal and not has_stop and len(atts) <= base_att_cnt:
                if refusal == refusal_last:
                    refusal_stable += 1
                else:
                    refusal_last, refusal_stable = refusal, 0
                if refusal_stable >= 4 and elapsed > 8.0:
                    raise MuseModelRefusalError(f"模型未生成媒体，仅返回文本: {refusal}")
            else:
                refusal_last, refusal_stable = "", 0

            if cur_cnt > base_agent_cnt and cur_txt and not has_stop and len(atts) <= base_att_cnt:
                # Media-status text can precede a real attachment; do not fail on it.
                is_media_report = self._media_status_pending(cur_txt)
                if not is_media_report:
                    if cur_txt == last_txt:
                        txt_stable += 1
                    else:
                        last_txt, txt_stable = cur_txt, 0
                    if txt_stable >= 15 and elapsed > 8.0:
                        clean = self._clean_model_text(cur_txt)
                        raise MuseModelRefusalError(
                            f"模型未生成媒体，仅返回文本: {clean}")
                else:
                    txt_stable = 0
            else:
                txt_stable = 0

            # Muse accepted the click but never created a thread. Requiring a completed
            # Stop-button lifecycle, an idle page, no messages/attachments and /thread/new
            # avoids treating ordinary long video rendering as failure.
            if (expect == "video" and seen_stop and not has_stop
                    and path.rstrip("/").endswith("/thread/new")
                    and cur_cnt <= base_agent_cnt and len(atts) <= base_att_cnt
                    and elapsed >= 180.0 and time.time() - last_activity >= 90.0):
                raise MuseGenerationError(
                    "Muse 未建立视频任务线程，页面在提交结束后持续无响应（已提前停止等待）")
        return None

    @staticmethod
    def _clean_model_text(text: str, limit: int = 240) -> str:
        clean = re.sub(r"\s+", " ", text or "").strip()
        return clean[:limit]

    @staticmethod
    def _media_status_pending(text: str) -> bool:
        """A success claim can precede an attachment; it is never delivery proof."""
        if re.search(
                r"(?:未|没(?:有)?|无法|不能|未能)[^。！？!?\n]{0,16}(?:生成|制作|提供|完成)|"
                r"(?:生成|制作|渲染|处理)[^。！？!?\n]{0,12}(?:失败|未成功)|"
                r"\b(?:not|never|hasn['’]t|wasn['’]t|isn['’]t)\s+(?:(?:be|been|successfully)\s+){0,3}"
                r"(?:generated|created|completed|ready)|"
                r"\b(?:no|without)\s+(?:video|clip|image|picture|animation)\b"
                r"[^.\n]{0,60}\b(?:generated|created|ready)|"
                r"\b(?:failed|cannot|can['’]t|could not|couldn['’]t|unable to)\b"
                r"[^.\n]{0,60}\b(?:generat(?:e|ed|ing)|creat(?:e|ed|ing)|render|provide|video|image)\b|"
                r"\b(?:video|image|render|upload)\b[^.\n]{0,32}\bfailed\b",
                text, re.I):
            return False
        return bool(re.search(
            r"\.(?:webp|png|jpe?g|mp4|webm)|imagine_media|deliverable|"
            r"generated\s+.*image|verified\s+generated|artifact|"
            r"^(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)(?:[，。！\s]|$)|"
            r"(?:视频|影片|短片|图片|图像)[^。！？!?\n]{0,40}"
            r"(?:已(?:经)?(?:成功)?生成|生成(?:成功|好了|完毕|完成)|已(?:经)?完成|做好了)|"
            r"(?:已(?:经)?(?:成功)?生成|生成好了|生成完毕|已完成|做好了)"
            r"[^。！？!?\n]{0,32}(?:视频|影片|短片|图片|图像)|"
            r"\b(?:video|clip|image|picture|animation)\b[^.\n]{0,60}"
            r"\b(?:generated|created|rendered|completed|ready)\b|"
            r"\b(?:generated|created|rendered|completed)\b[^.\n]{0,32}"
            r"\b(?:video|clip|image|picture|animation)\b", text, re.I))

    @classmethod
    def _media_refusal_text(cls, text: str, expect: str) -> str:
        """Return only an explicit media refusal line, never an arbitrary page tail."""
        if not text:
            return ""
        patterns = [
            r"^(?:没生成成|视频没生成成功|视频未生成成功|视频生成失败)[^\n]{0,240}"
            r"(?:国家|地区|country|region)[^\n]{0,160}(?:不支持|不可用|无法|not available|unavailable|not supported)",
            r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}video[^\n]{0,160}"
            r"(?:country|region|not available|unavailable|not supported)",
        ] if expect == "video" else [
            r"^(?:图片没生成成功|图片未生成成功|图片生成失败|无法生成图片)",
            r"^(?:could not|couldn't|cannot|can't|unable to)[^\n]{0,120}image",
        ]
        for line in reversed((text or "").splitlines()):
            clean = cls._clean_model_text(line)
            if clean and any(re.search(pattern, clean, re.I) for pattern in patterns):
                return clean
        return ""

    # ---------------- 取字节 ----------------
    _EXTRACT_JS = r"""
    (async function(src, expect){
      try{
        var u = src;
        if(!u) return JSON.stringify({ok:false,err:'no-media-src'});
        var controller = new AbortController();
        var timer = setTimeout(()=>controller.abort(), 45000);
        var r = await fetch(u, {signal:controller.signal});
        if(!r.ok) return JSON.stringify({ok:false,err:'media-http-'+r.status});
        var b = await r.blob();
        var ab = await b.arrayBuffer();
        clearTimeout(timer);
        var bytes = new Uint8Array(ab);
        var s = '';
        for(var i=0; i<bytes.length; i+=65536){
          s += String.fromCharCode.apply(null, bytes.subarray(i, i+65536));
        }
        return JSON.stringify({ok:true, mime:b.type||'', size:b.size, url:u, b64:btoa(s)});
      }catch(e){
        return JSON.stringify({ok:false, err:String(e)});
      }
    })(%s, %s)
    """

    # ---------------- 文本 / 代码对话 ----------------
    _AGENT_TEXT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "for(var i=bs.length-1;i>=0;i--){"
        "var cs=bs[i].className||'';"
        "if(/hatch-agent-bubble-bg/.test(cs))return bs[i].innerText||'';}"
        "return '';})()"
    )
    _USER_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/chat-user-bubble/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _AGENT_COUNT_JS = (
        "(function(){"
        "var bs=[].slice.call(document.querySelectorAll("
        "'div[class*=\"hatch-chat-groupable-bubble\"]'));"
        "var n=0;for(var i=0;i<bs.length;i++){"
        "if(/hatch-agent-bubble-bg/.test(bs[i].className||''))n++;}"
        "return String(n);})()"
    )
    _POLL_CHAT_JS = (
        "(function(){"
        "var els=[...document.querySelectorAll('*')].filter(function(e){"
        "var s=getComputedStyle(e);"
        "return (s.overflowY==='auto'||s.overflowY==='scroll')&&e.scrollHeight>e.clientHeight+100;});"
        "els.sort(function(a,b){return b.scrollHeight-a.scrollHeight;});"
        "if(els[0])els[0].scrollTop=els[0].scrollHeight;"
        "var scope=document.querySelector('main,[class*=\"chat-scroll\"],[class*=\"hatch-chat-scroll\"]')||document.body;"
        "var bs=[].slice.call(scope.querySelectorAll('div[class*=\"hatch-chat-groupable-bubble\"]'))"
        ".filter(function(b){return /hatch-agent-bubble-bg/.test(b.className||'');});"
        "var nonEmpty=bs.filter(function(b){return ((b.innerText||'').trim().length)>0;});"
        "var txt=nonEmpty.length?(nonEmpty[nonEmpty.length-1].innerText||'').trim():'';"
        "var stop=!!(document.querySelector('[data-testid=\"hatch-composer-stop-button\"]')"
        "||document.querySelector('button[aria-label*=\"Stop\" i]')"
        "||document.querySelector('button[aria-label*=\"停止\"]'));"
        "return JSON.stringify({cnt:nonEmpty.length,total:bs.length,txt:txt,stop:stop});})()"
    )

    def _agent_text(self) -> str:
        """最后一个助手气泡的文本（取不到就返回空串）。"""
        try:
            return (self.page.js(self._AGENT_TEXT_JS) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _agent_count(self) -> int:
        try:
            return int(self.page.js(self._AGENT_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _user_count(self) -> int:
        try:
            return int(self.page.js(self._USER_COUNT_JS) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def _poll_chat(self) -> tuple[int, str, bool]:
        try:
            raw = self.page.js(self._POLL_CHAT_JS)
            if raw:
                d = json.loads(raw)
                return int(d.get("cnt") or 0), (d.get("txt") or "").strip(), bool(d.get("stop"))
        except Exception:
            pass
        return 0, "", False

    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,
                    timeout: int | None = None, account_id: str | None = None,
                    stop_event=None, thread_url=None, on_result=None):
        """Read replaceable DOM snapshots; publish only the completed response.

        Muse may revise earlier text or insert options below it while generating.
        Such snapshots are not append-only deltas and must never be concatenated.
        """
        timeout = int(timeout or getattr(self.cfg, "chat_timeout", 600))
        self.ensure_page(cookies, expires, account_id=account_id)
        if thread_url:
            self.page.send("Page.navigate", {"url": thread_url})
            for _ in range(100):
                time.sleep(0.15)
                if self.page.js("document.readyState==='complete' && !!document.querySelector('textarea')"):
                    break
            else:
                raise MuseGenerationError("Không mở lại được hội thoại Muse")
            if not self._wait_ws_ready(self.page):
                raise MuseGenerationError("Không kết nối được hội thoại Muse; chưa gửi prompt")
            self._scroll_bottom()
            # Let the resumed transcript mount before taking the baseline; an
            # old answer arriving late must not count as the new reply.
            previous, stable = None, 0
            for _ in range(30):
                time.sleep(0.2)
                state = self._poll_chat()
                stable = stable + 1 if state == previous else 0
                previous = state
                if stable >= 4: break
            if previous and previous[2]:
                raise MuseGenerationError("Hội thoại Muse vẫn đang trả lời yêu cầu trước")
            actual = str(self.page.js('window.location.href') or '').split('?')[0].rstrip('/')
            if actual != thread_url.split('?')[0].rstrip('/'):
                raise MuseGenerationError("Muse không mở đúng hội thoại cũ; tin nhắn mới chưa được gửi")
        else:
            self.reset_thread(for_chat=on_result is None)
        baseline_media = {a.get(k) for a in self.attachments() for k in ("src", "vSrc", "iSrc") if a.get(k)} if on_result else set()
        base_agent, base_text, _ = self._poll_chat()
        if self._send(prompt) not in ("clicked", "enter-sent"):
            raise MuseGenerationError("Muse chưa xác nhận gửi tin nhắn")
        started = time.monotonic()
        deadline = started + timeout
        latest = ""
        completed_since = None
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise MuseGenerationError("Đã ngắt kết nối khi Muse còn đang trả lời")
            time.sleep(0.15)
            count, current, has_stop = self._poll_chat()
            now = time.monotonic()
            if now - started > 30 and count <= base_agent:
                tail = self.page.js("document.body ? (document.body.innerText || '').slice(-1200) : ''") or ''
                if re.search(r"Still sending|Delivery not confirmed|正在发送|未确认送达", tail, re.I):
                    raise MuseGenerationError("Muse chưa xác nhận nhận tin nhắn (Still sending / Delivery not confirmed). Kiểm tra hội thoại Muse trước khi gửi lại.")
            fresh_media = []
            waiting_media = False
            if on_result:
                for att in self.attachments():
                    kind = "video" if att.get("hasVideo") or "video" in att.get("tid", "") else "image"
                    src = (att.get("vSrc") if kind == "video" else att.get("src")) or ""
                    if kind == "video" and not src:
                        if att.get("src") not in baseline_media: waiting_media = True
                        continue
                    if src and src not in baseline_media: fresh_media.append((att, kind))
                # A success message is not a delivered file. Wait for the real attachment.
                if not fresh_media and self._media_status_pending(current or ''):
                    waiting_media = True
                if not fresh_media and re.search(r"(?:^|\n)\s*(?:I(?:'m| am)\s+)?(?:generating|rendering|processing)\s+(?:your\s+)?(?:video|image)|(?:^|\n)\s*đang\s+(?:tạo|dựng).{0,30}(?:video|ảnh)|正在生成|生成中", current or '', re.I):
                    waiting_media = True
            if waiting_media or ((not current or (count <= base_agent and current == base_text)) and not fresh_media):
                completed_since = None
                continue
            if current != latest:
                latest = current
                completed_since = None
            if has_stop:
                # Thinking/tool calls can pause text for far longer than 5 seconds.
                completed_since = None
                continue
            if completed_since is None:
                completed_since = now
            elif now - completed_since >= 2.0:
                if on_result:
                    results = []
                    errors = []
                    for att, kind in fresh_media:
                        try: results.append(self.retrieve_attachment(att, kind))
                        except Exception as exc: errors.append(str(exc))
                    on_result({"media": results, "media_errors": errors,
                               "thread_url": self.page.js("window.location.href")})
                yield latest
                return
        stage = "chưa bắt đầu trả lời" if not latest else "chưa hoàn tất câu trả lời"
        raise MuseGenerationError(f"Hết thời gian chờ chat {timeout} giây; Muse {stage}. Không gửi nội dung dở dang.")

    def chat(self, cookies: dict, prompt: str, expires: dict | None = None,
             timeout: int | None = None, account_id: str | None = None) -> str:
        """发一条消息，返回完整回复文本（非流式）。"""
        out = ""
        for chunk in self.chat_stream(cookies, prompt, expires, timeout, account_id=account_id):
            out += chunk
        return out

    def extract_bytes(self, src: str, expect: str = "image", retries: int = 4):
        last = "未知"
        for _ in range(retries):
            raw = self.page.js(self._EXTRACT_JS % (json.dumps(src), json.dumps(expect)),
                               await_promise=True, timeout=600)
            try:
                info = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                info = {"ok": False, "err": f"解析失败 {str(raw)[:150]}"}
            if info.get("ok"):
                data = base64.b64decode(info["b64"])
                if not data:
                    last = "Tệp kết quả rỗng"
                    continue
                if expect == "video" and not self._is_video_bytes(data):
                    last = "Nguồn video trả về dữ liệu không phải MP4/WebM"
                    continue
                return data, info.get("mime", ""), info.get("url", "")
            last = info.get("err", "未知")
            time.sleep(2)
        raise MuseGenerationError(f"未能取回生成结果: {last}")

    @staticmethod
    def _is_video_bytes(data: bytes) -> bool:
        # MP4/QuickTime ISO BMFF or WebM/Matroska; reject HTML and image posters.
        return (len(data) >= 12 and data[4:8] == b"ftyp") or data[:4] == b"\x1aE\xdf\xa3"

    # ---------------- 下载兜底 ----------------
    def _download_fallback(self, src: str, timeout: int = 180, expect: str = "image") -> str | None:
        # A dedicated folder and correct context prevent blocked incognito downloads
        # and accidentally returning a simultaneous task's output.
        folder = os.path.join(self.cfg.download_dir, "result_" + uuid.uuid4().hex)
        os.makedirs(folder, exist_ok=True)
        params = {"behavior": "allow", "downloadPath": folder}
        if self.current_context_id:
            params["browserContextId"] = self.current_context_id
        self.page.send("Browser.setDownloadBehavior", params)
        clicked = self.page.js(self._DOWNLOAD_JS % json.dumps(src))
        if clicked == "opened":
            for _ in range(20):
                time.sleep(0.25)
                clicked = self.page.js(self._DOWNLOAD_PANEL_JS % json.dumps(src))
                if clicked == "ok":
                    break
        if clicked != "ok":
            raise MuseGenerationError("Không tìm thấy nút Download của đúng video/ảnh đã chọn")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.5)
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if name.endswith((".crdownload", ".tmp")) or not os.path.isfile(path):
                    continue
                if os.path.getsize(path) <= 0:
                    continue
                if expect == "video":
                    with open(path, "rb") as f:
                        if not self._is_video_bytes(f.read(32)):
                            continue
                return path
        raise MuseGenerationError(f"Đã bấm Download nhưng chưa nhận được tệp sau {timeout} giây")

    _DOWNLOAD_JS = r"""
    (function(src){
      var media=[...document.querySelectorAll('video,img')]
        .find(m=>[m.currentSrc,m.src,m.getAttribute('src')].includes(src));
      var node=media && media.closest('[data-testid^="hatch-chat-attachment-presentation-"]');
      if(!node) return 'no-selected-result';
      node=node.closest('[class*="group/widget-presentation"]')||node;
      function label(x){return [x.getAttribute('aria-label'),x.title,x.innerText].filter(Boolean).join(' ');}
      function download(root){return [...root.querySelectorAll('button,[role=button],a[download]')]
        .find(x=>x.getClientRects().length && (/download|下载|保存|tải xuống/i.test(label(x))||x.hasAttribute('download')));}
      var b=download(node);
      if(b){b.click();return 'ok';}
      // A preview panel is safe only when it contains the selected source.
      var panel=media.closest('[role="dialog"],dialog,[data-testid*="panel"]');
      if(panel && (b=download(panel))){b.click();return 'ok';}
      window.__museDownloadSelection={src:src, panels:[...document.querySelectorAll('[role="dialog"],dialog')],
        buttons:[...document.querySelectorAll('button,[role=button]')].filter(x=>/download|下载|保存/i.test(label(x)))};
      var opener=[...node.querySelectorAll('button,[role=button],a')]
        .find(x=>x.getClientRects().length && /open.*video|open.*image|preview|打开|查看/i.test(label(x)));
      if(!opener) return 'no-result-download-or-preview';
      opener.click();return 'opened';
    })(%s)
    """

    _DOWNLOAD_PANEL_JS = r"""
    (function(src){
      var selected=window.__museDownloadSelection;
      if(!selected || selected.src!==src)return 'no-selection';
      function label(x){return [x.getAttribute('aria-label'),x.title,x.innerText].filter(Boolean).join(' ');}
      var panels=[...document.querySelectorAll('[role="dialog"],dialog')].filter(x=>x.getClientRects().length);
      var matching=panels.filter(x=>!selected.panels.includes(x)||[...x.querySelectorAll('video,img')]
        .some(m=>[m.src,m.currentSrc].includes(src)));
      var buttons=matching.flatMap(p=>[...p.querySelectorAll('button,[role=button],a[download]')]);
      // Muse's side preview is not always a role=dialog. Only accept a newly
      // rendered Download video button after opening this exact attachment.
      buttons.push(...[...document.querySelectorAll('button,[role=button]')]
        .filter(x=>!selected.buttons.includes(x)&&/download video|download image|下载视频|下载图片/i.test(label(x))));
      var b=buttons.find(x=>x.getClientRects().length && (/download|下载|保存|tải xuống/i.test(label(x))||x.hasAttribute('download')));
      if(!b)return 'waiting-panel';
      b.click();delete window.__museDownloadSelection;return 'ok';
    })(%s)
    """

    @staticmethod
    def _normalize_image(img: str) -> tuple[str, str]:
        """将各种形态的图片输入归一为 (base64_str, mime_type)。"""
        if not img:
            return "", "image/png"
        img = str(img).strip()
        if img.startswith("data:"):
            parts = img.split(",", 1)
            mime = "image/png"
            if ";" in parts[0]:
                mime = parts[0].split(";")[0].replace("data:", "").strip()
            return (parts[1].strip() if len(parts) > 1 else ""), mime
        if img.startswith("http://") or img.startswith("https://"):
            try:
                req = urllib.request.Request(img, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    mime = resp.headers.get_content_type() or "image/png"
                    return base64.b64encode(data).decode("ascii"), mime
            except Exception as e:
                log.warning("下载远程参考图失败: %s", e)
                return "", "image/png"
        if os.path.isfile(img):
            try:
                with open(img, "rb") as f:
                    data = f.read()
                    mime = mimetypes.guess_type(img)[0] or "image/png"
                    return base64.b64encode(data).decode("ascii"), mime
            except Exception as e:
                log.warning("读取本地参考图失败: %s", e)
                return "", "image/png"
        return img, "image/png"

    def _clear_attachments(self):
        """清除聊天输入框里遗留的附件缩略图（兼容中/英文 UI）。"""
        try:
            self.page.js(
                "(function(){"
                "var btns=Array.from(document.querySelectorAll('button[aria-label]')).filter(function(b){"
                "var al=b.getAttribute('aria-label')||'';"
                "return /remove\\s*attachment|移除附件|删除附件|移除|删除/i.test(al);"
                "});"
                "btns.forEach(function(b){b.click();});"
                "Array.from(document.querySelectorAll('input[type=\"file\"]')).forEach(function(inp){inp.value='';});"
                "return btns.length;"
                "})()")
            time.sleep(0.3)
        except Exception:
            pass

    def _attach_image(self, image_data: str | list[str]):
        """将参考图通过 DataTransfer 附加到输入框，杜绝使用旧历史图片。"""
        if not image_data:
            return
        images = image_data if isinstance(image_data, list) else [image_data]
        if not 1 <= len(images) <= 16:
            raise MuseGenerationError("Chọn tối đa 8 ảnh và 8 tài liệu")
        payload = []
        for image in images:
            if isinstance(image,dict) and 'data' in image:
                from reference_files import validate_files, MIMES
                validate_files([image])
                payload.append({'b64':image['data'].partition(',')[2], 'mime':MIMES[__import__('pathlib').Path(image['name']).suffix.lower()], 'name':image['name']})
                continue
            b64, mime = self._normalize_image(image)
            if not b64:
                raise MuseGenerationError("Không đọc được ảnh mẫu; đã dừng tạo video")
            payload.append({'b64': b64, 'mime': mime})

        self._clear_attachments()

        _INJECT_JS = """
        (function(images) {
            try {
                var files = [];
                for (var index = 0; index < images.length; index++) {
                var b64 = images[index].b64, mime = images[index].mime;
                var byteChars = atob(b64);
                var byteNumbers = new Array(byteChars.length);
                for (var i = 0; i < byteChars.length; i++) {
                    byteNumbers[i] = byteChars.charCodeAt(i);
                }
                var byteArray = new Uint8Array(byteNumbers);
                var blob = new Blob([byteArray], {type: mime});
                var ext = mime.split('/')[1] || 'png';
                if (ext === 'jpeg') ext = 'jpg';
                var file = new File([blob], images[index].name || ('reference_' + (index + 1) + '.' + ext), {type: mime});
                files.push(file);
            }
                // 现网 composer 里可能有多个 file input；优先取位于 composer 内的那个
                var inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                if (!inputs.length) return JSON.stringify({ok: false, err: 'no-file-input'});
                var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
                var composer = ov ? ov.closest('form') : null;
                var input = null;
                if (composer) {
                    input = inputs.find(function(x){ return composer.contains(x); }) || null;
                }
                if (!input) {
                    // 退而求其次：取不在对话气泡里的那个 input
                    input = inputs.find(function(x){
                        return !x.closest('[class*=chat-user-bubble], [class*="group/msg"]');
                    }) || inputs[0];
                }
                // 有些版本 accept 为空，补上 image/* 让页面接受本次 File
                input.removeAttribute('accept');
                var dt = new DataTransfer();
                if (files.length > 1) input.multiple = true;
                files.forEach(function(file){ dt.items.add(file); });
                input.files = dt.files;
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.dispatchEvent(new Event('input', {bubbles: true}));
                return JSON.stringify({ok: true});
            } catch(e) {
                return JSON.stringify({ok: false, err: String(e)});
            }
        })(%s)
        """
        try:
            raw_res = self.page.js(_INJECT_JS % json.dumps(payload))
            res_obj = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
            if not res_obj.get("ok"):
                raise MuseGenerationError("附加参考图失败，已停止生成")
        except Exception as e:
            raise MuseGenerationError("附加参考图失败，已停止生成") from e

        # 等待输入框附件确认；历史图片不能证明本次上传成功。
        # 现网 UI 已切中文：确认按钮 aria-label = "附加文件"/"移除附件"；
        # 同时兼容英文（Remove attachment / Attach file），并以 composer 内缩略图兜底。
        deadline = time.time() + (60.0 if len(images) > 1 else 15.0)
        while time.time() < deadline:
            has_attached = self.page.js(
                """(function(){
                var ov = document.querySelector('[data-testid="hatch-composer-placeholder-overlay"]');
                var composer = ov ? (ov.closest('form') || ov.parentElement.parentElement.parentElement) : null;
                if (!composer) return 0;
                var remove = Array.from(composer.querySelectorAll('button[aria-label]')).filter(function(b){
                    return /remove\\s*attachment|移除附件|删除附件/i.test(b.getAttribute('aria-label') || '');
                }).length;
                var thumbs = composer.querySelectorAll('img, video').length;
                return Math.max(remove, thumbs);
                })()"""
            )
            if isinstance(has_attached, (int, float)) and has_attached >= len(images):
                break
            time.sleep(0.3)
        else:
            raise MuseGenerationError(f"Muse chưa xác nhận đủ {len(images)} tệp đính kèm; đã dừng trước khi gửi prompt")
        time.sleep(0.5)
    # ---------------- 主流程 ----------------
    def generate(self, cookies: dict, prompt: str, expect: str = "image",
                 timeout: int = 240, expires: dict | None = None, account_id: str | None = None,
                 on_progress=None, reference_image: str | list[str] | None = None,
                 stop_event=None, thread_url=None, on_sent=None) -> dict:
        self.ensure_page(cookies, expires, account_id=account_id)
        if thread_url:
            self.page.send("Page.navigate", {"url": thread_url})
            for _ in range(100):
                time.sleep(0.15)
                if self.page.js("document.readyState==='complete' && !!document.querySelector('textarea')"): break
            else: raise MuseGenerationError("Không mở lại được hội thoại chuỗi cảnh")
            if not self._wait_ws_ready(self.page):
                raise MuseGenerationError("Không kết nối được hội thoại Muse; chưa gửi prompt")
            time.sleep(1)
            if str(self.page.js('window.location.href') or '').split('?')[0].rstrip('/') != thread_url.split('?')[0].rstrip('/'):
                raise MuseGenerationError("Muse không mở đúng hội thoại chuỗi; chưa gửi cảnh mới")
            if self._poll_chat()[2]: raise MuseGenerationError("Muse vẫn đang xử lý cảnh trước; chưa gửi cảnh mới")
        else:
            self.reset_thread(for_chat=False)
        self._scroll_bottom()
        if reference_image:
            self._attach_image(reference_image)
        else:
            self._clear_attachments()
        atts_before = self.attachments()
        baseline_sources = {a.get(k) for a in atts_before for k in ("src", "vSrc", "iSrc") if a.get(k)}
        base = atts_before[-1] if atts_before else {}
        baseline_src = base.get("src") or ""
        base_agent_cnt = self._agent_count()
        if self._send(prompt) not in ("clicked", "enter-sent"):
            raise MuseGenerationError("提示词发送未确认，已停止生成")
        if on_sent: on_sent()
        att = self._wait_attachment(
            baseline_src, timeout, expect, on_progress=on_progress,
            base_agent_cnt=base_agent_cnt, base_att_cnt=len(atts_before),
            stop_event=stop_event, baseline_sources=baseline_sources
        )
        if not att:
            self._debug_dump("no-attachment")
            raise MuseGenerationError("等待生成超时，未出现新的生成结果")

        return self.retrieve_attachment(att, expect)

    def retrieve_attachment(self, att, expect):
        """Retrieve an existing result without generating it again."""
        os.makedirs(self.cfg.media_dir, exist_ok=True)
        data = mime = url = None
        extraction_error = ""
        selected_src = (att.get("vSrc") if expect == "video" else None) or att.get("src") or ""
        try:
            data, mime, url = self.extract_bytes(selected_src, expect=expect)
        except Exception as exc:  # noqa: BLE001
            extraction_error = str(exc)
            log.warning("Video/media extraction failed: %s", extraction_error)
            self._debug_dump("extract-fail")

        if data:
            ext = self._pick_ext(mime, url, expect)
            from media_names import reserve
            target, stream = reserve(self.cfg.media_dir, expect, ext)
            dst, name = str(target), target.name
            with stream as f:
                f.write(data)
            kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
            if expect == "video" and kind != "video":
                raise MuseGenerationError(f"期望生成视频，但提取成品类型为图片 ({ext})，拒绝以图片冒充视频交付")
            return {"path": dst, "filename": name, "size": len(data), "ext": ext, "mime": mime,
                    "kind": kind,
                    "via": "blob", "attachment": att.get("tid"),
                    "w": att.get("w"), "h": att.get("h")}

        try:
            path = self._download_fallback(selected_src, expect=expect)
        except Exception as exc:
            self._debug_dump("download-fail")
            diagnostic = {"stage": "retrieval", "expect": expect,
                          "extract_error": extraction_error, "download_error": str(exc)}
            with open(os.path.join(self.cfg.data_dir, "debug-retrieval-error.json"), "w", encoding="utf-8") as f:
                json.dump(diagnostic, f, ensure_ascii=False, indent=2)
            raise MuseGenerationError(f"Muse đã tạo kết quả nhưng tải tệp thất bại: {exc}; lấy dữ liệu: {extraction_error}") from exc
        if not path:
            raise MuseGenerationError("Muse đã tạo kết quả nhưng chưa lấy được tệp")
        ext = os.path.splitext(path)[1].lower() or ".bin"
        from media_names import reserve
        target, stream = reserve(self.cfg.media_dir, expect, ext)
        dst, name = str(target), target.name
        try:
            with stream as output, open(path, 'rb') as source: shutil.copyfileobj(source, output)
        except Exception:
            target.unlink(missing_ok=True); raise
        os.unlink(path)
        kind = "video" if ext in (".mp4", ".webm", ".mov") else "image"
        if expect == "video" and kind != "video":
            raise MuseGenerationError(f"期望生成视频，但下载成品类型为图片 ({ext})，拒绝以图片冒充视频交付")
        return {"path": dst, "filename": name, "size": os.path.getsize(dst), "ext": ext, "mime": "",
                "kind": kind,
                "via": "download", "attachment": att.get("tid"),
                "w": att.get("w"), "h": att.get("h")}

    @staticmethod
    def _pick_ext(mime: str, url: str, expect: str) -> str:
        m = (mime or "").lower()
        for key, ext in (("mp4", ".mp4"), ("webm", ".webm"), ("png", ".png"),
                         ("jpeg", ".jpg"), ("jpg", ".jpg"), ("webp", ".webp"),
                         ("gif", ".gif")):
            if key in m:
                return ext
        for e in (".mp4", ".webm", ".png", ".jpg", ".webp"):
            if e in (url or "").lower():
                return e
        return ".mp4" if expect == "video" else ".png"

    def _debug_dump(self, tag: str):
        try:
            info = self.page.js(
                "JSON.stringify({atts:[...document.querySelectorAll('" + ATT_SEL + "')]"
                ".map(function(a){var m=a.querySelector('img,video');return {"
                "tid:a.getAttribute('data-testid'),"
                "src:m?(m.currentSrc||m.src||'').slice(0,60):''};}),"
                "buttons:[...document.querySelectorAll('button,[role=button]')]"
                ".filter(b=>b.offsetParent!==null)"
                ".map(b=>b.getAttribute('aria-label')||b.innerText.trim().slice(0,20))"
                ".filter(Boolean).slice(-40),"
                "tail:document.body.innerText.slice(-500)})")
            with open(os.path.join(self.cfg.data_dir, f"debug-{tag}.json"), "w",
                      encoding="utf-8") as f:
                f.write(str(info))
        except Exception:  # noqa: BLE001
            pass
