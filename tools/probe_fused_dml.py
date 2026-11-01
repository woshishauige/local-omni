"""Why DirectML refuses the fused predictor graph, and where ONNX parts company
with torch.

Part 1 builds one tiny model per suspicious operator and runs it on DirectML. The
fused graph is the first thing this project asks DML to run that contains TopK,
CumSum, Less, Cast and a few others, so the failing operator is likely one of them.

Part 2 measures the ONNX graph against torch one step at a time. Comparing whole
code sequences conflates cause and effect, because one flipped sample changes every
later input; comparing the logits at step 1, before anything has diverged, is the
only number that says whether the graph itself is right.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper, numpy_helper

FUSED = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom\qwen3_tts_predictor_fused.fp16.onnx")
TEMP = Path(os.environ.get("TEMP", "."))


def salvage(exc: BaseException, limit: int = 260) -> str:
    if isinstance(exc, UnicodeDecodeError):
        for codec in ("gbk", "mbcs", "latin-1"):
            try:
                text = exc.object.decode(codec, errors="replace")
            except Exception:
                continue
            flat = " ".join(text.split())
            return "<%s> %s" % (codec, flat[:limit])
                # noqa: E501 - keep the first readable decoding
    return " ".join(("%s: %s" % (type(exc).__name__, exc)).split())[:limit]


def run_model(nodes, inputs, outputs, initializer, name, feeds, providers):
    graph = helper.make_graph(nodes, name, inputs, outputs, initializer)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10
    path = TEMP / ("dmlop_%s.onnx" % name)
    onnx.save(model, str(path))
    line = []
    for provider in providers:
        options = ort.SessionOptions()
        options.log_severity_level = 4
        try:
            session = ort.InferenceSession(str(path), sess_options=options, providers=[provider])
            result = session.run(None, feeds)[0]
            line.append("%s OK %s" % (provider.split("Execution")[0], np.asarray(result).shape))
        except Exception as exc:  # noqa: BLE001
            line.append("%s FAIL %s" % (provider.split("Execution")[0], salvage(exc, 120)))
    print("%-26s %s" % (name, " | ".join(line)))


def part1():
    print("=== part 1: operator support on DirectML ===")
    providers = ("CPUExecutionProvider", "DmlExecutionProvider")
    f32 = TensorProto.FLOAT
    f16 = TensorProto.FLOAT16

    for dtype, np_dtype, tag in ((f32, np.float32, "f32"), (f16, np.float16, "f16")):
        # TopK
        run_model(
            [helper.make_node("TopK", ["x", "k"], ["v", "i"], axis=-1, largest=1, sorted=1)],
            [helper.make_tensor_value_info("x", dtype, [1, 2048])],
            [helper.make_tensor_value_info("v", dtype, [1, 50]),
             helper.make_tensor_value_info("i", TensorProto.INT64, [1, 50])],
            [numpy_helper.from_array(np.array([50], dtype=np.int64), "k")],
            "topk_" + tag, {"x": np.random.rand(1, 2048).astype(np_dtype)}, providers)

        # CumSum
        run_model(
            [helper.make_node("CumSum", ["x", "axis"], ["y"])],
            [helper.make_tensor_value_info("x", dtype, [1, 2048])],
            [helper.make_tensor_value_info("y", dtype, [1, 2048])],
            [numpy_helper.from_array(np.array(1, dtype=np.int32), "axis")],
            "cumsum_" + tag, {"x": np.random.rand(1, 2048).astype(np_dtype)}, providers)

        # Less -> Cast -> ReduceSum  (the inverse-CDF draw)
        run_model(
            [helper.make_node("Less", ["cdf", "u"], ["m"]),
             helper.make_node("Cast", ["m"], ["mf"], to=TensorProto.FLOAT),
             helper.make_node("ReduceSum", ["mf"], ["n"], keepdims=0)],
            [helper.make_tensor_value_info("cdf", dtype, [1, 2048]),
             helper.make_tensor_value_info("u", dtype, [1, 1])],
            [helper.make_tensor_value_info("n", TensorProto.FLOAT, [1])],
            [], "less_sum_" + tag,
            {"cdf": np.random.rand(1, 2048).astype(np_dtype),
             "u": np.array([[0.5]], dtype=np_dtype)}, providers)

    # int64 Gather with a tensor index, mixing dtypes, as the codec lookup does
    run_model(
        [helper.make_node("Gather", ["table", "idx"], ["y"], axis=0)],
        [helper.make_tensor_value_info("idx", TensorProto.INT64, [1])],
        [helper.make_tensor_value_info("y", f16, [1, 1024])],
        [numpy_helper.from_array(np.random.rand(2048, 1024).astype(np.float16), "table")],
        "gather_table", {"idx": np.array([7], dtype=np.int64)}, providers)

    # fp16 MatMul with a 30720-row output
    run_model(
        [helper.make_node("MatMul", ["x", "w"], ["y"])],
        [helper.make_tensor_value_info("x", f16, [1, 1024])],
        [helper.make_tensor_value_info("y", f16, [1, 30720])],
        [numpy_helper.from_array((np.random.rand(30720, 1024) * 0.01).astype(np.float16), "w")],
        "lm_head_f16", {"x": np.random.rand(1, 1024).astype(np.float16)}, providers)

    # fp16 Slice of a large initializer, as self.codec[step] produces
    run_model(
        [helper.make_node("Slice", ["table", "starts", "ends", "axes"], ["y"])],
        [],
        [helper.make_tensor_value_info("y", f16, [2048, 1024])],
        [numpy_helper.from_array(np.random.rand(16, 2048, 1024).astype(np.float16), "table"),
         numpy_helper.from_array(np.array([3], dtype=np.int64), "starts"),
         numpy_helper.from_array(np.array([4], dtype=np.int64), "ends"),
         numpy_helper.from_array(np.array([0], dtype=np.int64), "axes")],
        "slice_table", {}, providers)


def part2(frames: int = 8):
    print("\n=== part 2: fused ONNX vs torch, step-1 logits only ===")
    sys.path.insert(0, str(Path(__file__).parent))
    import torch
    from fused_predictor import CODEBOOK_SIZE, N_CODEBOOKS, FusedCodePredictor

    model_dir = FUSED.parent
    model = FusedCodePredictor(model_dir).eval().half()
    model.cos = model.cos.float()
    model.sin = model.sin.float()
    model.mask = model.mask.float()

    options = ort.SessionOptions()
    options.log_severity_level = 3
    session = ort.InferenceSession(str(FUSED), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    print("graph inputs :", [(i.name, i.type, i.shape) for i in session.get_inputs()])
    print("graph outputs:", [(o.name, o.type, o.shape) for o in session.get_outputs()])

    rng = np.random.default_rng(11)
    first_step_worst = 0.0
    first_code_match = 0
    full_match = 0
    for _ in range(frames):
        hidden = torch.from_numpy(rng.standard_normal(2048).astype(np.float32)).view(1, -1).half()
        code0 = torch.from_numpy(np.array([rng.integers(0, CODEBOOK_SIZE)], dtype=np.int64))
        uniforms = torch.from_numpy(rng.random(N_CODEBOOKS).astype(np.float32))
        onnx_codes, onnx_logits = session.run(None, {
            "talker_hidden": hidden.numpy(), "code0": code0.numpy(),
            "uniforms": uniforms.numpy()})
        with torch.no_grad():
            torch_codes, torch_logits = model(hidden, code0, uniforms, return_logits=True)
        step1 = slice(0, 2048)
        diff = np.abs(onnx_logits[0][step1].astype(np.float64)
                      - torch_logits[0][step1].float().numpy().astype(np.float64)).max()
        first_step_worst = max(first_step_worst, float(diff))
        first_code_match += int(onnx_codes[0][0] == torch_codes[0][0].item())
        full_match += int((onnx_codes == torch_codes.numpy()).all())
        print("  frame: step1 max|d|=%.4f  code1 onnx=%d torch=%d  full match=%s"
              % (diff, onnx_codes[0][0], torch_codes[0][0].item(),
                 bool((onnx_codes == torch_codes.numpy()).all())))
    print("\nworst step-1 logit difference: %.4f  (step-1 code agreement %d/%d, whole-frame %d/%d)"
          % (first_step_worst, first_code_match, frames, full_match, frames))


if __name__ == "__main__":
    part1()
    part2()
