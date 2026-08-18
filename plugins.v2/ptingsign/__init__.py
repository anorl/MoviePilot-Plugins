"""蜂巢（pting.club）每日签到插件。"""

import json
import random
import re
import time
from datetime import datetime, timedelta
from hashlib import md5
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.crypto import CryptoJsUtils

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    curl_requests = None
    HAS_CURL_CFFI = False

try:
    import requests
except ImportError:
    requests = None


class ptingsign(_PluginBase):
    plugin_name = "蜂巢签到"
    plugin_desc = "自动完成蜂巢每日签到，支持 CookieCloud"
    plugin_icon = "https://raw.githubusercontent.com/anorl/MoviePilot-Plugins/main/icons/ptingsign.png"
    plugin_version = "0.1.1"
    plugin_author = "anorl"
    author_url = "https://github.com/anorl"
    plugin_config_prefix = "ptingsign_"
    plugin_order = 1
    auth_level = 2

    _enabled = False
    _notify = False
    _onlyonce = False
    _cron = "0 9 * * *"
    _site_url = "https://pting.club"
    _cookie_source = "cookiecloud"
    _cookie = ""
    _use_proxy = True
    _verify_ssl = True
    _max_retries = 3
    _min_delay = 3
    _max_delay = 8
    _history_days = 30
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        logger.info("============= ptingsign 初始化 =============")

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._notify = bool(config.get("notify", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._cron = (config.get("cron") or "0 9 * * *").strip()
            self._site_url = (config.get("site_url") or "https://pting.club").strip().rstrip("/")
            self._cookie_source = (config.get("cookie_source") or "cookiecloud").strip().lower()
            self._cookie = (config.get("cookie") or "").strip()
            self._use_proxy = bool(config.get("use_proxy", True))
            self._verify_ssl = bool(config.get("verify_ssl", True))
            self._max_retries = self._int_config(config, "max_retries", 3, 1, 5)
            self._min_delay = self._int_config(config, "min_delay", 3, 0, 60)
            self._max_delay = self._int_config(config, "max_delay", 8, 0, 60)
            self._history_days = self._int_config(config, "history_days", 30, 1, 365)

        logger.info(
            f"[ptingsign] 配置: enabled={self._enabled}, notify={self._notify}, "
            f"cron={self._cron}, site_url={self._site_url}, "
            f"cookie_source={self._cookie_source}, use_proxy={self._use_proxy}, "
            f"max_retries={self._max_retries}"
        )

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="蜂巢签到",
            )
            self._onlyonce = False
            self.update_config(self._current_config())
            if self._scheduler.get_jobs():
                self._scheduler.start()

    @staticmethod
    def _int_config(config: dict, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(config.get(key, default))))
        except (TypeError, ValueError):
            return default

    def _current_config(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": False,
            "cron": self._cron,
            "site_url": self._site_url,
            "cookie_source": self._cookie_source,
            "cookie": self._cookie,
            "use_proxy": self._use_proxy,
            "verify_ssl": self._verify_ssl,
            "max_retries": self._max_retries,
            "min_delay": self._min_delay,
            "max_delay": self._max_delay,
            "history_days": self._history_days,
        }

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except ValueError as error:
            logger.error(f"[ptingsign] cron 表达式无效: {self._cron}: {error}")
            return []
        return [{
            "id": "ptingsign",
            "name": "蜂巢签到",
            "trigger": trigger,
            "func": self.sign,
            "kwargs": {},
        }]

    @staticmethod
    def _normalize_proxies(proxies_input):
        if not proxies_input:
            return None
        if isinstance(proxies_input, str):
            return {"http": proxies_input, "https": proxies_input}
        if isinstance(proxies_input, dict):
            http_url = proxies_input.get("http") or proxies_input.get("HTTP") or proxies_input.get("https") or proxies_input.get("HTTPS")
            https_url = proxies_input.get("https") or proxies_input.get("HTTPS") or proxies_input.get("http") or proxies_input.get("HTTP")
            if http_url or https_url:
                return {"http": http_url or https_url, "https": https_url or http_url}
        return None

    def _get_proxies(self):
        if not self._use_proxy:
            return None
        return self._normalize_proxies(getattr(settings, "PROXY", None))

    @staticmethod
    def _mask_cookie_for_log(cookie: Optional[str]) -> str:
        if not cookie:
            return "空"
        text = str(cookie).strip()
        return "***" if len(text) <= 16 else f"{text[:8]}...{text[-8:]}"

    def _resolve_cookiecloud_domain(self) -> str:
        try:
            return (urlparse(self._site_url).hostname or "").strip().lower()
        except Exception as error:
            logger.warning(f"[ptingsign] 解析站点域名失败: {error}")
            return ""

    @staticmethod
    def _domain_matches(cookie_domain: str, target_domain: str) -> bool:
        cookie_domain = (cookie_domain or "").strip().lower().lstrip(".")
        target_domain = (target_domain or "").strip().lower().lstrip(".")
        return bool(cookie_domain and target_domain) and (
            cookie_domain == target_domain
            or target_domain.endswith(f".{cookie_domain}")
        )

    def _match_cookiecloud_domain(
        self, cookie_data: Dict[str, Any], domain: str
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        if not isinstance(cookie_data, dict) or not domain:
            return None, []

        matched_items = []
        matched_keys = []
        discovered_domains = set()
        for group_key, items in cookie_data.items():
            if not isinstance(items, list):
                continue
            key_domain = str(group_key or "").strip().lower()
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_domain = str(item.get("domain") or key_domain).strip().lower()
                if item_domain:
                    discovered_domains.add(item_domain)
                if self._domain_matches(item_domain, domain):
                    matched_items.append(item)
                    if group_key not in matched_keys:
                        matched_keys.append(group_key)

        deduped = []
        seen_names = set()
        for item in reversed(matched_items):
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if not name or not value or name in seen_names:
                continue
            seen_names.add(name)
            deduped.append(item)
        deduped.reverse()

        logger.info(
            f"[ptingsign] CookieCloud 统计: groups={len(cookie_data)}, "
            f"domains={', '.join(sorted(discovered_domains)[:20]) or '无'}, "
            f"matched={len(deduped)}"
        )
        if not deduped:
            return None, []
        return ",".join(str(key) for key in matched_keys) or domain, deduped

    @staticmethod
    def _build_cookie_header(cookie_items: List[Dict[str, Any]]) -> str:
        pairs = []
        for item in cookie_items or []:
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name and value:
                pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    def _get_cookiecloud_crypt_key(self) -> bytes:
        generator = md5()
        generator.update(
            (f"{str(settings.COOKIECLOUD_KEY).strip()}-{str(settings.COOKIECLOUD_PASSWORD).strip()}").encode("utf-8")
        )
        return generator.hexdigest()[:16].encode("utf-8")

    def _decrypt_cookiecloud_payload(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        if not isinstance(payload, dict):
            return {}, "CookieCloud 响应格式不正确"
        if payload.get("encrypted"):
            try:
                decrypted = CryptoJsUtils.decrypt(
                    payload["encrypted"], self._get_cookiecloud_crypt_key()
                ).decode("utf-8")
                payload = json.loads(decrypted)
            except Exception as error:
                return {}, f"解密 CookieCloud 数据失败: {error}"

        cookie_data = payload.get("cookie_data") if isinstance(payload, dict) else None
        if isinstance(cookie_data, dict):
            return cookie_data, ""
        if isinstance(payload, dict):
            return payload, ""
        return {}, "CookieCloud 数据为空或格式不正确"

    def _decrypt_cookiecloud_file(self) -> Tuple[Dict[str, Any], str]:
        if not getattr(settings, "COOKIECLOUD_ENABLE_LOCAL", False):
            return {}, "MoviePilot 未启用本地 CookieCloud"
        if not getattr(settings, "COOKIECLOUD_KEY", None):
            return {}, "MoviePilot 未配置 COOKIECLOUD_KEY"
        if not getattr(settings, "COOKIECLOUD_PASSWORD", None):
            return {}, "MoviePilot 未配置 COOKIECLOUD_PASSWORD"

        cookie_path = settings.COOKIE_PATH / f"{settings.COOKIECLOUD_KEY}.json"
        if not cookie_path.exists():
            return {}, f"本地 CookieCloud 文件不存在: {cookie_path}"
        try:
            payload = json.loads(cookie_path.read_text(encoding="utf-8"))
        except Exception as error:
            return {}, f"读取 CookieCloud 文件失败: {error}"
        return self._decrypt_cookiecloud_payload(payload)

    def _fetch_remote_cookiecloud_data(self) -> Tuple[Dict[str, Any], str]:
        host = (getattr(settings, "COOKIECLOUD_HOST", "") or "").strip().rstrip("/")
        key = getattr(settings, "COOKIECLOUD_KEY", None)
        password = getattr(settings, "COOKIECLOUD_PASSWORD", None)
        if not host:
            return {}, "MoviePilot 未配置 COOKIECLOUD_HOST"
        if not key or not password:
            return {}, "MoviePilot 未配置 CookieCloud Key 或 Password"
        if requests is None:
            return {}, "requests 未安装，无法请求远端 CookieCloud"

        last_error = ""
        for url in (f"{host}/get/{key}", f"{host}/get/{key}.json", f"{host}/{key}", f"{host}/{key}.json"):
            try:
                response = requests.get(url, timeout=20, verify=self._verify_ssl)
                if response.status_code != 200:
                    last_error = f"HTTP {response.status_code}"
                    continue
                cookie_data, error = self._decrypt_cookiecloud_payload(response.json())
                if not error:
                    return cookie_data, ""
                last_error = error
            except Exception as error:
                last_error = str(error)
        return {}, f"远端 CookieCloud 获取失败: {last_error or '未知错误'}"

    def _load_cookie_from_cookiecloud(self) -> Tuple[Optional[str], str]:
        domain = self._resolve_cookiecloud_domain()
        if not domain:
            return None, "无法从站点 URL 推导蜂巢域名"

        if getattr(settings, "COOKIECLOUD_ENABLE_LOCAL", False):
            cookie_data, error = self._decrypt_cookiecloud_file()
        else:
            cookie_data, error = self._fetch_remote_cookiecloud_data()
        if error:
            return None, error

        matched_domain, cookie_items = self._match_cookiecloud_domain(cookie_data, domain)
        if not matched_domain or not cookie_items:
            return None, f"CookieCloud 中未找到域名 {domain} 的 Cookie"
        cookie_header = self._build_cookie_header(cookie_items)
        if not cookie_header:
            return None, f"CookieCloud 域名 {matched_domain} 下没有有效 Cookie"
        logger.info(
            f"[ptingsign] CookieCloud 命中 {matched_domain}，共 {len(cookie_items)} 项，"
            f"摘要: {self._mask_cookie_for_log(cookie_header)}"
        )
        return cookie_header, ""

    def _get_active_cookie(self) -> Tuple[Optional[str], str]:
        if self._cookie_source == "cookiecloud":
            return self._load_cookie_from_cookiecloud()
        if not self._cookie:
            return None, "未配置手工 Cookie"
        return self._cookie, ""

    def _wait_random_interval(self):
        minimum = min(self._min_delay, self._max_delay)
        maximum = max(self._min_delay, self._max_delay)
        if maximum > 0:
            delay = random.uniform(minimum, maximum)
            logger.info(f"[ptingsign] 请求前等待 {delay:.2f} 秒")
            time.sleep(delay)

    def _build_headers(self, cookie: str) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": cookie,
            "Origin": self._site_url,
            "Referer": f"{self._site_url}/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
        }

    def _post_check_in(self, cookie: str):
        url = f"{self._site_url}/api/check-in"
        proxies = self._get_proxies()
        headers = self._build_headers(cookie)
        payload = {"action": "check-in"}

        if HAS_CURL_CFFI:
            session = curl_requests.Session(impersonate="chrome")
            if proxies:
                session.proxies = proxies
            return session.post(
                url, headers=headers, json=payload, timeout=30, verify=self._verify_ssl
            )
        if requests is None:
            raise RuntimeError("requests 与 curl_cffi 均未安装")
        return requests.post(
            url, headers=headers, json=payload, proxies=proxies,
            timeout=30, verify=self._verify_ssl,
        )

    @staticmethod
    def _parse_check_in_response(response) -> Dict[str, Any]:
        status_code = getattr(response, "status_code", 0)
        try:
            payload = response.json()
        except Exception:
            text = (getattr(response, "text", "") or "").strip()
            if status_code in (401, 403) or "登录" in text:
                message = "登录态已失效，请刷新 CookieCloud"
            else:
                message = f"蜂巢返回非 JSON 响应（HTTP {status_code}）"
            return {"success": False, "message": message}

        if not isinstance(payload, dict):
            return {"success": False, "message": f"蜂巢响应格式异常（HTTP {status_code}）"}

        message = str(payload.get("message") or "").strip()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if status_code < 200 or status_code >= 300:
            return {
                "success": False,
                "message": message or f"签到请求失败（HTTP {status_code}）",
            }

        already_signed = bool(data.get("alreadyCheckedIn"))
        result = {
            "success": True,
            "signed": not already_signed,
            "already_signed": already_signed,
            "message": message or ("今天已经签到过了" if already_signed else "签到成功"),
        }
        field_map = {
            "date": "sign_date",
            "reward": "reward",
            "points": "points_balance",
            "currentStreak": "current_streak",
            "maxStreak": "max_streak",
        }
        for api_field, result_field in field_map.items():
            if data.get(api_field) is not None:
                result[result_field] = data[api_field]
        return result

    @staticmethod
    def _parse_points_summary_html(html_text: str) -> Dict[str, Any]:
        """从蜂巢积分明细页的 Next.js 服务端数据中提取积分汇总。"""
        normalized = (html_text or "").replace('\\"', '"')
        patterns = {
            "points_balance": r'当前余额.{0,2500}?"value"\s*:\s*(-?\d+(?:\.\d+)?)',
            "today_points": r'今日变动：?.{0,1500}?"value"\s*:\s*(-?\d+(?:\.\d+)?)',
        }
        summary = {}
        for field, pattern in patterns.items():
            match = re.search(pattern, normalized, flags=re.DOTALL)
            if not match:
                continue
            value = float(match.group(1))
            summary[field] = int(value) if value.is_integer() else value
        return summary

    def _fetch_points_summary(self, cookie: str) -> Dict[str, Any]:
        url = f"{self._site_url}/settings?tab=points"
        proxies = self._get_proxies()
        headers = self._build_headers(cookie)
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

        if HAS_CURL_CFFI:
            session = curl_requests.Session(impersonate="chrome")
            if proxies:
                session.proxies = proxies
            response = session.get(
                url, headers=headers, timeout=30, verify=self._verify_ssl
            )
        else:
            if requests is None:
                raise RuntimeError("requests 与 curl_cffi 均未安装")
            response = requests.get(
                url, headers=headers, proxies=proxies,
                timeout=30, verify=self._verify_ssl,
            )

        if getattr(response, "status_code", 0) != 200:
            raise RuntimeError(f"积分明细页返回 HTTP {getattr(response, 'status_code', 0)}")
        summary = self._parse_points_summary_html(getattr(response, "text", ""))
        if not summary:
            raise RuntimeError("未能从积分明细页解析积分汇总")
        logger.info(
            f"[ptingsign] 积分汇总: 今日积分={summary.get('today_points', '未知')}, "
            f"积分余额={summary.get('points_balance', '未知')}"
        )
        return summary

    def _run_check_in(self, cookie: str) -> Dict[str, Any]:
        response = self._post_check_in(cookie)
        logger.info(f"[ptingsign] 签到接口响应 HTTP {getattr(response, 'status_code', 0)}")
        return self._parse_check_in_response(response)

    def sign(self):
        logger.info("============= 开始蜂巢签到 =============")
        cookie, cookie_error = self._get_active_cookie()
        if not cookie:
            return self._record_result({"success": False, "message": cookie_error})
        if not self._site_url.startswith(("http://", "https://")):
            return self._record_result({"success": False, "message": "站点 URL 格式不正确"})

        self._wait_random_interval()
        result = None
        last_error = None
        for attempt in range(1, self._max_retries + 1):
            try:
                result = self._run_check_in(cookie)
                if result.get("success") or "登录态" in result.get("message", ""):
                    break
            except Exception as error:
                last_error = error
                logger.warning(f"[ptingsign] 签到异常 ({attempt}/{self._max_retries}): {error}")
            if attempt < self._max_retries:
                time.sleep(2)

        if result is None:
            result = {"success": False, "message": f"重试后仍失败: {last_error or '未知错误'}"}
        if result.get("success"):
            try:
                result.update(self._fetch_points_summary(cookie))
            except Exception as error:
                logger.warning(f"[ptingsign] 获取积分汇总失败，将保留签到接口数据: {error}")
        return self._record_result(result)

    def _record_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("success") and result.get("already_signed"):
            status = "今日已签到"
        elif result.get("success"):
            status = "签到成功"
        else:
            status = f"签到失败: {result.get('message') or '未知错误'}"

        record = {"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": status}
        for key in (
            "sign_date", "reward", "today_points", "points_balance",
            "current_streak", "max_streak",
        ):
            if result.get(key) is not None:
                record[key] = result[key]
        self._save_sign_history(record)
        if self._notify:
            self._send_notification(record, result)
        return record

    def _send_notification(self, record: Dict[str, Any], result: Dict[str, Any]):
        ok = bool(result.get("success"))
        lines = [
            f"时间：{record['date']}",
            f"状态：{record['status']}",
        ]
        if record.get("reward") is not None and not result.get("already_signed"):
            lines.append(f"奖励：{record['reward']} 积分")
        if record.get("today_points") is not None:
            lines.append(f"今日积分：{self._format_signed_number(record['today_points'])}")
        points_balance = record.get("points_balance", record.get("points"))
        if points_balance is not None:
            lines.append(f"积分余额：{points_balance}")
        if record.get("current_streak") is not None:
            lines.append(f"连续签到：{record['current_streak']} 天")
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【蜂巢签到成功】" if ok else "【蜂巢签到失败】",
            text="\n".join(lines),
        )

    @staticmethod
    def _format_signed_number(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if number.is_integer():
            return f"{int(number):+d}" if number else "0"
        return f"{number:+g}" if number else "0"

    def _save_sign_history(self, record: Dict[str, Any]):
        try:
            history = self.get_data("sign_history") or []
            history.append(record)
            cutoff = datetime.now() - timedelta(days=self._history_days)
            retained = []
            for item in history:
                try:
                    if datetime.strptime(item.get("date", ""), "%Y-%m-%d %H:%M:%S") >= cutoff:
                        retained.append(item)
                except (TypeError, ValueError):
                    continue
            self.save_data(key="sign_history", value=retained)
        except Exception as error:
            logger.error(f"[ptingsign] 保存签到历史失败: {error}", exc_info=True)

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as error:
            logger.warning(f"[ptingsign] 停止一次性任务失败: {error}")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        form = [{
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "notify", "label": "开启通知"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [{"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}]},
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "site_url", "label": "蜂巢站点 URL", "placeholder": "https://pting.club"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VTextField", "props": {"model": "cron", "label": "定时任务（cron）", "placeholder": "0 9 * * *"}}]},
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VSelect", "props": {"model": "cookie_source", "label": "Cookie 来源", "items": [{"title": "MoviePilot CookieCloud", "value": "cookiecloud"}, {"title": "手工填写", "value": "manual"}]}}]},
                        {"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextField", "props": {"model": "cookie", "label": "站点 Cookie", "placeholder": "仅手工模式使用", "show": "{{ cookie_source === 'manual' }}"}}]},
                    ],
                },
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "CookieCloud 模式会自动匹配 pting.club。请先在浏览器中登录蜂巢，并确认 CookieCloud 已同步该域名。签到接口为站点当前使用的 POST /api/check-in。",
                    },
                },
            ],
        }]
        defaults = {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "0 9 * * *",
            "site_url": "https://pting.club",
            "cookie_source": "cookiecloud",
            "cookie": "",
            "use_proxy": True,
            "verify_ssl": True,
            "max_retries": 3,
            "min_delay": 3,
            "max_delay": 8,
            "history_days": 30,
        }
        return form, defaults

    def get_page(self) -> List[dict]:
        history = sorted(
            self.get_data("sign_history") or [], key=lambda item: item.get("date", ""), reverse=True
        )
        if not history:
            return [{"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "暂无蜂巢签到记录"}}]

        rows = []
        for item in history:
            status = item.get("status", "未知")
            detail = []
            if item.get("reward") is not None:
                detail.append(f"奖励 {item['reward']}")
            if item.get("today_points") is not None:
                detail.append(f"今日积分 {self._format_signed_number(item['today_points'])}")
            points_balance = item.get("points_balance", item.get("points"))
            if points_balance is not None:
                detail.append(f"积分余额 {points_balance}")
            if item.get("current_streak") is not None:
                detail.append(f"连续 {item['current_streak']} 天")
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("date", "")},
                    {"component": "td", "content": [{"component": "VChip", "props": {"color": "success" if ("成功" in status or "已签到" in status) else "error", "size": "small", "variant": "outlined"}, "text": status}]},
                    {"component": "td", "text": "，".join(detail) or "-"},
                ],
            })
        return [{
            "component": "VCard",
            "props": {"variant": "outlined"},
            "content": [
                {"component": "VCardTitle", "text": "蜂巢签到历史"},
                {"component": "VCardText", "content": [{"component": "VTable", "props": {"hover": True, "density": "compact"}, "content": [
                    {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "text": "时间"}, {"component": "th", "text": "状态"}, {"component": "th", "text": "详情"}]}]},
                    {"component": "tbody", "content": rows},
                ]}]},
            ],
        }]

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []
