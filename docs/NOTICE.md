# 第三方组件与素材（NOTICE）

本仓库**只包含本项目自己写的代码**。以下第三方内容随仓库分发或不随仓库分发，列在这里备查。

## 随仓库分发

| 内容 | 位置 | 来源 | 许可 |
| --- | --- | --- | --- |
| llama.cpp 自带 Web UI 的构建产物 | `web/native/` | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) 的 webui dist | MIT |
| MiniMax-H3 ComfyUI 工作流 JSON（社区配方） | `workflows/` | 社区发布的 ComfyUI workflow 导出文件 | 未显式声明，使用前请与原作者确认 |
| H3 / 音乐提示词 | `prompts/` | 为本项目编写 / 社区整理 | 随本仓库 MIT |

## 不随仓库分发（需自行获取）

| 内容 | 获取方式 | 许可 |
| --- | --- | --- |
| llama.cpp 运行时（stock b11935 / Prism fork prism-b10826） | 各自 GitHub release | MIT |
| ComfyUI 及 13 个节点包 | 各自上游 / ComfyUI-Manager | 各自（多为 MIT/Apache-2.0/GPL） |
| 模型权重（Qwen 系列、MiniMax-H3、CyberRealistic、Chroma、TTS/ASR） | 见 `docs/MODELS.md` | 由各上游决定；Qwen 系列多为 Apache-2.0，社区微调与画质模型另有条款 |
| Qwen3-TTS-GGUF 引擎 | 第三方 checkout（`QWEN_TTS_PROJECT`） | 见其仓库 |
| ffmpeg / cloudflared | 官方构建 | GPL / Apache-2.0（cloudflared） |

## 说明

- 本仓库**不包含任何模型权重、聊天记录、声音样本或访问令牌**；`.gitignore` 已排除这些路径。
- 若你需要把模型或工作流再分发，请遵守各自上游的许可，并保留其 LICENSE / NOTICE 文件。
- 发现归属有误或缺少署名，请开 issue 指正。
