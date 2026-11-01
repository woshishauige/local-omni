"""Exercise real mode switching, streaming cancellation, image input and long prompts."""

import argparse
import asyncio
import base64
import json
from pathlib import Path
import time

import httpx

from qwen_config import read_settings


async def main(long_context):
    results = {}
    settings = read_settings()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8081", trust_env=False,
                                 timeout=httpx.Timeout(settings["server_timeout_seconds"], connect=5)) as client:
        async def mode(name):
            print(f"Loading mode: {name}", flush=True)
            response = await client.post("/local/mode", json={"mode": name})
            response.raise_for_status()
            started = time.monotonic()
            while time.monotonic() - started < 180:
                status = (await client.get("/local/status")).json()
                if status["state"] == "error":
                    raise RuntimeError(status["error"])
                if status["state"] == "ready" and status["mode"] == name:
                    props = (await client.get("/props")).json()
                    assert bool(props["modalities"]["vision"]) == (name == "vision")
                    assert status["mtp_enabled"] == (name == "text" and settings["mtp_enabled"])
                    assert props["default_generation_settings"]["n_ctx"] == settings["context_size"]
                    print(f"Ready: {name}, context={status['context_size']}, MTP={status['mtp_enabled']}", flush=True)
                    return status
                await asyncio.sleep(0.5)
            raise TimeoutError("Mode switch timed out")

        results["text"] = await mode("text")
        request = {"messages": [{"role": "user", "content": "Describe a detailed plan for a short product video."}],
                   "max_tokens": 128, "temperature": 0.7, "seed": 1234, "stream": True}
        async with client.stream("POST", "/v1/chat/completions", json=request) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    packet = json.loads(line[6:])
                    if any(choice.get("delta", {}).get("content") for choice in packet.get("choices", [])):
                        busy = await client.post("/local/mode", json={"mode": "vision"})
                        assert busy.status_code == 409, busy.text
                        results["streaming_switch_guard"] = True
                        break
        for _ in range(40):
            if (await client.get("/local/status")).json()["active_requests"] == 0:
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("Cancellation left the model busy")
        results["streaming_cancellation"] = True

        picture = base64.b64encode(Path("D:/AI/deployment/qwen-vision-test.png").read_bytes()).decode("ascii")
        vision_request = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "请用中文描述图形的颜色、形状和左右位置。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + picture}},
        ]}], "max_tokens": 128, "temperature": 0, "seed": 1234, "cache_prompt": False}
        rejected = await client.post("/v1/chat/completions", json=vision_request)
        assert rejected.status_code == 400
        results["text_image_rejection"] = True
        results["vision"] = await mode("vision")
        response = await client.post("/v1/chat/completions", json=vision_request)
        response.raise_for_status()
        results["image_response"] = response.json()
        answer = results["image_response"]["choices"][0]["message"]["content"]
        assert all(word in answer for word in ("红", "蓝", "左", "右")), answer
        results["text_return"] = await mode("text")

        if long_context:
            records = "\n".join(f"Record {i}: light=soft; camera=steady; audio=clear." for i in range(700))
            prompt = "Reference code: 592741.\n" + records + "\nReturn only the reference code from the first line."
            formatted = (await client.post("/apply-template", json={"messages": [{"role": "user", "content": prompt}],
                                                                    "add_generation_prompt": True})).json()["prompt"]
            tokens = (await client.post("/tokenize", json={"content": formatted})).json()["tokens"]
            assert 8192 < len(tokens) < read_settings()["context_size"] - 256, len(tokens)
            print(f"Long-context input: {len(tokens)} tokens", flush=True)
            response = await client.post("/completion", json={"prompt": formatted, "n_predict": 64,
                                                               "temperature": 0, "seed": 1234, "cache_prompt": False})
            response.raise_for_status()
            results["long_context"] = response.json()
            results["long_context"].pop("prompt", None)
            results["long_context_input_tokens"] = len(tokens)
            assert "592741" in results["long_context"]["content"]
    print(json.dumps(results, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--long-context", action="store_true")
    asyncio.run(main(parser.parse_args().long_context))
