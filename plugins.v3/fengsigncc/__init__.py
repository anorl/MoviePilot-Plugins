"""威锋网页签到；支持手工填写 Cookie 或从 MoviePilot 的 CookieCloud 获取。"""

import base64
import json
import time
from datetime import datetime, timedelta
from hashlib import md5
from threading import Lock
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.utilities import CryptoJsUtils


class fengsigncc(_PluginBase):
    plugin_name = "威锋网签到"
    plugin_desc = "自动完成威锋网每日签到，支持手工Cookie和CookieCloud"
    plugin_icon = "https://raw.githubusercontent.com/anorl/MoviePilot-Plugins/main/icons/feng.jpg"
    plugin_version = "1.0.0"
    plugin_author = "anorl"
    author_url = "https://github.com/anorl"
    plugin_config_prefix = "fengsigncc_"
    plugin_order = 1
    auth_level = 2

    # 来自威锋公开网页客户端的 AES-CBC 协议常量，并非用户凭据。
    _web_key = b"2b7e151628aed2a6"
    _api_url = "https://api.feng.com"
    _experience_path = "/v1/user/experience"
    _sign_path = "/v1/attendance/userSignIn"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._config = {
            "enabled": False, "notify": False, "onlyonce": False,
            "cron": "0 9 * * *", "use_proxy": False,
            "cookie_source": "cookiecloud", "cookie": "",
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
            logger.error("[fengsigncc] 定时表达式无效，请检查插件配置")
            return []
        return [{"id": "fengsigncc", "name": self.plugin_name,
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

    @staticmethod
    def _extract_identity(cookie_data):
        """网页使用 js-cookie 保存 JSON；优先当前站点 userInfo，其次共享Cookie。"""
        candidates = []
        for group, items in cookie_data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                domain = str(item.get("domain") or group).lower().lstrip(".")
                if domain not in ("feng.com", "www.feng.com"):
                    continue
                name = item.get("name")
                if name not in ("userInfo", "userInfo-shared"):
                    continue
                # Cookie 属于网页而非 API 域名，必须按网页路径筛选。
                if (item.get("path") or "/") != "/":
                    continue
                expiry = item.get("expirationDate", item.get("expires", -1))
                try:
                    expiry = float(expiry or -1)
                    if expiry > 10_000_000_000:
                        expiry /= 1000
                    if expiry > 0 and expiry <= time.time():
                        continue
                except (TypeError, ValueError):
                    continue
                try:
                    value = str(item.get("value") or "")
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    info = json.loads(unquote(value))
                    token = info.get("accessToken") if isinstance(info, dict) else None
                    if not isinstance(token, str) or not token.strip() or "\r" in token or "\n" in token:
                        continue
                    user = info.get("userInfo") or {}
                    username = user.get("userName", "") if isinstance(user, dict) else ""
                    score = (name == "userInfo", domain == "www.feng.com", expiry)
                    candidates.append((score, token, str(username)))
                except (ValueError, TypeError):
                    continue
        if not candidates:
            raise ValueError("没有有效的威锋 userInfo 登录Cookie，请在 www.feng.com 登录后更新Cookie")
        _, token, username = max(candidates, key=lambda item: item[0])
        return token, username

    def _get_identity(self):
        source = str(self._config.get("cookie_source") or "cookiecloud").strip().lower()
        if source == "cookiecloud":
            return self._extract_identity(self._load_cookiecloud())
        if source != "manual":
            raise ValueError("Cookie来源无效，请选择手工填写或CookieCloud")
        raw_cookie = str(self._config.get("cookie") or "").strip()
        if raw_cookie.lower().startswith("cookie:"):
            raw_cookie = raw_cookie.partition(":")[2].strip()
        if not raw_cookie:
            raise ValueError("请先填写威锋网Cookie，需包含userInfo或userInfo-shared")
        items = []
        for part in raw_cookie.split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name.strip() in ("userInfo", "userInfo-shared"):
                items.append({"name": name.strip(), "value": value.strip(),
                              "domain": "www.feng.com", "path": "/"})
        # 两种来源共用登录态解码与校验；手工模式不读取CookieCloud配置。
        return self._extract_identity({"www.feng.com": items})

    @classmethod
    def _request_id(cls, path, timestamp_ms=None):
        timestamp_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
        plaintext = f"url={path}$time={timestamp_ms}000000".encode("utf-8")
        padder = PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(cls._web_key), modes.CBC(cls._web_key)).encryptor()
        return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")

    def _request(self, session, method, path, token):
        response = session.request(
            method, self._api_url + path,
            headers={"Accept": "application/json, text/plain, */*",
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                     "Origin": "https://www.feng.com", "Referer": "https://www.feng.com/",
                     "X-Request-Id": self._request_id(path), "X-Access-Token": token},
            timeout=25, allow_redirects=False,
        )
        if response.status_code != 200:
            raise ValueError(f"威锋接口返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("威锋接口返回非JSON内容，可能遇到站点防护") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("status"), dict):
            raise ValueError("威锋接口响应格式异常")
        code = str(payload["status"].get("code"))
        if code == "1001":
            raise ValueError("威锋登录态已失效，请重新登录并更新手工Cookie或同步CookieCloud")
        return payload

    @staticmethod
    def _success(payload):
        return str(payload["status"].get("code")) == "0"

    @staticmethod
    def _signed(data):
        return data.get("isSignedIn") in (True, 1, "1", "true", "yes")

    def _query_after_sign(self, session, token):
        """提交后只查询一次；无法取得有效数据时不沿用签到前的数据。"""
        try:
            after = self._request(session, "GET", self._experience_path, token)
            if self._success(after) and isinstance(after.get("data"), dict):
                return after["data"]
        except (ValueError, requests.RequestException):
            pass
        return None

    def _run_sign(self, session, token):
        before = self._request(session, "GET", self._experience_path, token)
        if not self._success(before) or not isinstance(before.get("data"), dict):
            raise ValueError("无法读取威锋签到状态，请稍后重试")
        data = before["data"]
        if self._signed(data):
            return "今日已签到", data
        try:
            result = self._request(session, "POST", self._sign_path, token)
        except requests.RequestException:
            # 超时或断线时服务端可能已经完成签到，只读确认，不重发POST。
            after_data = self._query_after_sign(session, token)
            if after_data is not None and self._signed(after_data):
                return "今日已签到（请求异常后查询确认）", after_data
            raise ValueError("签到请求异常，结果未确认，请稍后查询网页签到状态") from None
        if self._success(result):
            # POST 成功后查询累计数据失败，不覆盖已经成功的签到结果。
            return "签到成功", self._query_after_sign(session, token) or {}
        # 重复签到/并发签到必须以当前账户状态确认，不能仅凭文案判成功。
        after_data = self._query_after_sign(session, token)
        if after_data is not None and self._signed(after_data):
            return "今日已签到", after_data
        code = str(result["status"].get("code", "未知"))[:30]
        raise ValueError(f"威锋拒绝签到（状态码 {code}），请在网页检查账户状态")

    def sign(self):
        if not self._lock.acquire(blocking=False):
            logger.info("[fengsigncc] 已有签到任务运行，跳过重复触发")
            return
        try:
            record = {"date": datetime.now(ZoneInfo(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")}
            try:
                token, username = self._get_identity()
                with requests.Session() as session:
                    session.trust_env = False
                    if self._config.get("use_proxy"):
                        proxies = getattr(settings, "PROXY", None)
                        if isinstance(proxies, str):
                            proxies = {"http": proxies, "https": proxies}
                        elif getattr(proxies, "host", None):
                            proxies = {"http": proxies.host, "https": proxies.host}
                        if proxies:
                            session.proxies.update(proxies)
                    status, data = self._run_sign(session, token)
                record.update(status=status, success=True, username=username)
                for field in ("signInCount", "currentExperience"):
                    if isinstance(data.get(field), (int, float, str)):
                        record[field] = data[field]
            except requests.RequestException:
                # 原始网络异常可能带有包含KEY的服务URL，不记录异常正文。
                record.update(status="签到请求异常，结果未确认，请检查网络或网页签到状态", success=False)
            except ValueError as error:
                record.update(status=str(error), success=False)
            except Exception:
                record.update(status="签到执行异常，请检查插件依赖及MoviePilot配置", success=False)
            try:
                history = self.get_data("sign_history") or []
                self.save_data(key="sign_history", value=(history + [record])[-100:])
            except Exception:
                logger.warning("[fengsigncc] 保存签到历史失败，本次签到结果不变")
            logger.info(f"[fengsigncc] {record['status']}")
            if self._config.get("notify"):
                try:
                    details = [record["date"], record["status"]]
                    if record.get("username"):
                        details.append(f"用户：{record['username']}")
                    if "signInCount" in record:
                        details.append(f"连续签到：{record['signInCount']} 天")
                    if "currentExperience" in record:
                        details.append(f"当前经验：{record['currentExperience']}")
                    self.post_message(mtype=NotificationType.SiteMessage,
                                      title=f"【{self.plugin_name}】", text="\n".join(details))
                except Exception:
                    logger.warning("[fengsigncc] 发送通知失败，本次签到结果不变")
            return record
        finally:
            self._lock.release()

    def stop_service(self):
        scheduler = getattr(self, "_scheduler", None)
        if scheduler:
            scheduler.remove_all_jobs()
            if scheduler.running:
                scheduler.shutdown(wait=False)
            self._scheduler = None

    def get_form(self):
        switches = [("enabled", "启用插件"), ("notify", "开启通知"),
                    ("onlyonce", "立即运行一次"), ("use_proxy", "使用MoviePilot系统代理")]
        return [{"component": "VForm", "content": [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                    {"component": "VSwitch", "props": {"model": key, "label": label}}
                ]} for key, label in switches]},
            {"component": "VTextField", "props": {
                "model": "cron", "label": "定时任务(cron)", "placeholder": "0 9 * * *"}},
            {"component": "VSelect", "props": {
                "model": "cookie_source", "label": "Cookie来源", "items": [
                    {"title": "手工填写", "value": "manual"},
                    {"title": "MoviePilot CookieCloud", "value": "cookiecloud"}]}},
            {"component": "VTextField", "props": {
                "model": "cookie", "label": "威锋网Cookie", "type": "password",
                "show": "{{ cookie_source === 'manual' }}",
                "hint": "粘贴www.feng.com登录后的完整Cookie，需包含userInfo或userInfo-shared；仅手工模式使用。",
                "persistent-hint": True}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                "text": "支持手工填写Cookie，也可复用MoviePilot的本地或远端CookieCloud配置。请在www.feng.com登录，手工复制登录Cookie或通过扩展同步。默认每天9点签到，最多保留100条记录。"}},
        ]}], {"enabled": False, "notify": False, "onlyonce": False,
              "cron": "0 9 * * *", "use_proxy": False,
              "cookie_source": "cookiecloud", "cookie": ""}

    def get_page(self):
        history = self.get_data("sign_history") or []
        if not history:
            return [{"component": "VAlert", "props": {"type": "info", "text": "暂无威锋签到记录"}}]
        return [{"component": "VTable", "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": label} for label in ("时间", "结果", "用户", "连续签到")]}]},
            {"component": "tbody", "content": [
                {"component": "tr", "content": [{"component": "td", "text": str(value)} for value in (
                    row.get("date", ""), row.get("status", ""), row.get("username", ""), row.get("signInCount", "-"))]}
                for row in reversed(history)]},
        ]}]

    def get_api(self):
        return []

    def get_command(self):
        return []
