import ast
import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
PLUGIN_IDS = ("deepfloodsign", "nodeseeksigncc")


class V3ContractTests(unittest.TestCase):
    """校验 V3 市场索引、源码版本和稳定 SDK 导入合同。"""

    @classmethod
    def setUpClass(cls):
        cls.package = json.loads(
            (ROOT / "package.v3.json").read_text(encoding="utf-8")
        )

    def test_all_plugins_have_v3_market_entries(self):
        self.assertEqual(set(self.package), set(PLUGIN_IDS))
        for plugin_id in PLUGIN_IDS:
            metadata = self.package[plugin_id]
            self.assertEqual(metadata["version"], "1.0.0")
            self.assertEqual(metadata["system_version"], ">=3.0.0")
            self.assertEqual(next(iter(metadata["history"])), "v1.0.0")

    def test_source_versions_match_market(self):
        for plugin_id in PLUGIN_IDS:
            source_path = ROOT / "plugins.v3" / plugin_id / "__init__.py"
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
            versions = [
                node.value.value
                for class_node in tree.body
                if isinstance(class_node, ast.ClassDef)
                for node in class_node.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "plugin_version"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Constant)
            ]
            self.assertEqual(versions, [self.package[plugin_id]["version"]])

    def test_v3_sources_use_stable_sdk_imports(self):
        forbidden = ("from app.core.", "from app.log ", "from app.utils.")
        for plugin_id in PLUGIN_IDS:
            source = (
                ROOT / "plugins.v3" / plugin_id / "__init__.py"
            ).read_text(encoding="utf-8")
            for old_import in forbidden:
                self.assertNotIn(old_import, source)
            self.assertIn("from app.sdk.config import settings", source)
            self.assertIn("from app.sdk.logging import logger", source)
            self.assertIn("from app.sdk.utilities import CryptoJsUtils", source)

    def test_v3_dependencies_use_pyproject(self):
        for plugin_id in PLUGIN_IDS:
            plugin_dir = ROOT / "plugins.v3" / plugin_id
            project = tomllib.loads(
                (plugin_dir / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]
            self.assertEqual(project["dynamic"], ["version"])
            self.assertGreater(len(project["dependencies"]), 0)
            self.assertFalse((plugin_dir / "requirements.txt").exists())

    def test_legacy_indexes_disable_v3_fallback(self):
        for package_name in ("package.json", "package.v2.json"):
            package = json.loads((ROOT / package_name).read_text(encoding="utf-8"))
            for plugin_id in PLUGIN_IDS:
                self.assertIs(package[plugin_id]["v3"], False)


if __name__ == "__main__":
    unittest.main()
