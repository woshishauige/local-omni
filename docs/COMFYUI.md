# ComfyUI 侧

生图 / 视频 / 超分都跑在同一个 ComfyUI 上，网关负责拉起它、注入图、读进度。
聊天不需要 ComfyUI。

## 目录布局（硬约束）

```
<上一级>/
├─ qwenweb/                    ← 本仓库
└─ ComfyUI-BSAI_v010/          ← 名字就是代码里的常量
   ├─ python/python.exe        ← 网关默认复用的解释器
   ├─ ComfyUI/main.py
   ├─ ComfyUI/models/          ← 权重（docs/MODELS.md 第 3、4 节）
   ├─ ComfyUI/custom_nodes/    ← 节点包
   ├─ ComfyUI/input/ output/   ← 生成产物
   └─ ComfyUI/user/default/workflows/MiniMax-H3/   ← 工作流 JSON（本仓库 workflows/ 覆盖过去）
```

代码位置：`app/image/image_comfy.py:39`（`COMFY_ROOT`）、`app/video/video_jobs.py:32`（`WORKFLOW_ROOT`）、
`app/mode_server.py:102`（`COMFY_INPUT`）。

## 启动参数（网关自动用，不需要手点）

`image_comfy.COMFY_FLAGS` 固定了这套：`--listen 127.0.0.1 --port 8188`、`--disable-auto-launch`、
`--enable-dynamic-vram`、`--fast-disk`、`--reserve-vram 0.8`、`--preview-method latent2rgb`、
`--use-pytorch-cross-attention`、`--disable-pinned-memory`、`--disable-api-nodes`、
**`--disable-all-custom-nodes` + `--whitelist-custom-nodes <名单>`**。

## 允许加载的节点包（白名单，13 个）

| 包名（= 安装目录名） | 用途 |
| --- | --- |
| `ComfyUI-GGUF` | 加载 GGUF 量化权重（H3、SeedVR2 都用） |
| `ComfyUI-H3-Multishot` | H3 分镜/多镜头 |
| `ComfyUI-H3-Motion-Context` | 上一段成片续接（运动上下文） |
| `ComfyUI-H3-LocalDirector` | H3 导演节点（`H3MP4ShotEditor` 等） |
| `ComfyUI-MiniMaxH3-FirstBlockCache` | 首块缓存加速 |
| `minimax-h3-audio-T8` | H3 双时钟采样器、音视频 latent 拆分/解码 |
| `ComfyUI-Frame-Interpolation` | RIFE 补帧（`RIFE VFI`） |
| `ComfyUI-VideoHelperSuite` | 视频读写（`LoadVideo`/`SaveVideo`/`CreateVideo`） |
| `ComfyUI-KJNodes` | 常用工具节点 |
| `rgthree-comfy` | 常用工具节点 |
| `ComfyUI_Comfyroll_CustomNodes` | 常用工具节点 |
| `ComfyUI-Easy-Use` | 常用工具节点 |
| `ComfyUI-Custom-Scripts` | 前端小工具 |

> 白名单之外的包被 `--disable-all-custom-nodes` 全部跳过（`ComfyUI/nodes.py:2381`，按**目录名精确比较**）。
> 上游地址以 ComfyUI-Manager 的搜索为准；安装后目录名必须与上表一致。

### 两个已知的坑

1. **改名的包不会被加载**：本机 RIFE 那个包目录叫 `comfyui-frame-interpolation_cudafull`（cudafull 变体），
   与白名单里的 `ComfyUI-Frame-Interpolation` 不同名 → 网关拉起 ComfyUI 时补帧节点会缺失，
   `app/video/video_interpolate.py` 提交的 `RIFE VFI` 会被 ComfyUI 拒绝。装成标准目录名即可。
2. **超分（SeedVR2）不在白名单里**：`ComfyUI-SeedVR2_VideoUpscaler` 提供 `SeedVR2Preprocess` 等节点，
   当前白名单没有它，`scripts/check_comfy_node_packs.py` 会报缺。两种解法：把它加进 `COMFY_FLAGS` 的白名单，
   或超分时手工启动 ComfyUI（不带 `--disable-all-custom-nodes`）。**这条在发布版里仍待决定。**

## 工作流

网关按文件名点名调用 `ComfyUI/user/default/workflows/MiniMax-H3/` 下的 JSON：

| 目录 | 文件 | 用途 |
| --- | --- | --- |
| `视频生成/` | `01_视频_质量_20步原生.json`、`02_视频_速度_缓存加速.json`、`03_视频_极速_3步蒸馏.json`、`04_视频_均衡_6步蒸馏.json`、`05_视频_精细_12步蒸馏.json`、`06_视频_极致细节_20步蒸馏.json`、`09_视频_加速_官方turbo_8步.json`、`10_视频_NSFW_6步.json` | 四条线路的配方（质量/速度/极速/均衡…） |
| `超分优化/` | `01_清晰度最佳_1p25x_3step.json`、`02_噪点最少_1x_3step.json`、`03_综合最佳_1p5x_2step.json` | SeedVR2 超分配方 |

本仓库 `workflows/MiniMax-H3/` 就是这批文件，装好后复制（或直接软链）到 ComfyUI 的 `user/default/workflows/`。

## 自检

```powershell
# 起一个临时实例（8191 端口、与生产同一套 flag），逐个核对工作流用到的 class_type 是否存在
& $env:QWEN_PYTHON -X utf8 scripts/check_comfy_node_packs.py
# 加 --dump-schema 可以打印关键节点的输入契约
```

它读的是 `image_comfy.COMFY_FLAGS`，所以白名单改了它会跟着改，不会与生产漂移。
