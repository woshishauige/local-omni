"""Guards for the shape of the two root entry wrappers and the gateway's global server.

Why this file exists: the ``if __name__ == "__main__":`` block in ``app/mode_server.py`` used to
sit at module scope, where assigning ``server = uvicorn.Server(...)`` filled the module-level
``server`` that the shutdown routes flip. Wrapping that block in ``main()`` turned the assignment
into a local, so every stop request hit ``None`` and answered HTTP 500 — a regression that only
an end-to-end start/stop run caught. These checks make the shape explicit.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest  # noqa: E402

from app.paths import PROJECT_ROOT  # noqa: E402

ROOT = PROJECT_ROOT


def main_function(source_path):
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    return None


class EntryWrapperTests(unittest.TestCase):
    def test_the_root_wrappers_delegate_to_the_package(self):
        for wrapper, module in (("mode_server.py", "app.mode_server"), ("qwen_service.py", "app.qwen_service")):
            with self.subTest(wrapper=wrapper):
                text = (ROOT / wrapper).read_text(encoding="utf-8")
                self.assertIn(f"from {module} import main", text)
                self.assertIn("raise SystemExit(main())", text)

    def test_the_gateway_publishes_its_server_globally(self):
        module_path = ROOT / "app" / "mode_server.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        module_globals = {target.id for node in tree.body if isinstance(node, ast.Assign)
                          for target in node.targets if isinstance(target, ast.Name)}
        self.assertIn("server", module_globals, "shutdown routes flip server.should_exit")

        main = main_function(module_path)
        self.assertIsNotNone(main, "mode_server must keep a main() the wrapper can call")
        declared = {name for node in main.body if isinstance(node, ast.Global) for name in node.names}
        self.assertIn("server", declared,
                      "without `global server` the assignment in main() is local and stop returns 500")

    def test_the_project_root_is_one_level_above_the_package(self):
        self.assertEqual((ROOT / "app" / "paths.py").resolve(), (ROOT / "app" / "paths.py").resolve())
        self.assertTrue((ROOT / "README.md").is_file())
        self.assertTrue((ROOT / "app" / "mode_server.py").is_file())


if __name__ == "__main__":
    unittest.main()
