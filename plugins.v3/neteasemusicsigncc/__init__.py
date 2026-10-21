"""网易云音乐每日签到；支持手工填写 Cookie 或从 MoviePilot 的 CookieCloud 获取。"""

import base64
import json
import time
from datetime import datetime, timedelta
from hashlib import md5
from threading import Lock
import re
import secrets
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


class neteasemusicsigncc(_PluginBase):
    plugin_name = "网易云音乐签到"
    plugin_desc = "网易云音乐每日签到，支持手工Cookie、CookieCloud及MP消息通知"
    plugin_icon = "https://raw.githubusercontent.com/anorl/MoviePilot-Plugins/main/icons/NetEaseMusic.png"
    plugin_version = "1.0.1"
    plugin_author = "anorl"
    author_url = "https://github.com/anorl"
    plugin_config_prefix = "neteasemusicsigncc_"
    plugin_order = 1
    auth_level = 2

    # WEAPI 公开客户端协议参数，不是用户凭据。
    _nonce = b"0CoJUm6Qyw8W8jud"
    _iv = b"0102030405060708"
    _modulus = int(
        "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
        "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
        "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
        "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b"
        "3ece0462db0a22b8e7", 16)
    _sign_path = "/weapi/point/dailyTask"

    @staticmethod
    def _validate_cookies(cookies):
        if not cookies.get("MUSIC_U"):
            raise ValueError("缺少网易云音乐MUSIC_U登录Cookie，请在music.163.com登录后更新Cookie")
        for name, value in cookies.items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name) or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Cookie格式无效，请重新复制浏览器Cookie")
        return cookies

    def _get_cookies(self):
        source = str(self._config.get("cookie_source") or "cookiecloud").strip().lower()
        if source == "manual":
            raw = str(self._config.get("cookie") or "").strip()
            if raw.lower().startswith("cookie:"):
                raw = raw.partition(":")[2].strip()
            if not raw:
                raise ValueError("请填写网易云音乐登录Cookie，需包含MUSIC_U")
            cookies = {}
            for part in raw.split(";"):
                name, separator, value = part.strip().partition("=")
                if separator:
                    cookies[name.strip()] = value.strip()
            return self._validate_cookies(cookies)
        if source != "cookiecloud":
            raise ValueError("Cookie来源无效，请选择手工填写或CookieCloud")
        data = self._load_cookiecloud()
        selected = {}
        now = time.time()
        for group, items in data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                domain = str(item.get("domain") or group).lower().lstrip(".")
                if domain not in ("music.163.com", "163.com"):
                    continue
                if item.get("hostOnly") and domain != "music.163.com":
                    continue
                path = str(item.get("path") or "/")
                if not (self._sign_path == path or self._sign_path.startswith(path.rstrip("/") + "/")):
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
                if not name or not value:
                    continue
                score = (len(path), len(domain), expiry)
                if name not in selected or score > selected[name][0]:
                    selected[name] = (score, value)
        return self._validate_cookies({name: entry[1] for name, entry in selected.items()})

    @classmethod
    def _weapi(cls, payload):
        key = secrets.token_hex(8).encode("ascii")
        def encrypt(value, aes_key):
            padder = PKCS7(128).padder()
            value = padder.update(value) + padder.finalize()
            cipher = Cipher(algorithms.AES(aes_key), modes.CBC(cls._iv)).encryptor()
            return base64.b64encode(cipher.update(value) + cipher.finalize())
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = encrypt(encrypt(raw, cls._nonce), key).decode("ascii")
        rsa = pow(int.from_bytes(key[::-1], "big"), 65537, cls._modulus)
        return {"params": encrypted, "encSecKey": format(rsa, "0256x")}

    def _request_sign(self, session, cookies, sign_type):
        # 所有请求固定发往网易云；Cookie不进入URL，不跟随重定向。
        for name, value in {**cookies, "os": "android" if sign_type == 0 else "pc"}.items():
            session.cookies.set(name, value, domain="music.163.com", path="/")
        csrf = cookies.get("__csrf", "")
        response = session.post(
            "https://music.163.com" + self._sign_path,
            params={"csrf_token": csrf},
            data=self._weapi({"type": sign_type, "csrf_token": csrf}),
            headers={"Referer": "https://music.163.com/", "Origin": "https://music.163.com",
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                     "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=25, allow_redirects=False,
        )
        if response.status_code != 200:
            raise ValueError(f"网易云接口返回HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("网易云接口返回非JSON内容，可能遇到站点防护") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("code"), (int, str)):
            raise ValueError("网易云接口响应格式异常")
        return payload

    def _run_sign(self, session, cookies):
        mode = str(self._config.get("sign_type") or "both")
        if mode not in ("both", "0", "1"):
            raise ValueError("签到类型无效，请重新选择移动端、网页端或两端")
        results = []
        for sign_type in ((0, 1) if mode == "both" else (int(mode),)):
            result = {"type": "移动端" if sign_type == 0 else "网页端", "success": False}
            try:
                response = self._request_sign(session, cookies, sign_type)
                code = str(response["code"])
                if code == "200":
                    result.update(status="签到成功", success=True)
                    point = response.get("point")
                    if isinstance(point, (int, float)) and not isinstance(point, bool):
                        result["point"] = point
                elif code == "-2":
                    result.update(status="今日已签到", success=True)
                elif code in ("301", "302"):
                    result["status"] = "登录态失效，请重新登录并更新Cookie"
                else:
                    # 不输出服务器原始message，以免包含用户凭据或不受控文本。
                    safe_code = code if re.fullmatch(r"-?\d{1,8}", code) else "未知"
                    result["status"] = f"签到失败（状态码 {safe_code}），请在客户端检查账户状态"
            except requests.RequestException:
                result["status"] = "请求异常，结果未确认，请在客户端查看；本次不重复提交"
            except ValueError as error:
                result["status"] = str(error)
            results.append(result)
        return results

    def sign(self):
        if not self._lock.acquire(blocking=False):
            logger.info("[neteasemusicsigncc] 已有签到任务运行，跳过重复触发")
            return
        try:
            record = {"date": datetime.now(ZoneInfo(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")}
            try:
                cookies = self._get_cookies()
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
                    results = self._run_sign(session, cookies)
                success = all(item["success"] for item in results)
                status = "签到完成" if success else ("部分完成" if any(item["success"] for item in results) else "签到未完成")
                record.update(status=status, success=success, results=results)
                points = [item["point"] for item in results if "point" in item]
                if points:
                    record["point"] = sum(points)
            except ValueError as error:
                record.update(status=str(error), success=False)
            except Exception:
                record.update(status="签到执行异常，请检查插件依赖及MoviePilot配置", success=False)
            try:
                history = self.get_data("sign_history") or []
                self.save_data(key="sign_history", value=(history + [record])[-100:])
            except Exception:
                logger.warning("[neteasemusicsigncc] 保存历史失败，本次签到结果不变")
            logger.info(f"[neteasemusicsigncc] {record['status']}")
            if self._config.get("notify", True):
                details = [record["date"], record["status"]]
                for item in record.get("results", []):
                    text = f"{item['type']}：{item['status']}"
                    if "point" in item:
                        text += f"，获得 {item['point']} 点"
                    details.append(text)
                try:
                    self.post_message(mtype=NotificationType.SiteMessage,
                                      title=f"【{self.plugin_name}】", text="\n".join(details))
                except Exception:
                    logger.warning("[neteasemusicsigncc] 发送消息失败，本次签到结果不变")
            return record
        finally:
            self._lock.release()

    def get_form(self):
        switches = [("enabled", "启用插件"), ("notify", "发送通知"),
                    ("onlyonce", "立即运行一次"), ("use_proxy", "使用MoviePilot系统代理")]
        return [{"component": "VForm", "content": [
            {"component": "VRow", "props": {"class": "mb-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                    {"component": "VSwitch", "props": {"model": key, "label": label}}
                ]} for key, label in switches]},
            {"component": "VTextField", "props": {"model": "cron", "label": "定时任务(cron)", "class": "mb-4", "placeholder": "0 9 * * *"}},
            {"component": "VSelect", "props": {"model": "sign_type", "label": "签到类型", "class": "mb-4", "items": [
                {"title": "移动端和网页端", "value": "both"}, {"title": "移动端", "value": "0"},
                {"title": "网页端", "value": "1"}]}},
            {"component": "VSelect", "props": {"model": "cookie_source", "label": "Cookie来源", "class": "mb-4", "items": [
                {"title": "手工填写", "value": "manual"}, {"title": "MoviePilot CookieCloud", "value": "cookiecloud"}]}},
            {"component": "VTextField", "props": {"model": "cookie", "label": "网易云音乐Cookie", "type": "password", "class": "mb-4",
                "show": "{{ cookie_source === 'manual' }}", "hint": "粘贴music.163.com登录后的完整Cookie，需包含MUSIC_U。", "persistent-hint": True}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                "text": "支持手工Cookie或MoviePilot本地/远端CookieCloud。默认每天9点签到，保存最近100条历史。通知默认开启；TG请在MoviePilot配置Telegram渠道并允许接收站点消息。"}},
        ]}], {"enabled": False, "notify": True, "onlyonce": False, "cron": "0 9 * * *",
              "use_proxy": False, "cookie_source": "cookiecloud", "cookie": "", "sign_type": "both"}

    def get_page(self):
        history = self.get_data("sign_history") or []
        if not history:
            return [{"component": "VAlert", "props": {"type": "info", "text": "暂无网易云音乐签到记录"}}]
        rows = []
        for record in reversed(history):
            details = "；".join(f"{item['type']}：{item['status']}" for item in record.get("results", []))
            rows.append({"component": "tr", "content": [
                {"component": "td", "text": str(value)} for value in (
                    record.get("date", ""), record.get("status", ""), details, record.get("point", "-"))]})
        return [{"component": "VTable", "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": label} for label in ("时间", "结果", "各端详情", "本次获得点数")]}]},
            {"component": "tbody", "content": rows},
        ]}]

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._config = {
            "enabled": False, "notify": True, "onlyonce": False,
            "cron": "0 9 * * *", "use_proxy": False,
            "cookie_source": "cookiecloud", "cookie": "", "sign_type": "both",
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
            logger.error("[neteasemusicsigncc] 定时表达式无效，请检查插件配置")
            return []
        return [{"id": "neteasemusicsigncc", "name": self.plugin_name,
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


