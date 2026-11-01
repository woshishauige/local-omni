"""守卫必须写在**手里有 request 的地方**。

踩过的坑：把 `require_feature(request, "chat")` 加进了 `_gpu_free_mib()` 这个没有 request 的
助手函数里，于是语音合成路径一走到那儿就 `NameError: name 'request' is not defined`，端点
把它报成 500。面板把合成失败当成"退回浏览器自己的声音"，所以那一轮听起来是成功的，只是永远
没有音频、也没有可回放的语音条——错误被静默掉了。这个文件用 AST 把这类写法挡在提交前：
每个守卫调用点向上找，必须能在某个外层函数里看到 `request`（形参或局部变量）。

Run: python -X utf8 -m pytest test_route_guards.py -q
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULES = ("mode_server.py", "video_routes.py", "image_jobs.py")
GUARDS = ("require_owner", "require_feature", "require_any_feature",
          "require_local_origin", "require_local_session")


def bound_names(node):
    """Names a function body binds: parameters and any assignment inside it."""
    names = {argument.arg for argument in node.args.args + node.args.kwonlyargs}
    if node.args.vararg:
        names.add(node.args.vararg.arg)
    if node.args.kwarg:
        names.add(node.args.kwarg.arg)
    for inner in ast.walk(node):
        if isinstance(inner, ast.Assign):
            targets = inner.targets
        elif isinstance(inner, ast.AnnAssign):
            targets = [inner.target]
        elif isinstance(inner, (ast.For, ast.AsyncFor)):
            targets = [inner.target]
        elif isinstance(inner, ast.withitem):
            targets = [inner.optional_vars] if inner.optional_vars else []
        elif isinstance(inner, ast.NamedExpr):
            targets = [inner.target]
        else:
            continue
        for target in targets:
            for piece in ast.walk(target):
                if isinstance(piece, ast.Name):
                    names.add(piece.id)
    return names


def offenders(path):
    """Guard call sites that cannot see a `request`, as (line, guard, chain) triples."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"), path)
    found = []

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, enclosing + [child])
            elif (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                  and child.func.id in GUARDS):
                if not any("request" in bound_names(frame) for frame in enclosing):
                    found.append((child.lineno, child.func.id,
                                  " -> ".join(frame.name for frame in enclosing)))
            else:
                walk(child, enclosing)

    walk(tree, [])
    return found


class GuardPlacementTests(unittest.TestCase):
    def test_every_guard_call_can_see_a_request(self):
        for path in MODULES:
            with self.subTest(module=path):
                self.assertEqual(
                    [], offenders(path),
                    "守卫写在了拿不到 request 的函数里；运行时会 NameError，"
                    "端点只会回一个 500")

    def test_the_tts_helper_takes_no_request(self):
        """它就是踩坑的那一处：合成助手不授权，守卫属于调用它的端点。"""
        import mode_server as module

        self.assertEqual([], [a.arg for a in
                              __import__("inspect").signature(module._gpu_free_mib).parameters.values()])
        source = (ROOT / "mode_server.py").read_text(encoding="utf-8")
        helper = source.split("def _gpu_free_mib(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("require_feature", helper,
                         "合成助手又开始自己判权限了——它没有 request")
        self.assertIn('require_feature(request, "chat")', source.split("async def voice_speak(", 1)[1]
                      .split("\nasync def ", 1)[0],
                      "语音合成端点自己必须判一次能力（访客的对话能力被收回时要 403）")

    def test_the_other_voice_routes_keep_their_guards(self):
        """转写与流式三条路由同样要判能力，且都写在路由自己身上。"""
        source = (ROOT / "mode_server.py").read_text(encoding="utf-8")
        for route in ("voice_transcribe", "voice_stream_feed", "voice_stream_finish",
                      "voice_stream_cancel"):
            with self.subTest(route=route):
                body = source.split(f"async def {route}(", 1)
                self.assertEqual(2, len(body), f"{route} 不存在了？")
                self.assertIn("require_feature(request", body[1].split("\nasync def ", 1)[0],
                              f"{route} 没有能力判定，收回对话能力后仍可用")


if __name__ == "__main__":
    unittest.main()
