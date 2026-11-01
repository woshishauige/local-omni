"""导入卫生：用了 `suppress` / `contextmanager` / `partial` 却没导入，会在**运行时**才炸。

真机踩到：`image_comfy.py` 的 `restart()` 用了 `suppress` 但文件里没导入，于是"清理掉一个已经死掉的
ComfyUI"这条恢复路径在真正需要它的那一刻抛 `NameError`，网关被留在 `error` 状态——从外面看就是
"网站没用"。这类错误 py_compile 与单元测试都发现不了（那条路径平时不走），所以在这里静态钉住。

Run: python -X utf8 -m pytest test_import_hygiene.py -q
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 只挑"模块级函数、名字醒目"的那几个：它们被局部变量遮挡的可能极小，`queue`/`csv`/`html`
# 这类常见变量名会造成误报，所以不在这里。
WATCHED = ("suppress", "contextmanager", "ExitStack", "partial")
MODULES = ("mode_server.py", "image_comfy.py", "image_jobs.py", "video_routes.py",
           "video_denoise.py", "video_interpolate.py", "qwen_service.py", "usage_tracking.py",
           "prompt_enhance.py", "context_compactor.py", "gpu_arbiter.py", "voice_mode.py",
           "share_store.py", "guest_policy.py", "guest_uploads.py", "conversation_ledger.py",
           "mode_isolation.py", "usage_capture.py", "usage_store.py", "image_pipelines.py")


def imported_names(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def used_names(tree):
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


class ImportHygieneTests(unittest.TestCase):
    def test_every_watched_name_is_imported_where_it_is_used(self):
        problems = []
        for name in MODULES:
            path = ROOT / name
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), name)
            missing = sorted(used_names(tree) & set(WATCHED) - imported_names(tree))
            problems.extend(f"{name}: {item}" for item in missing)
        self.assertEqual([], problems,
                         "这些名字被用了但没导入，运行到那一步就会 NameError：\n" + "\n".join(problems))

    def test_image_comfy_can_suppress_again(self):
        """踩坑的那一处单独钉一遍：restart() 的清理路径必须能真的 suppress。"""
        import image_comfy

        self.assertTrue(callable(getattr(image_comfy, "suppress", None)),
                        "image_comfy 又丢了 suppress 导入：生图结束后的清理会 NameError")


if __name__ == "__main__":
    unittest.main()
