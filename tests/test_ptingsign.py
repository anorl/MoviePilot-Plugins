import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_moviepilot_stubs():
    class _Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class _CronTrigger:
        @staticmethod
        def from_crontab(value):
            return value

    module_map = {
        "pytz": types.SimpleNamespace(timezone=lambda value: value),
        "apscheduler": types.ModuleType("apscheduler"),
        "apscheduler.schedulers": types.ModuleType("apscheduler.schedulers"),
        "apscheduler.schedulers.background": types.SimpleNamespace(BackgroundScheduler=object),
        "apscheduler.triggers": types.ModuleType("apscheduler.triggers"),
        "apscheduler.triggers.cron": types.SimpleNamespace(CronTrigger=_CronTrigger),
        "app": types.ModuleType("app"),
        "app.core": types.ModuleType("app.core"),
        "app.core.config": types.SimpleNamespace(settings=types.SimpleNamespace(TZ="Asia/Shanghai")),
        "app.log": types.SimpleNamespace(logger=_Logger()),
        "app.plugins": types.SimpleNamespace(_PluginBase=object),
        "app.schemas": types.SimpleNamespace(NotificationType=types.SimpleNamespace(SiteMessage="site")),
        "app.utils": types.ModuleType("app.utils"),
        "app.utils.crypto": types.SimpleNamespace(CryptoJsUtils=object),
    }
    for name, module in module_map.items():
        sys.modules.setdefault(name, module)


_install_moviepilot_stubs()
PLUGIN_PATH = Path(__file__).parents[1] / "plugins.v2" / "ptingsign" / "__init__.py"
SPEC = importlib.util.spec_from_file_location("ptingsign_plugin", PLUGIN_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class PtingSignTests(unittest.TestCase):
    def test_parses_fresh_check_in(self):
        result = MODULE.ptingsign._parse_check_in_response(_Response(200, {
            "message": "签到成功",
            "data": {
                "alreadyCheckedIn": False,
                "date": "2026-08-18",
                "reward": 2,
                "points": 5,
                "currentStreak": 3,
                "maxStreak": 7,
            },
        }))

        self.assertTrue(result["success"])
        self.assertTrue(result["signed"])
        self.assertFalse(result["already_signed"])
        self.assertEqual(result["reward"], 2)
        self.assertEqual(result["points_balance"], 5)
        self.assertEqual(result["current_streak"], 3)

    def test_parses_already_checked_in_as_success(self):
        result = MODULE.ptingsign._parse_check_in_response(_Response(200, {
            "message": "今天已经签到过了",
            "data": {"alreadyCheckedIn": True, "points": 5},
        }))

        self.assertTrue(result["success"])
        self.assertFalse(result["signed"])
        self.assertTrue(result["already_signed"])
        self.assertEqual(result["points_balance"], 5)

    def test_parses_points_summary(self):
        html = r'''<script>self.__next_f.push([1,"当前余额...{\"value\":7} 今日变动：...{\"value\":4}"])</script>'''

        summary = MODULE.ptingsign._parse_points_summary_html(html)

        self.assertEqual(summary, {"points_balance": 7, "today_points": 4})

    def test_formats_signed_today_points(self):
        self.assertEqual(MODULE.ptingsign._format_signed_number(4), "+4")
        self.assertEqual(MODULE.ptingsign._format_signed_number(-2), "-2")
        self.assertEqual(MODULE.ptingsign._format_signed_number(0), "0")

    def test_notification_and_history_show_today_points_and_balance(self):
        plugin = MODULE.ptingsign()
        messages = []
        plugin.post_message = lambda **kwargs: messages.append(kwargs)
        record = {
            "date": "2026-08-18 12:00:00",
            "status": "今日已签到",
            "today_points": 4,
            "points_balance": 7,
            "current_streak": 4,
        }

        plugin._send_notification(record, {"success": True, "already_signed": True})
        self.assertIn("今日积分：+4", messages[0]["text"])
        self.assertIn("积分余额：7", messages[0]["text"])

        plugin.get_data = lambda _key: [record]
        page_text = str(plugin.get_page())
        self.assertIn("今日积分 +4", page_text)
        self.assertIn("积分余额 7", page_text)

    def test_reports_expired_login(self):
        result = MODULE.ptingsign._parse_check_in_response(
            _Response(401, None, "请登录后再试")
        )

        self.assertFalse(result["success"])
        self.assertIn("登录态已失效", result["message"])

    def test_cookiecloud_matches_parent_domain_and_deduplicates(self):
        plugin = MODULE.ptingsign()
        matched, items = plugin._match_cookiecloud_domain({
            ".pting.club": [
                {"name": "session", "value": "old", "domain": ".pting.club"},
                {"name": "theme", "value": "dark", "domain": "pting.club"},
                {"name": "session", "value": "new", "domain": "pting.club"},
            ],
            "example.com": [{"name": "ignored", "value": "1", "domain": "example.com"}],
        }, "pting.club")

        self.assertEqual(matched, ".pting.club")
        self.assertEqual(plugin._build_cookie_header(items), "theme=dark; session=new")


if __name__ == "__main__":
    unittest.main()
