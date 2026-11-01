# 中文化维护说明

本地 llama.cpp b10819（`6a1a922d2`）前端中文化。原始前端源码见 [tools/ui](https://github.com/ggml-org/llama.cpp/tree/6a1a922d2/tools/ui)，本地原始 bundle 为 `../native/_app/immutable/bundle.H5ytpYYP.js`。

## 构建

从 `D:\AI\Qwen-Local` 执行：

```powershell
& 'D:\AI\ComfyUI-BSAI_v010\python\python.exe' 'web/localization/build.py'
```

用现有 Python 标准库与 `D:\nodejs\node.exe`（已验证 Node 24.18.0），无须安装依赖；Node 通过 `--expose-internals` 使用内置 Acorn 解析器。输出 `web/native/_app/immutable/bundle.zh-CN.js`，`edits.json` 是中间文件。原始 bundle 的 SHA256：

```text
67b3b101231de9d144bbb5d15683b5103ca6fdd8baaeb0cb167b03ba09414f37
```

哈希不匹配时构建停止，原始 bundle 保留，不用中文产物覆盖；升级原生前端或 Node 后应重新检查提取范围、Acorn 接口和浏览器交互。

## 审阅字典

`*.zh-CN.json` 为人工维护字典，键是原始 JavaScript 节点偏移与字符串片段偏移组成的 `id:start`，值为中文；`*.catalog.json` 只是候选清单，可能含不可翻译字段或漏掉短文案。重新生成候选：

```powershell
& 'D:\AI\ComfyUI-BSAI_v010\python\python.exe' 'web/localization/build.py' extract
```

仅翻译界面固定文案、帮助、提示、通知和无障碍标签；保留 API 字段、协议枚举、CSS 类、事件键、存储键、工具参数、系统提示词、示例代码与 URL。同时参与匹配或筛选的字符串必须追踪其生产和使用位置，不能只按英文外观决定翻译。模板中的 `{0}`、`{1}` 等占位符按原顺序保留，首尾空白保持原样；中文不需要英文复数后缀，字典只清除已确认用于显示的特定 `s` 字面量，不改动全局字符串规则。`catalog.cjs` 校验原文、模板表达式数量及顺序、修改区间重叠，并对最终产物重新执行 JavaScript 语法解析；认证错误识别同时保留英文并兼容中文提示，以保持 API 密钥输入流程。

## 验证范围

中文化不遍历或改写用户输入、聊天记录、附件正文、模型输出和代码块，已有存储键与数据结构保持一致；模型权重、上下文和推理参数不由此构建修改。智能体默认值由 `native/agent-defaults.js` 一次性迁移：在应用模块加载前从 `LlamaUi.disabledToolCategories` 移除 `server`，仅使新对话默认使用服务器工具分组，保留单工具禁用、永久授权、主题、采样和对话设置（含 IndexedDB 聊天记录）；原生前端在有可用工具时自动使用智能体循环，没有需要伪造的 `agenticMode` 配置字段，迁移后的手动分组选择不会在刷新时被重置。后端是否提供服务器工具由 `qwen_config.py` 的 `agent_enabled` 和 llama-server 的 `--agent` 控制。

更新字典后检查桌面与手机视口下的聊天、设置、模型详情、附件、确认对话框和连接错误状态，并验证模式栏、输入内容保留、文件拖放和中英文认证错误；可用浏览器路由模拟 API 响应完成界面检查，无须触发推理或加载模型。

```powershell
& 'D:\nodejs\node.exe' 'web/test-chinese.cjs'
& 'D:\nodejs\node.exe' 'web/test-cache-migration.cjs'
& 'D:\nodejs\node.exe' 'web/test-agent-defaults.cjs'
```

中文测试用独立 Edge 上下文，覆盖 7 个设置页、搜索、附件切换保护、模拟消息发送、会话刷新、原文和代码保留、认证错误及 1440/390/320 像素视口，截图生成于 `web/localization/screenshots`（已完成测试的临时截图已清理）。缓存迁移测试读取 8082 的原始静态资源，验证旧英文首页升级后中文与模式栏保持并保留已有浏览器存储。默认值迁移测试在独立浏览器和本地测试站点先建旧对话再加载迁移脚本，检查历史、其他配置、权限、单独工具禁用项保留，并在新对话中验证工具申请、允许一次和结果续写；所有模型和工具响应由测试模拟，不访问真实推理服务。

> 精简前原文（2026-09-14，逐字节归档）：D:\AI\_archive\Qwen-Local\web\localization\README.md。归档索引见 D:\AI\_archive\README-archive-index.md。
