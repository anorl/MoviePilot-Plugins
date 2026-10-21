"""在无 MoviePilot 环境中加载真实插件类，验证登录态、协议和执行结果。"""
import ast
import base64
import json
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from hashlib import md5
from pathlib import Path
from threading import Lock
from unittest.mock import Mock, patch
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


ROOT = Path(__file__).parents[2]
source = ROOT / "plugins.v3/fengsigncc/__init__.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
plugin_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
settings = types.SimpleNamespace(TZ="Asia/Shanghai", COOKIECLOUD_KEY="test-key",
                                 COOKIECLOUD_PASSWORD="test-password", COOKIECLOUD_ENABLE_LOCAL=False,
                                 COOKIECLOUD_HOST="https://cc.invalid")
crypto_utils = Mock()
namespace = dict(globals(), _PluginBase=object, settings=settings, logger=Mock(),
                 CryptoJsUtils=crypto_utils, NotificationType=types.SimpleNamespace(SiteMessage="site"))
exec(compile(ast.fix_missing_locations(ast.Module(body=[plugin_class], type_ignores=[])),
             str(source), "exec"), namespace)
Plugin = namespace["fengsigncc"]


def payload(code=0, data=None):
    return {"status": {"code": code}, "data": data or {}}


def cookie(name="userInfo", domain="www.feng.com", **kwargs):
    return dict(name=name, domain=domain, path="/", value=quote(json.dumps({
        "accessToken": "private-token", "userInfo": {"userName": "测试用户"}})), **kwargs)


class FengSignTests(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.plugin._lock = Lock()
        self.plugin._config = {"notify": False}

    def test_cookie_json_and_shared_fallback(self):
        for name in ("userInfo", "userInfo-shared"):
            self.assertEqual(self.plugin._extract_identity({"feng.com": [cookie(name=name)]}),
                             ("private-token", "测试用户"))
        primary = cookie()
        primary["value"] = json.dumps({"accessToken": "primary"})
        self.assertEqual(self.plugin._extract_identity({"feng.com": [cookie(name="userInfo-shared"), primary]})[0], "primary")

    def test_manual_cookie_does_not_read_cookiecloud(self):
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError("不应读取CookieCloud"))
        login = cookie()
        for prefix in ("", "Cookie: "):
            with self.subTest(prefix=prefix):
                self.plugin._config.update(cookie_source="manual", cookie=prefix + "other=x; userInfo=" + login["value"])
                self.assertEqual(self.plugin._get_identity(), ("private-token", "测试用户"))
        self.plugin._load_cookiecloud.assert_not_called()

    def test_manual_shared_cookie_supports_raw_json_and_equals_in_token(self):
        self.plugin._config.update(cookie_source="manual", cookie="userInfo-shared=" + json.dumps({
            "accessToken": "private=token==", "userInfo": {"userName": "测试用户"}}))
        self.assertEqual(self.plugin._get_identity(), ("private=token==", "测试用户"))

    def test_invalid_manual_cookie_fails_without_cloud_fallback_or_secret_leak(self):
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError("不应回退CookieCloud"))
        for raw in ("", "other=private-token", "userInfo=%7Bprivate-token",
                    "userInfo=" + quote(json.dumps({"accessToken": "private-token\r\nvalue"}))):
            with self.subTest(raw=raw):
                self.plugin._config.update(cookie_source="manual", cookie=raw)
                with self.assertRaises(ValueError) as error:
                    self.plugin._get_identity()
                self.assertNotIn("private-token", str(error.exception))
        self.plugin._load_cookiecloud.assert_not_called()

    def test_existing_config_defaults_to_cookiecloud_and_ignores_manual_cookie(self):
        self.plugin._config["cookie"] = "userInfo=invalid-manual-cookie"
        self.plugin._load_cookiecloud = Mock(return_value={"feng.com": [cookie()]})
        self.assertEqual(self.plugin._get_identity(), ("private-token", "测试用户"))
        self.plugin._load_cookiecloud.assert_called_once()

    def test_manual_mode_sign_uses_selected_identity_without_storing_credentials_in_history(self):
        self.plugin._config.update(cookie_source="manual", cookie="userInfo=" + cookie()["value"])
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError("不应读取CookieCloud"))
        self.plugin._run_sign = Mock(return_value=("今日已签到", {"signInCount": 4}))
        self.plugin.get_data = Mock(return_value=[])
        self.plugin.save_data = Mock()
        result = self.plugin.sign()
        self.assertTrue(result["success"])
        self.assertEqual(self.plugin._run_sign.call_args.args[1], "private-token")
        self.plugin._load_cookiecloud.assert_not_called()
        self.assertNotIn("private-token", json.dumps(self.plugin.save_data.call_args.kwargs["value"]))

    def test_unknown_cookie_source_does_not_silently_use_another_account(self):
        self.plugin._config["cookie_source"] = "unknown"
        self.plugin._load_cookiecloud = Mock()
        with self.assertRaisesRegex(ValueError, "Cookie来源无效"):
            self.plugin._get_identity()
        self.plugin._load_cookiecloud.assert_not_called()

    def test_expired_unrelated_domain_and_bad_json_are_rejected(self):
        invalid = [cookie(expirationDate=time.time()-1), cookie(domain="evilfeng.com"),
                   cookie(domain="www.feng.com.evil.test")]
        invalid.append(dict(cookie(), path="/other"))
        invalid.append(dict(cookie(), value="%7Bbroken"))
        invalid.append(dict(cookie(), value=json.dumps({"accessToken": "bad\r\nvalue"})))
        with self.assertRaisesRegex(ValueError, "没有有效"):
            self.plugin._extract_identity({"feng.com": invalid})

    def test_signature_matches_fixed_aes_vector(self):
        # 独立的 Node crypto AES-128-CBC 结果，固定网页毫秒时间及六个零。
        self.assertEqual(self.plugin._request_id("/v1/user/experience", 1792569600000),
                         "3nPMB8QgqCxUv3YkSyaDL4oMAZQm8EQj5hOv874pm4XUssVMaQBgV+po3jLUEiJTQR1/ta9HhDdCPpmB3svxyA==")

    def test_cookiecloud_decryption_and_local_file(self):
        crypto_utils.decrypt.return_value = json.dumps({"cookie_data": {"feng.com": [cookie()]}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "test-key.json").write_text('{"encrypted":"ciphertext"}', encoding="utf-8")
            with patch.object(settings, "COOKIECLOUD_ENABLE_LOCAL", True), patch.object(settings, "COOKIE_PATH", Path(directory), create=True):
                self.assertIn("feng.com", self.plugin._load_cookiecloud())
        crypto_utils.decrypt.assert_called_with("ciphertext", md5(b"test-key-test-password").hexdigest()[:16].encode())

    def test_remote_cookiecloud_errors_do_not_leak_key_or_url(self):
        with patch.object(requests, "get", side_effect=requests.ConnectionError("https://cc.invalid/get/test-key")):
            with self.assertRaises(ValueError) as error:
                self.plugin._load_cookiecloud()
        self.assertNotIn("test-key", str(error.exception))
        self.assertNotIn("cc.invalid", str(error.exception))

    def test_remote_cookiecloud_payload(self):
        response = Mock(status_code=200)
        response.json.return_value = {"cookie_data": {"feng.com": [cookie()]}}
        with patch.object(requests, "get", return_value=response) as get:
            self.assertIn("feng.com", self.plugin._load_cookiecloud())
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_remote_cookiecloud_tries_next_address_after_invalid_data(self):
        valid = Mock(status_code=200)
        valid.json.return_value = {"cookie_data": {"feng.com": [cookie()]}}
        invalid_responses = [Mock(status_code=200), Mock(status_code=200), Mock(status_code=200)]
        invalid_responses[0].json.side_effect = requests.exceptions.JSONDecodeError("invalid", "<html>", 0)
        invalid_responses[1].json.return_value = {"unexpected": "format"}
        invalid_responses[2].json.return_value = {"encrypted": "bad-encryption"}
        for invalid in invalid_responses:
            with self.subTest(response=invalid):
                with patch.object(crypto_utils, "decrypt", side_effect=ValueError("test-password")), \
                        patch.object(requests, "get", side_effect=[invalid, valid]) as get:
                    self.assertIn("feng.com", self.plugin._load_cookiecloud())
                self.assertEqual(get.call_count, 2)
                self.assertEqual(get.call_args.args[0], "https://cc.invalid/get/test-key.json")

    def test_all_remote_failures_are_summarized_without_secrets(self):
        bad_json = Mock(status_code=200)
        bad_json.json.side_effect = ValueError("test-password test-key private-token")
        invalid = Mock(status_code=200)
        invalid.json.return_value = {"unexpected": "format"}
        with patch.object(requests, "get", side_effect=[
                Mock(status_code=404), bad_json,
                requests.ConnectionError("https://cc.invalid/get/test-key"), invalid]) as get:
            with self.assertRaises(ValueError) as error:
                self.plugin._load_cookiecloud()
        self.assertEqual(get.call_count, 4)
        message = str(error.exception)
        for expected in ("候选1: HTTP 404", "候选2: 数据格式或解密错误", "候选3: 网络请求失败", "候选4: 数据格式或解密错误"):
            self.assertIn(expected, message)
        for secret in ("test-key", "test-password", "private-token", "cc.invalid"):
            self.assertNotIn(secret, message)

    def test_already_signed_never_posts(self):
        self.plugin._request = Mock(return_value=payload("0", {"isSignedIn": "yes", "signInCount": 5}))
        status, _ = self.plugin._run_sign(Mock(), "token")
        self.assertEqual(status, "今日已签到")
        self.assertEqual(self.plugin._request.call_count, 1)

    def test_success_and_failed_followup_preserve_sign_result(self):
        finals = (payload(0, {"isSignedIn": True, "signInCount": 8}),
                  requests.Timeout("secret"), payload(1002),
                  {"status": {"code": 0}, "data": []}, ValueError("登录态失效"))
        for index, final in enumerate(finals):
            with self.subTest(final=final):
                self.plugin._request = Mock(side_effect=[
                    payload(0, {"isSignedIn": False, "signInCount": 7, "currentExperience": 20}),
                    payload(0), final])
                status, data = self.plugin._run_sign(Mock(), "token")
                self.assertEqual(status, "签到成功")
                self.assertEqual(data, {"isSignedIn": True, "signInCount": 8} if index == 0 else {})

    def test_rejected_post_requires_confirmed_signed_state(self):
        for confirmed in (True, False):
            self.plugin._request = Mock(side_effect=[payload(0), payload(1021), payload(0, {"isSignedIn": confirmed})])
            if confirmed:
                self.assertEqual(self.plugin._run_sign(Mock(), "token")[0], "今日已签到")
            else:
                with self.assertRaisesRegex(ValueError, "拒绝签到"):
                    self.plugin._run_sign(Mock(), "token")

    def test_post_timeout_is_not_retried(self):
        for followup in (payload(0, {"isSignedIn": False}), requests.Timeout(),
                         payload(1002), ValueError("登录态失效")):
            with self.subTest(followup=followup):
                self.plugin._request = Mock(side_effect=[payload(0), requests.Timeout(), followup])
                with self.assertRaisesRegex(ValueError, "结果未确认"):
                    self.plugin._run_sign(Mock(), "token")
                methods = [call.args[1] for call in self.plugin._request.call_args_list]
                self.assertEqual(methods, ["GET", "POST", "GET"])

    def test_post_network_error_is_confirmed_by_read_only_query(self):
        for error in (requests.Timeout("private-token"), requests.ConnectionError("private-token")):
            self.plugin._request = Mock(side_effect=[
                payload(0, {"isSignedIn": False}), error,
                payload(0, {"isSignedIn": "yes", "signInCount": 8})])
            status, data = self.plugin._run_sign(Mock(), "token")
            self.assertIn("查询确认", status)
            self.assertEqual(data["signInCount"], 8)
            self.assertEqual([call.args[1] for call in self.plugin._request.call_args_list], ["GET", "POST", "GET"])

    def test_request_headers_and_auth_failure(self):
        session = Mock()
        session.request.return_value = Mock(status_code=200)
        session.request.return_value.json.return_value = payload(1001)
        with self.assertRaisesRegex(ValueError, "登录态已失效"):
            self.plugin._request(session, "GET", self.plugin._experience_path, "private-token")
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Access-Token"], "private-token")
        self.assertFalse(kwargs["allow_redirects"])
        session.request.return_value.status_code = 403
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            self.plugin._request(session, "GET", self.plugin._experience_path, "token")
        session.request.return_value.status_code = 200
        session.request.return_value.json.side_effect = ValueError("<html>secret</html>")
        with self.assertRaisesRegex(ValueError, "非JSON"):
            self.plugin._request(session, "GET", self.plugin._experience_path, "token")

    def test_history_records_result_without_credentials(self):
        self.plugin._load_cookiecloud = Mock(return_value={"feng.com": [cookie()]})
        self.plugin._run_sign = Mock(return_value=("签到成功", {"signInCount": "4", "currentExperience": "20"}))
        self.plugin.get_data = Mock(return_value=[{}] * 100)
        self.plugin.save_data = Mock()
        record = self.plugin.sign()
        self.assertTrue(record["success"])
        self.assertEqual(record["signInCount"], "4")
        history = self.plugin.save_data.call_args.kwargs["value"]
        self.assertEqual(len(history), 100)
        self.assertNotIn("private-token", json.dumps(history))
        self.assertFalse(self.plugin._lock.locked())

    def test_history_and_notification_failures_do_not_change_sign_result(self):
        for read_fails, save_fails, notify_fails in ((True, False, False), (False, True, False),
                                                    (False, False, True), (True, False, True)):
            with self.subTest(read=read_fails, save=save_fails, notify=notify_fails):
                self.plugin._config["notify"] = True
                self.plugin._load_cookiecloud = Mock(return_value={"feng.com": [cookie()]})
                self.plugin._run_sign = Mock(return_value=("签到成功", {"signInCount": 4}))
                self.plugin.get_data = Mock(return_value=[])
                self.plugin.save_data = Mock()
                self.plugin.post_message = Mock()
                if read_fails:
                    self.plugin.get_data.side_effect = RuntimeError("private-token")
                if save_fails:
                    self.plugin.save_data.side_effect = RuntimeError("private-token")
                if notify_fails:
                    self.plugin.post_message.side_effect = RuntimeError("private-token")
                with patch.dict(namespace, logger=Mock()):
                    result = self.plugin.sign()
                    log = namespace["logger"]
                self.assertTrue(result["success"])
                self.assertEqual(result["status"], "签到成功")
                self.plugin.post_message.assert_called_once()
                if not read_fails:
                    self.plugin.save_data.assert_called_once()
                self.assertNotIn("private-token", str(log.mock_calls))
                self.assertFalse(self.plugin._lock.locked())

    def test_immediate_run_is_reset_and_scheduled_even_when_disabled(self):
        scheduler_factory = Mock()
        self.plugin.update_config = Mock()
        with patch.dict(namespace, BackgroundScheduler=scheduler_factory):
            self.plugin.init_plugin({"enabled": False, "onlyonce": True,
                                     "cookie_source": "manual", "cookie": "userInfo=test-cookie"})
        self.assertFalse(self.plugin.update_config.call_args.args[0]["onlyonce"])
        self.assertEqual(self.plugin.update_config.call_args.args[0]["cookie_source"], "manual")
        self.assertEqual(self.plugin.update_config.call_args.args[0]["cookie"], "userInfo=test-cookie")
        scheduler_factory.return_value.add_job.assert_called_once()
        scheduler_factory.return_value.start.assert_called_once()
        self.assertEqual(self.plugin.get_service(), [])

    def test_schedule_uses_own_id_and_moviepilot_timezone(self):
        trigger = Mock()
        with patch.dict(namespace, CronTrigger=trigger):
            self.plugin._config.update(enabled=True, cron="0 9 * * *")
            service = self.plugin.get_service()[0]
            self.assertEqual(service["id"], "fengsigncc")
            trigger.from_crontab.assert_called_once_with("0 9 * * *", timezone="Asia/Shanghai")
            trigger.from_crontab.side_effect = ValueError()
            self.assertEqual(self.plugin.get_service(), [])

    def test_timeout_records_uncertainty_and_releases_lock(self):
        self.plugin._load_cookiecloud = Mock(return_value={"feng.com": [cookie()]})
        self.plugin._run_sign = Mock(side_effect=requests.Timeout("private-token"))
        self.plugin.get_data = Mock(return_value=[])
        self.plugin.save_data = Mock()
        result = self.plugin.sign()
        self.assertFalse(result["success"])
        self.assertIn("结果未确认", result["status"])
        self.assertNotIn("private-token", json.dumps(result))
        self.assertFalse(self.plugin._lock.locked())


if __name__ == "__main__":
    unittest.main()
