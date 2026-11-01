"""装不下的请求不要发给上游：它只会回一句英文 400，界面上看不见。

真机证据（2026-10-28「Fast Easy Hidden Money Earning Methods」）：一轮里 32 次工具调用，
最后一次请求 37,679 token，上游回

    request (37679 tokens) exceeds the available context size (32768 tokens)

网关原样转发这句英文的 400 JSON。界面是流式请求，等的是 SSE；一个 400 JSON 既不进气泡、
也不报错，用户看到的就是"卡了"——他连打 20 次「怎么卡了」都没有任何反馈。

压缩器救不了这种请求：它按设计只压**较早的回合**，而这里是一个回合自己长到超过窗口
（一轮里读了 14 KB 的网页和 8 KB 的目录列表）。所以这里只做一件事：请求发出前自己量一遍，
量到装不下就**用一条普通助手回复说清楚**——这次没发给模型、为什么、接下来怎么办。

为什么不是错误帧：界面的 SSE 解析器只读 `data:` 行、只取 `choices[0].delta`，`event: error`
整行被忽略，error 对象会被丢掉，用户仍然什么都看不到（前面那条"本地模型连接中断"的错误帧
也是靠阅读器直接抛异常才显形，聊天流里并不渲染）。所以流式这一路按真模型第一帧的形状发一段
助手消息；非流式仍然回 400 JSON（给脚本/API 用）。

量不准就不拦（`None`）：宁可让上游去回它的 400，也不要因为计数失败而拒掉一个本来能跑的请求。
"""
RESERVE_TOKENS = 1024   # 给生成留的余量：prompt + 输出都要装进同一个窗口
COUNT_ABOVE_BYTES = 12000  # 小请求不量：一次 tokenize 也要几十毫秒，别加到每一轮上

MESSAGE = (
    "这一轮已经涨到 {count} token，超过本机模型这次的窗口（{window} token），"
    "所以**这次请求没有发给模型**——直接发出去也只会拿回一句英文 400，界面上看起来就是卡住。\n\n"
    "接下来可以这样办：\n"
    "- 新开一个对话继续这件事：这个对话里的大段工具输出会一直占着窗口，删不掉。\n"
    "- 或者把这次任务拆小：一次只读一个文件/只查一个目录，"
    "需要汇总时先用 write_file 把中间结果落成文件，只把结论读回来。\n"
    "- 需要更大的窗口时由机主提高模型的上下文（`context_size`），不是靠重试。"
)


def budget(window, reserve=RESERVE_TOKENS):
    """这个窗口真正能收下的 prompt 长度。"""
    return max(1024, int(window) - int(reserve))


def overflows(count, window, reserve=RESERVE_TOKENS):
    """量出来的 token 数是否已经装不下。count 为 None（量不了）时一律不拦。"""
    if count is None:
        return False
    return int(count) > budget(window, reserve)


def message(count, window):
    return MESSAGE.format(count=int(count), window=int(window))


def reply_frame(text):
    """按界面的流式协议回一段"助手消息"。

    形状照着真模型的输出抄（`data: {"choices":[{"finish_reason":null,"delta":{"role":"assistant",…`），
    收尾补一个 stop 帧和 [DONE]——界面只读 `choices[0].delta`，别的形状一律不显示。
    """
    import json
    import time

    created = int(time.time())
    head = {"created": created, "id": "chatcmpl-context-guard", "model": "local",
            "system_fingerprint": "context-guard", "object": "chat.completion.chunk"}
    first = dict(head, choices=[{"finish_reason": None, "index": 0,
                                 "delta": {"role": "assistant", "content": text}}])
    last = dict(head, choices=[{"finish_reason": "stop", "index": 0, "delta": {}}])
    return ("\n\ndata: " + json.dumps(first, ensure_ascii=False)
            + "\n\ndata: " + json.dumps(last, ensure_ascii=False)
            + "\n\ndata: [DONE]\n\n").encode("utf-8")


def json_error(text):
    return {"error": {"message": text, "type": "context_overflow_error"}}


# 兼容旧调用名：mode_server.py 现在是一个临时加载器，跑的是 2026-10-28 22:10 编译好的字节码
# （源码在那之后被编码事故写坏，见 mode_server.py 开头）。那份字节码里调用的还是 `sse_frame`。
sse_frame = reply_frame
