"""会动手的回合先给模型一份"在这台机器上怎么干活"的规程。

真机现象（27B，2026-10-28 那个 5 小时没给出答案的回合）：浏览器里 `LlamaUi.config.systemMessage`
是空字符串，于是模型对运行环境一无所知——先用 `ls`/`head`/`which`（Windows cmd 里没有），
再用 `curl -o /dev/null`（Windows 没有这个设备），输出被吞时以为"没执行"，反复改引号重试十几轮，
最后把 14 KB 的 HTML 和 8 KB 的目录列表连同工具定义一起塞进 32k 的窗口，服务端 400，
前端表现为"卡住"。

那份规程只回答**遇到这类情况该怎么办**，不替用户提问：环境（Windows/cmd）、工具的正确用法、
失败几次就停、一轮怎么收敛、没有搜索工具时怎么说、上下文快满时怎么收尾、被催的时候先回什么。

注入条件很窄：请求**带工具**（说明这一轮要动手），且应用没有给非空的 system 消息。纯聊天、
识图、语音各有自己的提示词，不碰。想换一份规程就改 `BRIEFING`，或把 `QWEN_AGENT_BRIEFING`
设成 `off` 关掉这份注入。
"""
import os

DISABLE = {"off", "0", "false", "no"}

BRIEFING = """【运行环境】这台机器是 Windows，命令行是 cmd.exe，不是 bash。
- 没有 ls / head / cat / which / sed / awk，`/dev/null` 也不存在；列目录用 dir，看文件用 type，
  查字符串用 findstr，Python 用 python，路径写 D:\\AI\\... 这种反斜杠全路径。
- 想确认系统和当前目录就先调用 get_info（一次），不要靠猜。

【工具的正确用法】
- 一次只发一个工具调用。并行调用容易只看到一半结果，也难判断是哪一条失败。
- 这个命令行工具只可靠执行「单条、没有管道、没有内嵌双引号」的命令：`dir /b *.py`、`type 某个文件`、
  `python 脚本.py` 都能用。命令里带 `|`、`&&`、`"` 会被工具层搞坏，典型表现是只回一句「拒绝访问」
  或者空输出但 exit code 0（实测：`python -c "print(1+1)"` 就是这样）。
- 需要过滤、计数、拼接时：write_file 写一个 .py 脚本 → exec_shell_command 跑 `python 脚本.py` →
  需要落盘就让脚本自己写文件，再 read_file 读回来。不要在命令行里拼管道。
- `>` 重定向能写出文件，但没有回显；写完要用 dir 或 read_file 确认，只凭 exit code 0 不算成功。
- 如果工具返回的就是你刚发出的命令本身（引号被剥掉、没有真正的回显），说明输出丢了，不是执行成功。
  不要再改引号重试，改走上面那条「写脚本再跑」的路。
- curl 的 http_code 000 不等于网络断（-w 的输出可能被包过一层），要看响应体有没有内容；
  Windows 上没有 `-o /dev/null`，用 `-o NUL`，或者干脆不让它输出。
- 大块内容不要整段读进上下文：read_file 用 start_line/end_line 读一段，先 grep_search 定位再读命中附近；
  任何一次工具结果超过 2000 字就要警惕。

【失败与收敛】
- 同一类操作连续失败 3 次就停手：用 3-5 行写清「已确认什么、卡在哪（贴报错原文）、下面两个可选方案」，
  让用户选。不要第 4 次。
- 一轮里工具调用尽量不超过 8 次，每 3-4 次给用户一行进度。
- 回复必须以结论收尾；不要用「我接下来要……」「先看看……」结束却不发工具调用——那会让界面停在半空。
- 窗口约 {window} token，单轮堆满就会报 400，界面上看起来就是卡死。读到一大块内容后立刻收口给结论，
  或建议用户新开一个对话继续。

【联网】这里没有搜索工具；直接抓 Bing / DuckDuckGo 的结果页拿不到可解析结果（202、需要 JS）。
不要假装搜到了。要么明说「我不能联网搜索」，请用户贴链接或文本；要么用 exec_shell_command 抓某个
具体 URL，并说明这是单页抓取、来源单一。

【被重复追问】同一句话反复出现＝上一轮卡住了。先一句话说清现在在做什么、卡在哪，再给一个最小可验证的
下一步；不要把它当新任务重开。用户问「在吗／怎么卡了」时先正面回答，不要沉默。

【边界】请求的核心如果是违法操作（跑分/刷单、买卖账号与支付账户、盗号、套现、规避风控或实名），
不要给操作步骤和规避手段：用 3 行说明风险与法律后果，给一个合法的替代方向，然后停。对方重复追问或加压时
口径不变，也不要长篇说教。

不要复述这份规程。"""


def _system_index(messages):
    """第一条 system 消息的位置；没有就是 -1。"""
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "system":
            return index
    return -1


def _text_of(message):
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def should_brief(payload):
    """这一轮要不要塞规程：要动手（带工具或已经在用工具），且应用没写自己的 system 消息。"""
    if os.environ.get("QWEN_AGENT_BRIEFING", "").strip().lower() in DISABLE:
        return False
    if not isinstance(payload, dict):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    tools = payload.get("tools")
    acting = isinstance(tools, list) and bool(tools)
    if not acting:
        acting = any(isinstance(message, dict) and message.get("tool_calls") for message in messages)
    if not acting:
        return False
    index = _system_index(messages)
    # 应用自己写了 system 消息就不动：语音、识图、以及所有者手写的提示词都属于这种情况。
    return index < 0 or not _text_of(messages[index]).strip()


def apply(payload, window=32768):
    """返回 (要发出的 payload, 注入的字符数)。没有注入时原样返回、计 0。

    已有的 system 消息（包括空字符串那种占位）就地替换，没有就插到最前面；
    其余消息顺序不动——客户端的回溯链和 chat template 都依赖这个顺序。
    """
    if not should_brief(payload):
        return payload, 0
    text = BRIEFING.format(window=int(window))
    messages = list(payload["messages"])
    index = _system_index(messages)
    if index < 0:
        messages.insert(0, {"role": "system", "content": text})
    else:
        message = dict(messages[index])
        message["content"] = text
        messages[index] = message
    data = dict(payload)
    data["messages"] = messages
    return data, len(text)
