# 本地部署 AI 文字语音对话、图像视频生成一键式网站

> 一台 Windows + 一张显卡就能跑起来的本地 AI 站点：**文字/识图对话、智能体工具、生图、视频、语音**全在一个页面里，
> 权重、对话、语音都不出这台机器。

![对话界面](docs/screenshots/chat.png)

## 它能做什么

| 页面 | 能力 |
| --- | --- |
| **纯文本 / 识图** | 本地 GGUF 模型对话，支持图片输入、思考档位、工具调用（智能体）、上下文自动压缩 |
| **生图** | 接 ComfyUI：提示词、负面词、尺寸、步数、种子、参考图、一键工作流、LoRA 预设、生成记录 |
| **视频生成** | MiniMax-H3 逐段生成 → 自动拼接成片；4 条配方（3/6/12/20 步）、段落规划、超分重建、降噪、补帧、拼接 |
| **语音对话** | 本地 ASR + 本地 TTS，按住说话、识别、思考、朗读全部离线完成 |
| **使用日志** | 请求明细、Token/费用估算、延迟与吞吐统计（只存指标，不存正文） |
| **共享管理器** | 可选：生成口令 + 隧道，把站点临时分享给别人，访客能力可逐项开关 |

<table>
<tr>
<td width="50%"><img src="docs/screenshots/image-panel.png" alt="生图"></td>
<td width="50%"><img src="docs/screenshots/video-panel.png" alt="视频"></td>
</tr>
<tr>
<td><img src="docs/screenshots/voice.png" alt="语音"></td>
<td><em>生图 / 视频 / 语音三块面板，和聊天共用一个网关、一份模型。</em></td>
</tr>
</table>

## 三步跑起来

### 1. 装依赖

```powershell
git clone https://github.com/woshishauige/local-ai-studio.git
cd local-ai-studio
$env:QWEN_PYTHON = "C:\Python312\python.exe"      # 也可以复用 ComfyUI 自带的解释器
& $env:QWEN_PYTHON -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\setup.ps1   # 检查解释器/依赖/目录/权重
```

### 2. 放权重和运行时

只聊天：下一个 GGUF 语言模型就够。要生图/视频再加 ComfyUI 与对应权重。
**完整清单与下载地址见 [docs/MODELS.md](docs/MODELS.md)**，最省事的组合是 9B：

```
models/Qwen3.5-9B-Uncensored/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf
models/Qwen3.5-9B-Uncensored/mmproj-Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-BF16.gguf
```

运行时放在项目目录里：`runtime-stock-11935\`、`runtime-prism-10826\`（llama.cpp 两份构建），
`runtime\ffmpeg.exe`。ComfyUI 放在**兄弟目录** `..\ComfyUI-BSAI_v010\`。

### 3. 启动

```powershell
.\Start-Qwen.bat          # 或 .\Start-Qwen.ps1
# 浏览器打开 http://127.0.0.1:8081
.\Stop-Qwen.bat           # 停止并释放显存
```

## 目录与端口

```
mode_server.py   网关（8081）：网页 + /v1/chat/completions + 所有 /local/* 接口
qwen_service.py  看护进程：起停、心跳、崩溃恢复；llama-server 在 8082
image_*.py       生图：任务状态机、管线注册表、ComfyUI 进程与图注入（8188）
video_*.py       视频：逐段提交/拼接、代价模型、降噪、补帧
voice_*.py       语音：ASR 输入、语音对话、TTS worker
web/             前端（免构建，直接由网关提供）
docs/            模型清单、ComfyUI 要求、架构、测试、许可
```

## 配置

`settings.json` 控制上下文、显存层数、线程、自动启动 ComfyUI 等，样例见 `settings.example.json`。
常用环境变量：

| 变量 | 作用 |
| --- | --- |
| `QWEN_PYTHON` | 用哪个解释器跑网关（默认复用兄弟目录 ComfyUI 的解释器） |
| `QWEN_GATEWAY_PORT` | 网关端口，默认 8081；换端口后仍受 `Start-Qwen.ps1` 探测 |
| `QWEN_TTS_PROJECT` | 语音 TTS 引擎 checkout 的位置（可选功能） |
| `QWEN_SHARE_CONFIG` / `QWEN_ACCESS_TOKEN` | 共享模式的口令与配置 |

## 已知限制

- 只在 **Windows + NVIDIA CUDA** 上验证过；脚本按 Windows 写。
- ComfyUI 节点包按**目录名精确匹配**白名单（见 [docs/COMFYUI.md](docs/COMFYUI.md)），
  名字不对就不会加载；RIFE 与 SeedVR2 两条缺口该文档里写明了。
- 同一台机器**只跑一个实例**（后端固定 8082）；测试可用 `QWEN_GATEWAY_PORT` 另起网关，
  但会共用后端。
- 仓库不含任何权重、聊天记录、声音样本。

## 文档

[模型与下载](docs/MODELS.md) · [ComfyUI 要求](docs/COMFYUI.md) · [架构](docs/ARCHITECTURE.md) ·
[测试与验收](docs/TESTING.md) · [第三方与许可](docs/NOTICE.md)

## 许可

代码 [MIT](LICENSE)；模型权重不在本仓库，许可见各上游。

---

**English**: QwenWeb is a local-first AI site for a single Windows machine with one GPU — chat/vision/agent
tools, ComfyUI image and video generation, and offline speech, all behind one FastAPI gateway and a
build-free web UI. Weights and runtimes are not bundled; see `docs/MODELS.md`. MIT licensed (code only).
