# 前端资源来源

原生资源从本机 llama.cpp b10819（提交 `6a1a922d2`）HTTP 服务提取，同版本源码 [tools/ui](https://github.com/ggml-org/llama.cpp/tree/6a1a922d2/tools/ui)。

| 产物 | 说明 |
| --- | --- |
| `_app/immutable/bundle.H5ytpYYP.js` | 原始 JavaScript（另含原始 CSS、图标），SHA256 `67b3b101231de9d144bbb5d15683b5103ca6fdd8baaeb0cb167b03ba09414f37` |
| `_app/immutable/bundle.zh-CN.js` | 中文产物，由人工审阅字典生成，`index.html` 加载；HTML 语言标记和安装清单已中文化 |
| `agent-defaults.js?v=2` | 原生界面初始化前一次性启用新对话的服务器工具分类；该版本经可用工具进入 agent 循环，没有独立 `agentMode` 配置项 |

构建在 JavaScript AST 中定位固定字符串，HTML 字符串按文本和属性拆分，不对用户消息或模型输出执行全文替换；双模式工具栏由本地网关注入。迁移保留历史对话的工具选择、单项禁用工具、执行授权及其他设置，迁移后仍可手动更改默认选择。工具栏检查停止生成按钮和授权控件，避免在 agent 两次模型请求之间刷新页面或切换模型；富文本草稿无法安全序列化时等用户发送或清空草稿后再刷新。回归检查：`node web/test-resilience.cjs`。

```powershell
& 'D:\AI\ComfyUI-BSAI_v010\python\python.exe' 'web/localization/build.py'
```

构建脚本固定检查上述 SHA256；升级 llama.cpp 后需重新提取匹配版本前端、审阅字典并验证交互，不能只改哈希绕过检查。详见 [中文化维护说明](../localization/README.md)。

> 精简前原文（2026-09-14，逐字节归档）：D:\AI\_archive\Qwen-Local\web\native\SOURCE.md。归档索引见 D:\AI\_archive\README-archive-index.md。
