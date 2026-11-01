# 架构

一句话：**一个网关进程（8081）** 接管所有入口，**一个被看护的后端进程（8082）** 提供语言能力，
**一个按需拉起的 ComfyUI（8188）** 提供图像/视频能力，前端是**免构建**的静态资源。

```
浏览器 ──HTTP/SSE──> mode_server.py (uvicorn, 8081)
                          ├─ /v1/chat/completions ──> llama-server (8082)  ← qwen_service 看护/恢复
                          ├─ /local/image/* ────────> ComfyUI (8188)       ← image_comfy 拉起/注入图
                          ├─ /local/video/* ────────> ComfyUI + ffmpeg + RIFE
                          ├─ /local/voice/* ────────> ASR(whisper/Qwen3-ASR) + TTS worker(独立进程)
                          └─ /local/*(其余) ────────> 状态、用量、共享管理
```

## 进程与生命周期

| 进程 | 谁拉起 | 生命周期 | 关键点 |
| --- | --- | --- | --- |
| `qwen_service.py` | `Start-Qwen.bat` → `Start-Qwen.ps1` | 常驻（detached） | 单实例（系统锁 `supervisor.run.lock`）、心跳 3 次失败触发恢复、状态写 `supervisor.status.json` |
| `mode_server.py` | 被 supervisor 拉起 | 常驻 | 网关本体；所有对外 HTTP 都在这 |
| `llama-server.exe` | 网关按 profile 拉起 | 可休眠/重启 | 空闲 300 s 休眠；切换模式只重启它 |
| ComfyUI | `image_comfy.ComfySupervisor` | 按需 | 启动前让出显存；卡死队列按 GPU 利用率判定并恢复 |
| TTS worker | 网关按需拉起 | 常驻子进程 | 与网关分开，避免 ctypes 冲突和崩溃连带 |

`qwen_service.py` 为什么存在：模型进程会因驱动/显存/OOM 自己退出，而"打开网页没反应"是最难排查的失败形态。
它把"探活 → 判定 → 恢复"做成独立进程，并且**在切换权重的窗口内不计数**（`qwen_config.mark_switching()`），
否则正常换模型会被误判成卡死。

## 统一后端（`unified_backend`）

同一份 27B 权重同时承担文本与识图：命令里挂 `mmproj` 投影，按请求内容决定是否走视觉塔，
模式切换因此不需要重载权重（实测 0.0 s）。纯文本接口收到图片会**明确报错**而不是静默忽略。

## 上下文

- `context_compaction.py`：输入达到窗口 75% 时压缩**较早的完整回合**，原文留在浏览器，网关只替换发给模型的历史；
  摘要由同一模型生成，长度按窗口 `//16` 给，被 `max_tokens` 截断时先加长重试一次。
- `context_guard.py`：发请求前只对 ≥12 KB 的请求量一次 token（`/apply-template` + `/tokenize`），
  装不下就直接回 `context_overflow`，不让上游那句英文 400 静默卡住界面。
- `conversation_ledger.py`：纯文本 / 识图 / 语音共用一套历史与配额口径。

## 任务模型（生图 / 视频）

两类任务都遵循同一套状态机：`pending → running(带阶段与进度) → succeeded/failed/cancelled`，
进度来自 ComfyUI 的 WebSocket（`/ws` + 本任务 `client_id`），拿不到时退化为队列轮询 + 耗时估算，
进度条只增不减；超过 180 s 无事件报 `stalled` 并释放忙碌标记，避免"永久忙"。

视频是**逐段提交**：一次提交渲染一段，补齐最后一段时同时拼接成 `master.mp4`；
取消要中断当前段并停整条链。

## 显存仲裁（`gpu_arbiter.py`）

一张 12 GB 的卡上，语言模型、ComfyUI、TTS、ASR 会互相挤。策略写在 `gpu_arbiter` 一处：
请求方声明意图 → 仲裁决定谁让路；ComfyUI 有进行中任务时语言模型不加载（状态 `waiting`），
但**只提示不锁页**——切视图仍然可用。

## 安全边界

- 默认 loopback-only；共享模式才监听 `0.0.0.0`，并且必须带 `access_token`。
- 访客能力（能不能传参考图、能不能看别人产物）由 `guest_policy.py` 一处决定，按设备会话隔离。
- 使用日志只存指标；`usage_store.py` 的写入路径里没有正文、工具参数或密钥字段。

## 前端

`web/` 里没有打包步骤：面板脚本按需 `<script>` 加载，状态通过轮询 + SSE 获取。
`web/native/` 是 llama.cpp 自带 UI 的构建产物（MIT），保留它是为了旧界面兜底。

## 目录职责

见 README 的"目录结构"；每个模块的第一行 docstring 写了它自己回答什么问题，`grep -n '"""' *.py` 可以一次看完。
