import json
import re
import time
from datetime import datetime, timedelta
from hashlib import md5
from threading import Event, Lock
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.utilities import CryptoJsUtils


class tiebasigncc(_PluginBase):
    plugin_name = "百度贴吧签到"
    plugin_desc = "自动签到关注的百度贴吧，支持手工Cookie、CookieCloud及消息通知"
    plugin_icon = "https://raw.githubusercontent.com/anorl/MoviePilot-Plugins/main/icons/tiebasigncc.ico"
    plugin_version = "1.0.0"
    plugin_author = "anorl"
    author_url = "https://github.com/anorl"
    plugin_config_prefix = "tiebasigncc_"
    plugin_order = 1
    auth_level = 2
    _web_url = "https://tieba.baidu.com"
    _client_url = "https://c.tieba.baidu.com"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._config = {"enabled": False, "notify": True, "onlyonce": False,
                        "cron": "0 9 * * *", "use_proxy": False, "interval": 5,
                        "cookie_source": "cookiecloud", "cookie": ""}
        self._config.update(config or {})
        self._lock = getattr(self, "_lock", Lock())
        self._stop_event = Event()
        if self._config.get("onlyonce"):
            self._config["onlyonce"] = False
            self.update_config(self._config)
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                self.sign, "date", name=self.plugin_name,
                run_date=datetime.now(ZoneInfo(settings.TZ)) + timedelta(seconds=3))
            self._scheduler.start()

    @staticmethod
    def _validate_cookies(cookies):
        if not cookies.get("BDUSS"):
            raise ValueError("缺少贴吧BDUSS登录Cookie，请在tieba.baidu.com登录后更新Cookie")
        valid = {}
        invalid_names = invalid_values = 0
        for name, value in cookies.items():
            name_ok = bool(re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name))
            # requests使用HTTP头传输Cookie，控制字符和非ASCII值不能原样发送。
            value_ok = value.isascii() and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            if name_ok and value_ok:
                valid[name] = value
                continue
            if name == "BDUSS":
                raise ValueError("BDUSS登录Cookie含控制字符或非ASCII字符，请重新登录贴吧并更新Cookie或同步CookieCloud")
            invalid_names += not name_ok
            invalid_values += not value_ok
        if invalid_names or invalid_values:
            # 名称也可能是被错误解析的凭据，不输出名称或值。
            logger.warning(
                f"[tiebasigncc] 已忽略格式异常的非必要Cookie：名称异常{invalid_names}项，值异常{invalid_values}项；继续使用有效登录Cookie")
        return valid

    def _get_cookies(self):
        source = str(self._config.get("cookie_source") or "cookiecloud").strip().lower()
        if source == "manual":
            raw = str(self._config.get("cookie") or "").strip()
            if raw.lower().startswith("cookie:"):
                raw = raw.partition(":")[2].strip()
            if not raw:
                raise ValueError("请填写百度贴吧Cookie，需包含BDUSS")
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
                if domain not in ("tieba.baidu.com", "baidu.com"):
                    continue
                if item.get("hostOnly") and domain != "tieba.baidu.com":
                    continue
                # 登录验证与客户端接口路径不同，取全站根路径登录Cookie。
                if (item.get("path") or "/") != "/":
                    continue
                try:
                    expiry = float(item.get("expirationDate", item.get("expires", -1)) or -1)
                    if expiry > 10_000_000_000:
                        expiry /= 1000
                    if expiry > 0 and expiry <= now:
                        continue
                except (TypeError, ValueError):
                    continue
                name, value = str(item.get("name") or ""), str(item.get("value") or "")
                if name and value:
                    score = (len(domain), expiry)
                    if name not in selected or score > selected[name][0]:
                        selected[name] = (score, value)
        return self._validate_cookies({name: entry[1] for name, entry in selected.items()})

    @staticmethod
    def _signed_data(data):
        # 贴吧公开客户端协议：按参数名排序，再附加固定协议盐。
        raw = "".join(f"{key}={data[key]}" for key in sorted(data)) + "tiebaclient!!!"
        return {**data, "sign": md5(raw.encode("utf-8")).hexdigest().upper()}

    @staticmethod
    def _request_json(session, method, url, data=None):
        stages = {"/dc/common/tbs": "登录验证", "/c/f/forum/like": "获取关注列表", "/c/c/forum/sign": "逐吧签到"}
        original_path = urlsplit(url).path.rstrip("/")
        stage = stages.get(original_path, "贴吧")
        visited = {url}
        for hop in range(4):
            # 手动处理同名API的规范地址跳转，避免requests把301/302的POST变成GET。
            response = session.request(method, url, data=data, timeout=25, allow_redirects=False)
            if response.status_code not in (301, 302, 307, 308):
                break
            location = response.headers.get("Location", "")
            try:
                target = urljoin(url, location)
                parts = urlsplit(target)
                allowed = (
                    bool(location) and original_path in stages
                    and parts.scheme == "https"
                    and parts.hostname in ("tieba.baidu.com", "c.tieba.baidu.com", "tiebac.baidu.com")
                    and parts.port in (None, 443)
                    and parts.username is None and parts.password is None
                    and parts.path.rstrip("/") == original_path
                    and not parts.query and not parts.fragment
                )
            except ValueError:
                allowed = False
            if not allowed:
                raise ValueError(f"{stage}接口返回HTTP {response.status_code}，重定向目标不是受支持的HTTPS同名API，请在浏览器检查登录或验证状态")
            if target in visited or hop == 3:
                raise ValueError(f"{stage}接口重定向循环或超过3次，请稍后重试")
            visited.add(target)
            response.close()
            url = target
        if response.status_code != 200:
            raise ValueError(f"{stage}接口返回HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ValueError(f"{stage}接口返回非JSON内容，可能需要在浏览器完成验证") from None
        if not isinstance(payload, dict):
            raise ValueError(f"{stage}接口响应格式异常")
        return payload

    def _get_tbs(self, session):
        payload = self._request_json(session, "GET", self._client_url + "/dc/common/tbs")
        if str(payload.get("is_login")) != "1":
            raise ValueError("贴吧登录态未通过验证，请重新登录并更新Cookie")
        tbs = payload.get("tbs")
        if not isinstance(tbs, str) or not tbs.strip():
            raise ValueError("贴吧接口未返回有效的TBS令牌")
        return tbs

    @staticmethod
    def _forum_items(payload):
        if str(payload.get("error_code")) != "0":
            raise ValueError("获取关注贴吧失败，请检查账号状态或在浏览器完成验证")
        forums = payload.get("forum_list")
        if isinstance(forums, dict):
            items = []
            # 不同客户端版本分别使用普通列表或这两个分组。
            if not any(key in forums for key in ("gconforum", "non-gconforum")) and forums:
                raise ValueError("关注贴吧列表格式异常")
            for key in ("non-gconforum", "gconforum"):
                group = forums.get(key, [])
                if isinstance(group, dict):
                    group = [group]
                if not isinstance(group, list):
                    raise ValueError("关注贴吧分组格式异常")
                items.extend(group)
        elif isinstance(forums, list):
            items = forums
        else:
            raise ValueError("贴吧接口缺少关注列表，无法确认签到范围")
        normalized = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise ValueError("关注贴吧条目格式异常")
            normalized.append({"name": item["name"].strip(), "id": str(item.get("id") or "")})
        return normalized

    def _get_forums(self, session, bduss, stop_event):
        forums = {}
        for page in range(1, 101):
            if stop_event.is_set():
                raise ValueError("任务已停止，尚未开始逐吧签到")
            data = {"BDUSS": bduss, "_client_version": "9.7.8.0", "_client_type": "2",
                    "page_no": str(page), "page_size": "200"}
            payload = self._request_json(session, "POST", self._client_url + "/c/f/forum/like", self._signed_data(data))
            items = self._forum_items(payload)
            previous_count = len(forums)
            for item in items:
                forums.setdefault(item["name"], item)
            more = payload.get("has_more")
            if more is not None and str(more) not in ("0", "1"):
                raise ValueError("贴吧列表分页标记异常，无法确认完整列表")
            has_more = str(more) == "1" if more is not None else len(items) >= 200
            if not has_more:
                return list(forums.values())
            if len(forums) == previous_count:
                raise ValueError("贴吧列表分页重复或为空，无法确认完整列表")
            if stop_event.wait(1):
                raise ValueError("任务已停止，尚未开始逐吧签到")
        raise ValueError("贴吧列表超过分页上限，未开始签到，请检查接口响应")

    def _sign_forum(self, session, bduss, tbs, forum):
        data = {"BDUSS": bduss, "kw": forum["name"], "tbs": tbs}
        if forum["id"]:
            data["fid"] = forum["id"]
        payload = self._request_json(session, "POST", self._client_url + "/c/c/forum/sign", self._signed_data(data))
        code = str(payload.get("error_code"))
        if code == "0":
            return {"name": forum["name"], "kind": "signed", "status": "签到成功"}
        if code == "160002":
            return {"name": forum["name"], "kind": "already", "status": "今日已签到"}
        if code in ("2150040", "1102", "1989"):
            return {"name": forum["name"], "kind": "failed", "status": "遇到验证码或风控，请在浏览器检查", "halt": True}
        safe_code = code if re.fullmatch(r"-?\d{1,10}", code) else "未知"
        return {"name": forum["name"], "kind": "failed", "status": f"签到失败（状态码 {safe_code}）"}

    def _run_sign(self, session, cookies, stop_event):
        tbs = self._get_tbs(session)
        forums = self._get_forums(session, cookies["BDUSS"], stop_event)
        try:
            interval = max(3, min(60, int(self._config.get("interval", 5))))
        except (TypeError, ValueError):
            interval = 5
        results = []
        halt = False
        for index, forum in enumerate(forums):
            stopped = stop_event.is_set()
            if not halt and not stopped and index > 0:
                stopped = stop_event.wait(interval)
            if halt or stopped:
                results.append({"name": forum["name"], "kind": "skipped",
                                "status": "风控后停止，未提交" if halt else "任务停止，未提交"})
                continue
            try:
                result = self._sign_forum(session, cookies["BDUSS"], tbs, forum)
            except requests.RequestException:
                result = {"name": forum["name"], "kind": "unknown", "status": "请求异常，结果未确认，本次不重复提交"}
            except ValueError as error:
                result = {"name": forum["name"], "kind": "unknown", "status": str(error), "halt": True}
            results.append(result)
            halt = bool(result.get("halt"))
        counts = {key: sum(item["kind"] == key for item in results) for key in ("signed", "already", "failed", "unknown", "skipped")}
        success = not any(counts[key] for key in ("failed", "unknown", "skipped"))
        if not results:
            status = "未关注任何贴吧，无需签到"
        elif success:
            status = "签到完成"
        elif counts["signed"] + counts["already"]:
            status = "部分完成"
        elif counts["unknown"]:
            status = "结果未确认"
        else:
            status = "签到未完成"
        return {"success": success, "status": status, "total": len(results), "counts": counts, "results": results}

    def _notify_result(self, record):
        if not self._config.get("notify", True):
            return
        icon = "✅" if record["success"] else "❌"
        label = "成功" if record["success"] else "失败"
        if record["status"] in ("部分完成", "结果未确认"):
            icon, label = "⚠️", record["status"]
        if record.get("total") == 0:
            icon, label = "ℹ️", ""
        lines = ["📢 执行结果", "━━━━━━━━━━", f"🕐 时间：{record['date']}", f"✨ 状态：{record['status']}"]
        if "counts" in record:
            c = record["counts"]
            lines.extend([f"📊 关注贴吧：{record['total']} 个", f"✅ 新签到：{c['signed']} 个　📌 已签到：{c['already']} 个",
                          f"❌ 失败：{c['failed']} 个　❔ 未确认：{c['unknown']} 个　⏸️ 未提交：{c['skipped']} 个"])
            # 优先展示异常，不让大量关注贴吧撑满Telegram消息长度。
            problems = [item for item in record["results"] if item["kind"] not in ("signed", "already")]
            shown = problems[:15]
            for item in shown:
                lines.append(f"🔹 {item['name'][:40]}：{item['status']}")
            if len(problems) > len(shown):
                lines.append(f"📋 另有 {len(problems) - len(shown)} 条异常，详见插件签到历史")
        lines.append("━━━━━━━━━━")
        try:
            self.post_message(mtype=NotificationType.SiteMessage, title=f"【{icon} {self.plugin_name}{label}】", text="\n".join(lines))
        except Exception:
            logger.warning("[tiebasigncc] 发送通知失败，本次签到结果不变")

    def sign(self):
        if not self._lock.acquire(blocking=False):
            logger.info("[tiebasigncc] 已有签到任务运行，跳过重复触发")
            return
        stop_event = self._stop_event
        try:
            record = {"date": datetime.now(ZoneInfo(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")}
            try:
                cookies = self._get_cookies()
                with requests.Session() as session:
                    session.trust_env = False
                    session.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/130.0.0.0 Mobile Safari/537.36 tieba/12.67.1.0",
                                            "Referer": self._web_url + "/", "Accept": "application/json"})
                    for name, value in cookies.items():
                        session.cookies.set(name, value, domain=".baidu.com", path="/")
                    if self._config.get("use_proxy"):
                        proxies = getattr(settings, "PROXY", None)
                        if isinstance(proxies, str):
                            proxies = {"http": proxies, "https": proxies}
                        elif getattr(proxies, "host", None):
                            proxies = {"http": proxies.host, "https": proxies.host}
                        if proxies:
                            session.proxies.update(proxies)
                    record.update(self._run_sign(session, cookies, stop_event))
            except requests.RequestException:
                record.update(success=False, status="获取贴吧登录状态或关注列表时网络异常，未开始签到")
            except ValueError as error:
                record.update(success=False, status=str(error))
            except Exception:
                record.update(success=False, status="签到执行异常，请检查插件依赖及MoviePilot配置")
            try:
                history = self.get_data("sign_history") or []
                self.save_data(key="sign_history", value=(history + [record])[-100:])
            except Exception:
                logger.warning("[tiebasigncc] 保存历史失败，本次签到结果不变")
            logger.info(f"[tiebasigncc] {record['status']}")
            self._notify_result(record)
            return record
        finally:
            self._lock.release()

    def stop_service(self):
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        scheduler = getattr(self, "_scheduler", None)
        if scheduler:
            scheduler.remove_all_jobs()
            if scheduler.running:
                scheduler.shutdown(wait=False)
            self._scheduler = None

    def get_form(self):
        switches = [("enabled", "启用插件"), ("notify", "发送通知"),
                    ("onlyonce", "立即运行一次"), ("use_proxy", "使用MoviePilot系统代理")]
        return [{"component": "VForm", "content": [
            {"component": "VRow", "props": {"class": "mb-2"}, "content": [
                {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                    {"component": "VSwitch", "props": {"model": key, "label": label}}]} for key, label in switches]},
            {"component": "VTextField", "props": {"model": "cron", "label": "定时任务(cron)", "placeholder": "0 9 * * *", "class": "mb-4"}},
            {"component": "VTextField", "props": {"model": "interval", "label": "逐吧签到间隔（秒，3～60）", "type": "number", "min": 3, "max": 60, "class": "mb-4"}},
            {"component": "VSelect", "props": {"model": "cookie_source", "label": "Cookie来源", "class": "mb-4", "items": [
                {"title": "手工填写", "value": "manual"}, {"title": "MoviePilot CookieCloud", "value": "cookiecloud"}]}},
            {"component": "VTextField", "props": {"model": "cookie", "label": "百度贴吧Cookie", "type": "password", "class": "mb-4",
                "show": "{{ cookie_source === 'manual' }}", "hint": "粘贴tieba.baidu.com登录后的完整Cookie，需包含BDUSS。", "persistent-hint": True}},
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal",
                "text": "自动签到当前账号关注的贴吧，默认每天9点、逐吧间隔5秒，保留最近100次运行及逐吧结果。通知通过MP站点消息发送；TG需在MP配置Telegram渠道。遇到验证码或风控会停止后续签到，请在浏览器检查。"}},
        ]}], {"enabled": False, "notify": True, "onlyonce": False, "cron": "0 9 * * *",
              "use_proxy": False, "interval": 5, "cookie_source": "cookiecloud", "cookie": ""}

    def get_page(self):
        history = self.get_data("sign_history") or []
        if not history:
            return [{"component": "VAlert", "props": {"type": "info", "text": "暂无百度贴吧签到记录"}}]
        panels = []
        for record in reversed(history):
            rows = [{"component": "tr", "content": [{"component": "td", "text": value} for value in (item["name"], item["status"])]}
                    for item in record.get("results", [])]
            c = record.get("counts")
            summary = record.get("status", "")
            if c:
                summary += f" · 共{record['total']}个 · 新签到{c['signed']} · 已签到{c['already']} · 失败{c['failed']} · 未确认{c['unknown']} · 未提交{c['skipped']}"
            content = [{"component": "div", "text": summary}]
            if rows:
                content.append({"component": "VTable", "content": [
                    {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "text": label} for label in ("贴吧", "结果")]}]},
                    {"component": "tbody", "content": rows}]})
            panels.append({"component": "VExpansionPanel", "content": [
                {"component": "VExpansionPanelTitle", "text": f"{record.get('date', '')}　{record.get('status', '')}"},
                {"component": "VExpansionPanelText", "content": content}]})
        return [{"component": "VExpansionPanels", "content": panels}]

    def get_state(self) -> bool:
        return bool(getattr(self, "_config", {}).get("enabled"))


    def get_service(self):
        config = getattr(self, "_config", {})
        if not config.get("enabled") or not config.get("cron"):
            return []
        try:
            trigger = CronTrigger.from_crontab(config["cron"], timezone=settings.TZ)
        except (ValueError, TypeError):
            logger.error("[tiebasigncc] 定时表达式无效，请检查插件配置")
            return []
        return [{"id": "tiebasigncc", "name": self.plugin_name,
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


    def get_api(self):
        return []


    def get_command(self):
        return []


