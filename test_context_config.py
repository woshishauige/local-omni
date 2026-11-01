"""Capacity and memory bounds for large local context deployments."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import qwen_config as config


class ContextConfigTests(unittest.TestCase):
    def read(self, values):
        with tempfile.TemporaryDirectory() as stage:
            path = Path(stage) / "settings.json"
            path.write_text(json.dumps(values), encoding="utf-8")
            with patch.object(config, "SETTINGS_PATH", path):
                return config.read_settings()

    def test_native_limit_and_memory_policy_apply_to_both_modes(self):
        settings = self.read({"context_size": 262144, "mtp_enabled": False,
            "cache_ram_mib": 0, "context_checkpoints": 2,
            "checkpoint_min_step": 8192, "server_timeout_seconds": 14400})
        # The context size is the profile's, not settings.json's: a number tuned for one
        # model must not be validated against another (`expected_context` is the authority,
        # and the gateway's readiness check compares /props against it). Asking for 262144
        # here still exercises the bounded-cache policy without pretending the profile
        # loses that argument.
        expected_context = str(config.expected_context(settings))
        for mode in ("text", "vision"):
            command = config.server_command(mode, settings=settings)
            for flag, value in (("-c", expected_context), ("--cache-ram", "0"),
                    ("--ctx-checkpoints", "2"), ("--timeout", "14400"),
                    ("--spec-type", "ngram-mod"), ("-ctk", "q8_0"), ("-ctv", "q8_0")):
                self.assertEqual(command[command.index(flag)+1], value)
            self.assertIn("--agent", command)
            self.assertNotIn("--rope-scale", command)

    def test_invalid_memory_and_capacity_values_are_rejected(self):
        for key, value in (("context_size", 262145), ("context_size", True),
                ("cache_ram_mib", -1), ("context_checkpoints", 33),
                ("checkpoint_min_step", -1), ("server_timeout_seconds", 0)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    self.read({key: value})

    def test_existing_config_gets_bounded_caches(self):
        settings = self.read({"context_size": 16384, "mtp_enabled": True})
        command = config.server_command("text", settings=settings)
        self.assertEqual(command[command.index("--cache-ram")+1], "0")
        self.assertEqual(command[command.index("--ctx-checkpoints")+1], "2")
        # 2026-09-23 换血：随包发布的 27B 现在是三元 Bonsai 2，没有 MTP 草稿头，
        # 所以命令不能请求 draft-mtp。兜底不再是 none：同一个三元模型实测 ngram-mod
        # 让重复型负载 31.4→57.4 t/s（+82.8%），自然文本不变、输出逐字相同。
        self.assertEqual(command[command.index("--spec-type")+1], "ngram-mod")

    def test_a_hand_edited_settings_file_with_a_bom_still_loads(self):
        """记事本给 UTF-8 加 BOM，而 BOM 不是 JSON 文档。

        实测：一个 BOM 就让 mode_server 在 import 阶段挂掉（json 抛 "Unexpected UTF-8 BOM"
        at line 1），整条后端起不来。settings.json 就是给人手改的文件，所以读取必须容忍它。
        """
        with tempfile.TemporaryDirectory() as stage:
            path = Path(stage) / "settings.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mtp_enabled": True}).encode("utf-8"))
            with patch.object(config, "SETTINGS_PATH", path):
                settings = config.read_settings()
        self.assertIs(settings["mtp_enabled"], True)
        self.assertEqual(config.active_profile_name(settings), config.DEFAULT_PROFILE)

    def test_the_shipped_settings_file_is_readable_by_the_app(self):
        """线上那份 settings.json 必须能被 app 自己的读法解析。

        这条不是理论上的：2026-09-23 换血时写坏过它一次（BOM + 一个字符被按 GBK 往返），
        直到跑测试才发现——而那时服务还没重启，所以坏的配置还没暴露成故障。
        """
        settings = config.read_settings()
        self.assertIn(config.active_profile_name(settings), config.PROFILES)
        self.assertEqual(config.expected_context(settings),
                         config.PROFILES[config.active_profile_name(settings)]["context_size"])

    def test_voice_window_is_named_and_locked(self):
        """语音窗口是一个定下来的数，不是每次都会被重算的值。

        2026-09-23 起语音档位从 9B 换成三元 27B（实测大脑 @8192 + 识别器同驻 10725/12227
        MiB），所以这里钉住的既有"窗口必须小于自己的文字窗口"这条通用规则，也有这次换档的
        具体事实。改窗口或改档位都必须是有意的。
        """
        # 通用规则：任何声明了语音窗口的档位，其语音窗口都必须小于自己的文字/识图窗口
        # （语音恒定多一个识别器，更大的窗口就是显存 bug）。
        for name, entry in config.PROFILES.items():
            with self.subTest(profile=name):
                if "voice_context_size" in entry:
                    self.assertLess(entry["voice_context_size"], entry["context_size"])

        # 语音档位 = 三元 27B，语音窗口 8192（实测：大脑 +7653 MiB、识别器 +1244 MiB）。
        self.assertEqual(config.VOICE_PROFILE, config.DEFAULT_PROFILE)
        heavy = config.PROFILES[config.VOICE_PROFILE]
        self.assertEqual(heavy["voice_context_size"], 8192)
        self.assertEqual(config.voice_context({"model_profile": config.VOICE_PROFILE,
                                               "context_size": 32768}), 8192)
        # 显式标记仍然优先：调用方说"这个数是我给的"。
        self.assertEqual(config.voice_context({"model_profile": config.VOICE_PROFILE,
                                               "context_size": 4096,
                                               "context_key": "voice"}), 4096)

        # 9B 保留它自己的语音窗口常量（它不再是语音档位，但仍声明着这个数）。
        self.assertEqual(config.VOICE_CONTEXT_SIZE, 40960)
        self.assertEqual(config.PROFILES["qwen3.5-9b-uncensored"]["voice_context_size"],
                         config.VOICE_CONTEXT_SIZE)

        # 2026-09-23 换血后的事实：三元 Bonsai 2（7.21 GB 全量上卡、无草稿头、32K 窗口）。
        self.assertEqual((heavy["context_size"], heavy["gpu_layers"], heavy["mtp"]),
                         (32768, 64, False))

        # 所有者要求保留的旧版 27B：归档目录里的文件、原配 stock 引擎、34 层 / 16384。
        kept = config.PROFILES["qwen3.8-27b-iq4xs"]
        self.assertEqual((kept["gpu_layers"], kept["context_size"], kept["mtp"]),
                         (34, 16384, False))
        self.assertEqual(Path(kept["model"]).name,
                         "Qwen3.8-27B-ZeroRefusal-UD-IQ4_XS-V3-Final-MTP.gguf")
        self.assertEqual(kept["executable"], config.EXECUTABLE)


if __name__ == "__main__":
    unittest.main()
