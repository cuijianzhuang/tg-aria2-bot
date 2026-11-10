"""Keep shared business code independent of Telegram handler registration."""

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestModuleBoundaries(unittest.TestCase):
    def test_core_does_not_import_handlers(self):
        violations = []
        for path in (PROJECT_ROOT / "bot" / "core").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                    if node.module == "bot":
                        modules += [f"bot.{name.name}" for name in node.names]
                    if node.level == 2:
                        modules += [node.module or ""]
                        modules += [name.name for name in node.names]
                elif isinstance(node, ast.Import):
                    modules = [name.name for name in node.names]
                if any(
                    name in ("bot.handlers", "handlers") or name.startswith(("bot.handlers.", "handlers."))
                    for name in modules
                ):
                    violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual(violations, [], "core 不应依赖 handlers：" + ", ".join(violations))

    def test_source_parser_has_no_application_or_telegram_imports(self):
        path = PROJECT_ROOT / "bot" / "core" / "sources.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "纯解析模块不应使用应用内相对导入")
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [name.name for name in node.names]
            for module in modules:
                self.assertNotIn(module.split(".")[0], ("bot", "aiogram"))
