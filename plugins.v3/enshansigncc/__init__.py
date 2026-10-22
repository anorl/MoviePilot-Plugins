import json
import re
import time
from datetime import datetime, timedelta
from hashlib import md5
from threading import Lock
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.utilities import CryptoJsUtils


class enshansigncc(_PluginBase):
    plugin_name = "恩山无线论坛签到"
    plugin_desc = "恩山无线论坛每日签到，支持手工Cookie、CookieCloud及消息通知"
    plugin_icon = "https://raw.githubusercontent.com/anorl/MoviePilot-Plugins/main/icons/enshansigncc.ico"
    plugin_version = "1.0.3"
    plugin_author = "anorl"
    author_url = "https://github.com/anorl"
    plugin_config_prefix = "enshansigncc_"
    plugin_order = 1
    auth_level = 2
    _base_url = "https://www.right.com.cn/forum/"
    _sign_path = "plugin.php?id=erling_qd:action&action=sign"
    _sign_page = "erling_qd-sign_in.html"

    @staticmethod
    def _validate_cookies(cookies):
        auth_names = [name for name in cookies if name.endswith("_auth")]
        if not auth_names or not any(cookies[name] for name in auth_names):
            raise ValueError("缺少恩山登录Cookie（通常为TWcq_2132_auth），请登录www.right.com.cn后更新Cookie")
        valid = {}
        ignored = 0
        for name, value in cookies.items():
            if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name) and value.isascii() and not any(ord(c) < 32 or ord(c) == 127 for c in value):
                valid[name] = value
            elif name in auth_names:
                raise ValueError("恩山登录Cookie包含无法发送的字符，请重新复制或同步Cookie")
            else:
                ignored += 1
        if ignored:
            logger.warning(f"[enshansigncc] 已过滤{ignored}项格式异常的非必要Cookie，不输出名称或值")
        return valid

    def _get_cookies(self):
        source = str(self._config.get("cookie_source") or "cookiecloud").strip().lower()
        if source == "manual":
            raw = str(self._config.get("cookie") or "").strip()
            if raw.lower().startswith("cookie:"):
                raw = raw.partition(":")[2].strip()
            if not raw:
                raise ValueError("请先填写恩山无线论坛登录Cookie")
            cookies = {}
            for part in raw.split(";"):
                name, separator, value = part.strip().partition("=")
                if separator:
                    cookies[name.strip()] = value.strip()
            return self._validate_cookies(cookies)
        if source != "cookiecloud":
            raise ValueError("Cookie来源无效，请选择手工填写或CookieCloud")
        selected = {}
        now = time.time()
        for group, items in self._load_cookiecloud().items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                domain = str(item.get("domain") or group).lower().lstrip(".")
                if domain not in ("right.com.cn", "www.right.com.cn"):
                    continue
                if item.get("hostOnly") and domain != "www.right.com.cn":
                    continue
                path = str(item.get("path") or "/")
                if path != "/" and not "/forum/forum.php".startswith(path.rstrip("/") + "/"):
                    continue
                try:
                    expiry = float(item.get("expirationDate", item.get("expires", -1)) or -1)
                    if expiry > 10_000_000_000:
                        expiry /= 1000
                    if expiry > 0 and expiry <= now:
                        continue
                except (ValueError, TypeError):
                    continue
                name, value = str(item.get("name") or ""), str(item.get("value") or "")
                if name and value:
                    score = (len(path), len(domain), expiry)
                    if name not in selected or score > selected[name][0]:
                        selected[name] = (score, value)
        return self._validate_cookies({name: entry[1] for name, entry in selected.items()})

    @staticmethod
    def _is_verification(text):
        lower = text.lower()
        return any(marker in lower for marker in ("cf_app_waf", "waf-nc-h5-mask", "aliyunwaf", "cf-chl-", "challenge-platform", "验证您是真人"))

    def _request(self, session, method, path, stage, data=None):
        url = urljoin(self._base_url, path)
        original = urlsplit(url)
        for hop in range(3):
            response = session.request(method, url, data=data, timeout=25, allow_redirects=False,
                                       headers={"X-Requested-With": "XMLHttpRequest"} if method == "POST" else {})
            if response.status_code not in (301, 302, 307, 308):
                break
            location = response.headers.get("Location", "")
            try:
                target = urljoin(url, location)
                parts = urlsplit(target)
                allowed = (bool(location) and parts.scheme == "https" and parts.hostname in ("www.right.com.cn", "right.com.cn")
                           and parts.port in (None, 443) and parts.username is None and parts.password is None
                           and parts.path == original.path and parts.query == original.query and not parts.fragment)
            except ValueError:
                allowed = False
            if not allowed or hop == 2 or target == url:
                raise ValueError(f"{stage}遇到非预期重定向，请在浏览器检查登录或站点验证状态")
            response.close()
            url = target
        # 站点可能以HTTP 200返回滑块页，必须先识别防护，不能误报Cookie失效。
        text = response.text
        if self._is_verification(text):
            raise ValueError(f"{stage}触发站点验证，请在浏览器完成验证后重新同步Cookie；当前未判定登录态失效")
        if response.status_code != 200:
            raise ValueError(f"{stage}接口返回HTTP {response.status_code}")
        if response.encoding and response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"
        return response

    @staticmethod
    def _parse_login_page(text):
        soup = BeautifulSoup(text, "html.parser")
        logout = None
        for link in soup.find_all("a", href=True):
            if "action=logout" not in link["href"]:
                continue
            query = parse_qs(urlsplit(link["href"]).query)
            if query.get("action") == ["logout"] and query.get("mod") == ["logging"]:
                logout = link
                break
        if logout is None:
            raise ValueError("恩山页面未确认登录状态，请重新登录并更新Cookie或同步CookieCloud")
        field = soup.find("input", attrs={"name": "formhash"})
        token = str(field.get("value") or "") if field else ""
        if not token:
            token = (parse_qs(urlsplit(logout["href"]).query).get("formhash") or [""])[0]
        if not token:
            match = re.search(r"\b(?:FORMHASH|formhash)\s*[:=]\s*['\"]([a-zA-Z0-9]+)['\"]", text)
            token = match.group(1) if match else ""
        if not re.fullmatch(r"[a-zA-Z0-9]{4,64}", token):
            raise ValueError("已登录，但未获取到有效formhash，请检查恩山页面是否变化")
        user = soup.select_one("#um .vwmy a, .vwmy a")
        return token, user.get_text(" ", strip=True)[:80] if user else ""

    @staticmethod
    def _parse_sign_page(text):
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup.select("script, style, noscript"):
            tag.decompose()
        # 只认按钮/状态控件，不能把脚本中的成功文案、排行榜或历史天数当成今日签到状态。
        candidates = soup.select("button, a, [role='button'], .erqd-signin-btn, #signin-btn, #sign-btn")
        already = any(re.fullmatch(r"(?:今日|今天|本日)?(?:已(?:经)?签到|已完成签到|签到已完成)|明天再来", node.get_text("", strip=True)) for node in candidates)
        stats = {}
        if already:
            for field, selector in (("continuous_days", ".erqd-continuous-days"), ("total_days", ".erqd-total-days"), ("today_points", ".erqd-current-point")):
                node = soup.select_one(selector)
                if node:
                    match = re.fullmatch(r"\s*(\d+)\s*(?:天|积分|分)?\s*", node.get_text("", strip=True))
                    if match:
                        stats[field] = int(match.group(1))
        return already, stats

    def _query_signed(self, session):
        try:
            response = self._request(session, "GET", self._sign_page, "查询签到状态")
            already, stats = self._parse_sign_page(response.text)
            return stats if already else None
        except (ValueError, requests.RequestException):
            return None

    @staticmethod
    def _parse_result(payload):
        if not isinstance(payload, dict):
            raise ValueError("恩山签到接口响应格式异常")
        message = str(payload.get("message") or payload.get("msg") or "")
        already = bool(re.search(r"(?:今日|今天|本日).{0,4}已(?:经)?签到|已经签到过|请勿重复签到|您已(?:经)?签到", message))
        ok = payload.get("success") in (True, 1, "1", "true")
        if not already and not ok:
            if any(word in message for word in ("滑块", "验证码", "安全验证")):
                raise ValueError("恩山签到需要站点验证，请在浏览器完成验证后重试")
            if "formhash" in message.lower():
                raise ValueError("恩山拒绝formhash，请刷新登录页面后重试")
            if "登录" in message:
                raise ValueError("恩山签到接口要求重新登录，请更新Cookie")
            raise ValueError("恩山未确认签到成功，请在网页检查账号或签到状态")
        result = {"success": True, "status": "今日已签到" if already else "签到成功"}
        for field in ("continuous_days", "total_days"):
            value = payload.get(field)
            if not isinstance(value, bool) and re.fullmatch(r"\d{1,8}", str(value)):
                result[field] = int(value)
        return result

    def _account_info(self, session):
        info = {}
        try:
            response = self._request(session, "GET", "home.php?mod=spacecp&ac=credit&showcredit=1", "读取账号积分")
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup.select("script, style, noscript"):
                tag.decompose()
            text = soup.get_text(" ", strip=True)
            for field, label in (("coin", "恩山币"), ("credits", "积分")):
                match = re.search(re.escape(label) + r"\s*[:：]\s*([+-]?\d+(?:\.\d+)?)", text)
                if match:
                    info[field] = match.group(1)
        except Exception:
            pass
        return info

    def _run_sign(self, session):
        page = self._request(session, "GET", "forum.php", "读取登录页面")
        formhash, username = self._parse_login_page(page.text)
        if self._config.get("browser_fallback", True):
            # 在任何提交前探测签到页防护，才可以安全切换浏览器而不重复POST。
            sign_page = self._request(session, "GET", self._sign_page, "读取签到页面")
            formhash, page_user = self._parse_login_page(sign_page.text)
            username = page_user or username
            already, stats = self._parse_sign_page(sign_page.text)
            if already:
                return {"success": True, "status": "今日已签到", "username": username, **stats, **self._account_info(session)}
        try:
            response = self._request(session, "POST", self._sign_path, "提交签到", {"formhash": formhash})
        except (requests.RequestException, ValueError) as error:
            confirmed = self._query_signed(session)
            if confirmed is not None:
                result = {"success": True, "status": "今日已签到（请求异常后查询确认）", **confirmed}
            else:
                # 网络异常正文可能包含凭据；只保留插件主动生成的固定错误提示。
                hint = str(error) if isinstance(error, ValueError) and not isinstance(error, requests.RequestException) else "网络请求异常"
                return {"success": False, "unknown": True, "username": username,
                        "status": f"签到结果未确认：{hint}；本次不重复提交"}
        else:
            try:
                payload = response.json()
            except ValueError:
                confirmed = self._query_signed(session)
                if confirmed is None:
                    return {"success": False, "unknown": True, "username": username,
                            "status": "签到接口未返回JSON，结果未确认；本次不重复提交"}
                result = {"success": True, "status": "今日已签到（查询确认）", **confirmed}
            else:
                result = self._parse_result(payload)
        result["username"] = username
        result.update(self._account_info(session))
        return result

    def _launch_browser(self, cookies):
        """惰性调用MP稳定浏览器接口，不自行安装或启动另一套浏览器实现。"""
        from app.sdk.browser import launch_browser_context
        mode = "有界面" if self._config.get("browser_headed", False) else "无头"
        logger.info(f"[enshansigncc] MP浏览器模式：{mode}")
        options = {"headless": not self._config.get("browser_headed", False), "locale": "zh-CN", "timezone": settings.TZ,
                   "viewport": {"width": 1280, "height": 900},
                   "storage_state": {"cookies": [{"name": name, "value": value, "domain": "www.right.com.cn", "path": "/",
                                                    "secure": True, "httpOnly": False, "sameSite": "Lax"}
                                                   for name, value in cookies.items()], "origins": []}}
        if self._config.get("use_proxy"):
            proxy = getattr(settings, "PROXY", None)
            if isinstance(proxy, dict):
                proxy = proxy.get("https") or proxy.get("http")
            elif getattr(proxy, "host", None):
                proxy = proxy.host
            if proxy:
                options["proxy"] = proxy
        return launch_browser_context(**options)

    @staticmethod
    def _try_aliyun_slider(page):
        """仅操作已实测的阿里云直线滑块，不识别未知图片或拼图题型。"""
        pressed = False
        try:
            handle = page.query_selector("#aliyunCaptcha-sliding-slider")
            track = page.query_selector("#aliyunCaptcha-sliding-body")
            if handle is None or track is None:
                return False
            knob, rail = handle.bounding_box(), track.bounding_box()
            if not isinstance(knob, dict) or not isinstance(rail, dict):
                return False
            if knob["width"] <= 0 or knob["height"] <= 0 or rail["width"] <= knob["width"]:
                return False
            start_x = knob["x"] + knob["width"] / 2
            center_y = knob["y"] + knob["height"] / 2
            end_x = rail["x"] + rail["width"] - knob["width"] / 2
            if end_x <= start_x:
                return False
            # 每次读取实时控件位置，避免视口尺寸变化导致拖动落在轨道外。
            page.mouse.move(start_x, center_y)
            page.mouse.down()
            pressed = True
            page.wait_for_timeout(150)
            # steps仅表示事件数量，不保证耗时。分段等待，让控件有时间处理移动。
            for step in range(1, 33):
                progress = step / 32
                fraction = progress * progress * (3 - 2 * progress)
                page.mouse.move(start_x + (end_x - start_x) * fraction, center_y)
                page.wait_for_timeout(35)
            page.wait_for_timeout(120)
            page.mouse.up()
            pressed = False
            return True
        except Exception:
            logger.warning("[enshansigncc] 滑块操作未完成，保留人工验证提示")
            return False
        finally:
            if pressed:
                try:
                    page.mouse.up()
                except Exception:
                    pass

    def _browser_is_verification(self, html):
        """登录页仍有验证控件时继续等待；仅残留脚本不覆盖已确认的登录状态。"""
        if not self._is_verification(html):
            return False
        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select("script, style, noscript"):
            node.decompose()
        text = soup.get_text(" ", strip=True)
        if soup.select_one('[id^="aliyunCaptcha"], #waf-nc-h5-mask, [id^="cf-chl-"]') or "验证您是真人" in text:
            return True
        try:
            self._parse_login_page(html)
        except ValueError:
            return True
        return False

    @staticmethod
    def _verification_detail(html):
        """只输出预设状态，不回显页面原文、Request ID、URL或令牌。"""
        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select("script, style, noscript"):
            node.decompose()
        label = soup.select_one("#aliyunCaptcha-sliding-text")
        text = label.get_text(" ", strip=True) if label else soup.get_text(" ", strip=True)
        if re.search(r"验证失败|验证未通过|请重试|重新验证|刷新重试", text):
            return "页面提示验证失败或需要重试"
        if re.search(r"验证成功|验证通过", text):
            return "页面提示验证通过，但尚未确认论坛登录页面"
        if "验证中" in text:
            return "页面停留在验证中"
        if re.search(r"按住滑块|拖动.*最右", text):
            return "页面仍显示拖动滑块提示"
        if re.search(r"拼图|图形验证|依次点击|图片验证|选择.*图片", text):
            return "页面出现尚未适配的图形验证"
        return "页面仍有防护标记，未识别到明确验证结果"

    _verify_codes = {
        "T001": "客户端验证通过", "T002": "触发限流容灾放行", "T005": "测试模式放行", "T006": "白名单放行",
        "F001": "风险策略拒绝", "F004": "测试模式拒绝", "F008": "验证数据重复提交",
        "F009": "检测到虚拟设备环境", "F010": "IP访问频率受限", "F011": "设备访问频率受限",
        "F014": "初始化记录缺失或过期", "F015": "验证交互未通过", "F016": "URL校验未通过",
        "F017": "协议或参数异常", "F022": "加密模式校验失败", "F024": "检测到模拟点击或滑动",
        "F025": "智能风险检测未通过",
    }

    def _watch_verification(self, page, state):
        """被动读取官方验证响应；不打印URL、请求体、令牌或服务端原文。"""
        def on_response(response):
            try:
                host = urlsplit(response.url).hostname or ""
                if host not in ("captcha.cn-shanghai.aliyuncs.com", "captcha.cn-singapore.aliyuncs.com"):
                    return
                payload = response.json()
                result = payload.get("Result") if isinstance(payload, dict) else None
                code = result.get("VerifyCode") if isinstance(result, dict) else None
                if isinstance(code, str) and code in self._verify_codes:
                    state["verify_code"] = code
                    logger.info(f"[enshansigncc] 阿里云验证结果：{code}（{self._verify_codes[code]}）")
            except Exception:
                # 响应不是JSON或导航已结束时不影响页面验证流程。
                pass
        try:
            page.on("response", on_response)
        except Exception:
            logger.info("[enshansigncc] 当前MP浏览器不支持验证响应监听，继续使用页面状态判断")

    def _browser_load(self, page, path, challenge_state=None):
        """先执行站点脚本，再至多尝试一次已适配滑块，并检查真实页面状态。"""
        if challenge_state is None:
            challenge_state = {"slider_attempted": False}
        response = page.goto(urljoin(self._base_url, path), wait_until="domcontentloaded", timeout=30000)
        html = ""
        for _ in range(6):
            page.wait_for_timeout(1000)
            try:
                html = page.content()
            except Exception:
                continue
            if html and not self._browser_is_verification(html):
                break
        if not html:
            raise ValueError("浏览器加载恩山页面失败，请检查网络或站点状态")
        if self._browser_is_verification(html) and self._config.get("auto_slider", True) and not challenge_state["slider_attempted"]:
            challenge_state["slider_attempted"] = True
            if self._try_aliyun_slider(page):
                logger.info("[enshansigncc] 已尝试阿里云滑块，等待验证后的页面状态")
                for _ in range(20):
                    page.wait_for_timeout(1000)
                    try:
                        html = page.content()
                    except Exception:
                        # 验证成功可能触发页面跳转，导航过程中暂时不能读取DOM。
                        continue
                    if html and not self._browser_is_verification(html):
                        try:
                            self._parse_login_page(html)
                        except ValueError:
                            # 短暂的空白页或导航中间页不能作为验证通过的依据。
                            continue
                        break
        parts = urlsplit(page.url)
        if parts.scheme != "https" or parts.hostname not in ("www.right.com.cn", "right.com.cn") or not parts.path.startswith("/forum/"):
            raise ValueError("浏览器未停留在恩山论坛，请检查登录或站点验证状态")
        if self._browser_is_verification(html):
            detail = self._verification_detail(html)
            code = challenge_state.get("verify_code")
            if code in self._verify_codes:
                detail += f"；{code}（{self._verify_codes[code]}）"
            logger.info(f"[enshansigncc] 验证页面诊断：{detail}")
            raise ValueError(f"仍需人工完成恩山真人验证：{detail}；可尝试MP有界面浏览器；人工验证应尽量使用与MP相同的网络出口，同步Cookie不保证解除验证；当前未判定Cookie失效")
        if response is not None and response.status != 200:
            # 初始403验证页可能已跳转到正常登录页，不能再用旧响应覆盖当前状态。
            if response.status != 403 or not challenge_state["slider_attempted"]:
                raise ValueError("浏览器加载恩山页面失败，请检查网络或站点状态")
            self._parse_login_page(html)
        return html

    def _run_browser(self, cookies):
        context = None
        submitted = False
        username = ""
        challenge_state = {"slider_attempted": False}
        try:
            context = self._launch_browser(cookies)
            page = context.new_page()
            self._watch_verification(page, challenge_state)
            html = self._browser_load(page, self._sign_page, challenge_state)
            formhash, username = self._parse_login_page(html)
            already, stats = self._parse_sign_page(html)
            if already:
                result = {"success": True, "status": "今日已签到", **stats}
            else:
                # 在同一浏览器会话中提交，保留站点脚本产生的Cookie与浏览器状态。
                submitted = True
                reply = page.evaluate("""async ({formhash, path}) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 25000);
                    try {
                        const response = await fetch(path, {
                            method: 'POST', credentials: 'same-origin', redirect: 'manual',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                      'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'},
                            body: new URLSearchParams({formhash}).toString(), signal: controller.signal
                        });
                        let data = null;
                        try { data = JSON.parse(await response.text()); } catch (_) {}
                        return {status: response.status, data};
                    } catch (_) { return {network_error: true}; }
                    finally { clearTimeout(timer); }
                }""", {"formhash": formhash, "path": self._sign_path})
                if isinstance(reply, dict) and reply.get("status") == 200 and isinstance(reply.get("data"), dict):
                    result = self._parse_result(reply["data"])
                else:
                    # 浏览器请求超时也只做只读确认，绝不再提交一次。
                    html = self._browser_load(page, self._sign_page, challenge_state)
                    confirmed, stats = self._parse_sign_page(html)
                    if not confirmed:
                        return {"success": False, "unknown": True, "username": username,
                                "status": "浏览器签到结果未确认；本次不重复提交，请在网页检查"}
                    result = {"success": True, "status": "今日已签到（浏览器查询确认）", **stats}
            result["username"] = username
            # 使用已通过验证的同一浏览器读取账号数据；失败不覆盖已确认的签到。
            try:
                html = self._browser_load(page, "home.php?mod=spacecp&ac=credit&showcredit=1", challenge_state)
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup.select("script, style, noscript"):
                    tag.decompose()
                text = soup.get_text(" ", strip=True)
                for field, label in (("coin", "恩山币"), ("credits", "积分")):
                    match = re.search(re.escape(label) + r"\s*[:：]\s*([+-]?\d+(?:\.\d+)?)", text)
                    if match:
                        result[field] = match.group(1)
            except Exception:
                pass
            return result
        except ImportError:
            return {"success": False, "status": "MP浏览器接口或依赖不可用，请检查MP浏览器环境；也可在浏览器完成验证后同步Cookie"}
        except ValueError as error:
            message = str(error)
            if not message.startswith(("仍需人工完成", "浏览器未停留", "浏览器加载", "恩山页面未确认", "已登录，但未获取", "恩山签到", "恩山拒绝", "恩山未确认")):
                message = "浏览器执行失败，请检查MP浏览器环境；不输出异常中的URL或凭据"
            return {"success": False, "unknown": submitted, "username": username,
                    "status": ("浏览器签到结果未确认：" if submitted else "") + message}
        except Exception:
            return {"success": False, "unknown": submitted,
                    "status": "浏览器签到结果未确认，本次不重复提交" if submitted else "无法完成浏览器验证，请检查MP浏览器环境；有界面模式需MP显示环境支持"}
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    logger.warning("[enshansigncc] 关闭浏览器上下文失败")

    def _notify_result(self, record):
        if not self._config.get("notify", True):
            return
        icon, label = ("✅", "成功") if record["success"] else (("⚠️", "结果未确认") if record.get("unknown") else ("❌", "失败"))
        lines = ["📢 执行结果", "━━━━━━━━━━", f"🕐 时间：{record['date']}", f"✨ 状态：{record['status']}"]
        for field, prefix, unit in (("username", "👤 用户", ""), ("continuous_days", "📆 连续签到", " 天"),
                                    ("total_days", "🗓️ 累计签到", " 天"), ("today_points", "🎁 今日积分", ""),
                                    ("coin", "💰 恩山币", ""), ("credits", "📊 积分", "")):
            if field in record and record[field] != "":
                lines.append(f"{prefix}：{record[field]}{unit}")
        lines.append("━━━━━━━━━━")
        try:
            self.post_message(mtype=NotificationType.SiteMessage, title=f"【{icon} {self.plugin_name}{label}】", text="\n".join(lines))
        except Exception:
            logger.warning("[enshansigncc] 发送通知失败，本次签到结果不变")

    def sign(self):
        if not self._lock.acquire(blocking=False):
            logger.info("[enshansigncc] 已有签到任务运行，跳过重复触发")
            return
        try:
            record = {"date": datetime.now(ZoneInfo(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")}
            try:
                cookies = self._get_cookies()
                with requests.Session() as session:
                    session.trust_env = False
                    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
                                            "Referer": self._base_url, "Accept": "application/json, text/javascript, text/html, */*; q=0.01"})
                    for name, value in cookies.items():
                        session.cookies.set(name, value, domain="www.right.com.cn", path="/")
                    if self._config.get("use_proxy"):
                        proxies = getattr(settings, "PROXY", None)
                        if isinstance(proxies, str):
                            proxies = {"http": proxies, "https": proxies}
                        elif getattr(proxies, "host", None):
                            proxies = {"http": proxies.host, "https": proxies.host}
                        if proxies:
                            session.proxies.update(proxies)
                    try:
                        result = self._run_sign(session)
                    except ValueError as error:
                        # 只在尚未POST的登录页面遇到防护时切换，避免重复签到。
                        protected = any(str(error).startswith(stage + "触发站点验证") or str(error) == stage + "接口返回HTTP 403"
                                        for stage in ("读取登录页面", "读取签到页面"))
                        if not protected or not self._config.get("browser_fallback", True):
                            raise
                        logger.info("[enshansigncc] 登录页面触发防护，使用MP浏览器加载站点脚本")
                        result = self._run_browser(cookies)
                    record.update(result)
            except requests.RequestException:
                record.update(success=False, status="获取恩山登录页面时网络异常，未提交签到")
            except ValueError as error:
                record.update(success=False, status=str(error))
            except Exception:
                record.update(success=False, status="签到执行异常，请检查插件依赖及MoviePilot配置")
            try:
                history = self.get_data("sign_history") or []
                self.save_data(key="sign_history", value=(history + [record])[-100:])
            except Exception:
                logger.warning("[enshansigncc] 保存历史失败，本次签到结果不变")
            logger.info(f"[enshansigncc] {record['status']}")
            self._notify_result(record)
            return record
        finally:
            self._lock.release()

    def get_form(self):
        switches = [("enabled", "启用插件"), ("notify", "发送通知"), ("onlyonce", "立即运行一次"), ("use_proxy", "使用MoviePilot系统代理")]
        return [{"component": "VForm", "content": [
            {"component": "VRow", "props": {"class": "mb-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                    {"component": "VSwitch", "props": {"model": key, "label": label}}]} for key, label in switches]},
            {"component": "VTextField", "props": {"model": "cron", "label": "定时任务(cron)", "class": "mb-4", "placeholder": "0 9 * * *"}},
            {"component": "VSwitch", "props": {"model": "browser_fallback", "label": "站点验证时使用MP浏览器", "class": "mb-4"}},
            {"component": "VSwitch", "props": {"model": "browser_headed", "label": "使用有界面浏览器（需要MP显示环境）", "class": "mb-4"}},
            {"component": "VSwitch", "props": {"model": "auto_slider", "label": "自动尝试阿里云滑块（每次运行一次）", "class": "mb-4"}},
            {"component": "VSelect", "props": {"model": "cookie_source", "label": "Cookie来源", "class": "mb-4", "items": [
                {"title": "手工填写", "value": "manual"}, {"title": "MoviePilot CookieCloud", "value": "cookiecloud"}]}},
            {"component": "VTextField", "props": {"model": "cookie", "label": "恩山无线论坛Cookie", "type": "password", "class": "mb-4",
                "show": "{{ cookie_source === 'manual' }}", "hint": "粘贴www.right.com.cn登录后的完整Cookie，包含TWcq_2132_auth等登录字段。", "persistent-hint": True}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                "text": "支持手工Cookie或MP本地/远端CookieCloud。默认每天9点签到，保留100条历史，通知通过MP站点消息投递。默认无头模式；验证失败可启用有界面模式，由MP管理显示资源，并非打开本机浏览器。每次仅尝试一次直线滑块，未知图形题型需人工处理。人工验证应尽量与MP使用同一网络出口，同步Cookie不保证解除验证。不会重复提交签到。"}},
        ]}], {"enabled": False, "notify": True, "onlyonce": False, "cron": "0 9 * * *",
              "use_proxy": False, "cookie_source": "cookiecloud", "cookie": "", "browser_fallback": True, "auto_slider": True, "browser_headed": False}

    def get_page(self):
        history = self.get_data("sign_history") or []
        if not history:
            return [{"component": "VAlert", "props": {"type": "info", "text": "暂无恩山无线论坛签到记录"}}]
        return [{"component": "VTable", "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": label} for label in ("时间", "结果", "用户", "连续签到", "恩山币", "积分")]}]},
            {"component": "tbody", "content": [{"component": "tr", "content": [
                {"component": "td", "text": str(record.get(field, "-"))} for field in ("date", "status", "username", "continuous_days", "coin", "credits")]
            } for record in reversed(history)]},
        ]}]

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._config = {
            "enabled": False, "notify": True, "onlyonce": False,
            "cron": "0 9 * * *", "use_proxy": False,
            "cookie_source": "cookiecloud", "cookie": "", "browser_fallback": True, "auto_slider": True, "browser_headed": False,
        }
        self._config.update(config or {})
        self._lock = getattr(self, "_lock", Lock())
        if self._config.get("onlyonce"):
            self._config["onlyonce"] = False
            self.update_config(self._config)
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                self.sign, "date", name=self.plugin_name,
                run_date=datetime.now(ZoneInfo(settings.TZ)) + timedelta(seconds=3),
            )
            self._scheduler.start()


    def get_state(self) -> bool:
        return bool(getattr(self, "_config", {}).get("enabled"))


    def get_service(self):
        config = getattr(self, "_config", {})
        if not config.get("enabled") or not config.get("cron"):
            return []
        try:
            trigger = CronTrigger.from_crontab(config["cron"], timezone=settings.TZ)
        except (ValueError, TypeError):
            logger.error("[enshansigncc] 定时表达式无效，请检查插件配置")
            return []
        return [{"id": "enshansigncc", "name": self.plugin_name,
                 "trigger": trigger, "func": self.sign, "kwargs": {}}]


    @staticmethod
    def _decode_cookiecloud(payload):
        if not isinstance(payload, dict):
            raise ValueError("CookieCloud 数据格式不正确")
        if payload.get("encrypted"):
            key = str(getattr(settings, "COOKIECLOUD_KEY", "") or "").strip()
            password = str(getattr(settings, "COOKIECLOUD_PASSWORD", "") or "").strip()
            if not key or not password:
                raise ValueError("请先配置 MoviePilot CookieCloud 的用户KEY和端到端加密密码")
            crypt_key = md5(f"{key}-{password}".encode()).hexdigest()[:16].encode()
            try:
                payload = json.loads(CryptoJsUtils.decrypt(payload["encrypted"], crypt_key).decode("utf-8"))
            except Exception:
                raise ValueError("CookieCloud 解密失败，请检查KEY和加密密码") from None
        data = payload.get("cookie_data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("CookieCloud 响应缺少 cookie_data")
        return data


    def _load_cookiecloud(self):
        key = str(getattr(settings, "COOKIECLOUD_KEY", "") or "").strip()
        if not key:
            raise ValueError("MoviePilot 尚未配置 CookieCloud 用户KEY")
        if getattr(settings, "COOKIECLOUD_ENABLE_LOCAL", False):
            try:
                path = settings.COOKIE_PATH / f"{key}.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ValueError("无法读取本地 CookieCloud 数据，请检查同步状态") from None
            return self._decode_cookiecloud(payload)
        host = str(getattr(settings, "COOKIECLOUD_HOST", "") or "").strip().rstrip("/")
        if not host:
            raise ValueError("MoviePilot 尚未配置 CookieCloud 服务地址")
        # 沿用仓库现有插件支持的服务和静态文件地址，不在日志中输出带KEY的URL。
        failures = []
        suffixes = (f"/get/{key}", f"/get/{key}.json", f"/{key}", f"/{key}.json")
        for index, suffix in enumerate(suffixes, start=1):
            try:
                response = requests.get(host + suffix, timeout=20, allow_redirects=False)
                if response.status_code != 200:
                    failures.append(f"候选{index}: HTTP {response.status_code}")
                    continue
                return self._decode_cookiecloud(response.json())
            except ValueError:
                # requests.JSONDecodeError 同时属于 RequestException，优先按格式错误处理。
                failures.append(f"候选{index}: 数据格式或解密错误")
            except requests.RequestException:
                failures.append(f"候选{index}: 网络请求失败")
        raise ValueError("无法获取远端 CookieCloud 数据；" + "；".join(failures))


    def stop_service(self):
        scheduler = getattr(self, "_scheduler", None)
        if scheduler:
            scheduler.remove_all_jobs()
            if scheduler.running:
                scheduler.shutdown(wait=False)
            self._scheduler = None


    def get_api(self):
        return []


    def get_command(self):
        return []


