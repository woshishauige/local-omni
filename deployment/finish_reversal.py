"""Finish a partially reversed file: fill the literals that still hide bytes.

``reverse_mojibake.py`` rebuilds the byte stream of a CP936-mangled file and
marks the byte pairs Windows could not map as NUL pairs. Every such spot sits
inside a string literal whose intended text is known, so this table rewrites the
literal wholesale: ``literal`` rules replace the quoted body, ``span`` rules
replace everything between two anchors and restore a closing quote the decoder
ate as well.
"""

from __future__ import annotations

import sys
from pathlib import Path

# (kind, anchor, end anchor (span only), intended text, occurrences)
RULES: list[tuple[str, str, str, str, int]] = [
    # -- mocked gateway payloads ----------------------------------------------
    ("span", 'family: "flux2", summary: "', ', base_models: [',
     '原管线", nsfw: "受模型自身安全限制，NSFW 效果差"', 1),
    ("span", 'summary: "一体化 checkpoint", nsfw: "', ', base_models: [',
     '最成熟：Pony/Illustrious 系专为 NSFW 训练，LoRA 生态最大"', 1),
    ("literal", 'sampler: "euler", enhancer: ', "", "Qwen3.8-27B 本地", 1),
    ("literal", 'assert.ok(modeState.modelRows.includes(', "", "Qwen3.8-27B 本地", 1),
    ("span", 'state: "running", phase: "sampling", prompt: "', ', negative_prompt',
     '一只戴帽子的猫"', 1),
    ("span", 'state: "completed", prompt: "', ', negative_prompt', '一只戴帽子的猫"', 1),
    ("span", 'phase: "cancelled", progress: 0.5, error: "', " });", "已取消\"", 1),
    # -- editable workflow templates -----------------------------------------
    # The graph entry ends with `, inputs:`, the template row with `, fields:`;
    # the nearer anchor must be tried first or the span runs into the next
    # template in the file and swallows everything between them.
    ("span", 'class_type: "CheckpointLoaderSimple", title: "', ', inputs:', '主模型"', 1),
    ("span", 'class_type: "CheckpointLoaderSimple", title: "', ', fields:', '主模型"', 1),
    ("span", 'class_type: "CLIPTextEncode", title: "正向提示', ', fields:', '词"', 1),
    ("span", '{ input: "text", label: "正向提示', ', type:', '词"', 1),
    ("span", 'class_type: "CLIPTextEncode", title: "负面提示', ', fields:', '词"', 1),
    ("span", '{ input: "text", label: "负面提示', ', type:', '词"', 1),
    ("span", 'class_type: "CLIPLoader", title: "', ', fields:', '文本编码器"', 1),
    ("span", '{ input: "clip_name", label: "', ', type:', '编码器文件"', 1),
    ("span", 'class_type: "EmptyLatentImage", title: "', ', fields:', '画布尺寸"', 1),
    ("span", 'class_type: "EmptyFlux2LatentImage", title: "', ', fields:', '画布尺寸"', 1),
    ("literal", '{ node: "3", input: "width", title: ', "", "画布尺寸", 1),
    ("literal", '{ node: "3", input: "height", title: ', "", "画布尺寸", 1),
    ("literal", '{ node: "6", input: "width", title: ', "", "画布尺寸", 1),
    ("literal", '{ node: "6", input: "height", title: ', "", "画布尺寸", 1),
    ("span", '{ input: "sampler_name", label: "', ', type:', '采样器"', 3),
    ("span", '{ input: "scheduler", label: "', ', type:', '调度器"', 1),
    ("span", 'class_type: "KSamplerSelect", title: "', ', fields:', '采样器"', 1),
    ("span", 'class_type: "CFGGuider", title: "', ', fields:', '提示词引导"', 1),
    ("span", '{ node: "10", input: "cfg", title: "', ', label:', '提示词引导"', 1),
    # -- LoRA preset catalogue -------------------------------------------------
    ("span", '{ key: "lcm", label: "', ', lora:', 'LCM-LoRA · 8 步"', 1),
    ("span", 'note: "Latent Consistency', ', params:', 'Latent Consistency：4–8 步、CFG 1–2。"', 1),
    ("span", '{ key: "hyper8", label: "', ', lora:', 'Hyper-SDXL · 8 步"', 1),
    ("span", 'note: "Hyper-SD 8 ', ', params:', 'Hyper-SD 8 步 CFG 版。"', 1),
    ("literal", '{ key: "face", label: ', "", "面部结构修复", 1),
    ("span", 'note: "face helper', ', params:', 'face helper：改善五官结构。"', 1),
    ("literal", '.qg-preset-chip", { hasText: ', "", "面部结构修复", 1),
    # -- panel assertions ------------------------------------------------------
    ("span", 'assert.deepEqual(chipState.roles, [', '], "chips are grouped', '"加速", "画质"', 1),
    ("span", '.qg-status-line").textContent.includes("已恢', "))", '复默认"', 1),
    ("span", '.qg-status-line").textContent.includes("', "))", '已保存"', 2),
    ("span", 'assert.ok(live.speed.includes("', ")", '步/秒"', 1),
    ("span", 'assert.notEqual(live.elapsed, "', ', "elapsed time', '—"', 1),
    ("span", 'assert.notEqual(live.eta, "', ', "ETA', '—"', 1),
    ("span", 'assert.notEqual(done.averageTime, "', ', "average duration', '—"', 1),
    ("literal", ', "步数 8", ', "", "提示词引导 1.5", 1),
    ("literal", ', "提示词引导 1.5", ', "", "参考强度 0.8", 1),
    ("span", 'done.chips.some(chip => chip.includes("', "))", '步/秒"', 1),
    ("span", 'assert.ok(gpuLive.hint.includes("', ', `utilisation', 'GPU 利用率")', 1),
    ("literal", '.qg-workflow-badge").textContent === ', "", "本次运行", 1),
    ("literal", 'assert.equal(gpuLive.badge, ', "", "本次运行", 1),
    ("literal", '.qg-workflow-badge").innerText(), ', "", "下次运行", 1),
    ("span", 'assert.deepEqual(done.actions, [', '], "history actions',
     '"恢复参数", "基于此生成", "对比", "删除", "打开原图"', 1),
    ("span", '{ hasText: "恢', ' }).first()', '复参数"', 1),
    ("span", '{ hasText: "基', " })", '于此生成"', 1),
    ("span", '.qg-prompt-input").fill("', ");", '一只戴帽子的猫"', 1),
    ("span", '.qg-prompt-input").inputValue(), "', ', "restore brings', '一只戴帽子的猫"', 1),
    ("span", '.qg-history-prompt").textContent === "', ',\n', '一只戴帽子的猫"', 1),
    ("span", '.qg-workflow-actions .qg-button", { hasText: "恢', " }).click()", '复默认"', 1),
    ("span", 'historyEntry({ id: "img-shot", prompt: "', ' })];',
     '一只戴帽子的猫站在窗台上，暖色阳光"', 1),
    ("span", 'historyEntry({ id: "img-fail", state: "error", error: "', ', images: []',
     'ComfyUI 未启动或无法连接（http://127.0.0.1:8188），请先启动 ComfyUI 后再生成。"', 1),
    ("span", 'makeJob({ state: "error", phase: "error", error: "', ', preview: null',
     'ComfyUI 未启动或无法连接（http://127.0.0.1:8188），请先启动 ComfyUI 后再生成。"', 1),
    ("span", 'state: "error", error: "ComfyUI 工', ', images: []',
     'ComfyUI 工作流执行失败"', 1),
]


def damaged(chunk: bytes) -> bool:
    """True when this text still hides bytes the ANSI decoder swallowed.

    A hidden pair shows up either as the NUL placeholder or as a byte sequence
    the decoder broke in the middle (``\\xe2\\x80?`` for a lost ``—``).
    """
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


MAX_SPAN = 400


def replace_literal(data: bytes, anchor: str, fixed: str, expected: int) -> tuple[bytes, int]:
    """Replace every quote-delimited literal after an anchor that hides bytes."""
    raw_anchor = anchor.encode("utf-8")
    filled = 0
    cursor = 0
    while True:
        position = data.find(raw_anchor, cursor)
        if position < 0:
            break
        start = position + len(raw_anchor)
        quote = data[start:start + 1]
        end = data.find(quote, start + 1)
        if quote not in (b'"', b"'", b"`") or end < 0:
            raise SystemExit(f"cannot delimit the literal after {anchor!r}")
        # A swallowed quote would let the search run past the line and swallow
        # the following code, so the literal must close on the same line.
        if b"\n" not in data[start:end] and damaged(data[start + 1:end]):
            data = data[:start + 1] + fixed.encode("utf-8") + data[end:]
            filled += 1
        cursor = start + 1
    if not filled:
        print(f"  WARNING: no damaged literal after {anchor!r}")
    return data, filled


def replace_span(data: bytes, start_anchor: str, end_anchor: str, replacement: str,
                 expected: int) -> tuple[bytes, int]:
    """Replace everything between two anchors, quote or no quote."""
    raw_start = start_anchor.encode("utf-8")
    raw_end = end_anchor.encode("utf-8")
    filled = 0
    cursor = 0
    while True:
        position = data.find(raw_start, cursor)
        if position < 0:
            break
        start = position + len(raw_start)
        end = data.find(raw_end, start)
        cursor = start
        # A distant anchor belongs to a different block (another template row
        # shares the same node class), so only a near one is accepted.
        if end < 0 or end - start > MAX_SPAN:
            continue
        if damaged(data[start:end]):
            data = data[:start] + replacement.encode("utf-8") + data[end:]
            filled += 1
        cursor = position + len(raw_start)
    if not filled:
        print(f"  WARNING: nothing damaged between {start_anchor!r} and {end_anchor!r}")
    return data, filled


def main(path: Path) -> int:
    data = path.read_bytes()
    for kind, anchor, end_anchor, text, count in RULES:
        if text is None:
            continue
        if kind == "span":
            data, _ = replace_span(data, anchor, end_anchor, text, count)
        else:
            data, _ = replace_literal(data, anchor, text, count)

    # The one literal the ANSI decoder left unterminated: restore the sentence and
    # the closing quote that the byte pair ate.
    marker = "mtp_enabled: false, mtp_reason: \"".encode("utf-8")
    position = data.find(marker)
    if position < 0:
        raise SystemExit("mtp_reason literal not found")
    start = position + len(marker)
    end = data.find(b", revision: 2 };", start)
    if end < 0:
        raise SystemExit("cannot find the end of the mtp_reason literal")
    data = data[:start] + "生图模式由 ComfyUI 托管 Klein，MTP 关闭。\"".encode("utf-8") + data[end:]

    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        snippet = data[max(0, exc.start - 70):exc.end + 40]
        raise SystemExit(f"still invalid at {exc.start}: {snippet!r}") from None
    if b"\x00" in data:
        raise SystemExit("a NUL placeholder survived")
    path.write_bytes(data)
    print(f"{path.name}: {len(RULES)} rules applied, text is valid UTF-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
