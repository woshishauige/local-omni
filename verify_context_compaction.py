"""Exercise real local compaction with an isolated usage DB and the running model.

No model process is started/stopped. Temporary DBs are removed on exit; only the
chosen JSON validation report survives. This intentionally takes several minutes.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time

import httpx


async def main(report_path):
    import mode_server as gateway
    from context_compaction import ContextCompactor
    from usage_store import UsageStore

    report = {"started_at": time.time(), "checks": [], "measurements": {}}
    def save():
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    def checked(name, **values):
        report["checks"].append({"name": name, **values})
        save()
        print(json.dumps(report["checks"][-1], ensure_ascii=False), flush=True)

    async with httpx.AsyncClient(timeout=3600, trust_env=False) as model:
        status = (await model.get("http://127.0.0.1:8081/local/status")).json()
        if status.get("state") != "ready" or status.get("active_requests"):
            raise RuntimeError("The existing model must be ready and idle.")
        report["runtime"] = status
        with tempfile.TemporaryDirectory(prefix="qwen-context-live-") as directory:
            gateway.manager.state = "ready"
            gateway.manager.mode = "text"
            gateway.manager.client = model
            gateway.usage_store = UsageStore(Path(directory) / "usage.sqlite3")
            gateway.context_compactor = ContextCompactor(gateway.UPSTREAM, lambda: gateway.usage_store)
            ctx = status["context_size"]
            gateway.manager.settings["context_size"] = ctx
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app),
                                         base_url="http://127.0.0.1:8081", timeout=3600) as client:
                facts = [
                    "项目代号必须逐字保留：AURORA-7319。",
                    "旧端口先暂定8124，等待后面最终确认。",
                    "唯一工作目录为 D:\\AI\\Qwen-Local，禁止删除 models 目录。",
                    "我纠正之前的端口：最终端口是9137，8124已撤销。",
                    "任务目标：验证长对话压缩后仍能继续工作。",
                    "此处仅为重复的已归档进度，不添加新的项目决定。",
                    "最近确认：必须使用中文回答。",
                    "最近确认：最终检查标记为 TAIL-2846。",
                ]
                filler = "Archived progress: the completed checks found no new decisions. "
                def payload(repeats):
                    messages = [{"role": "system", "content": "你是严谨的本地助手。依据对话中最后确认的事实作答，不编造。"}]
                    for index, fact in enumerate(facts):
                        messages.extend([
                            {"role": "user", "content": fact + "\n" + (filler * repeats if index < 6 else "")},
                            {"role": "assistant", "content": "已记录本轮事实与约束，归档进度不影响已确认决定。"},
                        ])
                    messages.append({"role": "user", "content": "请只输出JSON，字段code、port、directory、protected、tail。分别填写项目代号、最终端口、工作目录、禁止删除的目录名称、最近最终检查标记。"})
                    return {"messages": messages, "model": "qwen3.8-27b-local", "max_tokens": 160,
                            "temperature": 0, "stream": True}
                sample = payload(100)
                baseline = await gateway.context_compactor._count(payload(0), model)
                sample_tokens = await gateway.context_compactor._count(sample, model)
                repeats = int((ctx * .78 - baseline) / (sample_tokens - baseline) * 100) + 1
                body = payload(repeats)
                count = await gateway.context_compactor._count(body, model)
                assert ctx * .75 < count < ctx - 4096, count
                checked("real tokenizer calibrated history above automatic threshold", tokens=count, context=ctx)

                async def invoke(data, name, force=None):
                    headers = {"X-Conversation-Id": "local-context-validation"}
                    if force:
                        headers["X-Qwen-Context-Compression"] = force
                    started = time.monotonic()
                    response = await client.post("/v1/chat/completions", json=data, headers=headers)
                    elapsed = time.monotonic() - started
                    if response.status_code != 200:
                        raise AssertionError((name, response.status_code, response.text[:500]))
                    events = [json.loads(line[6:]) for line in response.text.splitlines()
                              if line.startswith("data: {")]
                    text = "".join(choice.get("delta", {}).get("content") or ""
                                   for event in events for choice in event.get("choices", []))
                    assert "[DONE]" in response.text
                    usage = next(event["usage"] for event in reversed(events) if event.get("usage"))
                    state = gateway.context_compactor.get_status("local-context-validation")
                    value = {"seconds": round(elapsed, 3), "usage": usage, "context": state,
                             "answer": text, "http_status": response.status_code}
                    report["measurements"][name] = value
                    save()
                    return value, events

                first, _ = await invoke(body, "first_auto_compaction")
                assert first["context"]["state"] == "compressed", first["context"]
                assert first["context"]["saved_tokens"] > count * .4
                assert abs(first["usage"]["prompt_tokens"] - first["context"]["prompt_tokens"]) <= 4, first
                for fact in ("AURORA-7319", "9137", "Qwen-Local", "models", "TAIL-2846"):
                    assert fact in first["answer"], (fact, first["answer"])
                assert "8124" not in first["answer"]
                checked("real automatic compression, SSE usage, old facts, correction and recent facts preserved",
                        original_tokens=count, effective_tokens=first["context"]["prompt_tokens"], seconds=first["seconds"])

                second_body = dict(body, messages=body["messages"] + [
                    {"role": "assistant", "content": first["answer"]},
                    {"role": "user", "content": "复核一次，只输出相同的五个JSON字段。"},
                ])
                second, _ = await invoke(second_body, "cached_followup")
                assert second["context"]["state"] == "cached", second["context"]
                assert second["context"]["compression_calls"] == 0
                for fact in ("AURORA-7319", "9137", "Qwen-Local", "models", "TAIL-2846"):
                    assert fact in second["answer"], (fact, second["answer"])
                checked("next real turn reuses exact history summary without new summary inference", seconds=second["seconds"])

                # A real, inert tool call verifies Agent continuation; no tool or
                # external action is executed by this harness.
                agent_body = dict(body, messages=body["messages"][:-1] + [{"role": "user", "content":
                    "立即调用record_check工具，code传AURORA-7319，port传最终确认的整数端口9137。"}],
                    tools=[{"type": "function", "function": {"name": "record_check", "description": "Record a local test result.",
                        "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "port": {"type": "integer"}},
                                       "required": ["code", "port"]}}}], tool_choice="required")
                # Force on a compact fixture avoids a second full 38K prefill;
                # the production threshold was already exercised above.
                agent_body["messages"] = payload(12)["messages"][:-1] + agent_body["messages"][-1:]
                agent_body["messages"][1]["content"] += filler * 150
                agent, events = await invoke(agent_body, "agent_after_compaction", "force")
                assert agent["context"]["state"] == "compressed", agent["context"]
                names, arguments, identifier = "", "", ""
                for event in events:
                    for choice in event.get("choices", []):
                        for call in choice.get("delta", {}).get("tool_calls", []):
                            names += call.get("function", {}).get("name", "")
                            arguments += call.get("function", {}).get("arguments", "")
                            identifier += call.get("id", "")
                assert names == "record_check", names
                args = json.loads(arguments)
                assert args == {"code": "AURORA-7319", "port": 9137}, args
                checked("real Agent tool decision after compression", function=names, arguments=args)

                tool_followup = dict(agent_body, tool_choice="auto", messages=agent_body["messages"] + [
                    {"role": "assistant", "content": None, "tool_calls": [{"id": identifier, "type": "function",
                         "function": {"name": names, "arguments": arguments}}]},
                    {"role": "tool", "tool_call_id": identifier, "content": '{"ok":true,"receipt":"RECEIPT-6293"}'},
                    {"role": "user", "content": "工具测试已完成，不要再次调用工具。请仅回复工具返回的receipt。"},
                ])
                receipt, _ = await invoke(tool_followup, "agent_tool_result", "off")
                assert "RECEIPT-6293" in receipt["answer"]
                checked("real completed tool chain continues correctly", answer=receipt["answer"])

                rows = gateway.usage_store.query({"page_size": "100"})["rows"]
                summaries = [row for row in rows if row["endpoint"] == "/local/context/compact"]
                assert len(summaries) >= 2 and all(row["status"] == "success" for row in summaries), summaries
                assert all(row["input_tokens"] > 0 and row["output_tokens"] > 0 for row in summaries)
                assert gateway.manager.active_requests == 0 and not gateway.pending_context_streams
                report["summary_usage"] = [{key: row.get(key) for key in
                    ("source", "input_tokens", "output_tokens", "duration_ms", "status")} for row in summaries]
                checked("summary token costs separately recorded and requests released")
                report["passed"] = True
                report["finished_at"] = time.time()
                save()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
