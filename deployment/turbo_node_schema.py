"""Print the input schema of the nodes the Turbo graph needs."""
import json

import httpx

NODES = ["TextEncodeQwenImage21", "ManualSigmas", "CFGGuider", "SamplerCustomAdvanced",
         "RandomNoise", "KSamplerSelect", "UnetLoaderGGUF", "CLIPLoader", "VAELoader",
         "EmptyLatentImage", "VAEDecode", "SaveImage", "CLIPTextEncode"]


def simple(definition):
    first = definition[0] if isinstance(definition, list) and definition else definition
    if isinstance(first, list):
        return first[:6]
    return first


with httpx.Client(base_url="http://127.0.0.1:8188", timeout=60) as client:
    for node in NODES:
        try:
            data = client.get(f"/object_info/{node}").json()
        except Exception as exc:  # noqa: BLE001
            print(f"{node}: FAILED {exc}")
            continue
        if not data:
            print(f"{node}: MISSING")
            continue
        spec = data[node]
        required = {key: simple(value) for key, value in spec.get("input", {}).get("required", {}).items()}
        optional = {key: simple(value) for key, value in spec.get("input", {}).get("optional", {}).items()}
        print(f"{node}:")
        print(f"  required: {required}")
        if optional:
            print(f"  optional: {optional}")
        print(f"  outputs: {spec.get('output')} names={spec.get('output_name')}")
