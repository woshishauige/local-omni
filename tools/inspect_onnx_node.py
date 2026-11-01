"""Locate and describe the ONNX nodes DirectML refuses to run.

DirectML threw inside 'node_Reshape_1561' of the TTS decoder. A Reshape that runs
fine on CPU but fails on DML is usually a shape that is only known at run time, or
a reshape whose target comes from a computed tensor instead of an initializer.
This prints the neighbourhood of that node so the graph can be judged directly.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import onnx

TARGETS = sys.argv[1:] or ["node_Reshape_1561"]
MODEL = Path(r"D:\AI\Qwen-Local\tools\Qwen3-TTS-GGUF\model-custom\qwen3_tts_decoder.fp16.onnx")


def main():
    graph = onnx.load(str(MODEL), load_external_data=False).graph

    op_counts = Counter(node.op_type for node in graph.node)
    print("nodes:", len(graph.node), "| initializers:", len(graph.initializer))
    print("op histogram:", dict(op_counts.most_common()))

    producer = {}
    for node in graph.node:
        for out in node.output:
            producer[out] = node

    init_names = {t.name for t in graph.initializer}
    value_info = {v.name: v for v in list(graph.value_info) + list(graph.input) + list(graph.output)}

    for target in TARGETS:
        print("\n=== %s ===" % target)
        node = next((n for n in graph.node if n.name == target), None)
        if node is None:
            print("  not found")
            continue
        print("  op_type :", node.op_type)
        print("  inputs  :", list(node.input))
        print("  outputs :", list(node.output))
        for attr in node.attribute:
            if attr.type == onnx.AttributeProto.INT:
                print("  attr %s = %d" % (attr.name, attr.i))
            elif attr.type == onnx.AttributeProto.FLOATS:
                print("  attr %s = %s" % (attr.name, list(attr.floats)))
            elif attr.type == onnx.AttributeProto.INTS:
                print("  attr %s = %s" % (attr.name, list(attr.ints)))

        for idx, name in enumerate(node.input):
            src = producer.get(name)
            dtype = ""
            if name in init_names:
                tensor = next(t for t in graph.initializer if t.name == name)
                dims = [d for d in tensor.dims]
                dtype = "initializer dims=%s" % dims
            elif name in value_info:
                info = value_info[name]
                dims = [d.dim_value if d.HasField("dim_value") else (d.dim_param or "?")
                        for d in info.type.tensor_type.shape.dim]
                dtype = "value_info dims=%s" % dims
            origin = "PRODUCED BY %s" % src.op_type if src else "graph input/initializer"
            print("    in[%d] %-28s %-16s <- %s" % (idx, name, dtype, origin))

        # Walk a couple of hops upstream to show the shape provenance.
        for hop in (1, 2):
            name = node.input[1] if len(node.input) > 1 else None
            for _ in range(hop):
                if not name:
                    break
                up = producer.get(name)
                if not up:
                    break
                print("  upstream(%d): %s inputs=%s" % (hop, up.op_type, list(up.input)))
                name = up.input[0] if up.input else None


if __name__ == "__main__":
    main()
