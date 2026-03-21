from __future__ import annotations

import html
import random
import re
import time
from http.cookies import SimpleCookie
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except Exception:
    HAS_CLOUDSCRAPER = False

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except Exception:
    HAS_CURL_CFFI = False

try:
    import requests
except Exception:
    requests = None


class wuaipojiesign(_PluginBase):
    plugin_name = "吾爱破解论坛签到"
    plugin_desc = "自动完成 52pojie 每日打卡签到，支持定时任务与通知。"
    plugin_icon = "https://www.52pojie.cn/favicon.ico"
    plugin_version = "0.1.3"
    plugin_author = "anorl"
    author_url = "https://github.com/anorl"
    plugin_config_prefix = "wuaipojiesign_"
    plugin_order = 2
    auth_level = 2

    _enabled = False
    _notify = False
    _onlyonce = False
    _cron = "0 9 * * *"

    _cookie = ""
    _base_url = "https://www.52pojie.cn"
    _task_id = 2
    _ua_mode = "auto"

    _use_proxy = True
    _verify_ssl = True
    _max_retries = 3
    _min_delay = 3
    _max_delay = 8
    _history_days = 30

    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._cron = config.get("cron") or "0 9 * * *"
            self._cookie = (config.get("cookie") or "").strip()
            self._base_url = (config.get("base_url") or "https://www.52pojie.cn").strip().rstrip("/")

            try:
                self._task_id = int(config.get("task_id", 2))
            except Exception:
                self._task_id = 2
            self._ua_mode = (config.get("ua_mode") or "auto").strip().lower()
            if self._ua_mode not in {"auto", "desktop", "mobile"}:
                self._ua_mode = "auto"

            self._use_proxy = bool(config.get("use_proxy", True))
            self._verify_ssl = bool(config.get("verify_ssl", True))

            try:
                self._max_retries = int(config.get("max_retries", 3))
            except Exception:
                self._max_retries = 3

            try:
                self._min_delay = int(config.get("min_delay", 3))
                self._max_delay = int(config.get("max_delay", 8))
            except Exception:
                self._min_delay, self._max_delay = 3, 8

            try:
                self._history_days = int(config.get("history_days", 30))
            except Exception:
                self._history_days = 30

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="吾爱破解论坛签到",
            )
            self._onlyonce = False
            self.update_config(
                {
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "onlyonce": False,
                    "cron": self._cron,
                    "cookie": self._cookie,
                    "base_url": self._base_url,
                    "task_id": self._task_id,
                    "ua_mode": self._ua_mode,
                    "use_proxy": self._use_proxy,
                    "verify_ssl": self._verify_ssl,
                    "max_retries": self._max_retries,
                    "min_delay": self._min_delay,
                    "max_delay": self._max_delay,
                    "history_days": self._history_days,
                }
            )
            if self._scheduler.get_jobs():
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [
                {
                    "id": "wuaipojiesign",
                    "name": "吾爱破解论坛签到",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sign,
                    "kwargs": {},
                }
            ]
        return []

    def _normalize_proxies(self, proxies_input):
        if not proxies_input:
            return None
        if isinstance(proxies_input, str):
            return {"http": proxies_input, "https": proxies_input}
        if isinstance(proxies_input, dict):
            http_url = proxies_input.get("http") or proxies_input.get("HTTP")
            https_url = proxies_input.get("https") or proxies_input.get("HTTPS")
            if not http_url and not https_url:
                return None
            return {"http": http_url or https_url, "https": https_url or http_url}
        return None

    def _get_proxies(self):
        if not self._use_proxy:
            return None
        try:
            if hasattr(settings, "PROXY") and settings.PROXY:
                return self._normalize_proxies(settings.PROXY)
        except Exception:
            return None
        return None

    def _headers(self, referer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cookie": self._cookie,
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _http_get(self, url: str, referer: Optional[str] = None, timeout: int = 30):
        if requests is None:
            raise RuntimeError("requests 未安装")
        return requests.get(
            url,
            headers=self._headers(referer=referer),
            timeout=timeout,
            proxies=self._get_proxies(),
            verify=self._verify_ssl,
            allow_redirects=True,
        )

    @staticmethod
    def _is_js_challenge(content: str) -> bool:
        keys = [
            "Please enable JavaScript and refresh the page",
            "<noscript>",
            "function L(",
            "document.cookie",
            "jschl",
        ]
        return any(k in (content or "") for k in keys)

    def _ua_candidates(self) -> List[str]:
        if self._ua_mode == "desktop":
            return ["desktop"]
        if self._ua_mode == "mobile":
            return ["mobile"]
        # auto: 先移动端再桌面端，优先命中你提供的移动结构
        return ["mobile", "desktop"]

    def _ua_string(self, ua_kind: str) -> str:
        if ua_kind == "mobile":
            return (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
            )
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )

    def _build_session(self, client: str = "requests", ua_kind: str = "desktop"):
        if client == "cloudscraper":
            if not HAS_CLOUDSCRAPER:
                raise RuntimeError("cloudscraper 未安装")
            try:
                session = cloudscraper.create_scraper(browser="chrome")
            except Exception:
                session = cloudscraper.create_scraper()
        elif client == "curl_cffi":
            if not HAS_CURL_CFFI:
                raise RuntimeError("curl_cffi 未安装")
            session = curl_requests.Session(impersonate="chrome124")
        else:
            if requests is None:
                raise RuntimeError("requests 未安装")
            session = requests.Session()

        session.headers.update(
            {
                "User-Agent": self._ua_string(ua_kind),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        try:
            session.proxies = self._get_proxies() or {}
        except Exception:
            pass
        try:
            session.verify = self._verify_ssl
        except Exception:
            pass

        # 把配置中的 Cookie 串写入会话，让后续请求同时保留服务端新增 cookie
        try:
            raw_cookie = self._cookie or ""
            parsed = SimpleCookie()
            parsed.load(raw_cookie)
            for key, morsel in parsed.items():
                session.cookies.set(key, morsel.value)
        except Exception:
            # 回退到 Header Cookie
            session.headers["Cookie"] = self._cookie
        return session

    def _session_get(self, session, url: str, referer: Optional[str] = None, timeout: int = 30):
        if referer:
            session.headers["Referer"] = referer
        return session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            verify=self._verify_ssl,
        )

    def _wait_random_interval(self):
        try:
            if self._max_delay >= self._min_delay > 0:
                delay = random.uniform(float(self._min_delay), float(self._max_delay))
                time.sleep(delay)
        except Exception:
            pass

    def _extract_apply_link(self, content: str) -> Optional[str]:
        patterns = [
            rf'href="([^"]*home\.php\?mod=task(?:&amp;|&)do=apply(?:&amp;|&)id={self._task_id}[^"]*)"',
            rf"href='([^']*home\.php\?mod=task(?:&amp;|&)do=apply(?:&amp;|&)id={self._task_id}[^']*)'",
            r'href="([^"]*home\.php\?mod=task(?:&amp;|&)do=apply[^"]*)"',
            r"href='([^']*home\.php\?mod=task(?:&amp;|&)do=apply[^']*)'",
            rf"(home\.php\?mod=task(?:&amp;|&)do=apply(?:&amp;|&)id={self._task_id}[^\s<\"']*)",
        ]
        for pattern in patterns:
            matched = re.search(pattern, content, flags=re.IGNORECASE)
            if matched:
                href = html.unescape(matched.group(1))
                return urljoin(f"{self._base_url}/", href)
        return None

    def _extract_draw_link(self, content: str) -> Optional[str]:
        # 兼容 href / JS 字符串 / inajax 返回中的 draw 链接
        patterns = [
            rf'href="([^"]*home\.php\?mod=task(?:&amp;|&)do=draw(?:&amp;|&)id={self._task_id}[^"]*)"',
            rf"href='([^']*home\.php\?mod=task(?:&amp;|&)do=draw(?:&amp;|&)id={self._task_id}[^']*)'",
            rf'"(home\.php\?mod=task(?:&amp;|&)do=draw(?:&amp;|&)id={self._task_id}[^"]*)"',
            rf"'(home\.php\?mod=task(?:&amp;|&)do=draw(?:&amp;|&)id={self._task_id}[^']*)'",
            rf"(home\.php\?mod=task(?:&amp;|&)do=draw(?:&amp;|&)id={self._task_id}[^\s<\"']*)",
        ]
        for pattern in patterns:
            matched = re.search(pattern, content, flags=re.IGNORECASE)
            if matched:
                href = html.unescape(matched.group(1))
                return urljoin(f"{self._base_url}/", href)
        return None

    @staticmethod
    def _is_cookie_invalid(content: str) -> bool:
        keys = [
            "您需要先登录",
            "请先登录",
            "登录后",
            "member.php?mod=logging&action=login",
        ]
        return any(k in content for k in keys)

    @staticmethod
    def _is_already_signed(content: str) -> bool:
        keys = [
            "您今天已经申请过此任务",
            "您已申请过此任务",
            "今日已打卡",
            "今日已签到",
            "已经打卡",
            "任务已完成",
        ]
        return any(k in content for k in keys)

    @staticmethod
    def _is_sign_success(content: str) -> bool:
        keys = [
            "任务已成功完成",
            "任务申请成功",
            "您已申请此任务",
            "打卡签到成功",
            "签到成功",
            "恭喜",
            "奖励",
        ]
        return any(k in content for k in keys)

    @staticmethod
    def _extract_gain(content: str) -> Optional[str]:
        patterns = [
            r"奖励[^\d]{0,8}(\d+[.]?\d*)",
            r"获得[^\d]{0,8}(\d+[.]?\d*)",
            r"增加[^\d]{0,8}(\d+[.]?\d*)",
        ]
        for pattern in patterns:
            matched = re.search(pattern, content)
            if matched:
                return matched.group(1)
        return None

    def _do_sign_once_with_session(self, session, client_name: str, ua_kind: str) -> Dict[str, Any]:
        home_url = f"{self._base_url}/"
        direct_apply_url = f"{self._base_url}/home.php?mod=task&do=apply&id={self._task_id}"
        direct_draw_url = f"{self._base_url}/home.php?mod=task&do=draw&id={self._task_id}"
        logger.info(f"[wuaipojiesign] 使用 {client_name} 客户端({ua_kind} UA)访问首页并解析签到入口")
        home_resp = self._session_get(session=session, url=home_url, referer=None, timeout=30)
        home_text = home_resp.text or ""
        if self._is_js_challenge(home_text):
            return {"success": False, "retryable": True, "js_challenge": True, "message": f"{client_name}/{ua_kind} 命中 JS 风控页(home)"}

        if self._is_cookie_invalid(home_text):
            return {"success": False, "message": "Cookie 失效或未登录"}

        apply_link = self._extract_apply_link(home_text)
        if not apply_link:
            if self._is_already_signed(home_text):
                return {"success": True, "already_signed": True, "message": "今日已签到"}
            logger.info("[wuaipojiesign] 首页未解析到 apply，尝试直连 apply URL")
            apply_link = direct_apply_url
        logger.info(f"[wuaipojiesign] 解析到 apply 链接: {apply_link}")

        apply_resp = self._session_get(session=session, url=apply_link, referer=home_url, timeout=30)
        apply_text = apply_resp.text or ""
        logger.info(
            f"[wuaipojiesign] apply 响应: status={getattr(apply_resp, 'status_code', None)}, len={len(apply_text)}, final_url={getattr(apply_resp, 'url', '')}"
        )
        if self._is_js_challenge(apply_text):
            return {"success": False, "retryable": True, "js_challenge": True, "message": f"{client_name}/{ua_kind} 命中 JS 风控页(apply)"}

        if self._is_cookie_invalid(apply_text):
            return {"success": False, "message": "签到请求被重定向到登录页，Cookie 可能已失效"}

        if self._is_already_signed(apply_text):
            return {"success": True, "already_signed": True, "message": "今日已签到"}

        draw_link = self._extract_draw_link(apply_text)
        if not draw_link and any(key in apply_text for key in ["任务申请成功", "领取奖励", "do=draw"]):
            draw_link = direct_draw_url
        if draw_link:
            logger.info(f"[wuaipojiesign] 解析到 draw 链接: {draw_link}")
            draw_resp = self._session_get(session=session, url=draw_link, referer=apply_link, timeout=30)
            draw_text = draw_resp.text or ""
            logger.info(
                f"[wuaipojiesign] draw 响应: status={getattr(draw_resp, 'status_code', None)}, len={len(draw_text)}, final_url={getattr(draw_resp, 'url', '')}"
            )
            if self._is_js_challenge(draw_text):
                return {"success": False, "retryable": True, "js_challenge": True, "message": f"{client_name}/{ua_kind} 命中 JS 风控页(draw)"}
            if self._is_cookie_invalid(draw_text):
                return {"success": False, "message": "领奖请求登录失效，Cookie 可能已过期"}
            if self._is_already_signed(draw_text):
                return {"success": True, "already_signed": True, "message": "今日已签到"}
            if self._is_sign_success(draw_text):
                return {
                    "success": True,
                    "signed": True,
                    "message": "签到成功",
                    "gain": self._extract_gain(draw_text),
                }
            return {"success": False, "message": "签到领奖返回未知结果，请检查日志"}

        if self._is_sign_success(apply_text):
            return {
                "success": True,
                "signed": True,
                "message": "签到成功",
                "gain": self._extract_gain(apply_text),
            }

        snippet = re.sub(r"\s+", " ", apply_text)[:260]
        logger.warning(f"[wuaipojiesign] apply 未识别成功，响应片段: {snippet}")
        return {"success": False, "retryable": True, "message": "签到请求未返回成功状态，可能页面规则已变更"}

    def _do_sign_once(self) -> Dict[str, Any]:
        clients: List[str] = []
        if HAS_CLOUDSCRAPER:
            clients.append("cloudscraper")
        if HAS_CURL_CFFI:
            clients.append("curl_cffi")
        clients.append("requests")

        last_result: Dict[str, Any] = {"success": False, "message": "未知错误"}
        for client_name in clients:
            for ua_kind in self._ua_candidates():
                try:
                    session = self._build_session(client=client_name, ua_kind=ua_kind)
                    result = self._do_sign_once_with_session(session=session, client_name=client_name, ua_kind=ua_kind)
                    if result.get("success"):
                        return result
                    last_result = result
                    if result.get("js_challenge"):
                        logger.info(f"[wuaipojiesign] {client_name}/{ua_kind} 被 JS 风控拦截，继续尝试")
                        continue
                    return result
                except Exception as err:
                    last_result = {"success": False, "retryable": True, "message": f"{client_name}/{ua_kind} 请求异常: {err}"}
                    logger.info(f"[wuaipojiesign] {client_name}/{ua_kind} 请求异常，继续尝试: {err}")
                    continue

        if last_result.get("js_challenge"):
            return {
                "success": False,
                "retryable": True,
                "message": "触发站点 JS 风控，已尝试 cloudscraper/curl_cffi/requests + 移动/桌面 UA，仍被拦截",
            }
        return last_result

    def sign(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("[wuaipojiesign] 开始执行签到任务")
        if not self._cookie:
            record = {"date": now, "status": "签到失败", "detail": "未配置 Cookie"}
            self._save_sign_history(record)
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="吾爱破解论坛签到失败",
                    text="未配置 Cookie",
                )
            return record

        self._wait_random_interval()

        result: Dict[str, Any] = {"success": False, "message": "未知错误"}
        last_err = None
        retries = max(1, int(self._max_retries))
        for attempt in range(1, retries + 1):
            try:
                logger.info(f"[wuaipojiesign] 第 {attempt}/{retries} 次签到尝试")
                result = self._do_sign_once()
                if result.get("success"):
                    break
                logger.info(
                    f"[wuaipojiesign] 第 {attempt}/{retries} 次签到失败: {result.get('message', '未知错误')}"
                )
                if attempt < retries:
                    time.sleep(2)
            except Exception as err:
                last_err = err
                logger.info(f"[wuaipojiesign] 第 {attempt}/{retries} 次请求异常: {err}")
                if attempt < retries:
                    time.sleep(2)

        if not result.get("success") and last_err:
            result = {"success": False, "message": f"重试后仍失败: {last_err}"}

        if result.get("success") and result.get("signed"):
            status = "签到成功"
        elif result.get("success") and result.get("already_signed"):
            status = "今日已签到"
        else:
            status = "签到失败"

        record = {
            "date": now,
            "status": status,
            "detail": result.get("message", ""),
        }
        if result.get("gain"):
            record["gain"] = str(result.get("gain"))

        self._save_sign_history(record)

        if self._notify:
            if status in {"签到成功", "今日已签到"}:
                title = "吾爱破解论坛签到"
            else:
                title = "吾爱破解论坛签到失败"
            text = f"{status}\n{record.get('detail', '')}\n{record['date']}"
            if record.get("gain"):
                text += f"\n奖励: {record['gain']}"
            self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)

        return record

    def _save_sign_history(self, sign_data: Dict[str, Any]):
        history = self.get_data("sign_history") or []
        history.append(sign_data)
        now = datetime.now()
        valid: List[Dict[str, Any]] = []
        for item in history:
            try:
                dt = datetime.strptime(item.get("date", ""), "%Y-%m-%d %H:%M:%S")
                if (now - dt).days < int(self._history_days):
                    valid.append(item)
            except Exception:
                valid.append(item)
        self.save_data(key="sign_history", value=valid)

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception:
            pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "开启通知"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "use_proxy", "label": "使用代理"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "base_url",
                                            "label": "站点地址",
                                            "placeholder": "https://www.52pojie.cn",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时任务(cron)",
                                            "placeholder": "0 9 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "task_id",
                                            "label": "签到任务ID",
                                            "placeholder": "2",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "ua_mode",
                                            "label": "请求UA模式",
                                            "items": [
                                                {"title": "自动(移动优先)", "value": "auto"},
                                                {"title": "仅移动端", "value": "mobile"},
                                                {"title": "仅桌面端", "value": "desktop"}
                                            ]
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_retries",
                                            "label": "重试次数",
                                            "placeholder": "3",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "verify_ssl",
                                            "label": "校验SSL证书",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_delay",
                                            "label": "最小随机延迟(秒)",
                                            "placeholder": "3",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_delay",
                                            "label": "最大随机延迟(秒)",
                                            "placeholder": "8",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cookie",
                                            "label": "站点 Cookie",
                                            "placeholder": "从浏览器复制完整 Cookie",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "默认通过首页自动解析打卡链接（task apply）。若页面结构变化，请更新 task_id 或查看日志。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

        defaults = {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "0 9 * * *",
            "cookie": "",
            "base_url": "https://www.52pojie.cn",
            "task_id": 2,
            "ua_mode": "auto",
            "use_proxy": True,
            "verify_ssl": True,
            "max_retries": 3,
            "min_delay": 3,
            "max_delay": 8,
            "history_days": 30,
        }
        return form, defaults

    def get_page(self) -> List[dict]:
        history = self.get_data("sign_history") or []
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无签到记录",
                        "class": "mb-2",
                    },
                }
            ]

        rows = []
        for item in sorted(history, key=lambda x: x.get("date", ""), reverse=True):
            status = item.get("status", "未知")
            color = "success" if status in {"签到成功", "今日已签到"} else "error"
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": item.get("date", "")},
                        {
                            "component": "td",
                            "content": [
                                {
                                    "component": "VChip",
                                    "props": {"color": color, "size": "small", "variant": "outlined"},
                                    "text": status,
                                }
                            ],
                        },
                        {"component": "td", "text": item.get("detail", "-")},
                        {"component": "td", "text": item.get("gain", "-")},
                    ],
                }
            )

        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mb-4"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "text-h6"}, "text": "吾爱破解签到历史"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VTable",
                                "props": {"hover": True, "density": "compact"},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [
                                            {
                                                "component": "tr",
                                                "content": [
                                                    {"component": "th", "text": "时间"},
                                                    {"component": "th", "text": "状态"},
                                                    {"component": "th", "text": "详情"},
                                                    {"component": "th", "text": "奖励"},
                                                ],
                                            }
                                        ],
                                    },
                                    {"component": "tbody", "content": rows},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

