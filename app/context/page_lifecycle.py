"""两个标识位决定后台要不要收工（所有者规定的口径）。

* **页面位**：当前有没有前端的 Qwen 页面 → 有 **1**、没有 **0**。自动检测：页面打开时立刻心跳
  一次、之后每 20 秒一次，超过 `PAGE_IDLE_SECONDS` 没心跳就算"没有页面"。页面在 `pagehide`
  时也会发一个"我关了"，但那个信标**只是把计时从那一刻重新开始**，不能立刻判成"没有页面"——
  原因见第 4 条。
* **任务位**：页面上有没有任务在跑（对话、切模型、生图、视频、补帧……）→ 有 **1**、没有 **0**。
  每次判定现读，任务一结束就自动回 0。

**两位同时为 0 → 后台自动关闭；只要有一位是 1，就一直不关。**

四个细节都是为了不误伤，写在这里免得以后被"简化"掉：

1. **见过页面才关**。服务刚起来、浏览器还没打开页面的那几秒，页面位也是 0；不加这条，网关会
   把自己关掉（启动即自杀）。所以只有"这次启动见过至少一次心跳"之后，页面位从 1 变 0 才算数。
2. **判定失败按"有任务"处理**。任务位探不到（探针自己抛异常）时宁可不停——停错了要人来拉，
   不停只是多占一会儿。
3. **想取消或暂停收工，看 `enabled`**（`settings.json` 的 `auto_stop_on_page_close`，默认开）。
4. **`pagehide` 不等于"页面关掉了"**。刷新、站内跳转、地址栏回车都会触发它，而这个前端会自我
   重载（`/props` 失败时自刷新）。信标如果立刻把页面位清零，"刷新一次"就等于"把整个栈收工"：
   网关退出后页面反而连不上，表现就是"启动坏了"。所以信标只重置窗口——窗口内来一次心跳即作废，
   真的关掉则照旧在窗口之后收工。
"""
import logging
import time

LOGGER = logging.getLogger("qwen.lifecycle")

HEARTBEAT_SECONDS = 20      # 前端心跳间隔（也用于状态展示）
# 超过这么久没有心跳 = 没有页面。给到 150 秒是有原因的：后台标签页的定时器会被浏览器节流
# （隐藏页可降到一分钟才响一次），45 秒会把"标签还在、只是没在前台"误判成"页面关了"——页面明明
# 还开着，服务却自己收工了，表现就是所有者反馈的"网页没关但一直说没连接上"。150 秒容得下两三次
# 被节流的心跳；页面只要回来发一次心跳，窗口立刻重新开始。
PAGE_IDLE_SECONDS = 150
# 但**收到"我关了"的信标**时不该再等满 150 秒：信标只有真的关窗/刷新才会发。取一个短窗口，
# 刷新场景靠新页面的第一次心跳作废（几秒内），真的关掉则二十秒左右收工。
CLOSE_AFTER_BEACON_SECONDS = 20
TICK_SECONDS = 2.0          # 判定周期；只读几个内存标志，代价可忽略


class PageLifecycle:
    """两个位 + 一个收工动作。不认识进程、不认识路由，两个输入都由调用方注入，所以能被单测完整驱动。"""

    def __init__(self, task_check, stopper, enabled=None, clock=time.monotonic,
                 page_idle=PAGE_IDLE_SECONDS, logger=LOGGER):
        self.task_check = task_check          # () -> str：在跑什么；空串 = 没有任务
        self.stopper = stopper                # () -> 收工（可以返回 awaitable）
        self.enabled = enabled or (lambda: True)
        self.clock = clock
        self.page_idle = float(page_idle)
        # 当前生效的窗口：平时是 page_idle；收到"我关了"的信标后收窄成 CLOSE_AFTER_BEACON_SECONDS
        # （上限不超过 page_idle，所以单测里把 page_idle 设小时行为不变）。
        self.window = float(page_idle)
        self.logger = logger
        self.heartbeat_at: float | None = None    # 最近一次心跳（None = 页面自己说了"关了"）
        self.page_ever_seen = False
        self.closed_at: float | None = None       # 收到"我关了"的时刻（仅记录，供诊断）
        self.stopped = False
        self.last_task = ""
        self.checks = 0

    # ------------------------------------------------------------------ 页面侧
    def note_page_alive(self) -> dict:
        """一次心跳（开页面时立刻一次，之后每 20 秒一次）：页面位 → 1。"""
        self.heartbeat_at = self.clock()
        self.window = self.page_idle          # 页面回来了：窗口恢复成正常长度
        self.page_ever_seen = True
        self.closed_at = None
        return self.flags()

    def note_page_closed(self) -> dict:
        """页面说"我关了"：从这一刻起重新计时，窗口内没有新心跳才算真的没有页面。

        这里**不能**把心跳清成 None。`pagehide` 在刷新和站内跳转时同样会触发，而这个前端会自我
        重载（`/props` 失败时自刷新），"立刻归零"于是等于"刷新一次就把整个栈收工"——网关退出后
        页面还连不上，看起来就是"启动坏了"。改成从此刻计时：新页面在窗口内发一次心跳就作废
        （`note_page_alive` 会清掉 `closed_at`），真关掉则照旧在窗口之后收工。

        窗口取 `min(page_idle, CLOSE_AFTER_BEACON_SECONDS)`：信标几乎只在真的关窗/刷新时出现，
        没必要为它等满 150 秒（那是给"被节流的后台标签"留的余量）。
        """
        self.closed_at = self.clock()
        self.heartbeat_at = self.clock()
        self.window = min(self.page_idle, CLOSE_AFTER_BEACON_SECONDS)
        self.logger.info("page reported closed (pagehide): closing in %.0fs unless a heartbeat "
                         "arrives", self.window)
        return self.flags()

    # ------------------------------------------------------------------ 两个位
    def page_flag(self) -> int:
        """有页面 = 1；没有（或超时没心跳）= 0。"""
        if self.heartbeat_at is None:
            return 0
        return 1 if self.clock() - self.heartbeat_at <= self.window else 0

    def task_flag(self) -> int:
        """有任务在跑 = 1；没有 = 0；探针失败按 1 处理（宁可不关）。"""
        try:
            label = str(self.task_check() or "")
        except Exception as exc:  # noqa: BLE001 - 探不动就别关
            self.last_task = f"探针失败：{exc}"
            return 1
        self.last_task = label
        return 1 if label else 0

    def flags(self) -> dict:
        page = self.page_flag()
        task = self.task_flag()
        seconds_since_beat = (None if self.heartbeat_at is None
                              else round(self.clock() - self.heartbeat_at, 1))
        return {
            "page": page,
            "task": task,
            "enabled": bool(self.enabled()),
            "page_ever_seen": self.page_ever_seen,
            "heartbeat_seconds_ago": seconds_since_beat,
            "heartbeat_seconds": HEARTBEAT_SECONDS,
            "page_idle_seconds": self.page_idle,
            "window_seconds": self.window,
            "task_label": self.last_task,
            "closed_seconds_ago": (None if self.closed_at is None
                                   else round(self.clock() - self.closed_at, 1)),
            "stopped": self.stopped,
            "checks": self.checks,
        }

    def should_close(self) -> bool:
        """两个位同时为 0 —— 而且这次启动见过页面、开关是开的。"""
        if self.stopped or not self.enabled() or not self.page_ever_seen:
            return False
        return self.page_flag() == 0 and self.task_flag() == 0

    # ------------------------------------------------------------------ 判定循环
    async def run(self, tick: float = TICK_SECONDS):
        import asyncio

        while True:
            await asyncio.sleep(tick)
            if self.stopped or not self.enabled() or not self.page_ever_seen:
                continue
            if self.page_flag() == 1:
                continue
            self.checks += 1
            if self.task_flag() == 1:
                # 任务位是 1：页面关了也不收工，等它跑完（下一轮再看）。
                continue
            self.stopped = True
            self.logger.warning(
                "page closed and no task is running: shutting the stack down "
                "(window %.0fs%s, last heartbeat %.0fs ago)",
                self.window,
                ", started by the pagehide beacon" if self.closed_at is not None else "",
                -1.0 if self.heartbeat_at is None else self.clock() - self.heartbeat_at)
            try:
                outcome = self.stopper()
                if hasattr(outcome, "__await__"):
                    outcome = await outcome
                self.logger.warning("page-close shutdown finished: %s", outcome)
            except Exception:  # noqa: BLE001 - 收工失败要留痕，但循环不能死
                self.logger.exception("page-close shutdown failed")
                self.stopped = False
