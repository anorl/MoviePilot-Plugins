"""贴吧登录态、完整分页、逐吧结果、风控停止和消息/历史回归测试。"""
import ast
import json
import re
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from hashlib import md5
from pathlib import Path
from threading import Event, Lock
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).parents[2]
source = ROOT / "plugins.v3/tiebasigncc/__init__.py"
cls = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body if isinstance(n, ast.ClassDef))
settings = types.SimpleNamespace(TZ="Asia/Shanghai", COOKIECLOUD_ENABLE_LOCAL=False,
                                 COOKIECLOUD_KEY="private-key", COOKIECLOUD_PASSWORD="private-password",
                                 COOKIECLOUD_HOST="https://cc.invalid")
crypto_utils = Mock()
namespace = dict(globals(), _PluginBase=object, logger=Mock(), settings=settings,
                 CryptoJsUtils=crypto_utils, NotificationType=types.SimpleNamespace(SiteMessage="site"))
exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(source), "exec"), namespace)
Plugin = namespace["tiebasigncc"]


def cookie(name="BDUSS", value="private-bduss", **kwargs):
    return {"name": name, "value": value, "domain": ".baidu.com", "path": "/", **kwargs}


def page(items, more="0"):
    return {"error_code": "0", "forum_list": items, "has_more": more}


def forum(name, fid="1"):
    return {"name": name, "id": fid}


class TiebaSignTests(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.plugin._config = {"notify": True, "cookie_source": "manual", "cookie": "BDUSS=private-bduss", "interval": 5}
        self.plugin._lock = Lock()
        self.plugin._stop_event = Mock()
        self.plugin._stop_event.is_set.return_value = False
        self.plugin._stop_event.wait.return_value = False
        self.plugin.get_data = Mock(return_value=[])
        self.plugin.save_data = Mock()
        self.plugin.post_message = Mock()

    def prepare_run(self, forums=None):
        self.plugin._get_tbs = Mock(return_value="private-tbs")
        self.plugin._get_forums = Mock(return_value=forums if forums is not None else [forum("python"), forum("linux", "2")])

    def test_market_metadata_and_icon(self):
        index = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))["tiebasigncc"]
        for key, attr in (("name", "plugin_name"), ("description", "plugin_desc"), ("icon", "plugin_icon"), ("version", "plugin_version")):
            self.assertEqual(index[key], getattr(self.plugin, attr))
        self.assertTrue((ROOT / "icons/tiebasigncc.ico").exists())

    def test_manual_cookie_preserves_values_and_never_reads_cloud(self):
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError())
        self.plugin._config["cookie"] = "Cookie: BDUSS=private-bduss==; STOKEN=token; other=ok"
        self.assertEqual(self.plugin._get_cookies(), {"BDUSS": "private-bduss==", "STOKEN": "token", "other": "ok"})
        self.plugin._load_cookiecloud.assert_not_called()

    def test_invalid_or_missing_cookie_does_not_leak_or_fallback(self):
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError())
        for raw in ("", "BDUSS_BFESS=private-bduss", "BDUSS=private-bduss\r\nbad", "BDUSS=ok; bad name=private-bduss"):
            self.plugin._config["cookie"] = raw
            with self.assertRaises(ValueError) as error:
                self.plugin._get_cookies()
            self.assertNotIn("private-bduss", str(error.exception))
        self.plugin._load_cookiecloud.assert_not_called()

    def test_cloud_filters_scope_expiry_and_duplicate_names(self):
        self.plugin._config["cookie_source"] = "cookiecloud"
        self.plugin._load_cookiecloud = Mock(return_value={"baidu.com": [
            cookie(value="parent"), cookie(domain="tieba.baidu.com"), cookie("STOKEN", "stoken"),
            cookie("bad-domain", domain="tieba.baidu.com.evil.test"), cookie("passport", domain="passport.baidu.com"),
            cookie("expired", expirationDate=time.time()-1), cookie("wrong-path", path="/passport"),
            cookie("parent-host", hostOnly=True)]})
        self.assertEqual(self.plugin._get_cookies(), {"BDUSS": "private-bduss", "STOKEN": "stoken"})

    def test_unknown_source_rejected(self):
        self.plugin._config["cookie_source"] = "unknown"
        with self.assertRaisesRegex(ValueError, "来源无效"):
            self.plugin._get_cookies()

    def test_local_cookiecloud_decrypts_with_configured_key(self):
        crypto_utils.decrypt.return_value = json.dumps({"cookie_data": {"baidu.com": [cookie()]}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "private-key.json").write_text('{"encrypted":"ciphertext"}', encoding="utf-8")
            with patch.object(settings, "COOKIECLOUD_ENABLE_LOCAL", True), patch.object(settings, "COOKIE_PATH", Path(directory), create=True):
                self.assertIn("baidu.com", self.plugin._load_cookiecloud())
        crypto_utils.decrypt.assert_called_with("ciphertext", md5(b"private-key-private-password").hexdigest()[:16].encode())

    def test_remote_cookiecloud_fallback_and_safe_error(self):
        invalid = Mock(status_code=200)
        invalid.json.side_effect = ValueError("private-key")
        valid = Mock(status_code=200)
        valid.json.return_value = {"cookie_data": {"baidu.com": [cookie()]}}
        with patch.object(requests, "get", side_effect=[invalid, valid]) as get:
            self.assertIn("baidu.com", self.plugin._load_cookiecloud())
        self.assertEqual(get.call_count, 2)
        with patch.object(requests, "get", side_effect=requests.ConnectionError("private-key")):
            with self.assertRaises(ValueError) as error:
                self.plugin._load_cookiecloud()
        self.assertNotIn("private-key", str(error.exception))

    def test_signature_sorts_fields_before_hashing(self):
        data = {"kw": "python", "tbs": "test", "BDUSS": "dummy"}
        expected = md5(b"BDUSS=dummykw=pythontbs=testtiebaclient!!!").hexdigest().upper()
        self.assertEqual(self.plugin._signed_data(data), {**data, "sign": expected})
        self.assertNotIn("sign", data)

    def test_request_uses_https_timeout_and_disables_redirects(self):
        session = Mock()
        response = session.request.return_value
        response.status_code = 200
        response.json.return_value = {"is_login": 0}
        self.plugin._request_json(session, "GET", self.plugin._web_url + "/dc/common/tbs")
        self.assertEqual(session.request.call_args.kwargs["timeout"], 25)
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        self.assertTrue(session.request.call_args.args[1].startswith("https://"))
        response.status_code = 403
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            self.plugin._request_json(session, "GET", self.plugin._web_url)
        response.status_code = 200
        response.json.side_effect = ValueError("private-bduss")
        with self.assertRaisesRegex(ValueError, "非JSON"):
            self.plugin._request_json(session, "GET", self.plugin._web_url)

    def test_tbs_requires_both_login_and_token(self):
        self.plugin._request_json = Mock(return_value={"is_login": 0, "tbs": "anonymous"})
        with self.assertRaisesRegex(ValueError, "登录态"):
            self.plugin._get_tbs(Mock())
        self.plugin._request_json.return_value = {"is_login": "1"}
        with self.assertRaisesRegex(ValueError, "TBS"):
            self.plugin._get_tbs(Mock())
        self.plugin._request_json.return_value = {"is_login": 1, "tbs": "private-tbs"}
        self.assertEqual(self.plugin._get_tbs(Mock()), "private-tbs")

    def test_paginated_grouped_and_list_formats_are_merged_and_deduped(self):
        self.plugin._request_json = Mock(side_effect=[
            page({"non-gconforum": [forum("a")], "gconforum": forum("b", 2)}, "1"),
            page([forum("b", 2), forum("c", 3)])])
        result = self.plugin._get_forums(Mock(), "private-bduss", self.plugin._stop_event)
        self.assertEqual([item["name"] for item in result], ["a", "b", "c"])
        calls = self.plugin._request_json.call_args_list
        self.assertEqual([call.args[3]["page_no"] for call in calls], ["1", "2"])
        self.assertIn("sign", calls[0].args[3])

    def test_bad_list_or_later_page_failure_does_not_silently_return_partial_list(self):
        bad_pages = [{"error_code": 1}, {"error_code": 0}, page({"unexpected": []}),
                     page([{}]), page([], "invalid")]
        for bad in bad_pages:
            self.plugin._request_json = Mock(side_effect=[page([forum("first")], "1"), bad])
            with self.assertRaises(ValueError):
                self.plugin._get_forums(Mock(), "bduss", self.plugin._stop_event)

    def test_repeated_page_is_detected(self):
        self.plugin._request_json = Mock(return_value=page([forum("same")], "1"))
        with self.assertRaisesRegex(ValueError, "分页重复"):
            self.plugin._get_forums(Mock(), "bduss", self.plugin._stop_event)
        self.assertEqual(self.plugin._request_json.call_count, 2)

    def test_success_repeat_and_unknown_code_are_separate(self):
        for code, kind in ((0, "signed"), ("160002", "already"), ("340006", "failed"), (None, "failed")):
            self.plugin._request_json = Mock(return_value={"error_code": code, "error_msg": "private-bduss"})
            result = self.plugin._sign_forum(Mock(), "private-bduss", "private-tbs", forum("python"))
            self.assertEqual(result["kind"], kind)
            self.assertNotIn("private-bduss", json.dumps(result))

    def test_per_forum_counts_and_notification(self):
        self.prepare_run()
        self.plugin._sign_forum = Mock(side_effect=[
            {"name": "python", "kind": "signed", "status": "签到成功"},
            {"name": "linux", "kind": "already", "status": "今日已签到"}])
        result = self.plugin.sign()
        self.assertTrue(result["success"])
        self.assertEqual(result["counts"]["signed"], 1)
        self.assertEqual(result["counts"]["already"], 1)
        self.plugin._stop_event.wait.assert_called_once_with(5)
        msg = self.plugin.post_message.call_args.kwargs
        self.assertIn("✅", msg["title"])
        self.assertIn("关注贴吧：2", msg["text"])
        self.assertEqual(msg["mtype"], "site")
        self.assertNotIn("private-bduss", json.dumps(self.plugin.save_data.call_args.kwargs))
        self.assertNotIn("private-tbs", msg["text"])

    def test_timeout_marks_unknown_without_retrying_same_forum(self):
        self.prepare_run()
        self.plugin._sign_forum = Mock(side_effect=[requests.Timeout("private-bduss"),
            {"name": "linux", "kind": "signed", "status": "签到成功"}])
        result = self.plugin.sign()
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "部分完成")
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(self.plugin._sign_forum.call_count, 2)
        self.assertNotIn("private-bduss", json.dumps(result))

    def test_captcha_stops_later_forums(self):
        self.prepare_run()
        self.plugin._request_json = Mock(return_value={"error_code": "2150040"})
        result = self.plugin.sign()
        self.assertEqual(result["counts"]["failed"], 1)
        self.assertEqual(result["counts"]["skipped"], 1)
        self.assertEqual(self.plugin._request_json.call_count, 1)
        self.plugin._stop_event.wait.assert_not_called()

    def test_http_or_format_error_stops_further_submissions(self):
        self.prepare_run()
        self.plugin._sign_forum = Mock(side_effect=ValueError("贴吧接口返回HTTP 403"))
        result = self.plugin.sign()
        self.assertEqual(result["counts"]["unknown"], 1)
        self.assertEqual(result["counts"]["skipped"], 1)
        self.plugin._sign_forum.assert_called_once()

    def test_cancel_during_interval_skips_remaining_forums(self):
        self.prepare_run()
        self.plugin._stop_event.wait.return_value = True
        self.plugin._sign_forum = Mock(return_value={"name": "python", "kind": "signed", "status": "签到成功"})
        result = self.plugin.sign()
        self.assertEqual(result["counts"]["skipped"], 1)
        self.plugin._sign_forum.assert_called_once()

    def test_no_followed_forums_does_not_submit_sign(self):
        self.prepare_run([])
        self.plugin._sign_forum = Mock()
        result = self.plugin.sign()
        self.assertEqual(result["total"], 0)
        self.assertIn("无需签到", result["status"])
        self.plugin._sign_forum.assert_not_called()

    def test_login_or_list_failure_is_saved_and_notified(self):
        self.plugin._get_tbs = Mock(side_effect=ValueError("登录态未通过验证"))
        self.plugin._get_forums = Mock()
        result = self.plugin.sign()
        self.assertFalse(result["success"])
        self.plugin._get_forums.assert_not_called()
        self.plugin.save_data.assert_called_once()
        self.plugin.post_message.assert_called_once()

    def test_notifications_are_bounded_and_history_keeps_all_details(self):
        self.prepare_run([forum(str(i), str(i)) for i in range(80)])
        self.plugin._sign_forum = Mock(side_effect=lambda s, b, t, f: {"name": f["name"], "kind": "failed", "status": "签到失败"})
        self.plugin.get_data.return_value = [{}] * 100
        result = self.plugin.sign()
        self.assertEqual(len(result["results"]), 80)
        self.assertEqual(len(self.plugin.save_data.call_args.kwargs["value"]), 100)
        self.assertLess(len(self.plugin.post_message.call_args.kwargs["text"]), 3500)
        self.assertIn("另有 65 条", self.plugin.post_message.call_args.kwargs["text"])

    def test_storage_and_notification_failures_preserve_result_and_unlock(self):
        self.prepare_run([])
        self.plugin.save_data.side_effect = RuntimeError("private-bduss")
        self.plugin.post_message.side_effect = RuntimeError("private-tbs")
        self.assertTrue(self.plugin.sign()["success"])
        self.assertFalse(self.plugin._lock.locked())

    def test_notify_switch_and_overlap(self):
        self.prepare_run([])
        self.plugin._config["notify"] = False
        self.plugin.sign()
        self.plugin.post_message.assert_not_called()
        self.plugin._get_tbs.reset_mock()
        self.plugin._lock.acquire()
        self.assertIsNone(self.plugin.sign())
        self.plugin._get_tbs.assert_not_called()
        self.plugin._lock.release()

    def test_immediate_run_schedule_and_stopping(self):
        old_event = self.plugin._stop_event
        self.plugin.update_config = Mock()
        scheduler, trigger = Mock(), Mock()
        with patch.dict(namespace, BackgroundScheduler=scheduler, CronTrigger=trigger):
            self.plugin.init_plugin({"onlyonce": True, "cookie_source": "manual", "cookie": "BDUSS=private-bduss"})
            old_event.set.assert_called_once()
            self.assertFalse(self.plugin._stop_event.is_set())
            self.assertFalse(self.plugin.update_config.call_args.args[0]["onlyonce"])
            self.assertEqual(self.plugin.update_config.call_args.args[0]["cookie"], "BDUSS=private-bduss")
            self.plugin._config["enabled"] = True
            self.assertEqual(self.plugin.get_service()[0]["id"], "tiebasigncc")
            trigger.from_crontab.assert_called_with("0 9 * * *", timezone="Asia/Shanghai")
            self.plugin.stop_service()
            self.assertTrue(self.plugin._stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
