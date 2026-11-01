# 测试与验收

分四档，按"这条改动能被哪一档证伪"选最低的那一档，不要一律上全流程。

| 档 | 成本 | 覆盖 | 入口 |
| --- | --- | --- | --- |
| 1 静态 | 秒级、零显存 | 纯函数、状态机、路由契约、进程生命周期（假子进程） | `python -m unittest ...` |
| 2 接口 | 秒级、不占卡 | 真 HTTP 契约、鉴权、流式、限流 | `verify_*.py` / 直接 `curl` |
| 3 浏览器 | 十几秒、不占卡 | 界面显隐、布局、清空、交互 | `node web/check_*.cjs`、`web/test_*.cjs` |
| 4 真机 | 分钟级、占卡 | 出图 / 出片 / 识别 / 语音 | `verify_stability.py`、`check_*_live.py` |

## 1. 静态回归（不加载模型、不占显存）

```powershell
& $env:QWEN_PYTHON -X utf8 -B -m unittest `
    test_mode_server test_qwen_service test_image_jobs test_video_routes `
    test_context_compaction test_context_gateway test_usage_store test_access_gate
```

全部用例都设计成"不碰生产端口、不碰生产数据库、不加载权重"：临时目录 + 假子进程 + 临时 SQLite。
`test_import_hygiene.py` 会抓"用了 `suppress`/`contextmanager` 却没 import"这类运行期才炸的问题。

```powershell
& $env:QWEN_PYTHON -X utf8 -m py_compile *.py tools\*.py     # 全量语法检查
```

## 2. 接口级

```powershell
& $env:QWEN_PYTHON -X utf8 verify_stability.py    # 临时端口 18081/18082，跑完即停，不碰生产
& $env:QWEN_PYTHON -X utf8 verify_usage.py        # 隔离的用量库，验证一次真实请求的记账
& $env:QWEN_PYTHON -X utf8 verify_context_compaction.py <报告路径>
```

`check_*_live.py` 系列是"对着正在运行的服务验"，会读 `127.0.0.1:8081`；它们**不改生产配置**，
但需要服务已经在跑。

## 3. 浏览器行为

前置：Node 18+ 与 Playwright（脚本里默认指向本机的 playwright 安装路径，克隆到别处要改那一行，
或 `npm i playwright` 后用 `require("playwright")`）。

```powershell
node web/test-image-history.cjs      # 生图视图：切换、参数、进度、记录、取消
node web/test-context-control.cjs    # 上下文压缩面板
node web/test-usage-log.cjs          # 用量日志面板
node web/check-layout-live.cjs       # 三栏布局与溢出（需要服务在跑）
```

`web/lib/cdp.cjs` 是公共脚手架（起 Edge → 连 CDP → `evaluate` → 收尾），新脚本复用它即可。

## 4. 真机（**会占卡，按需单独跑**）

```powershell
# 启动器 + 看护 + 普通/流式回复 + 工具调用 + 进程退出恢复，全程临时目录
& $env:QWEN_PYTHON -X utf8 verify_stability.py

# 生图一次（会自动把 ComfyUI 拉起来），需要空闲显卡
& $env:QWEN_PYTHON -X utf8 check_generate_once.py
& $env:QWEN_PYTHON -X utf8 ..\deployment\verify_img2img.py 参考图.png --denoise 0.55

# 视频：最小 960×640 一段 + 显存采样
& $env:QWEN_PYTHON -X utf8 check_video_min_live.py
```

这些脚本的耗时/显存代价写在各自 docstring 里，跑之前先看。**不要把四镜头成片渲染当回归手段。**

## 环境隔离

- `QWEN_TEST_ISOLATION=1`：让被测代码用临时目录（进程记录、锁、日志），不碰生产文件。
- 测试端口固定为 18081/18082（网关/后端）、8191（ComfyUI 探针），与生产 8081/8082/8188 错开。
- 浏览器检查用临时 profile，跑完删除，不动你的登录态。

## 失败归因

先把"改动导致"和"环境导致"分开：模型没加载、ComfyUI 没起、显卡被占都会让一批用例假失败。
`logs/` 下每个验收脚本都有自己的报告文件（JSON/日志），先看报告再改代码。
