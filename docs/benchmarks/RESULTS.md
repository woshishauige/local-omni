# Local Qwen Benchmark Results

Historical measurements recorded 2026-09-06; isolated benchmark processes ran sequentially and stopped after testing. The deployment was updated to 49,152 tokens on 2026-09-08. See [the current context and speed report](D:/AI/Qwen-Local/benchmarks/CONTEXT_BALANCE_20260907.md) for the three-round 16K/48K comparison, long-input results and current configuration.

## Deployed 16K Context

The local GGUF declares qwen35.context_length=262144; context was raised from 8192 to 16384 for text and vision without RoPE scaling, weight requantization, KV precision changes or image processing changes. At 42 GPU layers the 16K text/MTP configuration left only 303 MiB free, so the deployment uses 38 text GPU layers, 40 vision GPU layers and 16 generation/batch threads, with MTP at one draft token in text and off in vision. Moving layers to CPU preserves formats but can reduce throughput. Raw responses: `text_38_t16_ctx16384_mtp1.json`, `text_42_t16_mtp1.json`, `text_40_t16_mtp1.json`, `vision_42_t16.json`, `vision_40_t12_chat.json`.

| Run | Results per task (prose, video, code, prose_sampled; temperature 0, 0, 0, 0.7) | Draft accepted/generated | Peak / min free MiB |
| --- | --- | --- | ---: |
| text_38_t16_ctx16384_mtp1 (16K, 38-layer, live 8081 proxy) | 7.630, 7.830, 8.647, 7.542 tok/s | - | - |
| vision_40_t12_chat | prose 7.153 tok/s / 40.54 prompt tok/s | - | 10113 / 1832 |
| text_40_t12 | 6.796 / 40.94, 6.710 / 55.57, 6.910 / 45.30, 6.879 / 41.23 | - | 10623 / 1322 |
| text_40_t8 | prose 6.313 / 36.72 | - | 10596 / 1349 |
| text_40_t16 | prose 7.222 / 42.92 | - | 10190 / 1755 |
| text_42_t16 | 7.645 / 45.15, 7.571 / 60.47, 7.680 / 53.02, 7.213 / 44.82 | - | 10566 / 1379 |
| text_44_t16 | prose 7.822 / 44.40 | - | 11016 / 929 |
| text_42_t16_mtp2 | 7.026 / 40.08, 7.553 / 53.69, 10.320 / 48.79, 6.738 / 40.81 | 63/128 (49.2%), 67/118 (56.8%), 83/88 (94.3%), 61/131 (46.6%) | 11135 / 810 |
| vision_42_t16 | prose 7.451 / 39.96; vision 7.598 / 49.96 | - | 10586 / 1359 |

Cells are decode tok/s or decode tok/s / prompt tok/s. Historical 8K baseline for the same four tasks at 42 GPU layers / MTP 1: prose 8.618, video 8.819, code 9.786, prose_sampled 7.720 tok/s. `context_16k_validation.json`: real HTTP mode switching, stream cancellation, busy-switch rejection, text-mode image rejection, image inference, and a 13,224-token input that correctly retrieves the code from its first line without truncation (prompt evaluation 81.705 seconds, 161.85 tok/s); its seven-token answer is too short for a stable decode benchmark, the 128-token image response ran at 7.398 tok/s, and memory sampling covering only part of the long prefill/decode observed 947 MiB minimum free VRAM and 5267 MiB minimum available system RAM. Real browser button transitions text -> vision -> text passed with draft restoration and 16K backend capability checks; screenshots/layout checks passed at 1440x900, 390x844 and 320x640. Startup MTP verification reads an actual completion's generation settings because b10819 /props reports a default speculative.types=none even when the slot uses native MTP.

## Conditions

Same installed model and executable in every run: no weight requantization, no smaller context, no lower KV-cache precision; context 8192, Q8_0 K/V, batch 256/128, Flash Attention on, one slot, reasoning off/low. GPU/CPU placement and thread count were the only conventional tuning variables; MTP used draft-mtp with maximum 2 draft tokens and p-min 0. Each server received a warmup, then identical native completion requests through its own /apply-template chat formatter: cache_prompt=false, seed=1234, 128 generated tokens, top_p=0.9, top_k=20. The context allocation remained 8192, but these are short-prompt tests, not a full-length 8192-token conversation stress test. The earlier baseline_vision_40_t12 files used a raw prompt during harness setup and are excluded; vision_40_t12_chat is the formatted replacement.

## MTP Compared With The Same Configuration

| Task | MTP-off tok/s | MTP-on tok/s | Change | Exact output match |
| --- | ---: | ---: | ---: | --- |
| prose | 7.645 | 7.026 | -8.1% | False |
| video | 7.571 | 7.553 | -0.2% | True |
| code | 7.680 | 10.320 | +34.4% | False |
| prose_sampled | 7.213 | 6.738 | -6.6% | False |

## Follow-up: One Draft Token

`text_42_t16_mtp1.json` tested the same target and configuration with one MTP draft token and supersedes the initial recommendation to disable MTP entirely.

| Task | MTP off | MTP, 1 draft token | Change versus MTP off | Change versus original 40/12 |
| --- | ---: | ---: | ---: | ---: |
| Chinese prose, temperature 0 | 7.645 | 8.618 | +12.7% | +26.8% |
| Video prompt, temperature 0 | 7.571 | 8.819 | +16.5% | +31.4% |
| Python, temperature 0 | 7.680 | 9.786 | +27.4% | +41.6% |
| Chinese prose, temperature 0.7 | 7.213 | 7.720 | +7.0% | +12.2% |

Peak GPU memory 11101 MiB with 844 MiB free at the tightest sample; draft acceptance 50/76, 53/74, 61/66, 44/83. The prose greedy output matched its MTP-off counterpart, the other three outputs did not, and target weights, target sampling and all numeric formats stayed unchanged. An attempted 40-layer follow-up (`text_40_t16_mtp1.json`) was stopped during initialization when other GPU memory use left too little free VRAM: not a valid speed measurement, and the benchmark guard stopped only its own process.

## Initial Recommendation (Two Draft Tokens)

Without MTP, use 42 GPU layers and 16 generation threads with batch threads at 16: 42 layers left about 1.3 GiB of observed GPU headroom, whereas 44 layers gained only 2.3% on the screening prompt while spending about 450 MiB more VRAM, not a worthwhile default tradeoff for a laptop also rendering the browser. MTP at 42 layers loaded and verified drafts, but ordinary Chinese prose slowed by 8.1%, sampled prose slowed by 6.6%, the video prompt was essentially unchanged, and code improved by 34.4%, with peak VRAM at 11135 MiB and only 810 MiB free: not a general-purpose speed improvement on this machine.

## Quality Observations

At temperature zero, only 1 of 3 128-token MTP outputs was byte-for-byte identical to the same-placement baseline; differences in prose and code were wording/docstring formatting. That is not by itself evidence of degraded model quality, since changing CPU/GPU placement without MTP also changed 1 of 3 deterministic outputs and floating-point execution paths can differ, and the truncated code sample is not a complete executable code-quality test. **These measurements establish a useful candidate only: they are bounded local measurements, not statistical quality equivalence, not a guaranteed percentage improvement for every prompt, and short samples from different runs are not a controlled estimate of the isolated context-size cost.** The retained measurements include raw responses, timings, outputs, per-second nvidia-smi telemetry and commands; temporary server logs have been cleaned up.

The vision test at 42 layers/16 threads correctly identified the white background, red square on the left, blue circle on the right, and absence of text in deployment/qwen-vision-test.png: 115 tokens at 7.598 tok/s with 191 prompt tokens and 3.823 seconds of prompt evaluation, retained in vision_42_t16.json.

> 精简前原文（2026-09-14，逐字节归档）：D:\AI\_archive\Qwen-Local\benchmarks\RESULTS.md。归档索引见 D:\AI\_archive\README-archive-index.md。
