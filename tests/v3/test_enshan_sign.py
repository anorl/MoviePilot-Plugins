"""恩山登录态、formhash、签到确认和浏览器人工验证分支测试。"""
import ast
import base64
import json
import re
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from hashlib import md5
from pathlib import Path
from threading import Lock
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).parents[2]
source = ROOT / "plugins.v3/enshansigncc/__init__.py"
cls = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body if isinstance(n, ast.ClassDef))
settings = types.SimpleNamespace(TZ="Asia/Shanghai", COOKIECLOUD_ENABLE_LOCAL=False,
                                 COOKIECLOUD_KEY="private-key", COOKIECLOUD_PASSWORD="private-password",
                                 COOKIECLOUD_HOST="https://cc.invalid", PROXY="http://proxy.invalid:8080")
crypto_utils = Mock()
namespace = dict(globals(), _PluginBase=object, logger=Mock(), settings=settings,
                 CryptoJsUtils=crypto_utils, NotificationType=types.SimpleNamespace(SiteMessage="site"))
exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(source), "exec"), namespace)
Plugin = namespace["enshansigncc"]

LOGIN = '''<html><div id="um"><strong class="vwmy"><a href="home.php?mod=space&amp;uid=2">测试用户</a></strong>
<a href="member.php?mod=logging&amp;action=logout&amp;formhash=abcd1234">退出</a></div>
<input type="hidden" value="abcd1234" name="formhash"><button>立即签到</button></html>'''
SIGNED = LOGIN.replace("立即签到", "今日已签到") + '<span class="erqd-continuous-days">9</span><span class="erqd-total-days">20</span>'
WAF = '<html>请完成以下操作，验证您是真人<script>appkey="CF_APP_WAF"; var requestInfo={token:"private-waf-token"};</script></html>'


def response(text="", payload=None, status=200, headers=None):
    r = Mock(text=text, status_code=status, headers=headers or {}, encoding="utf-8")
    r.json.return_value = payload
    return r


def cookie(name="TWcq_2132_auth", value="private-auth", **kwargs):
    return {"name": name, "value": value, "domain": ".right.com.cn", "path": "/forum/", **kwargs}


class EnshanSignTests(unittest.TestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.plugin._lock = Lock()
        self.plugin._config = {"notify": True, "browser_fallback": False, "cookie_source": "manual",
                               "cookie": "TWcq_2132_auth=private-auth; TWcq_2132_saltkey=salt"}
        self.plugin.get_data = Mock(return_value=[])
        self.plugin.save_data = Mock()
        self.plugin.post_message = Mock()

    def browser(self, html=LOGIN, reply=None):
        context, page = Mock(), Mock()
        context.new_page.return_value = page
        def goto(url, **_):
            page.url = url
            return types.SimpleNamespace(status=200)
        page.goto.side_effect = goto
        page.content.side_effect = lambda: '<p>恩山币: 12.5 积分: 30</p>' if 'showcredit' in page.url else html
        page.evaluate.return_value = reply if reply is not None else {"status": 200, "data": {"success": True, "continuous_days": 9}}
        self.plugin._launch_browser = Mock(return_value=context)
        self.plugin._slider_ready = Mock(return_value=True)
        return context, page

    def test_market_identity_and_history(self):
        meta = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))["enshansigncc"]
        for key, attr in (("name", "plugin_name"), ("description", "plugin_desc"), ("icon", "plugin_icon"), ("version", "plugin_version")):
            self.assertEqual(meta[key], getattr(self.plugin, attr))
        self.assertEqual(next(iter(meta["history"])), "v1.0.4")
        self.assertTrue((ROOT / "icons/enshansigncc.ico").exists())

    def test_manual_cookie_preserves_encoding_and_never_reads_cloud(self):
        self.plugin._config["cookie"] = "Cookie: TWcq_2132_auth=abc%2F==; TWcq_2132_saltkey=salt"
        self.plugin._load_cookiecloud = Mock(side_effect=AssertionError())
        self.assertEqual(self.plugin._get_cookies()["TWcq_2132_auth"], "abc%2F==")
        self.plugin._load_cookiecloud.assert_not_called()

    def test_auth_required_and_bad_auth_rejected_without_leaks(self):
        for raw in ("", "other=private-auth", "TWcq_2132_auth=private-auth\r\ninvalid"):
            self.plugin._config["cookie"] = raw
            with self.assertRaises(ValueError) as error:
                self.plugin._get_cookies()
            self.assertNotIn("private-auth", str(error.exception))

    def test_cloud_filters_expiry_scope_path_and_bad_auxiliary_fields(self):
        self.plugin._config["cookie_source"] = "cookiecloud"
        self.plugin._load_cookiecloud = Mock(return_value={"right.com.cn": [
            cookie(path="/", value="parent"), cookie(), cookie("TWcq_2132_saltkey", "salt"),
            cookie("bad:name"), cookie("bad-value", "private\tvalue"),
            cookie("expired", expirationDate=time.time()-2), cookie("foreign", domain="www.right.com.cn.evil.test"),
            cookie("host-only", domain="right.com.cn", hostOnly=True), cookie("path", path="/other")]})
        self.assertEqual(self.plugin._get_cookies(), {"TWcq_2132_auth": "private-auth", "TWcq_2132_saltkey": "salt"})

    def test_local_cookiecloud_decryption(self):
        crypto_utils.decrypt.return_value = json.dumps({"cookie_data": {"right.com.cn": [cookie()]}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "private-key.json").write_text('{"encrypted":"ciphertext"}', encoding="utf-8")
            with patch.object(settings, "COOKIECLOUD_ENABLE_LOCAL", True), patch.object(settings, "COOKIE_PATH", Path(directory), create=True):
                self.assertIn("right.com.cn", self.plugin._load_cookiecloud())
        crypto_utils.decrypt.assert_called_with("ciphertext", md5(b"private-key-private-password").hexdigest()[:16].encode())

    def test_remote_cookiecloud_fallback_and_redaction(self):
        invalid, valid = Mock(status_code=200), Mock(status_code=200)
        invalid.json.side_effect = ValueError("private-key")
        valid.json.return_value = {"cookie_data": {"right.com.cn": [cookie()]}}
        with patch.object(requests, "get", side_effect=[invalid, valid]) as get:
            self.assertIn("right.com.cn", self.plugin._load_cookiecloud())
        self.assertEqual(get.call_count, 2)

    def test_login_requires_authenticated_marker_not_just_formhash(self):
        with self.assertRaisesRegex(ValueError, "未确认登录"):
            self.plugin._parse_login_page('<input name="formhash" value="abcd1234"><p>请登录</p>')
        self.assertEqual(self.plugin._parse_login_page(LOGIN), ("abcd1234", "测试用户"))
        without_input = re.sub(r'<input[^>]+>', '', LOGIN)
        self.assertEqual(self.plugin._parse_login_page(without_input)[0], "abcd1234")

    def test_signed_state_ignores_script_leaderboard_and_historical_days(self):
        html = LOGIN + '<script>var success="今日已签到";</script><div>历史记录：今日已签到 连续99天</div><span class="erqd-continuous-days">99</span>'
        self.assertEqual(self.plugin._parse_sign_page(html), (False, {}))
        self.assertEqual(self.plugin._parse_sign_page(SIGNED), (True, {"continuous_days": 9, "total_days": 20}))

    def test_json_success_and_already_are_explicit(self):
        self.assertEqual(self.plugin._parse_result({"success": True, "continuous_days": "9"})["continuous_days"], 9)
        self.assertEqual(self.plugin._parse_result({"success": False, "message": "您今天已经签到过了"})["status"], "今日已签到")
        for data in ({"success": "false", "message": "签到成功"}, {"message": "签到成功"}, {"success": False, "message": "尚未签到"}, []):
            with self.assertRaises(ValueError):
                self.plugin._parse_result(data)

    def test_server_raw_error_does_not_leak(self):
        with self.assertRaises(ValueError) as error:
            self.plugin._parse_result({"success": False, "message": "private-auth private-formhash"})
        self.assertNotIn("private-auth", str(error.exception))

    def test_waf_is_classified_before_login_expiry(self):
        session = Mock()
        session.request.return_value = response(WAF)
        with self.assertRaisesRegex(ValueError, "站点验证") as error:
            self.plugin._request(session, "GET", "forum.php", "读取登录页面")
        self.assertNotIn("private-waf-token", str(error.exception))
        self.assertTrue(self.plugin._is_verification("请完成以下操作，验证您是真人"))

    def test_http_redirect_preserves_post_and_blocks_external_targets(self):
        session = Mock()
        session.request.side_effect = [response(status=301, headers={"Location": "https://right.com.cn/forum/plugin.php?id=erling_qd:action&action=sign"}), response(payload={"success": True})]
        self.plugin._request(session, "POST", self.plugin._sign_path, "提交签到", {"formhash": "abcd1234"})
        self.assertEqual([call.args[0] for call in session.request.call_args_list], ["POST", "POST"])
        self.assertEqual(session.request.call_args.kwargs["data"], {"formhash": "abcd1234"})
        session = Mock()
        session.request.return_value = response(status=302, headers={"Location": "https://evil.test/private-auth"})
        with self.assertRaisesRegex(ValueError, "非预期重定向") as error:
            self.plugin._request(session, "GET", "forum.php", "读取登录页面")
        self.assertNotIn("private-auth", str(error.exception))
        session.request.assert_called_once()

    def test_http_success_does_not_depend_on_account_query(self):
        self.plugin._request = Mock(side_effect=[response(LOGIN), response(payload={"success": True, "continuous_days": 9})])
        self.plugin._account_info = Mock(return_value={})
        result = self.plugin._run_sign(Mock())
        self.assertTrue(result["success"])
        self.assertEqual(result["continuous_days"], 9)
        self.assertEqual(self.plugin._request.call_args.args[1], "POST")
        self.assertEqual(self.plugin._request.call_args.args[4], {"formhash": "abcd1234"})

    def test_unexpected_account_query_failure_preserves_confirmed_result(self):
        self.plugin._request = Mock(side_effect=[response(LOGIN), response(payload={"success": True}), RuntimeError("private-auth")])
        self.assertTrue(self.plugin._run_sign(Mock())["success"])

    def test_timeout_only_queries_state_and_never_reposts(self):
        self.plugin._request = Mock(side_effect=[response(LOGIN), requests.Timeout("private-auth")])
        self.plugin._query_signed = Mock(return_value=None)
        result = self.plugin._run_sign(Mock())
        self.assertFalse(result["success"])
        self.assertTrue(result["unknown"])
        self.assertEqual(self.plugin._request.call_count, 2)
        self.plugin._query_signed.assert_called_once()
        self.assertNotIn("private-auth", json.dumps(result))

    def test_timeout_can_be_confirmed_by_read_only_state(self):
        self.plugin._request = Mock(side_effect=[response(LOGIN), requests.Timeout()])
        self.plugin._query_signed = Mock(return_value={"continuous_days": 9})
        self.plugin._account_info = Mock(return_value={})
        self.assertTrue(self.plugin._run_sign(Mock())["success"])

    def test_sign_page_is_checked_before_post_and_already_skips_post(self):
        self.plugin._config["browser_fallback"] = True
        self.plugin._request = Mock(side_effect=[response(LOGIN), response(SIGNED)])
        self.plugin._account_info = Mock(return_value={})
        self.assertTrue(self.plugin._run_sign(Mock())["success"])
        self.assertEqual([call.args[1] for call in self.plugin._request.call_args_list], ["GET", "GET"])

    def test_preflight_waf_uses_browser_once(self):
        self.plugin._config["browser_fallback"] = True
        self.plugin._request = Mock(side_effect=[response(LOGIN), ValueError("读取签到页面触发站点验证")])
        self.plugin._run_browser = Mock(return_value={"success": True, "status": "签到成功"})
        self.assertTrue(self.plugin.sign()["success"])
        self.plugin._run_browser.assert_called_once()
        self.assertEqual([call.args[1] for call in self.plugin._request.call_args_list], ["GET", "GET"])

    def test_post_verification_does_not_trigger_second_browser_submission(self):
        self.plugin._config["browser_fallback"] = True
        self.plugin._request = Mock(side_effect=[response(LOGIN), response(LOGIN), ValueError("提交签到触发站点验证")])
        self.plugin._query_signed = Mock(return_value=None)
        self.plugin._run_browser = Mock()
        result = self.plugin.sign()
        self.assertTrue(result["unknown"])
        self.plugin._run_browser.assert_not_called()

    def test_disabled_browser_reports_verification_and_saves_history(self):
        self.plugin._run_sign = Mock(side_effect=ValueError("读取登录页面触发站点验证"))
        self.plugin._run_browser = Mock()
        self.assertFalse(self.plugin.sign()["success"])
        self.plugin._run_browser.assert_not_called()
        self.plugin.save_data.assert_called_once()
        self.plugin.post_message.assert_called_once()

    def test_browser_success_closes_context_and_records_only_safe_stats(self):
        context, page = self.browser()
        result = self.plugin._run_browser({"TWcq_2132_auth": "private-auth"})
        self.assertTrue(result["success"])
        self.assertEqual(result["continuous_days"], 9)
        self.assertEqual(result["coin"], "12.5")
        self.assertEqual(result["credits"], "30")
        page.evaluate.assert_called_once()
        self.assertNotIn("private-auth", str(page.evaluate.call_args))
        context.close.assert_called_once()

    def test_browser_still_needs_human_does_not_submit(self):
        context, page = self.browser(WAF)
        result = self.plugin._run_browser({"TWcq_2132_auth": "private-auth"})
        self.assertFalse(result["success"])
        self.assertIn("人工完成", result["status"])
        self.assertNotIn("private-waf-token", json.dumps(result))
        page.evaluate.assert_not_called()
        context.close.assert_called_once()

    def test_browser_already_signed_skips_post(self):
        context, page = self.browser(SIGNED)
        result = self.plugin._run_browser({"TWcq_2132_auth": "private-auth"})
        self.assertTrue(result["success"])
        self.assertEqual(result["continuous_days"], 9)
        page.evaluate.assert_not_called()

    def test_browser_timeout_does_not_retry_post(self):
        context, page = self.browser(reply={"network_error": True})
        result = self.plugin._run_browser({"TWcq_2132_auth": "private-auth"})
        self.assertTrue(result["unknown"])
        self.assertFalse(result["success"])
        page.evaluate.assert_called_once()
        context.close.assert_called_once()

    def test_browser_unavailable_or_raw_error_never_leaks_credentials(self):
        for error in (ImportError("private-auth"), ValueError("proxy=private-auth"), RuntimeError("private-auth")):
            self.plugin._launch_browser = Mock(side_effect=error)
            result = self.plugin._run_browser({"TWcq_2132_auth": "private-auth"})
            self.assertFalse(result["success"])
            self.assertNotIn("private-auth", json.dumps(result))

    def test_browser_uses_mp_sdk_in_memory_cookies_and_matching_proxy(self):
        browser_module = types.ModuleType("app.sdk.browser")
        browser_module.launch_browser_context = Mock()
        with patch.dict(sys.modules, {"app": types.ModuleType("app"), "app.sdk": types.ModuleType("app.sdk"), "app.sdk.browser": browser_module}):
            self.plugin._config["use_proxy"] = True
            self.plugin._launch_browser({"TWcq_2132_auth": "private-auth"})
        options = browser_module.launch_browser_context.call_args.kwargs
        self.assertEqual(options["proxy"], settings.PROXY)
        self.assertEqual(options["storage_state"]["cookies"][0]["value"], "private-auth")
        self.assertEqual(options["storage_state"]["cookies"][0]["domain"], "www.right.com.cn")
        self.assertTrue(options["headless"])

    def test_headed_mode_uses_mp_managed_browser(self):
        browser_module = types.ModuleType("app.sdk.browser")
        browser_module.launch_browser_context = Mock()
        self.plugin._config["browser_headed"] = True
        with patch.dict(sys.modules, {"app": types.ModuleType("app"), "app.sdk": types.ModuleType("app.sdk"), "app.sdk.browser": browser_module}):
            self.plugin._launch_browser({"TWcq_2132_auth": "private-auth"})
        self.assertFalse(browser_module.launch_browser_context.call_args.kwargs["headless"])

    def test_browser_script_validation_can_finish_without_captcha_actions(self):
        context, page = self.browser()
        self.plugin._slider_ready.return_value = False
        page.content.side_effect = [WAF, LOGIN, '<p>恩山币: 12 积分: 30</p>']
        self.assertTrue(self.plugin._run_browser({"TWcq_2132_auth": "private-auth"})["success"])
        page.click.assert_not_called()
        page.evaluate.assert_called_once()

    def test_slider_uses_current_widget_geometry(self):
        page = Mock()
        knob, track = Mock(), Mock()
        knob.bounding_box.return_value = {"x": 759, "y": 508, "width": 40, "height": 40}
        track.bounding_box.return_value = {"x": 759, "y": 508, "width": 360, "height": 40}
        page.query_selector.side_effect = [knob, track]
        self.assertTrue(self.plugin._try_aliyun_slider(page))
        self.assertEqual(page.mouse.move.call_args_list[0].args, (779, 528))
        moves = page.mouse.move.call_args_list
        self.assertEqual(len(moves), 33)
        self.assertEqual(moves[-1].args, (1099, 528))
        self.assertTrue(all(a.args[0] < b.args[0] for a, b in zip(moves, moves[1:])))
        self.assertEqual(sum(call.args[0] for call in page.wait_for_timeout.call_args_list), 1390)
        page.mouse.down.assert_called_once()
        page.mouse.up.assert_called_once()

    def test_slider_releases_pointer_after_drag_error(self):
        page = Mock()
        knob, track = Mock(), Mock()
        knob.bounding_box.return_value = {"x": 10, "y": 10, "width": 40, "height": 40}
        track.bounding_box.return_value = {"x": 10, "y": 10, "width": 360, "height": 40}
        page.query_selector.side_effect = [knob, track]
        page.mouse.move.side_effect = [None, RuntimeError("private-auth")]
        self.assertFalse(self.plugin._try_aliyun_slider(page))
        page.mouse.up.assert_called_once()

    def test_unknown_graphic_challenge_does_not_use_slider_coordinates(self):
        page = Mock()
        page.query_selector.return_value = None
        self.assertFalse(self.plugin._try_aliyun_slider(page))
        page.mouse.down.assert_not_called()

    def test_slider_success_waits_for_actual_authenticated_page_after_navigation(self):
        _, page = self.browser()
        page.content.side_effect = [WAF] * 6 + [RuntimeError("navigation"), "<html></html>", SIGNED]
        self.plugin._try_aliyun_slider = Mock(return_value=True)
        state = {"slider_attempted": False}
        html = self.plugin._browser_load(page, self.plugin._sign_page, state)
        self.assertEqual(html, SIGNED)
        self.assertTrue(state["slider_attempted"])
        self.plugin._try_aliyun_slider.assert_called_once_with(page)
        page.evaluate.assert_not_called()

    def test_failed_slider_is_not_repeated_in_same_run(self):
        _, page = self.browser(WAF)
        self.plugin._try_aliyun_slider = Mock(return_value=True)
        state = {"slider_attempted": False}
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "仍需人工完成"):
                self.plugin._browser_load(page, self.plugin._sign_page, state)
        self.plugin._try_aliyun_slider.assert_called_once()
        page.evaluate.assert_not_called()

    def test_authenticated_page_with_residual_waf_script_is_accepted(self):
        html = SIGNED + '<script>var key="CF_APP_WAF";</script>'
        _, page = self.browser(html)
        self.assertEqual(self.plugin._browser_load(page, self.plugin._sign_page), html)
        page.mouse.down.assert_not_called()

    def test_authenticated_page_with_challenge_control_is_not_accepted(self):
        html = SIGNED + '<div id="aliyunCaptcha-sliding-text">请按住滑块，拖动到最右边</div><script>"CF_APP_WAF"</script>'
        self.assertTrue(self.plugin._browser_is_verification(html))

    def test_verification_diagnostics_are_fixed_labels_without_page_secrets(self):
        cases = (
            ("验证失败，请重试", "页面提示验证失败或需要重试"),
            ("验证中…", "页面停留在验证中"),
            ("验证成功", "页面提示验证通过，但尚未确认论坛登录页面"),
            ("请按住滑块，拖动到最右边", "页面仍显示拖动滑块提示"),
            ("请依次点击图片", "页面出现尚未适配的图形验证"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                html = f'<div id="aliyunCaptcha-sliding-text">{text} private-token</div><script>private-auth</script>'
                self.assertEqual(self.plugin._verification_detail(html), expected)

    def test_failure_notification_contains_verification_diagnostic(self):
        html = WAF + '<div id="aliyunCaptcha-sliding-text">验证失败，请重试</div>'
        _, page = self.browser(html)
        self.plugin._try_aliyun_slider = Mock(return_value=True)
        result = self.plugin._run_browser({"TWcq_2132_auth": "private-auth"})
        self.assertFalse(result["success"])
        self.assertIn("页面提示验证失败或需要重试", result["status"])
        self.assertNotIn("private-", result["status"])
        page.evaluate.assert_not_called()

    def test_verification_response_only_records_known_codes_from_official_hosts(self):
        page, state = Mock(), {}
        self.plugin._watch_verification(page, state)
        callback = next(call.args[1] for call in page.on.call_args_list if call.args[0] == "response")
        for host, payload in (
            ("example.com", {"Result": {"VerifyCode": "F024"}}),
            ("captcha.cn-shanghai.aliyuncs.com.evil.test", {"Result": {"VerifyCode": "F024"}}),
            ("captcha.cn-shanghai.aliyuncs.com", {"Result": {"VerifyCode": "private-token"}}),
            ("captcha.cn-shanghai.aliyuncs.com", {"Result": None}),
        ):
            item = Mock(url=f"https://{host}/?token=private-token")
            item.json.return_value = payload
            callback(item)
            self.assertEqual(state, {})
        item = Mock(url="https://captcha.cn-shanghai.aliyuncs.com/?token=private-token")
        item.json.return_value = {"Result": {"VerifyCode": "F024", "CertifyId": "private-token"}}
        callback(item)
        self.assertEqual(state, {"verify_code": "F024"})

    def test_known_verification_code_is_in_failure_status(self):
        _, page = self.browser(WAF)
        state = {"slider_attempted": True, "verify_code": "F024"}
        with self.assertRaisesRegex(ValueError, "F024（检测到模拟点击或滑动）"):
            self.plugin._browser_load(page, self.plugin._sign_page, state)
        page.evaluate.assert_not_called()

    def test_optional_response_listener_failure_does_not_abort(self):
        page = Mock()
        page.on.side_effect = AttributeError("private-error")
        self.plugin._watch_verification(page, {})

    def test_slider_waits_for_widget_loaded_after_six_seconds(self):
        _, page = self.browser()
        self.plugin._slider_ready.side_effect = lambda _: page.wait_for_timeout.call_count >= 9
        page.content.side_effect = [WAF] * 9 + [SIGNED]
        self.plugin._try_aliyun_slider = Mock(return_value=True)
        self.assertEqual(self.plugin._browser_load(page, self.plugin._sign_page), SIGNED)
        self.plugin._try_aliyun_slider.assert_called_once_with(page)
        self.assertGreaterEqual(page.wait_for_timeout.call_count, 10)

    def test_missing_widget_does_not_consume_slider_attempt(self):
        _, page = self.browser(WAF)
        self.plugin._slider_ready.return_value = False
        self.plugin._try_aliyun_slider = Mock()
        state = {"slider_attempted": False}
        with self.assertRaisesRegex(ValueError, "等待20秒后仍未找到"):
            self.plugin._browser_load(page, self.plugin._sign_page, state)
        self.assertFalse(state["slider_attempted"])
        self.plugin._try_aliyun_slider.assert_not_called()
        page.evaluate.assert_not_called()

    def test_widget_readiness_rejects_invisible_geometry(self):
        page, handle, track = Mock(), Mock(), Mock()
        page.query_selector.side_effect = [handle, track]
        handle.bounding_box.return_value = None
        track.bounding_box.return_value = {"width": 360, "height": 40}
        self.assertFalse(self.plugin._slider_ready(page))

    def test_resource_diagnostics_strip_credentials_path_and_query(self):
        page, state = Mock(), {}
        self.plugin._watch_verification(page, state)
        events = {call.args[0]: call.args[1] for call in page.on.call_args_list}
        request = Mock(url="https://private-user:private-password@captcha.cn-shanghai.aliyuncs.com/private-path?token=private-token",
                       resource_type="script", failure="net::ERR_NAME_NOT_RESOLVED private-token")
        events["requestfailed"](request)
        events["pageerror"](RuntimeError("private-token"))
        events["response"](Mock(url=request.url, status=403, request=request))
        self.assertEqual(state["script_error_count"], 1)
        self.assertEqual(state["resource_failures"], ["captcha.cn-shanghai.aliyuncs.com: ERR_NAME_NOT_RESOLVED",
                                                       "captcha.cn-shanghai.aliyuncs.com: HTTP 403"])
        self.assertNotIn("private-", str(state))

    def test_failure_screenshot_and_diagnostic_are_visible_without_sign_history(self):
        page = Mock()
        page.screenshot.return_value = b"jpeg-test"
        self.plugin._save_verification_diagnostic(page, "控件未加载", {"resource_failures": ["example.com: HTTP 403"]})
        key, record = self.plugin.save_data.call_args.args
        self.assertEqual(key, "verification_diagnostic")
        self.assertEqual(base64.b64decode(record["image"].split(",", 1)[1]), b"jpeg-test")
        self.plugin.get_data.side_effect = lambda name: record if name == key else []
        content = self.plugin.get_page()
        self.assertEqual(content[1]["component"], "VImg")
        self.assertIn("控件未加载", content[0]["props"]["text"])
        self.assertIn("暂无", content[-1]["props"]["text"])

    def test_screenshot_failure_does_not_hide_verification_diagnostic(self):
        page = Mock()
        page.screenshot.side_effect = RuntimeError("private-token")
        self.plugin._save_verification_diagnostic(page, "控件未加载", {})
        record = self.plugin.save_data.call_args.args[1]
        self.assertNotIn("image", record)
        self.assertEqual(record["detail"], "控件未加载")
        self.assertNotIn("private-token", str(record))

    def test_slider_can_be_disabled(self):
        _, page = self.browser(WAF)
        self.plugin._config["auto_slider"] = False
        self.plugin._try_aliyun_slider = Mock()
        with self.assertRaisesRegex(ValueError, "人工完成"):
            self.plugin._browser_load(page, self.plugin._sign_page)
        self.plugin._try_aliyun_slider.assert_not_called()

    def test_failure_cookie_and_notification_are_saved_without_credentials(self):
        self.plugin._config["cookie"] = "other=value"
        result = self.plugin.sign()
        self.assertFalse(result["success"])
        self.plugin.save_data.assert_called_once()
        message = self.plugin.post_message.call_args.kwargs
        self.assertIn("❌", message["title"])
        self.assertNotIn("private-auth", message["text"])
        self.assertFalse(self.plugin._lock.locked())

    def test_history_limit_and_notifications_with_icons(self):
        self.plugin._run_sign = Mock(return_value={"success": True, "status": "签到成功", "username": "测试用户", "continuous_days": 9, "coin": "12"})
        self.plugin.get_data.return_value = [{}] * 100
        self.plugin.sign()
        self.assertEqual(len(self.plugin.save_data.call_args.kwargs["value"]), 100)
        message = self.plugin.post_message.call_args.kwargs
        self.assertIn("✅", message["title"])
        self.assertIn("📆 连续签到：9 天", message["text"])
        self.assertIn("💰 恩山币：12", message["text"])
        self.assertEqual(message["mtype"], "site")

    def test_storage_and_notification_failures_preserve_success(self):
        self.plugin._run_sign = Mock(return_value={"success": True, "status": "签到成功"})
        self.plugin.save_data.side_effect = RuntimeError("private-auth")
        self.plugin.post_message.side_effect = RuntimeError("private-auth")
        self.assertTrue(self.plugin.sign()["success"])
        self.assertFalse(self.plugin._lock.locked())

    def test_notify_can_be_disabled_and_overlap_skipped(self):
        self.plugin._config["notify"] = False
        self.plugin._run_sign = Mock(return_value={"success": True, "status": "今日已签到"})
        self.plugin.sign()
        self.plugin.post_message.assert_not_called()
        self.plugin._run_sign.reset_mock()
        self.plugin._lock.acquire()
        self.assertIsNone(self.plugin.sign())
        self.plugin._run_sign.assert_not_called()
        self.plugin._lock.release()

    def test_immediate_run_persists_settings_and_registers_own_service(self):
        self.plugin.update_config = Mock()
        scheduler, trigger = Mock(), Mock()
        with patch.dict(namespace, BackgroundScheduler=scheduler, CronTrigger=trigger):
            self.plugin.init_plugin({"onlyonce": True, "cookie_source": "manual", "cookie": "TWcq_2132_auth=private-auth"})
            self.assertFalse(self.plugin.update_config.call_args.args[0]["onlyonce"])
            self.assertTrue(self.plugin._config["browser_fallback"])
            scheduler.return_value.start.assert_called_once()
            self.plugin._config["enabled"] = True
            self.assertEqual(self.plugin.get_service()[0]["id"], "enshansigncc")


if __name__ == "__main__":
    unittest.main()
