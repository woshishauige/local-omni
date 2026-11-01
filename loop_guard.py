"""循环防护：把"无限重试"改成"打断一次 → 反思 → 换一条可行方案继续"。

真机现象（9B 与 27B 都有）：碰到"查文档"或一条报错的命令，模型会**无限循环**——反复用同样的
参数调用同一个工具，既不退出，也不分析失败原因。打包界面里没有任何步数上限
（实测 `bundle.zh-CN.js` 中 `max_steps`/`maxSteps` 出现 **0** 次），所以循环没有刹车。

**只摘工具是不够的**（所有者指出）：那等于把这一轮直接废掉，模型只能解释，没法接着干活。
所以处置是**分级**的，而且每一级都把"换一条可行方案继续"写进要求里：

1. **第 1 级**（判定打转：同一调用重复 ≥3 次且结果相同/都失败；或最近 6 次同一工具全失败）
   → **工具照给**，只追加指令：说清失败原因 + 给出**另一条可行方案**（换工具、改参数、
   或先做哪一步检查）+ **立刻按新方案继续执行**。
2. **第 2 级**（标记之后又出现同一个被拦下的调用）→ 把那**一个**工具从 `tools` 里摘掉，
   其余照给，要求换工具或换目标继续。
3. **第 3 级**（又被拦下）→ 摘掉全部工具，要求文字说明根因、已排除哪些方案、下一步具体怎么做。
4. **超预算**（一轮内工具调用 ≥24 次）→ 直接要文字总结 + 下一步可行方案（这一档是硬停）。

**自愈**：判据只看**最近一条防护指令之后**的调用。老历史里那几次重复不再反复触发，所以
中断一次之后工具会自己回来，模型可以接着用别的方式干——这在旧版是真 bug：一旦判过一次，
那段历史在整段对话里都会让每一轮都被摘工具（等于永久致哑）。

**无状态**：拦下的目标与级别写在追加的那条 user 消息里（`block=工具#签名 level=N reason=…`），
客户端回传历史时它自然回到网关，重启也不丢；不需要任何服务端记忆。
"""
import hashlib
import json
import re

REPEAT_LIMIT = 3          # 同一 (工具名 + 参数) 重复到这个次数即判循环
SAME_TOOL_LIMIT = 6       # 连续这么多次都打同一个工具且全部失败，也判循环
STEP_BUDGET = 24          # 一轮对话里工具调用的总上限（界面里没有上限，网关来兜底）
RESULT_SAMPLE = 200       # 指令里引用失败信息的长度上限
LEVEL_LIMIT = 3           # 处置级别上限：到这一级才摘掉全部工具
STEP_BUDGET_LEVEL = LEVEL_LIMIT   # 超预算等同于最高一级：这一轮必须交给文字
GUARD_MARK = "【循环防护】"  # 追加指令的开头，用来认出"这条 user 消息是我们自己塞的"

FAILURE_MARKERS = ("error", "exception", "traceback", "not found", "no such file", "denied",
                   "refused", "timed out", "timeout", "unavailable", "cannot ", "failed",
                   "失败", "错误", "异常", "找不到", "不存在", "无法", "拒绝", "超时", "未找到",
                   "未启用", "不可用", "中断", "[exit code", "exit code:")
FAILURE_RE = re.compile("|".join(re.escape(marker) for marker in FAILURE_MARKERS), re.IGNORECASE)

# 指令末尾的机器可读标记：拦的是什么、已经拦到第几级、为什么。
BLOCK_RE = re.compile(r"block=([^\s#）)]+)(?:#([0-9a-f]{8}))?\s+level=(\d+)\s+reason=([a-z-]+)")

REFLECT_INSTRUCTION = (
    GUARD_MARK + "你刚才连续 {count} 次执行了同一个操作（{blocked}），每一次都得到同样的结果{failure}。"
    "**不要再发起这次调用**，请立刻换一条**可行方案**把这个任务做完："
    "① 先说清它为什么失败（结合上面的报错原文）；"
    "② 再说清新方案具体怎么做——换成哪个工具、参数怎么改、或者先做哪一步检查，"
    "以及为什么这次会不一样；"
    "③ 然后就**按新方案继续执行**，不要停在解释上。确实无路可走时，才说明“没有可行方案”"
    "以及需要我做什么。\n{marker}"
)
FILTER_INSTRUCTION = (
    GUARD_MARK + "你又一次发起了刚刚被判定不可行的调用（{blocked}，已经拦下 {count} 次）。"
    "这次我已经把工具 `{tool}` 从可用列表里移除了。请改用别的工具、或换一个目标继续，"
    "并说明新方案为什么可行——同一条路不要再试。\n{marker}"
)
HARD_INSTRUCTION = (
    GUARD_MARK + "同一处调用已经拦下两次（{blocked}），这次不能再给你任何工具。请用文字回答："
    "① 失败的根本原因；② 你已经排除掉哪些方案、为什么都不行；"
    "③ 下一步的具体方案（换成哪个工具、哪条命令，或需要我开什么权限）——我确认后再继续。\n{marker}"
)
BUDGET_INSTRUCTION = (
    GUARD_MARK + "本轮你已经调用了 {count} 次工具（最多 {budget} 次），其中打得最多的是 `{tool}`。"
    "请停下来用文字总结：① 目前已经确认的进展；② 卡在哪里、报错原文是什么；"
    "③ 下一步的可行方案（具体到工具与参数）。\n{marker}"
)


def _canonical(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _content_text(message) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content
                 if isinstance(part, dict) and isinstance(part.get("text"), str)]
        return "\n".join(parts)
    return ""


def _looks_failed(text: str) -> bool:
    return bool(text) and bool(FAILURE_RE.search(text))


def _signature(tool: str, args) -> str:
    """一次调用（工具 + 参数）的短指纹：标记里用它认出"就是这一次"。"""
    return hashlib.sha1(f"{tool}\n{_canonical(args)}".encode("utf-8")).hexdigest()[:8]


def _label(tool: str, args) -> str:
    # 参数本身就是客户端的原始文本（JSON 字符串），直接用它，别再套一层引号。
    text = (args if isinstance(args, str) else _canonical(args)).strip()
    if text in ("", "{}", "null", "None"):
        return f"工具 `{tool}`"
    if len(text) > 120:
        text = text[:117] + "…"
    return f"工具 `{tool}`，参数 {text}"


def note_marker(tool: str, signature: str = "", level: int = 1, reason: str = "") -> str:
    blocked = f"{tool}#{signature}" if signature else tool
    return f"（防护标记 block={blocked} level={level} reason={reason}）"


def collect(messages) -> list[dict]:
    """按顺序取出每一次工具调用：工具名、参数、以及它的结果文本。"""
    calls, results, index = [], {}, 0
    for position, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = function.get("name") if isinstance(function, dict) else None
                if not isinstance(name, str) or not name:
                    continue
                index += 1
                calls.append({
                    "index": index,
                    "at": position,
                    "id": call.get("id") if isinstance(call.get("id"), str) else "",
                    "tool": name,
                    "args": function.get("arguments") if isinstance(function, dict) else "",
                })
        elif role == "tool":
            identifier = message.get("tool_call_id")
            if isinstance(identifier, str) and identifier:
                results[identifier] = _content_text(message)
    for call in calls:
        call["result"] = results.get(call["id"], "")
        call["failed"] = _looks_failed(call["result"])
        call["signature"] = _signature(call["tool"], call["args"])
    return calls


def last_note(messages) -> dict | None:
    """最近一条防护指令（我们自己追加的那条 user 消息），含它拦下的目标与级别。"""
    messages = messages or []
    for position in range(len(messages) - 1, -1, -1):
        message = messages[position]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _content_text(message)
        if not text.startswith(GUARD_MARK):
            continue
        found = BLOCK_RE.search(text)
        return {
            "at": position,
            "tool": found.group(1) if found else "",
            "signature": (found.group(2) or "") if found else "",
            "level": int(found.group(3)) if found else 1,
            "reason": (found.group(4) or "") if found else "",
        }
    return None


def _hit(verdict, *, reason, tool, count, failure, level, blocked, signature="") -> dict:
    verdict.update(loop=True, reason=reason, tool=tool, count=count, failure=failure,
                   level=level, blocked=blocked, signature=signature)
    return verdict


def inspect(messages) -> dict:
    """判定这次请求是不是"在打转"（或又走了那条被拦下的路）。返回的字典同时用于日志与响应头。"""
    calls = collect(messages)
    verdict = {"loop": False, "reason": "", "tool": "", "count": 0, "failure": "",
               "calls": len(calls), "level": 1, "blocked": "", "signature": ""}
    if not calls:
        return verdict

    note = last_note(messages)
    window = [call for call in calls if note is None or call["at"] > note["at"]]

    # 复发：标记之后又走了同一条被拦下的路（同一签名再来一次，或那个工具继续失败）→ 升级处置。
    if note and note["tool"] and note["level"] < LEVEL_LIMIT and window:
        if note["signature"]:
            repeated = [call for call in window
                        if call["tool"] == note["tool"] and call["signature"] == note["signature"]]
            needed = 1
        else:
            repeated = [call for call in window
                        if call["tool"] == note["tool"] and call["failed"]]
            needed = REPEAT_LIMIT
        if len(repeated) >= needed:
            last = repeated[-1]
            return _hit(verdict, reason="repeat-blocked", tool=last["tool"], count=len(repeated),
                        failure=last["result"][:RESULT_SAMPLE], level=note["level"] + 1,
                        blocked=_label(last["tool"], last["args"]), signature=last["signature"])

    # 判据一：同一次调用原地打转（参数完全相同，结果相同或都失败）。只看标记之后的窗口。
    groups: dict[tuple, list[dict]] = {}
    for call in window:
        groups.setdefault((call["tool"], call["signature"]), []).append(call)
    for (tool, signature), occurrences in groups.items():
        if len(occurrences) < REPEAT_LIMIT:
            continue
        results = [occurrence["result"] for occurrence in occurrences]
        stuck = results[-1] == results[-2] or (occurrences[-1]["failed"] and occurrences[-2]["failed"])
        if stuck:
            return _hit(verdict, reason="repeat-identical", tool=tool, count=len(occurrences),
                        failure=results[-1][:RESULT_SAMPLE], level=1,
                        blocked=_label(tool, occurrences[-1]["args"]), signature=signature)

    # 判据二：连着很多次都打同一个工具且全部失败（换参数试探也算）。
    tail = window[-SAME_TOOL_LIMIT:]
    if len(tail) >= SAME_TOOL_LIMIT and len({call["tool"] for call in tail}) == 1 \
            and all(call["failed"] for call in tail):
        return _hit(verdict, reason="same-tool-failing", tool=tail[-1]["tool"], count=len(tail),
                    failure=tail[-1]["result"][:RESULT_SAMPLE], level=1,
                    blocked=_label(tail[-1]["tool"], tail[-1]["args"]))

    # 判据三：本轮工具调用次数超出预算（打包界面没有任何步数上限，这里是最后一道刹车）。
    # 本条指令自己也是一条 user 消息（客户端会照原样回传），它不算新的一轮：否则
    # "提醒一次 → 计数清零 → 再放 24 次"就成了绕过刹车的路。所有者真的再问一句才算新轮。
    turn_start = 0
    for position, message in enumerate(messages or []):
        if isinstance(message, dict) and message.get("role") == "user" \
                and not _content_text(message).startswith(GUARD_MARK):
            turn_start = position
    turn_calls = [call for call in calls if call["at"] > turn_start]
    if len(turn_calls) >= STEP_BUDGET:
        counts: dict[str, int] = {}
        for call in turn_calls:
            counts[call["tool"]] = counts.get(call["tool"], 0) + 1
        busiest = max(counts.items(), key=lambda item: item[1])[0]
        return _hit(verdict, reason="step-budget", tool=busiest, count=len(turn_calls),
                    failure=turn_calls[-1]["result"][:RESULT_SAMPLE], level=LEVEL_LIMIT,
                    blocked=_label(busiest, turn_calls[-1]["args"]))
    return verdict


def _tool_name(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    function = entry.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return entry.get("name") if isinstance(entry.get("name"), str) else ""


def _drop_all_tools(data: dict) -> dict:
    for key in ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"):
        data.pop(key, None)
    return data


def _drop_one_tool(data: dict, name: str) -> dict:
    """只摘掉打转的那一个工具（OpenAI 的 `tools` 与旧的 `functions` 两种写法都照顾）。"""
    if not name:
        return data
    for key in ("tools", "functions"):
        entries = data.get(key)
        if not isinstance(entries, list):
            continue
        kept = [entry for entry in entries if _tool_name(entry) != name]
        if len(kept) != len(entries):
            if kept:
                data[key] = kept
            else:
                data.pop(key, None)
    for key in ("tool_choice", "function_call"):
        choice = data.get(key)
        if isinstance(choice, dict) and _tool_name(choice) == name:
            data.pop(key, None)
    return data


def break_loop(payload: dict, verdict: dict) -> tuple[dict, str]:
    """按级别处置，并追加一条指令。原始 payload 不被改动。

    第 1 级**不动工具**：模型要能立刻按新方案继续；第 2 级摘掉那一个工具；第 3 级与超预算
    才摘掉全部工具（那时确实需要人来定方案）。
    """
    data = dict(payload)
    level = int(verdict.get("level") or 1)
    reason = str(verdict.get("reason") or "")
    tool = str(verdict.get("tool") or "")
    signature = str(verdict.get("signature") or "")
    blocked = str(verdict.get("blocked") or f"工具 `{tool}`")
    failure = f"，失败信息是：{verdict.get('failure', '').strip()}" if verdict.get("failure") else ""
    marker = note_marker(tool, signature, STEP_BUDGET_LEVEL if reason == "step-budget" else level,
                         reason)
    if reason == "step-budget":
        _drop_all_tools(data)
        note = BUDGET_INSTRUCTION.format(count=verdict.get("count", 0), budget=STEP_BUDGET,
                                         tool=tool, marker=marker)
    elif level >= LEVEL_LIMIT:
        _drop_all_tools(data)
        note = HARD_INSTRUCTION.format(blocked=blocked, marker=marker)
    elif level == 2:
        _drop_one_tool(data, tool)
        note = FILTER_INSTRUCTION.format(blocked=blocked, count=verdict.get("count", 0),
                                         tool=tool, marker=marker)
    else:
        note = REFLECT_INSTRUCTION.format(count=verdict.get("count", 0), blocked=blocked,
                                          failure=failure, marker=marker)
    messages = list(data.get("messages") or [])
    messages.append({"role": "user", "content": note})
    data["messages"] = messages
    return data, note
