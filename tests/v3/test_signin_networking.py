import ast
import re
import time
import types
import typing
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
PLUGIN_IDS = ("deepfloodsign", "nodeseeksigncc")
METHODS = {
    "_response_header",
    "_response_diagnostics",
    "_is_cloudflare_challenge",
    "_save_response_diagnostics",
    "_parse_sign_json",
    "_warmup_http_session",
    "_extract_gain",
    "_match_cookiecloud_domain",
    "_attach_http_cookies",
    "_run_api_sign",
}


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Response:
    def __init__(self, status_code=200, headers=None, text="", payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _CookieJar:
    def __init__(self):
        self.items = []

    def set(self, name, value, **kwargs):
        self.items.append((name, value, kwargs))


def _load_test_class(plugin_id):
    source_path = ROOT / "plugins.v3" / plugin_id / "__init__.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    plugin_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    methods = [
        node for node in plugin_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in METHODS
    ]
    test_class = ast.ClassDef(
        name="PluginUnderTest",
        bases=[],
        keywords=[],
        body=methods,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[test_class], type_ignores=[]))
    namespace = {
        "Any": typing.Any,
        "Dict": typing.Dict,
        "List": typing.List,
        "Optional": typing.Optional,
        "Tuple": typing.Tuple,
        "logger": _Logger(),
        "re": re,
        "time": time,
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["PluginUnderTest"], source_path.read_text(encoding="utf-8")


class SigninNetworkingTests(unittest.TestCase):
    def test_cloudflare_challenge_is_classified_without_body_persistence(self):
        for plugin_id in PLUGIN_IDS:
            with self.subTest(plugin=plugin_id):
                cls, _ = _load_test_class(plugin_id)
                plugin = cls()
                saved = {}
                plugin.save_data = lambda key, value: saved.update({key: value})
                response = _Response(
                    status_code=403,
                    headers={
                        "Content-Type": "text/html",
                        "Server": "cloudflare",
                        "CF-Ray": "safe-ray-id",
                    },
                    text="<html><head><title>Just a moment...</title></head></html>",
                )

                self.assertTrue(plugin._is_cloudflare_challenge(response))
                plugin._save_response_diagnostics(response, "attendance")
                diagnostics = saved["last_sign_response"]
                self.assertEqual(diagnostics["status_code"], 403)
                self.assertEqual(diagnostics["title"], "Just a moment...")
                self.assertNotIn("text", diagnostics)
                self.assertNotIn("snippet", diagnostics)

    def test_cookiecloud_filters_expired_path_conflicts_and_duplicate_names(self):
        now = time.time()
        cookie_data = {
            "deepflood.com": [
                {"name": "auth", "value": "root", "domain": ".deepflood.com", "path": "/"},
                {"name": "auth", "value": "api", "domain": "www.deepflood.com", "path": "/api"},
                {"name": "expired", "value": "old", "domain": ".deepflood.com", "expirationDate": now - 10},
                {"name": "board_only", "value": "x", "domain": ".deepflood.com", "path": "/board"},
            ]
        }
        for plugin_id in PLUGIN_IDS:
            with self.subTest(plugin=plugin_id):
                cls, _ = _load_test_class(plugin_id)
                plugin = cls()
                plugin._attendance_path = "/api/attendance"
                _, items = plugin._match_cookiecloud_domain(cookie_data, "www.deepflood.com")
                values = {item["name"]: item["value"] for item in items}
                self.assertEqual(values, {"auth": "api"})

    def test_authenticated_session_cookie_is_injected_after_warmup(self):
        for plugin_id in PLUGIN_IDS:
            with self.subTest(plugin=plugin_id):
                cls, _ = _load_test_class(plugin_id)
                plugin = cls()
                jar = _CookieJar()
                plugin._http_session = types.SimpleNamespace(cookies=jar)
                plugin._resolve_cookiecloud_domain = lambda: "www.example.com"
                plugin._attach_http_cookies("auth=ok; session=authenticated; cf_clearance=clear")
                names = [item[0] for item in jar.items]
                self.assertEqual(names, ["auth", "session", "cf_clearance"])

    def test_request_implementation_uses_aligned_curl_profile(self):
        for plugin_id in PLUGIN_IDS:
            with self.subTest(plugin=plugin_id):
                _, source = _load_test_class(plugin_id)
                self.assertIn('Session(impersonate="chrome110")', source)
                self.assertIn("Chrome/110.0.0.0", source)
                self.assertNotIn("cloudscraper", source)
                self.assertNotIn("非JSON签到响应文本片段", source)
                self.assertNotIn("尝试请求远端 CookieCloud: {url}", source)

    def test_already_signed_takes_precedence_over_success_flag(self):
        for plugin_id in PLUGIN_IDS:
            with self.subTest(plugin=plugin_id):
                cls, _ = _load_test_class(plugin_id)
                result = cls()._parse_sign_json(
                    {"success": True, "message": "今日已完成签到"}, 200
                )
                self.assertTrue(result["success"])
                self.assertTrue(result["already_signed"])
                self.assertFalse(result["signed"])

    def test_attendance_challenge_returns_clear_failure_without_retrying(self):
        for plugin_id in PLUGIN_IDS:
            with self.subTest(plugin=plugin_id):
                cls, _ = _load_test_class(plugin_id)
                plugin = cls()
                plugin._site_url = "https://www.example.com"
                plugin._attendance_method = "POST"
                plugin._http_session = object()
                plugin._browser_user_agent = "test-agent"
                plugin._get_active_cookie = lambda: ("auth=ok", "")
                plugin._create_http_session = lambda: None
                plugin._warmup_http_session = lambda _cookie: None
                plugin._attach_http_cookies = lambda _cookie: None
                plugin._build_attendance_url = lambda: "https://www.example.com/api/attendance"
                plugin._get_proxies = lambda: None
                calls = []
                response = _Response(
                    status_code=403,
                    headers={"Content-Type": "text/html", "Server": "cloudflare"},
                    text="<title>Just a moment...</title>",
                )
                plugin._smart_post = lambda *args, **kwargs: calls.append(args) or response
                plugin._save_response_diagnostics = lambda *args, **kwargs: None

                result = plugin._run_api_sign()

                self.assertEqual(len(calls), 1)
                self.assertFalse(result["success"])
                self.assertIn("Cloudflare/WAF", result["message"])

    def test_anonymous_warmup_challenge_does_not_block_cookie_attempt(self):
        for plugin_id in PLUGIN_IDS:
            with self.subTest(plugin=plugin_id):
                cls, _ = _load_test_class(plugin_id)
                plugin = cls()
                plugin._site_url = "https://www.example.com"
                plugin._http_session = object()
                plugin._browser_user_agent = "test-agent"
                plugin._get_proxies = lambda: None
                plugin._smart_get = lambda *args, **kwargs: _Response(
                    status_code=403,
                    headers={"Content-Type": "text/html", "Server": "cloudflare"},
                    text="<title>Just a moment...</title>",
                )
                plugin._save_response_diagnostics = lambda *args, **kwargs: None

                self.assertIsNone(plugin._warmup_http_session("auth=ok"))


if __name__ == "__main__":
    unittest.main()
