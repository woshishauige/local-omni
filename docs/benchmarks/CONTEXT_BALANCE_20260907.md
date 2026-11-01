# Qwen 本机上下文容量、速度与内存平衡实测

2026-09-07 至 2026-09-08 实测并部署，归档 `benchmarks/context-balance-20260907`。最终配置：**49,152 Token（48K，原 16,384 的 3 倍）**。短输入基本持平：16K **7.6934** / 48K **7.6611** Token/s（**−0.42%**），整请求 **17.6550 → 17.7396 秒**（+0.48%），短提示词平均预填充 **1101.572 → 1115.422 毫秒**（+13.85 毫秒，1.26%，平均整请求 +84.6 毫秒）。

**长历史并非同速**：48,128 Token 输入预填充 318.40 秒，随后 33 Token 输出 **4.263 Token/s**（低于短输入样本约 **7.7** Token/s）；同一 12,288 Token 输入对照两档预填充均约 75.2 秒，生成 16K **6.95212** / 48K **6.75884** Token/s（即约 6.759，−2.78%），整请求 79.89 / 80.02 秒。**不能把“相同短输入时基本持平”解释为“历史填满 48K 后仍有 7.7 Token/s”；全部数字来自固定题目的短样本与单次长输入配对，不支持严格零回退的承诺。**

[settings.json](D:/AI/Qwen-Local/settings.json)：上下文 49,152、文本 / 识图 GPU 层数 38 / 40、文本 MTP 1、历史缓存 1024 MiB、4 个检查点、生成读取超时 3600 秒、默认 Agent 保留、生产空闲休眠 300 秒；三轮短测速统一用缓存 0 MiB、2 个检查点，近满窗口与识图验收用 1024 MiB / 4。容量含系统提示词、Agent 工具定义、历史、当前输入、图片 Token 和输出。

## 硬件与参数

**Intel Core i9-13900HX、32 GB 系统内存（32,479 MiB）、NVIDIA GeForce RTX 5070 Ti Laptop GPU 12 GB**；Windows 11，24 个物理核心、32 个逻辑处理器；显存余量以每次采样为准。

| 参数 | 值 |
| --- | --- |
| 模型权重 | 原 `Qwen3.8-27B-ZeroRefusal-UD-IQ4_XS-V3-Final-MTP.gguf` |
| 运行时 | llama.cpp b10819，commit `6a1a922d2` |
| 模式 | 纯文本，单模型、单槽位 |
| GPU 层数 / 线程（生成 / 批处理） | 38 / 16 / 16 |
| MTP | 启用，最多 1 个草稿 Token，由主模型验证 |
| K / V 缓存精度；Flash Attention | Q8_0 / Q8_0；开启 |
| batch / ubatch | 256 / 128 |
| 历史状态缓存 / 上下文检查点 | 0 MiB / 2，最小间距 8192 |
| RoPE、权重、采样及思考设置 | 未增加 RoPE 缩放，未改权重 |

未通过量化权重或 K/V 缓存换容量；CPU/GPU 调度、批处理与浮点执行顺序仍可能产生舍入差异，不承诺逐字相同。

## 测速方法与短输出结果

脚本 [compare_context_speed.py](D:/AI/Qwen-Local/benchmarks/compare_context_speed.py) 复用 [run_api_benchmark.py](D:/AI/Qwen-Local/benchmarks/run_api_benchmark.py)：学习日建议、文生视频提示词、Python 众数函数、温度 0.7 的学习日建议（前三题温度 0）；每配置先同四题热身一轮（**不计入统计**），每题 128 Token，`seed=1234`、`top_p=0.9`、`top_k=20`、`cache_prompt=false`、经 `/apply-template`；速度取 `timings.predicted_per_second`，几何平均 `exp(mean(log(各样本 Token/s)))` 等权。

| 任务 | 每次输入 / 输出 Token | 16K，Token/s | 48K，Token/s | 48K 变化 |
| --- | ---: | ---: | ---: | ---: |
| 学习日建议，温度 0 | 41 / 128 | 7.4091 | 7.3724 | −0.50% |
| 视频提示词，温度 0 | 58 / 128 | 7.6068 | 7.6403 | +0.44% |
| Python 众数函数，温度 0 | 50 / 128 | 8.5119 | 8.4507 | −0.72% |
| 学习日建议，温度 0.7 | 41 / 128 | 7.3025 | 7.2370 | −0.90% |
| **全部 12 个正式样本几何平均** | — | **7.6934** | **7.6611** | **−0.42%** |

24 份正式记录按题的 `response.content`、格式化提示词及 `generation_settings` 一致；MTP“接受数 / 草稿数”50 / 76、52 / 74、60 / 67、49 / 78。

| 批次 | 容量 | 正式样本数 | 几何均速，Token/s | 最低剩余显存，MiB | 最低可用 RAM，MiB | 最低可用提交量，MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `ouzd2kkb` | 16,384 | 4 | 7.724812 | 2021 | 8750.3 | 10045.5 |
| `z3l1_3ye` | 49,152 | 4 | 7.696432 | 1053 | 8153.3 | 8503.3 |
| `w9xe3ilm` | 49,152 | 8 | 7.643567 | 1045 | 8071.9 | 8385.0 |
| `b22dush6` | 16,384 | 8 | 7.677681 | 1913 | 8283.6 | 9677.7 |

四批次 GPU 最高采样温度 69、68、68、68℃，平均约 63.38、63.90、63.99、64.35℃；平均采样功率约 46.7、47.4、48.8、49.1 W；GPU 查询失败 0。

## 长输入实测

| 指标 | 48K 近满窗口 `34akp971` | 同一 12K 输入的 16K `1wnmq72l` | 同一 12K 输入的 48K `tppe2y9l` | 48K 识图 `0q8zud1y` |
| --- | ---: | ---: | ---: | ---: |
| 实际输入 / 输出 Token | 48,128 / 33 | 12,288 / 33 | 12,288 / 33 | 186 / 128 |
| 预填充耗时 / 速度 | 318.399 秒（约 5 分 18 秒）/ 151.156 Token/s | 75,196.041 ms / 163.41286 Token/s | 75,198.960 ms / 163.40652 Token/s | 3.728 秒 / 49.893 Token/s |
| 生成耗时 / 速度 | 7.506 秒 / **4.263 Token/s** | 4,602.915 ms / **6.95212 Token/s** | 4,734.538 ms / **6.75884 Token/s** | 18.184 秒 / 6.984 Token/s |
| 整请求墙时 / 整个验收流程 | 326.00 秒（约 5 分 26 秒）/ 350.97 秒 | 79.89 秒 / — | 80.02 秒 / — | — / 48.27 秒 |
| 最低观察显存余量 | 1007 MiB | 1979 MiB | 988 MiB | 1198 MiB |
| 最低观察可用 RAM / 可用提交量 | 7089.3 / 7424.3 MiB | 7871.2 / 8991.4 MiB | 6979.0 / 7580.0 MiB | 6508.4 / 6930.5 MiB |
| GPU 查询失败 / 护栏触发 | 0 / 无 | 0 / 无 | 0 / 无 | 0 / 无 |

- `34akp971`：49,152 上下文、38 GPU 层、MTP 1、Q8 K/V、1024 MiB 缓存、4 个检查点；返回 `tokens_evaluated=48128`、`truncated=false`，按序找回哨兵 `QWEN-592741`、`QWEN-836205`、`QWEN-174963`；协议通过：强制 `local_probe` 且参数 `context_check`（未执行工具）、正文 + `[DONE]`、49,153 Token 超限返回 HTTP 400（`n_ctx=49152`、`n_prompt_tokens=49153`）、随后短请求恢复；两个“正常”回复仅 2 Token，不与 128 Token 测速混合。
- 12K 对照：均无截断，哨兵与全文相同，MTP 均 16 草稿 / 16 接受；预填充差 **2.919 毫秒**，生成差 **131.623 毫秒**。
- 无效运行 `33qdjpeu`：约 50.1 秒时显存余量降至 758 MiB 被护栏停止，因生产 8081 / 8082 重启与 16K 后端驻留导致外部争用；**排除出性能比较，不记为 48K 自身显存不足或有效速度样本**。
- `0q8zud1y`：40 GPU 层、原 CPU 视觉投影、Q8 K/V、1024 MiB 缓存、4 个检查点，识图关闭 MTP；`D:/AI/deployment/qwen-vision-test.png` 识别出白色背景、左侧红色正方形、右侧蓝色圆形；五项协议检查（工具调用格式、SSE + `[DONE]`、超限 400 及容量字段、超限后恢复、红／蓝／左／右识别）通过；输出受 128 Token 上限截断。

## 更大上下文的筛选与停止原因

| 配置 | 已观察结果 | 决策及限制 |
| --- | --- | --- |
| 32K、38 GPU 层、MTP 1 | 未热身短样本 7.458 Token/s；显存余量 1518 MiB | 保守容量备选；不可与热身后均速混合 |
| 48K、38 GPU 层、MTP 1 | 未热身短样本 7.423 Token/s；显存余量 1064 MiB | 进入正式对比 |
| 56K（57,344）、38 GPU 层 | 显存余量 838 MiB | 低于 900 MiB 护栏自动停止，未稳定通过 |
| 64K（65,536）、38 GPU 层 | 显存余量 520 MiB | 低于护栏自动停止，未稳定通过 |
| 256K（262,144）、20 GPU 层、无 MTP | 短样本 4.345 Token/s | 不符合速度约束，未部署 |
| 256K、22 GPU 层、无 MTP | 短样本 4.732 Token/s | 慢于原约 7–9 Token/s，未部署 |

256K、22 层的 `57y0xb49` 曾开始 261,120 Token 输入测试，随后主动取消（用户要求不降速度与精度），其 JSON 记 `failed` 与连接重置：**不就该记录宣称满 256K 通过，也不归因为模型崩溃**；56K / 64K 护栏仅证明当时桌面占用下达不到余量，不代表更低层数或别的硬件不能加载。

## 缓存策略、护栏与精度边界

统一条件用 `--cache-ram 0 --ctx-checkpoints 2`，禁用槽位外的历史提示词状态缓存（活动槽位内 KV 复用仍在），可能增加多会话／改前缀时的预填充重算。最终为 **1024 MiB 上限、最多 4 个检查点**；本机同 commit 会在分配前算状态总量、先淘汰再分配、单条超上限跳过：**满 48K 会话单条状态超过 1024 MiB 即不能进入该缓存**，当前槽位 KV 仍可复用；1024 MiB 是有限预算，不是“可永久缓存多个满 48K 会话”的保证。[缓存分配实现](https://github.com/ggml-org/llama.cpp/blob/6a1a922d2/tools/server/server-task.cpp)、[槽位及检查点处理](https://github.com/ggml-org/llama.cpp/blob/6a1a922d2/tools/server/server-context.cpp)

近满窗口验收命令：`--context 49152 --layers 38 --long --features --cache-ram 1024 --checkpoints 4`。护栏：仅启 18084；8081、8082、18084 占用即拒绝；RAM < 2048 MiB、显存 < 900 MiB、Windows 可用提交量 < 2048 MiB 即停自身进程并记失败；生产端口出现时记 `production_service_interference` 并排除。约一至两秒采样只代表“最低观察余量”，Windows 提交量含页面文件；读取超时 `server_timeout_seconds` = 3600 秒，连接 5 秒、写入及连接池 600 秒。满窗口预填充与注意力成本高于短输入，**不能承诺 48K 输入与几百 Token 用时相同**。

## 验收状态与部署记录

`b22dush6`（16K 回测）纳入统计；48K 近满窗口、Agent、SSE、超限 400、错误后恢复由 `34akp971` 通过；12K 对比 `1wnmq72l` / `tppe2y9l` 通过；识图 `0q8zud1y` 通过；最终配置后 117 项回归通过。正式服务实例 `1206419813d54a22b7a24ce39ca9a149`，唯一后端 llama-server PID 26892，参数 49,152 / 38 层 / 1024 MiB / 4 检查点 / 3600 秒；`/local/status` 与 `/props` 返回 49,152、`ready`、`active_requests=0`，Agent 与文本 MTP 启用；8081 网关 `context_probe(context=49152)` 返回 HTTP 200 正确工具调用（未执行动作），输入 281 / 输出 29 / 合计 310 Token，记录 ID `f8085425f8604e9abf5815cd8876db41`。

## 原始证据

速度与容量归档均在 `D:/AI/Qwen-Local/benchmarks/context-balance-20260907/`：`speed-16k-round1.json`(`ouzd2kkb`)、`speed-48k-round1.json`(`z3l1_3ye`)、`speed-48k-round2-3.json`(`w9xe3ilm`)、`speed-16k-round2-3.json`(`b22dush6`)、`capacity-32k-checks.json`、`capacity-56k-guard.json`、`capacity-64k-guard.json`、`capacity-256k-short.json`、`capacity-256k-cancelled.json`、`capacity-48k-full.json`(`34akp971`)、`matched-12k-in-16k.json`(`1wnmq72l`)、`matched-12k-in-48k.json`(`tppe2y9l`)、`vision-48k-checks.json`(`0q8zud1y`)、`excluded-production-interference.json`(`33qdjpeu`，不纳入性能结论)。部署与验收汇总：[context-validation-20260908.json](D:/AI/Qwen-Local/logs/context-validation-20260908.json)。容量归档仅去掉服务端回显的合成提示词，生成输出、timings 与环境遥测未改。

> 精简前原文（2026-09-14，逐字节归档）：D:\AI\_archive\Qwen-Local\benchmarks\CONTEXT_BALANCE_20260907.md。归档索引见 D:\AI\_archive\README-archive-index.md。
