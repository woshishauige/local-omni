# QwenWeb

> 单机、Windows、单人使用的大模型网关 + 网页面板：**聊天/识图、Agent 工具、生图、视频、语音、使用日志、可选共享**，
> 全部在本机跑，权重和对话不出机器。
>
> A local-first LLM gateway with a build-free web UI for Windows: chat + vision, agent tools,
> ComfyUI image/video generation, local TTS/ASR voice, usage telemetry and an opt-in share mode.

本仓库**不含模型权重和模型运行时**（体积 40 GB 级别），只包含自己写的代码、工作流、提示词和验证脚本；
权重与运行时的下载地址见 [docs/MODELS.md](docs/MODELS.md)。

---

## 这是什么

| 能力 | 实现 | 依赖 |
| --- | --- | --- |
| 纯文本 / 识图聊天 | `mode_server.py` 网关 + `llama-server` 后端，一次切换只重启模型进程 | llama.cpp + GGUF 权重 |
| Agent 工具调用 | llama-server `--agent` + 网关侧工具授权/历史 | 同上 |
| 上下文自动压缩 | `context_compaction.py`、`context_guard.py` | 同上 |
| 生图 | `image_jobs.py` / `image_pipelines.py` 调 ComfyUI，支持 SDXL / Chroma / Flux 管线与 LoRA 预设 | ComfyUI |
| 视频 | `video_routes.py` 逐段提交 H3 分镜、拼接成片、RIFE 补帧、ffmpeg 降噪 | ComfyUI + ffmpeg |
| 语音 | `voice_input.py`（ASR）+ `voice_mode.py`（对话）+ `tools/tts_server.py`（TTS） | Qwen3-ASR / Qwen3-TTS |
| 使用日志 | `usage_store.py`，只存统计指标，不存正文 | 无 |
| 共享给他人 | `share_store.py` / `guest_policy.py` + cloudflared 隧道 | 可选 |
| 前端 | `web/`，**无需构建**（原生 JS/CSS，直接由网关以静态文件提供） | 无 |

设计取舍写在 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：单实例看护、统一后端、显存仲裁、
任务状态机、失败可观测（每一条都有对应的回归测试）。

---

## 快速开始

### 0. 硬件与系统

- Windows 10/11 + NVIDIA 显卡。**12 GB 显存**可跑三元量化的 27B（默认档）；**8 GB** 级更建议 9B 档。
- Python **3.12**（本项目在 3.12.10 上开发；ComfyUI 自带解释器可直接复用）。
- 磁盘：仅语言模型就要 7 GB 级，生图/视频另算（见 docs/MODELS.md）。

### 1. 取代码

```powershell
git clone <this-repo> qwenweb
cd qwenweb
```

**目录布局是硬约束**：ComfyUI 要放在本项目的**兄弟目录**（`..\ComfyUI-BSAI_v010\`），
网关按 `ROOT.parent / "ComfyUI-BSAI_v010"` 找它（`image_comfy.py:39`、`video_jobs.py:32`）。

### 2. 解释器与依赖

```powershell
# 二选一：
$env:QWEN_PYTHON = "$PSScriptRoot\..\ComfyUI-BSAI_v010\python\python.exe"   # 复用 ComfyUI 的
$env:QWEN_PYTHON = "C:\Python312\python.exe"                                 # 或独立装

& $env:QWEN_PYTHON -m pip install -r requirements.txt
# 语音（可选）：
& $env:QWEN_PYTHON -m pip install -r requirements-voice.txt
```

`setup.ps1` 会做同样的检查（解释器、依赖、必需目录、权重是否就位），可以直接跑：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

### 3. 放运行时（llama.cpp）

网关按**两个固定目录名**找可执行文件，缺一个就少一档模型：

| 目录 | 构建 | 用途 |
| --- | --- | --- |
| `runtime-stock-11935\` | [`ggml-org/llama.cpp`](https://github.com/ggml-org/llama.cpp) release **b11935**，Windows CUDA 包 | 9B / 常规 4-bit 权重 |
| `runtime-prism-10826\` | [`PrismML-Eng/llama.cpp`](https://github.com/PrismML-Eng/llama.cpp) release **prism-b10826** | 三元量化的 27B（`PQ2_0` 张量，上游不认） |

把压缩包解到对应目录（含 `llama-server.exe` 与同目录 DLL），或让 `tools/runtime_updater.py`
自动下载+校验（它按 GitHub 公布的 digest 校验，默认不重启服务）：

```powershell
& $env:QWEN_PYTHON -X utf8 tools\runtime_updater.py --track all --dry-run
```

`ffmpeg.exe` / `ffprobe.exe` 放 `runtime\`（视频降噪、拼接、抽帧用；不读 PATH）。

### 4. 放模型

按 [docs/MODELS.md](docs/MODELS.md) 的表下载并放到位，最小可用组合是：

```
models/Qwen3.5-9B-Uncensored/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf
models/Qwen3.5-9B-Uncensored/mmproj-Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-BF16.gguf
```

（默认档是三元 27B，见 `settings.json` 的 `model_profile`；9B 档在 `qwen_config.PROFILES` 里。）

### 5. （可选）ComfyUI

生图/视频/超分才需要，聊天不需要。见 [docs/COMFYUI.md](docs/COMFYUI.md)：13 个节点包白名单、
工作流 JSON 的放置位置、启动参数（网关自己会拉起来，不用手点）。

### 6. 启动

```powershell
.\Start-Qwen.bat          # 或 .\Start-Qwen.ps1
# 打开 http://127.0.0.1:8081
.\Stop-Qwen.bat           # 停
```

首次启动会加载权重（27B 三元约 7 GB 上卡），随后 `/local/status` 变 `ready`。

### 7. 自检

```powershell
# 纯静态（不占卡）
& $env:QWEN_PYTHON -X utf8 -B -m unittest test_mode_server test_qwen_service test_image_jobs test_video_routes
node web/test-image-history.cjs        # 隔离无头浏览器（需要 Node + Playwright）
```

更多真机验收脚本见 [docs/TESTING.md](docs/TESTING.md)。

---

## 目录结构

```
qwenweb/
├─ mode_server.py       网关（FastAPI/uvicorn）：网页、/v1/chat/completions、/tools、/local/*
├─ qwen_service.py      detached 单实例看护：起停、心跳、故障恢复、锁
├─ qwen_config.py       模型 profile（路径/层数/窗口）、后端命令行、运行时切换
├─ image_*.py           生图：任务状态机、管线注册表、ComfyUI 进程与图注入
├─ video_*.py           视频：逐段提交/拼接、代价模型、降噪、补帧
├─ voice_*.py           语音：ASR 输入、语音对话
├─ context_*.py         上下文压缩与溢出保护
├─ usage_*.py           用量遥测（不含正文）
├─ share_*.py guest_*.py 共享模式：设备会话、访客能力、上传隔离
├─ tools/               下载器、探针、TTS worker、运行时更新
├─ web/                 前端（免构建）
├─ prompts/             视频/音乐提示词
├─ workflows/           MiniMax-H3 的 ComfyUI 工作流 JSON
├─ deployment/          数据集/画质/工作流校验脚本
└─ test_*.py check_*.py 回归与真机验收（见 docs/TESTING.md）
```

## 端口

| 端口 | 用途 |
| --- | --- |
| 8081 | 网关（唯一入口） |
| 8082 | llama-server（网关独占，不对外） |
| 8188 | ComfyUI（本地） |

## 配置

`settings.json`（不存在时用默认值）控制上下文、显存层数、线程、自动启动 ComfyUI、上下文压缩等；
样例见 `settings.example.json`。路径与解释器用环境变量覆盖：`QWEN_PYTHON`、`QWEN_TTS_PROJECT`、
`QWEN_SHARE_CONFIG`、`QWEN_ACCESS_TOKEN`、`QWEN_TUNNEL_PORT`。

## 安全与隐私

- 默认**只监听 127.0.0.1**；共享模式是显式 opt-in（`Start-Qwen-Share.ps1`），会生成 `share.config.json`
  （含访问令牌、主机白名单），该文件已在 `.gitignore` 中。
- 日志 `logs/usage.sqlite3` 只记录用量指标（token 数、耗时、状态），**不存提示词、回复正文、工具参数或密钥**。
- 访客能力由 `guest_policy.py` 一处决定，上传内容按设备隔离。
- 本仓库不含任何权重、聊天记录、声音样本；克隆后也不会自动上传任何东西（无遥测）。

## 已知限制

- 只在 Windows + NVIDIA CUDA 上验证过；脚本里仍有若干本机路径（`check_*`/`probe_*` 是验收脚本，
  路径按需改）。
- ComfyUI 节点包按**目录名精确**匹配白名单（`ComfyUI/nodes.py:2381`），安装时目录名必须与
  [docs/COMFYUI.md](docs/COMFYUI.md) 列的一致，否则对应节点不会加载。
- 语音 TTS 需要额外的第三方引擎 checkout（`tools/Qwen3-TTS-GGUF`）与其独立 venv，见 docs/MODELS.md。

## 许可

本仓库代码以 **MIT** 发布，见 [LICENSE](LICENSE)。第三方组件/素材的来源与许可见 [docs/NOTICE.md](docs/NOTICE.md)。
**模型权重不在此仓库**，其许可由各自上游决定（例如 Qwen 系列为 Apache-2.0，社区微调各有条款）。

---

## English overview (short)

QwenWeb is a local-first LLM gateway for Windows. A single FastAPI gateway (`mode_server.py`, port 8081)
fronts a `llama-server` backend (port 8082) for chat/vision/agent tools, a ComfyUI instance (port 8188)
for image and video generation, and a local TTS/ASR pair for voice. The UI in `web/` is plain JS with no
build step. Weights and runtimes are **not** in this repository — see `docs/MODELS.md`. Everything listens
on loopback by default; sharing is an explicit opt-in. Licensed MIT (code only).
