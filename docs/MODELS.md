# 模型与运行时

**本仓库不包含任何权重。** 下表是这份代码实际加载过的文件、体积与上游地址
（HF 路径均为 `https://huggingface.co/<repo>/resolve/main/<file>`；带 revision 的按 revision 取）。

下载单文件可以直接用仓库自带的下载器：

```powershell
& $env:QWEN_PYTHON -X utf8 tools\fetch_hf_file.py --help    # 单文件、断点续传、可走 hf-mirror
& $env:QWEN_PYTHON -X utf8 deployment\download_models.py     # MiniMax-H3 五个文件（带 sha256 校验）
& $env:QWEN_PYTHON -X utf8 deployment\download_image_models.py  # 生图 checkpoint / VAE
& $env:QWEN_PYTHON -X utf8 fetch_asr_model.py small          # whisper（走 ModelScope）
```

HF 对单连接限速明显，并发才是有效加速手段（`$env:DSH_FETCH_WORKERS=24` 之类，见各下载器说明）。

---

## 1. 语言模型（二选一，`settings.json` 的 `model_profile` 决定）

### 默认：三元 27B（需要 Prism fork）

| 文件 → 放到 | 大小 | 上游 |
| --- | --- | --- |
| `Bonsai-2-27B-PQ2_0-CRACK.gguf` → `models/Bonsai-2-27B-CRACK/` | 6.72 GB | `dealignai/Bonsai-2-27B-Ternary-CRACK-GGUF` |
| `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` → 同目录 | 0.59 GB | `prism-ml/Ternary-Bonsai-2-27B-gguf` |

> 它们是 2.13 bpw 的三元量化（`{−1,0,+1}` + 组缩放），**stock llama.cpp 会以 unknown tensor type 拒绝加载**，
> 所以必须用 `runtime-prism-10826\`。显存占用约 7 GB，64 层全部上卡。

### 备选：9B Q6（stock 构建即可）

| 文件 → 放到 | 大小 | 上游 |
| --- | --- | --- |
| `Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf` → `models/Qwen3.5-9B-Uncensored/` | 6.85 GB | `HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive` |
| `mmproj-Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-BF16.gguf` → 同目录 | 0.86 GB | 同一仓库 |

## 2. 语音（可选）

| 文件 → 放到 | 大小 | 上游 |
| --- | --- | --- |
| `Qwen3-ASR-0.6B-Q8_0.gguf` + `mmproj-Qwen3-ASR-0.6B-Q8_0.gguf` → `models/asr/qwen3-asr-0.6b/` | 0.77 + 0.20 GB | `ggml-org/Qwen3-ASR-0.6B-GGUF` |
| `Qwen3-TTS-12Hz-1.7B-CustomVoice/`（整个目录）→ `models/tts/` | 3.66 GB | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` |
| `Qwen3-TTS-Tokenizer-12Hz/model.safetensors` → `models/tts/Qwen3-TTS-Tokenizer-12Hz/` | 0.65 GB | `Qwen/Qwen3-TTS-Tokenizer-12Hz` |
| whisper `small`（`model.bin` 等）→ `models/asr/small/` | 0.46 GB | ModelScope `pengzhendong/faster-whisper-small`（脚本 `fetch_asr_model.py`） |

TTS worker 还需要一个第三方引擎 checkout（`tools/Qwen3-TTS-GGUF`，含 `qwen3_tts_gguf` 包）
与它自己的 venv（`tools/tts-venv`，`window` 下由网关按需创建/复用）。
把 checkout 放到别处时用 `QWEN_TTS_PROJECT=<路径>` 指定。

## 3. 生图（放到 ComfyUI 的 `models/` 下）

| 文件 → 子目录 | 大小 | 上游 |
| --- | --- | --- |
| `CyberRealisticPony_V18.0_F16.safetensors` → `checkpoints/` | 6.94 GB | `cyberdelia/CyberRealisticPony` |
| `CyberRealistic_PonySemi_V5.safetensors` → `checkpoints/` | 7.11 GB | `cyberdelia/CyberRealisticSemiRealPony` |
| `t5xxl_fp8_e4m3fn.safetensors` → `text_encoders/` | 4.90 GB | `comfyanonymous/flux_text_encoders` |
| `ae.safetensors` → `vae/chroma_ae.safetensors` | 0.34 GB | `lodestones/Chroma` |
| `chroma-unlocked-v21.safetensors` → `diffusion_models/`（仅 Chroma 管线） | 17.8 GB | `lodestones/Chroma` |
| LoRA：`lcm-lora-sdxl`、`dmd2_sdxl_4step_lora_fp16`、`Hyper-SDXL-8steps-CFG-lora`、`sdxl_photorealistic_slider_v1-0`、`face_xl_v0_1`、`extremely-detailed` → `loras/` | 0.01–0.79 GB | 依次见 `deployment/fetch_image_models.py` 里的 URL |

## 4. 视频：MiniMax-H3（放到 ComfyUI 的 `models/` 下）

下表的 size / sha256 / revision 与 `deployment/model-manifest.json` 一致，`deployment/download_models.py` 会校验。

| 文件 → 子目录 | 大小 | 上游（repo@revision） |
| --- | --- | --- |
| `MiniMax-H3-ref2va-curve-Q4_0.gguf` → `diffusion_models/minimax_h3/` | 11.46 GB | `joeygambino/MiniMax-H3-curve-GGUF@defd1c4e` |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` → `text_encoders/` | 15.69 GB | `Comfy-Org/MiniMax-H3@4cc1d817` |
| `minimax_h3_video_vae_fp16.safetensors` → `vae/minimax/` | 5.21 GB | `Comfy-Org/MiniMax-H3@4cc1d817` |
| `minimax_h3_audio_vae_fp32.safetensors` → `vae/minimax/` | 0.61 GB | `Comfy-Org/MiniMax-H3@4cc1d817` |
| `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` → `loras/MiniMaxH3/` | 1.96 GB | `Comfy-Org/MiniMax-H3@4cc1d817` |

> 视频是这套东西里最吃资源的部分：单段 768×512 / 3 步蒸馏约 220 s 量级，四镜头成片按小时算。
> 先确认你能接受这个代价再下 H3 的权重。

## 5. 运行时（llama.cpp / ffmpeg）

| 目录 | 内容 |
| --- | --- |
| `runtime-stock-11935\` | `ggml-org/llama.cpp` release **b11935**（Windows CUDA 包，解压即可） |
| `runtime-prism-10826\` | `PrismML-Eng/llama.cpp` release **prism-b10826** |
| `runtime\` | `ffmpeg.exe` / `ffprobe.exe`（视频拼接、降噪、抽帧） |

`tools/runtime_updater.py` 可以自动完成"下载 → 校验 → 探针 → 配对测速 → 决定"，
两条轨道（stock / fork）各自独立，默认不重启服务，上一个构建永不删除，可 `--rollback`。

## 6. 下载完的自检

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

它会检查解释器版本、依赖、两个运行时目录、`models\` 下是否有 gguf、ComfyUI 是否在兄弟目录。
