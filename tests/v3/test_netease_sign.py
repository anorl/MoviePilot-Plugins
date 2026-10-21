"""网易云登录Cookie筛选、WEAPI协议、各端结果及MP通知回归测试。"""
import ast
import base64
import json
import re
import secrets
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from hashlib import md5
from pathlib import Path
from threading import Lock
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


ROOT = Path(__file__).parents[2]
source = ROOT / "plugins.v3/neteasemusicsigncc/__init__.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
settings = types.SimpleNamespace(TZ="Asia/Shanghai", COOKIECLOUD_KEY="private-key",
                                 COOKIECLOUD_PASSWORD="private-password", COOKIECLOUD_HOST="https://cc.invalid",
                                 COOKIECLOUD_ENABLE_LOCAL=False)
crypto_utils = Mock()
namespace = dict(globals(), _PluginBase=object, settings=settings, logger=Mock(),
                 CryptoJsUtils=crypto_utils, NotificationType=types.SimpleNamespace(SiteMessage="site"))
exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(source), "exec"), namespace)
Plugin = namespace["neteasemusicsigncc"]


def cookie(name="MUSIC_U", value="private-token", **kwargs):
    return dict(name=name, value=value, domain=".music.163.com", path="/", **kwargs)


class NeteaseSignTests(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.plugin._lock = Lock()
        self.plugin._config = {"notify": True, "sign_type": "both", "cookie_source": "manual",
                               "cookie": "MUSIC_U=private-token; __csrf=private-csrf"}
        self.plugin.get_data = Mock(return_value=[])
        self.plugin.save_data = Mock()
        self.plugin.post_message = Mock()

    def test_market_identity_matches_plugin(self):
        metadata = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))["neteasemusicsigncc"]
        self.assertEqual(metadata["name"], self.plugin.plugin_name)
        self.assertEqual(metadata["description"], self.plugin.plugin_desc)
        self.assertEqual(metadata["icon"], self.plugin.plugin_icon)

    def test_manual_cookie_preserves_equals_and_never_reads_cloud(self):
        self.plugin._config["cookie"] = "Cookie: MUSIC_U=token==; __csrf=csrf-value; other=ok"
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError())
        self.assertEqual(self.plugin._get_cookies(), {"MUSIC_U": "token==", "__csrf": "csrf-value", "other": "ok"})
        self.plugin._load_cookiecloud.assert_not_called()

    def test_invalid_cookie_does_not_leak_values_or_fall_back(self):
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError())
        for raw in ("", "MUSIC_A=private-token", "MUSIC_U=private-token\r\nx", "MUSIC_U=ok; bad name=private-token"):
            self.plugin._config["cookie"] = raw
            with self.assertRaises(ValueError) as error:
                self.plugin._get_cookies()
            self.assertNotIn("private-token", str(error.exception))
        self.plugin._load_cookiecloud.assert_not_called()

    def test_cloud_domain_expiry_path_and_name_conflicts(self):
        self.plugin._config["cookie_source"] = "cookiecloud"
        items = [dict(cookie(), domain=".163.com", value="parent"),
                 cookie(), cookie("__csrf", "csrf"), cookie("expired", expirationDate=time.time()-2),
                 dict(cookie("foreign"), domain="music.163.com.evil.test"),
                 dict(cookie("wrong-path"), path="/weapi/point/dailyTask-other"),
                 dict(cookie("host-only"), domain="163.com", hostOnly=True),
                 dict(cookie("other-domain"), domain="mail.163.com")]
        self.plugin._load_cookiecloud = Mock(return_value={"music.163.com": items})
        self.assertEqual(self.plugin._get_cookies(), {"MUSIC_U": "private-token", "__csrf": "csrf"})

    def test_cloud_auth_cookie_expired_is_rejected(self):
        self.plugin._config["cookie_source"] = "cookiecloud"
        self.plugin._load_cookiecloud = Mock(return_value={"music.163.com": [cookie(expirationDate=time.time()-2)]})
        with self.assertRaisesRegex(ValueError, "MUSIC_U"):
            self.plugin._get_cookies()

    def test_cookiecloud_local_decryption(self):
        crypto_utils.decrypt.return_value = json.dumps({"cookie_data": {"music.163.com": [cookie()]}}).encode()
        self.plugin._config["cookie_source"] = "cookiecloud"
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "private-key.json").write_text('{"encrypted":"ciphertext"}', encoding="utf-8")
            with patch.object(settings, "COOKIECLOUD_ENABLE_LOCAL", True), patch.object(settings, "COOKIE_PATH", Path(directory), create=True):
                self.assertEqual(self.plugin._get_cookies()["MUSIC_U"], "private-token")
        crypto_utils.decrypt.assert_called_with("ciphertext", md5(b"private-key-private-password").hexdigest()[:16].encode())

    def test_cookiecloud_remote_fallback_and_redacted_failure(self):
        invalid = Mock(status_code=200)
        invalid.json.side_effect = ValueError("private-key private-password")
        valid = Mock(status_code=200)
        valid.json.return_value = {"cookie_data": {"music.163.com": [cookie()]}}
        with patch.object(requests, "get", side_effect=[invalid, valid]) as get:
            self.assertIn("music.163.com", self.plugin._load_cookiecloud())
        self.assertEqual(get.call_count, 2)
        with patch.object(requests, "get", side_effect=requests.ConnectionError("https://cc.invalid/private-key")):
            with self.assertRaises(ValueError) as error:
                self.plugin._load_cookiecloud()
        self.assertNotIn("private-key", str(error.exception))

    def test_weapi_matches_independent_node_vector(self):
        with patch.object(secrets, "token_hex", return_value="0123456789abcdef"):
            result = self.plugin._weapi({"type": 0, "csrf_token": "test-csrf"})
        self.assertEqual(result, {
            "params": "5XU6N2rL263dxRhUX0TqM7119xV+Hm0LYb/1ig6do+2U1AXAnfEPrSXZ4J9muQNyyj9iLXAwzb4F3U04C1TTyvhboNdW2Npe68F1PhpoXJ0=",
            "encSecKey": "35701388baf89fed412e11269b9c76625d095ecaf17f03fa018abe19ea2d38b949debf242ee39a71ca1f6cda71b1b86a45aa909ee27f7e78e267d34e732f0de948206c3340a788d0003372183e2f753c1f78b66ac23d134ac1fc9b993156520ea826b8aa89a962d4491b4b8d7e08738e1da9b07aa39bf4a7ef0b1c210728cd52",
        })

    def test_request_contains_csrf_and_distinct_client_os(self):
        session = requests.Session()
        response = Mock(status_code=200)
        response.json.return_value = {"code": 200, "point": 3}
        session.post = Mock(return_value=response)
        with patch.object(self.plugin, "_weapi", return_value={"params": "encrypted", "encSecKey": "rsa"}) as encrypt:
            for sign_type, os_name in ((0, "android"), (1, "pc")):
                self.plugin._request_sign(session, {"MUSIC_U": "private-token", "__csrf": "private-csrf"}, sign_type)
                self.assertEqual(session.cookies.get("os"), os_name)
                encrypt.assert_called_with({"type": sign_type, "csrf_token": "private-csrf"})
                kwargs = session.post.call_args.kwargs
                self.assertFalse(kwargs["allow_redirects"])
                self.assertEqual(kwargs["params"], {"csrf_token": "private-csrf"})
                self.assertNotIn("private-token", str(kwargs["data"]))
        session.close()

    def test_response_http_html_and_bad_shape(self):
        response = Mock(status_code=403)
        session = Mock()
        session.post.return_value = response
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            self.plugin._request_sign(session, {"MUSIC_U": "token"}, 0)
        response.status_code = 200
        response.json.side_effect = ValueError("private-token")
        with self.assertRaisesRegex(ValueError, "非JSON"):
            self.plugin._request_sign(session, {"MUSIC_U": "token"}, 0)
        response.json.side_effect = None
        response.json.return_value = []
        with self.assertRaisesRegex(ValueError, "格式异常"):
            self.plugin._request_sign(session, {"MUSIC_U": "token"}, 0)

    def test_both_success_and_repeat_results(self):
        self.plugin._request_sign = Mock(side_effect=[{"code": 200, "point": 3}, {"code": -2, "point": 999}])
        record = self.plugin.sign()
        self.assertTrue(record["success"])
        self.assertEqual(record["point"], 3)
        self.assertEqual(record["results"][1]["status"], "今日已签到")
        self.assertEqual([call.args[2] for call in self.plugin._request_sign.call_args_list], [0, 1])
        self.plugin.post_message.assert_called_once()
        message = self.plugin.post_message.call_args.kwargs
        self.assertEqual(message["mtype"], "site")
        self.assertIn("移动端：签到成功", message["text"])
        self.assertIn("网页端：今日已签到", message["text"])
        self.assertNotIn("private-token", json.dumps(self.plugin.save_data.call_args.kwargs))

    def test_mixed_failure_is_not_reported_as_full_success(self):
        self.plugin._request_sign = Mock(side_effect=[{"code": 200, "point": 3}, {"code": 301}])
        record = self.plugin.sign()
        self.assertEqual(record["status"], "部分完成")
        self.assertFalse(record["success"])
        self.assertIn("登录态失效", self.plugin.post_message.call_args.kwargs["text"])

    def test_timeout_does_not_retry_and_other_side_still_runs(self):
        self.plugin._request_sign = Mock(side_effect=[requests.Timeout("private-token"), {"code": 200}])
        record = self.plugin.sign()
        self.assertEqual(self.plugin._request_sign.call_count, 2)
        self.assertIn("结果未确认", record["results"][0]["status"])
        self.assertNotIn("private-token", json.dumps(record))
        self.assertFalse(self.plugin._lock.locked())

    def test_single_type_and_invalid_mode(self):
        for mode in ("0", "1"):
            self.plugin._config["sign_type"] = mode
            self.plugin._request_sign = Mock(return_value={"code": 200})
            self.assertEqual(len(self.plugin._run_sign(Mock(), {"MUSIC_U": "token"})), 1)
            self.assertEqual(self.plugin._request_sign.call_args.args[2], int(mode))
        self.plugin._config["sign_type"] = "bad"
        with self.assertRaisesRegex(ValueError, "类型无效"):
            self.plugin._run_sign(Mock(), {})

    def test_login_cookie_error_has_history_and_failure_notification(self):
        self.plugin._config["cookie"] = "MUSIC_A=anonymous"
        record = self.plugin.sign()
        self.assertFalse(record["success"])
        self.plugin.save_data.assert_called_once()
        self.plugin.post_message.assert_called_once()
        self.assertIn("MUSIC_U", self.plugin.post_message.call_args.kwargs["text"])

    def test_history_limit_and_delivery_errors_preserve_result(self):
        self.plugin._request_sign = Mock(return_value={"code": 200, "point": 2})
        self.plugin.get_data.return_value = [{}] * 100
        self.plugin.post_message.side_effect = RuntimeError("private-token")
        self.assertTrue(self.plugin.sign()["success"])
        self.assertEqual(len(self.plugin.save_data.call_args.kwargs["value"]), 100)
        self.plugin.save_data.side_effect = RuntimeError("private-token")
        self.assertTrue(self.plugin.sign()["success"])
        self.assertFalse(self.plugin._lock.locked())

    def test_notify_can_be_disabled_and_overlap_is_skipped(self):
        self.plugin._config["notify"] = False
        self.plugin._request_sign = Mock(return_value={"code": -2})
        self.plugin.sign()
        self.plugin.post_message.assert_not_called()
        self.plugin._request_sign.reset_mock()
        self.plugin._lock.acquire()
        self.assertIsNone(self.plugin.sign())
        self.plugin._request_sign.assert_not_called()
        self.plugin._lock.release()

    def test_immediate_run_persists_cookie_and_schedule_is_unique(self):
        self.plugin.update_config = Mock()
        scheduler = Mock()
        trigger = Mock()
        with patch.dict(namespace, BackgroundScheduler=scheduler, CronTrigger=trigger):
            self.plugin.init_plugin({**self.plugin._config, "onlyonce": True})
            saved = self.plugin.update_config.call_args.args[0]
            self.assertFalse(saved["onlyonce"])
            self.assertIn("MUSIC_U", saved["cookie"])
            scheduler.return_value.start.assert_called_once()
            self.plugin._config["enabled"] = True
            self.assertEqual(self.plugin.get_service()[0]["id"], "neteasemusicsigncc")
            trigger.from_crontab.assert_called_with("0 9 * * *", timezone="Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
