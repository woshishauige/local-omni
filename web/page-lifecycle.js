/**
 * 页面还活着 / 页面关了 —— 让后台跟着页面走（所有者要求：不要一直挂着抢占端口）。
 *
 * 这一层只做两件事，判定与宽限期都在网关侧（`page_lifecycle.py`）：
 *
 *  1. **心跳**：打开页面时立刻发一次，之后每 20 秒一次（页面重新可见时也补一次），
 *     告诉网关"页面还在"。取消倒计时**只认这个心跳**，因为"任何请求都算在用"是错的：
 *     监督进程每 10 秒探一次网关，那样倒计时永远走不到头，关页面也就永远不会收工。
 *  2. **信标**：`pagehide`（关闭或刷新）时发一次"我关了"，网关开始 60 秒倒计时；
 *     刷新的话新页面几秒内就会发出第一个心跳，倒计时随即取消。
 *
 * 访客的页面同样会加载这一层：心跳和信标都只认机主身份（网关侧 `require_owner`），
 * 访客关自己的标签页不会把机主的服务停掉；访客"还在用"由网关按会话活动自己判断。
 */
(function () {
  "use strict";

  var ALIVE = "/local/lifecycle/page-alive";
  var CLOSED = "/local/lifecycle/page-closed";
  var HEARTBEAT_MS = 20000;
  var timer = null;

  function send(where) {
    try {
      // sendBeacon 在页面卸载途中也能可靠发出：fetch 会被取消，同步 XHR 会卡住关闭。
      if (navigator.sendBeacon) {
        navigator.sendBeacon(where, "");
        return;
      }
      var request = new XMLHttpRequest();
      request.open("POST", where, false);
      request.send(null);
    } catch (error) {
      // 发不出去就算了：网关那边只是少了一次提示，不会反过来影响页面。
    }
  }

  function beat() {
    send(ALIVE);
  }

  function start() {
    if (timer !== null) return;
    beat();
    timer = setInterval(beat, HEARTBEAT_MS);
  }

  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function closed() {
    stop();          // 关掉的页面不再有心跳，倒计时才能走到头
    send(CLOSED);
  }

  start();
  window.addEventListener("pagehide", closed);
  // 有些浏览器整窗关闭时只给 beforeunload；重复发送在服务端是幂等的。
  window.addEventListener("beforeunload", closed);
  // 从往返缓存里恢复（前进/后退）：也算"页面又回来了"。
  window.addEventListener("pageshow", function () { start(); beat(); });
  // 后台标签页的定时器会被浏览器节流（可能一分钟才响一次），所以**切到后台和切回前台都立刻补一次
  // 心跳**：前者让网关知道"页面还活着"（网关给的窗口是 150 秒，容得下几次被节流的间隔），后者
  // 保证刚回来就被认出来。少了这两下，"标签还在、只是没在前台"会被误判成"页面关了"。
  document.addEventListener("visibilitychange", function () {
    beat();
  });
})();
