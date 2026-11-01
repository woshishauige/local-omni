"""Check that the SeedVR2 graph builder emits the wiring the model requires."""

import image_comfy as ic

built = ic.build_graph(
    "seedvr2", prompt="", references=["seedvr2test/face-test.png"],
    steps=3, cfg=1.0, filename_prefix="qwen/upscaled",
    overrides={"3": {"resize_type.multiplier": 1.25}},
)
g = built["prompt"]
print("parameters:", built["parameters"])
print()
for nid in sorted(g, key=int):
    n = g[nid]
    links = {k: v for k, v in n["inputs"].items() if isinstance(v, list)}
    vals = {k: v for k, v in n["inputs"].items() if not isinstance(v, list)}
    print(f"  {nid:>3} {n['class_type']:<24} links={links}")
    print(f"      values={vals}")

print(f"\ntotal nodes: {len(g)}")
print("\n--- the two invariants the model enforces ---")
print("  conditioning reads node 6 :", g["8"]["inputs"]["vae_conditioning"] == ["6", 0])
print("  sampler reads node 6      :", g["10"]["inputs"]["latent_image"] == ["6", 0])
print("  scale reaches the resize  :", g["3"]["inputs"]["resize_type.multiplier"])
print("  steps reach the sampler   :", g["10"]["inputs"]["steps"])

print("\n--- guard rails ---")
for label, kwargs in (
    ("no reference", {"references": None}),
    ("empty prompt is fine", {"references": ["x.png"], "prompt": ""}),
):
    try:
        ic.build_graph("seedvr2", prompt=kwargs.get("prompt", ""),
                       references=kwargs["references"], steps=2, overrides={})
    except ValueError as exc:
        print(f"  {label:<22} -> ValueError: {exc}")
    else:
        print(f"  {label:<22} -> built OK")

# a prompt-less generation pipeline must still be refused
try:
    ic.build_graph("sdxl", prompt="   ", references=None, steps=8, overrides={})
except ValueError as exc:
    print(f"  {'sdxl empty prompt':<22} -> ValueError: {exc}")
