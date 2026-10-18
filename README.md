# API 中转站测活工具（api-probe） v1.7.0

单文件 Windows 桌面小工具，一条线走完：**输入地址+Key → 探测模型 → 测能不能跑得动 → 接入 Claude Code / Codex**。
沿途把「协议族、首字延迟、疑似掺水」等信息摊开给你看。

- 纯 Python 标准库实现（tkinter + urllib），**零第三方依赖**，不需要 `pip install`
- 一个 `.py` 文件，可直接双击运行，也可打包成单个 exe 发给别人
- 多个站点各自记住地址 / 默认模型 / **多把 Key**（每把带格式标签），Key 加密存在本机

**能做什么（总览）**

| # | 能力 | 一句话 |
| --- | --- | --- |
| 1 | 多站点 + 多 Key | 任意多个中转站；**每个站点可存多把 Key**（各带名称与 OpenAI/Anthropic 格式标签），可一键导入批量列表 |
| 2 | 站点体检（第①步） | 站点指纹（是不是 New API/one-api 系）、协议自动识别（OpenAI/Anthropic/Gemini）、拉回真实模型列表 |
| 3 | 模型列表 | 过滤、只看主流、自动标记类型、复制、导出 CSV、双击即测 |
| 4 | 真实生成测试（第②步） | 流式最小对话，测首字延迟、比对返回模型、校验 usage，参数不兼容自动重试 |
| 5 | 掺水/降级鉴别 | 返回模型≠请求模型、首字异常快、usage 缺失、模型不在列表、面板报错特征等线索 |
| 6 | 批量巡检 | 一次跑一堆站点（多 Key 的站点每把各一行），并发可调，导出 CSV/JSON 做横向对比 |
| 7 | 持久化与安全 | 站点信息明文存、每把 Key 用 Windows DPAPI 按用户加密，可一键清除 |
| 8 | 命令行/自动化 | `--list-only` / `--model` / `--json`，退出码 0/2 可接 CI 或计划任务 |
| 9 | 自检 | 本地 mock 服务器，核心 159 项 + 界面流程 127 项断言，不碰你的真实配置、`~/.claude` 与 `~/.codex` |
| 10 | **Claude Code 一键接入** | 检测 /v1/messages 通道 → 映射模型别名 → 写 settings.json / 生成启动脚本 → 端到端跑通 `claude -p PONG` |
| 11 | **Codex 一键接入（OpenAI 格式）** | 检测 `/v1/responses` 与 `/v1/chat/completions` 决定 `wire_api` → 选 GPT 模型 → 合并 `~/.codex/config.toml` + 写 `.env` → 端到端跑通 `codex exec PONG` |
| 12 | **每把 Key 各归其位** | Claude Code 页默认挑 Anthropic 格式的 Key，Codex 页默认挑 OpenAI 格式的；也可以手动改。

---

## 1. 站点管理（多个中转站）

顶部第一行就是站点下拉，可以放任意多个中转站：

```
站点 [ 中转站A ▾ ] [＋ 添加站点] [编辑] [删除]                     超时(秒) [20]
API 地址 [ https://relay.example.com                                 ]
API Key  [ OpenAI 主 Key ▾ ] [ sk-•••••••• ] [ ]显示 [＋存为 Key] [－删除]  指定模型 [ ... ]
```

- **＋ 添加站点**：弹窗填 `站点名称 / API 地址 / API Key / 默认模型`，里面有「常用预设」下拉可一键填入
  OpenAI、DeepSeek、Kimi、OpenRouter 等官方地址；也可以先把地址粘到主界面、测通了再点添加（会自动带入）。
- **编辑 / 删除**：对下拉里当前选中的站点操作，改名字、换地址、更新 Key 都行。
- **切换站点**：自动带出该站的 地址 + Key + 默认模型，同时把上一个站点的改动存回去。
- **✎ 自定义（临时）**：内置的第一项，不属于站点列表，但它也**单独记住**你敲进去的地址/Key/模型——
  所以「切到 Kimi 官方 → 再切回自定义」，你原来那个自建站地址会原样回来（v1.3 修的就是这个）。
- **批量巡检**：在「批量测试」页点 **导入站点列表**，一键把所有站点填成批量行；
  一个站点有多把 Key 时，**每把 Key 各占一行**，并在上一行加注释标明是哪个站点的哪把。

切换站点后会清空上一次的模型列表与测试结果（列表属于上一个站点，留着会误导），重新点第 ① 步即可。

### 一个站点多把 Key

同一个中转站常常给你不止一把 Key，格式还可能不一样（一把走 OpenAI 兼容接口、一把走 Anthropic 接口）。
所以站点记录里存的是**一个 Key 列表**，每把带「名称 + 格式标签」：

| 操作 | 说明 |
| --- | --- |
| 切换 Key | 主界面「Key 下拉」选一下即可，输入框与后续所有测试都用它 |
| **＋存为 Key** | 把输入框里当前的 Key 存成一把具名 Key，弹窗填名称 + 选格式（自动识别 / OpenAI 格式 / Anthropic 格式） |
| **－删除** | 删掉当前选中的那把；如果删的正好是正在用的，会自动切到剩下的第一把 |
| 格式标签的作用 | **只决定「哪个 CLI 页默认用哪把」**：Claude Code 页默认挑 `Anthropic 格式`（没有就挑「自动识别」），Codex 页默认挑 `OpenAI 格式`；两个页面里都有「使用哪个 Key」下拉可手动改，标签不限制实际调用 |
| 老配置 | v3 及更早的单 Key 结构会自动迁移成一把名为「默认」的 Key，不丢东西 |

`▶ 全流程` 会**分别用两页选中的 Key** 去测两条通道（Anthropic 用一把、OpenAI 用另一把），
所以"一个站两把 Key、一把接 Claude 一把接 Codex"这种玩法可以一次跑完。

---

## 2. 工作流程：一条线走到底

```
输入 API 地址 + Key（或从「站点」下拉里选一个已保存的中转站）
        ↓
① 获取模型列表       只读探测：站点指纹、协议族、真实模型清单（不发任何生成请求）
        ↓
② 对选中模型发测试   真发一次最小对话：能不能出字、首字多快 → 给出「流畅度」判断
        ↓
③ 接入 Claude Code   检测 /v1/messages → 映射模型别名 → 写配置 → 端到端验证
        ↓
④ 接入 Codex         检测 /v1/responses 与 /v1/chat/completions → 写 config.toml → 端到端验证
```

③ 和 ④ 是**两个独立页面**：一页管 Anthropic 格式（Claude Code），一页管 OpenAI 格式（Codex），
两边的通道探测、模型选择、接入方式、门禁与日志互不干扰；站点列表与 Key 两边共用。

顶部按钮栏就是这个顺序：`① 获取模型列表`、`② 对选中模型发测试`、`③ 接入 Claude Code`、`④ 接入 Codex`，
另外有一个 **`▶ 全流程（探测 → 测速 → 接入）`** 把整条线一次跑完：它做完 ① 探测、② 生成测试、
③ 两条通道的检测（Anthropic + OpenAI）与模型映射，然后停在「接入」前等你确认——
**真正的写入永远由你点一下**。

### 接入按钮有门禁

每个页面的「一键接入」默认都是**灰色不可点**的，解锁条件是**对应通道实测可用**：

| 页面 | 解锁条件（二选一即可） |
| --- | --- |
| Claude Code | Anthropic `/v1/messages` 探测通过 |
| Codex | OpenAI 通道探测通过（`/v1/responses` 或 `/v1/chat/completions`） |

为什么这样就够：**通道探测本身就是一次真实的最小生成**（发一句 `Reply with exactly: PONG` 并拿到回复），
所以"探测通过" = "它刚刚真的跑成功了一次"。第 ② 步的生成测试走的是工具自己挑的协议路径，
与 CLI 实际使用的路径不一定重合（例如只代理 Anthropic 的站，② 会失败但 Claude Code 完全可用），
因此 ② 只作为**提示**：通道过了而 ② 没过时按钮照样解锁，但旁边会写明
「注意：第 ② 步生成测试未通过，但 Claude Code 用的 /v1/messages 已实测可用」。

没解锁时按钮旁边会写明**缺哪一步**。换站点、改地址或改 Key 会立刻把两边的门禁一起锁上——
避免拿 A 站的结果去写 B 站的配置。

`▶ 全流程` 跑完且对应通道通过时，会弹一句「现在就把这套配置写进 Claude Code / Codex 吗？」，
点「是」才写入，且写入前自动备份。

### 流畅度

第 ② 步除了「通不通」，还会按首字延迟给一个日常可用性判断：

| 首字延迟 | 判断 |
| --- | --- |
| < 1s | 流畅 |
| 1–3s | 良好 |
| 3–8s | 偏慢 |
| > 8s | 很慢（日常用会明显卡顿） |
| 没拿到流式首字 | 无法判断（并提示可能不支持流式） |

---

## 3. 快速开始

| 方式 | 操作 |
| --- | --- |
| 图形界面 | 双击 **`启动.bat`**（或用 `python api_probe.py`） |
| 自检 | 双击 **`自检.bat`**（本地 mock 服务器，159 项断言，不访问外网） |
| 打包 exe | 双击 **`打包exe.bat`** → 产物在 `dist\ApiProbe.exe` |
| 命令行 | `python api_probe.py --url https://xxx.com/v1 --key sk-xxx --list-only` |

环境要求：**Python 3.9+ 且自带 tkinter**（开发与验证环境是 Windows 11 + Python 3.14.5）。
代码只依赖标准库，理论上 macOS / Linux 也能跑，但没有做过适配与测试，已知差异：

- 三个 `.bat` 是 Windows 专用；其他平台直接 `python api_probe.py`
- Key 的本机加密用 Windows DPAPI，其他平台会退化为明文存储（配置里 `key_encrypted: false`）
- Claude Code 的启动脚本同样是 `.bat`（其他平台用「写入 settings.json」或隔离配置目录的方式）
- 界面字体指定了微软雅黑 / Consolas，其他平台会自动回退到默认字体

> **为什么 `.bat` 里全是英文提示？**
> cmd.exe 是按系统 OEM 码页（中文 Windows 是 936）逐行解析 `.bat` 的，而文件本身是 UTF-8。
> 往里写中文注释/`echo`，字符会被拆成乱码当成命令执行，于是出现
> `'st' 不是内部或外部命令`、`'""' 不是内部或外部命令` 这类报错。
> 所以这三个启动脚本一律**纯 ASCII、不带 BOM、不用 `chcp`**；
> 中文提示都由 Python 输出（Windows 控制台走 Unicode API，不受码页影响）。
> **改这几个文件时请保持 ASCII 内容。**

---

## 4. 界面说明

### 顶部输入区
站点下拉 + `＋ 添加站点 / 编辑 / 删除`、API 地址、API Key（可点「显示」）、指定模型、超时，
以及三个开关：校验 TLS 证书、深度检测、**记住 Key**；右侧显示配置文件位置，
旁边有「清除已存 Key」按钮。这些状态下次启动会自动恢复（含上次选中的站点）。

地址写法很随意，会自动归一化：
`https://x.com/v1/chat/completions`、`https://x.com/v1`、`x.com` → 都按 `https://x.com` 处理，再拼回正确的 `/v1/models`。

> 改了地址或 Key 之后，上一次拉到的列表会自动作废，需要重新点第 ① 步。

### ① 获取模型列表
点下去后：抓站点指纹 → 按顺序试 OpenAI / Gemini / Anthropic 三种鉴权方式 → 把模型填进「模型列表」页，
并自动切到该页、高亮推荐的那个模型（**只是高亮，不会自己发测试**）。

### 模型列表页
全部模型，支持关键字过滤、「只看主流模型」，双击某行＝选中并立即发测试；
可一键复制全部模型名、或导出 CSV。页面顶部会显示「当前选中：xxx」。

每个模型会自动打上类型标记，方便快速筛掉不能做对话测试的：

| 标记 | 命中条件（举例） |
| --- | --- |
| `主流` | gpt-4o / gpt-4.1 / gpt-5 / o3 / claude-sonnet-4-5 / claude-opus / gemini-2.5-pro / deepseek-chat / qwen3 / glm-4-6 / kimi-k2 / grok-4 等 |
| `向量/重排` | 名字含 `embed`、`rerank` |
| `语音` | 含 `tts`、`whisper`、`audio`、`speech` |
| `图像/视频` | 含 `image`、`dall-e`、`flux`、`video`、`sora` |
| `审核` | 含 `moderation` |

拉完列表会自动把光标定位到一个**推荐模型**（优先主流、避开 `:batch`/`:free`/embedding 变体），
但这只是高亮，**不会自己发测试**，测哪个由你决定。

### 概览页
分成两个分区显示，每一条检查项都带颜色等级（`✔ 校验` / `ℹ 提示` / `⚠ 警告` / `⛔ 风险` / `✘ 错误`）：

- **第 1 步：站点与模型列表** —— 归一化地址、协议族、站点指纹、模型数量、列表耗时、站点级提示
- **第 2 步：生成测试** —— 请求模型、返回模型、首字延迟、生成总耗时、usage、HTTP 状态、返回内容片段、生成错误

第 2 步还没跑时，该分区会显示「尚未执行」，提示你去模型列表里挑一个模型。

### 批量测试页
每行一个站点，格式：

```
地址 | Key | [模型]        ← 第三列可省略；分隔符支持 | 、逗号、Tab、两个以上空格
# 以 # 开头的行会被忽略
```

点 **导入站点列表** 可把你保存的所有站点一键填成批量行（含 Key），改都不用改。
批量模式没有人盯着，所以**自动挑模型**（优先主流、避开 `:batch`/`:free`/embedding 变体）；
想指定就写第三列。

结果表每行显示：`地址 / 协议 / HTTP / 列表耗时 / 首字延迟 / 模型数 / 请求→返回模型 / 指纹 / 结论`，
并同样按等级着色。双击某行会把该地址填回主界面，方便单独看详情。

### 导出字段

| 导出 | 字段 |
| --- | --- |
| 概览页「导出 CSV / JSON」 | 输入地址、归一化Base、协议、HTTP状态、可用、模型数、模型列表耗时ms、请求模型、返回模型、首字延迟ms、生成总耗时ms、站点指纹、结论、提示 |
| 批量页「导出 CSV / JSON」 | 同上，每个站点一行 |
| 模型列表页「导出模型列表」 | 模型ID、标记、归属 |
| JSON 导出 | 除上表字段外，还包含全部原始 model 对象、逐次探测的 attempts（每次请求的 URL/状态码/耗时） |

### 原始日志页
每个 HTTP 请求的地址、状态码、耗时，失败时含响应片段。排查「为什么不通」先看这里。

### Claude Code 页
第 6 节的主题：检测 `/v1/messages` 通道 → 映射模型别名 → 写配置 / 生成启动脚本 / 隔离目录 →
跑一次 `claude -p PONG` 端到端验证。顶部按钮栏的「③ 接入 Claude Code」会直接跳到这里，
并自动带上当前站点的候选模型；「▶ 全流程」跑完也会自动切到这一页并填好映射。

---

## 5. 它到底测了什么

### 第 ① 步（只读，最多 3~4 个 GET）
1. **站点指纹** `GET {base}/api/status`：New API / one-api 会匿名返回版本号、站点名等；
   命中 `quota_per_unit`、`display_in_currency`、`enable_online_topup` 等字段则判为 New API 系。
   失败时退化为抓首页 `<title>`。
2. **协议识别 + 模型列表**（按顺序尝试，命中即停）：

   | 协议 | 端点 | 鉴权 |
   | --- | --- | --- |
   | OpenAI 兼容 | `/v1/models`、`/models` | `Authorization: Bearer` |
   | Gemini | `/v1beta/models`、`/v1/models` | `x-goog-api-key`，失败再试 `?key=` |
   | Anthropic | `/v1/models` | `x-api-key` + `anthropic-version` |

   站点选择也会影响顺序：URL 含 `generativelanguage` 先试 Gemini，Key 以 `sk-ant` 开头先试 Anthropic。

### 第 ② 步（你选定模型后才发）
发一条最小对话（`Say OK`，`stream: true`）测**首字延迟**（从发请求到收到第一个 token 的毫秒数，
比总耗时更能反映链路质量）。会自动处理这些情况，不用你操心：

| 情况 | 自动行为 |
| --- | --- |
| 新模型只认 `max_completion_tokens`、拒绝 `max_tokens` | 换参数重试一次 |
| 该站/该模型不支持流式输出 | 改非流式重试一次，只降级指标不判失败 |
| HTTP 401 / 403 | 视为定论，不再浪费一次重试，直接把错误原文报出来 |
| 流式响应不带 usage | 勾了「深度检测」时补一次非流式小请求把 usage 拿回来 |

产出：请求模型 → 返回模型、首字延迟、生成总耗时、usage（token）、HTTP 状态、返回内容片段、错误原文。

### 判定出的可用性
- `可用`：真的返回了内容
- `该模型不可用`：HTTP 401/403、余额不足、无可用渠道等，错误原文会展示
- 第 ① 步若 `/v1/models` 全挂但仍可访问，会提示你手工填模型名直接做第 ② 步

### 疑似掺水 / 降级信号（启发式，不是判决）

检查项分 5 个等级，用颜色区分：`✔ 校验`（正常）、`ℹ 提示`（情报）、`⚠ 警告`（可疑）、`⛔ 风险`（明显不对）、`✘ 错误`（有报错）。

| 信号 | 判定依据 | 什么情况要警惕 |
| --- | --- | --- |
| ⛔ 返回模型 ≠ 请求模型 | 响应体 `model` 字段与请求的做**归一化后**比对 | 别名映射（`gpt-4o` → `gpt-4o-2024-08-06`、`claude-sonnet-4-5-20250929`、`openai/gpt-4o`）会自动归一化不误报；真不一致则可能是被替换/降级 |
| ⚠ 首字延迟 < 250ms | 非缓存请求不该这么快 | 可能命中缓存，或根本没真跑推理 |
| ⚠ 请求的模型不在站点列表里 | 与第 ① 步的列表比对 | 站点列表里没这道菜，请求却"成功"了 |
| ⚠ 未返回 usage / token 为 0 | 解析响应体，及非流式补测 | 无法核验计费统计，正规上游一般会返回 |
| ℹ 面板特征 | 错误串匹配「无可用渠道」「当前分组上游负载已饱和」、`one_api_error`、`veloera` 等 | 可确认站点底座是 New API / one-api 系 |
| ℹ 聚合站 | 模型列表覆盖 OpenAI/Anthropic/Google/DeepSeek/Qwen/GLM/Kimi 中 ≥3 家 | 便于判断它是不是"什么都卖"的聚合站 |

> ⚠️ 这些只是**线索**。真要确认注水，唯一可靠的土办法是拿同一个只有海外大模型能答对的问题
> 反复问，对比答案质量；或者用自己的官转 Key 做 A/B。工具不会替你下结论。

---

## 6. Claude Code 一键接入（Anthropic 格式，独立一页）

Claude Code CLI 2.x 支持直连第三方网关，但有两个坑：**网关必须实现 Anthropic Messages 协议**、
**模型名必须做映射**。这一页把「检测 → 配置 → 验证」串成一步。

### 前提

| 条件 | 说明 |
| --- | --- |
| CLI 版本 2.x | 本机实测 `@anthropic-ai/claude-code` 2.1.302 |
| 网关实现 `POST <BASE>/v1/messages` | 只提供 OpenAI 协议（`/v1/chat/completions`）的站**无法直连**，需要协议转换层 |

### ① 检测通道 /v1/messages

用当前站点的地址与 Key 发一次最小请求（`Reply with exactly: PONG`），依次尝试两种鉴权头：

- `Authorization: Bearer <key>` —— 绝大多数中转站，对应 `ANTHROPIC_AUTH_TOKEN`
- `x-api-key: <key>` —— 官方 Anthropic 与部分网关，对应 `ANTHROPIC_API_KEY`

之后补一次**流式**测试：CLI 全程走流式，只测非流式不够。判读：

| 现象 | 含义 |
| --- | --- |
| 200 且有内容 | 通道 OK，可以直接接入 |
| 401 / 403 | 端点存在但鉴权失败：Key 错/过期，或该 Key 没有这个分组的权限 |
| **404 + 提到模型** | **认证已通过**，只是模型 id 不在你账号的可用集合里 → 去做模型映射 |
| 404（路径级，不提模型） | 该地址没有 `/v1/messages`：要么 BASE 写法不对，要么这站只有 OpenAI 协议，无法直连 |

> 每次尝试限时 20 秒（流式测试用你设的超时），避免碰到挂死的站把检测拖成几分钟。

### ② 模型映射

CLI 内部用 `sonnet` / `opus` / `haiku` / `fable` 别名。不映射，它就拿着**官方模型名**去打你的网关，
报 `404 model_not_found` —— **90% 的失败都在这里**。点「从当前站点模型列表自动匹配」会按名字自动挑：

```
claude-sonnet-5-5         → sonnet（同时作为默认 ANTHROPIC_MODEL）
claude-opus-5-5           → opus
claude-haiku-4-5-20251001 → haiku
claude-fable-5-1          → fable
```

匹配结果可以直接改，也可以手打列表里没有的 id。

### ③ 一键接入（可多选）

> 这一步有门禁：第 ② 步生成测试与本节通道检测**都通过**后，按钮才会从灰色变成可点；
> 没通过时按钮旁边会写明缺哪一步。换站点 / 改地址 / 改 Key 会立刻重新上锁。详见第 2 节。

| 方式 | 做什么 | 适合 |
| --- | --- | --- |
| 写入 `~/.claude/settings.json` | **合并**写入 `env` 块（只替换本工具管理的 `ANTHROPIC_*` 键），写入前自动备份 | 个人机长期使用 |
| 生成启动脚本 | 生成 `start-claude-<站点>.bat`：先设环境变量再调 `claude`，**Key 不落盘** | 不想把 Key 写进配置文件 |
| 生成隔离配置目录 | 生成 `~/.api_probe/claude-code-<站点>/config/`，配合 `CLAUDE_CONFIG_DIR` 使用 | 多个网关来回切、与正式配置彻底隔离 |
| 运行端到端验证 | 在**空的隔离配置目录**里跑 `claude -p "Reply with exactly: PONG" --max-turns 1 --output-format json` | 确认真通了，而不是"看起来配好了" |

### 安全设计

- **合并而非覆盖**：只动 `env` 里的 8 个 `ANTHROPIC_*` 键，你的 `permissions`、MCP 设置、
  其它环境变量一律保留；`settings.local.json` 完全不碰
- **先备份后写入**：备份为 `settings.json.bak-apiprobe-<时间戳>`，页面上有「恢复上次备份」按钮
- **坏配置不覆盖**：现有 `settings.json` 解析不了就中止并报错，绝不写入
- **Key 只出一种**：写入前会清掉旧的 `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`，
  保证两者不同时存在（同时存在时行为不确定，是最隐蔽的排查噩梦）
- **验证不污染正式配置**：端到端验证使用临时 `CLAUDE_CONFIG_DIR` + 临时工作目录
- **日志里 Key 打码**：预览配置时只显示成 `sk-abc***`

### 命令行

```bash
python api_probe.py --url https://your-gateway.com --key sk-xxx --claude-check
```

打印通道检测结论、两次尝试的状态码，以及建议写入的 `env`（Key 已打码）；退出码 `0` = 通道可用。

### 排错速查

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| `404 model_not_found` / `not supported by any configured account` | 模型 id 不在该 Key 的可用集合里 | 做模型映射；模型清单来自 `/v1/models` |
| `401 authentication_error` | Key 错/过期，或网关只认另一种头 | 看检测结果里的「认证方式」，它会告诉你哪种头可用 |
| 404 且不提模型 | `ANTHROPIC_BASE_URL` 带了 `/v1`，或该站没有此协议 | 只写网关根地址（本工具会自动剥掉 `/v1`） |
| 回复空 / 流式中断 | 网关未完整实现流式或 reasoning/usage 字段 | 检测结果里的「流式首字」会暴露；换渠道 |
| 报的上下文窗口、成本"很离谱" | 第三方不实现官方 usage / prompt cache，CLI 本地按官方定价估算 | 属预期行为，计费以网关后台为准 |
| `claude: command not found` | PATH 未刷新 | 重开终端；`where claude` 复核 |

---

## 6b. Codex 一键接入（OpenAI 格式）

**独立一页**（顶部 `④ 接入 Codex`）。Codex 与 Claude Code 的配置思路完全不同，要点就三条：

| 要点 | 说明 |
| --- | --- |
| **base_url 要带 `/v1`** | 与 `ANTHROPIC_BASE_URL` 正好相反。写 `https://gw.example.com/v1`，Codex 自己拼 `/responses` 或 `/chat/completions` |
| **`wire_api` 二选一** | `responses`（打 `/v1/responses`）或 `chat`（打 `/v1/chat/completions`）。工具会**两条都试**，把能用的那条写进配置 |
| **Key 不进配置文件** | 配置里只写 `env_key = "变量名"`，真正的 Key 放在该环境变量里。Codex 会加载 `~/.codex/.env`，所以工具默认把 Key 写在那儿 |

### ① 检测通道

依次对 `POST {base}/v1/responses` 与 `POST {base}/v1/chat/completions` 发最小请求（`Reply with exactly: PONG`），
判读与 Claude 页一致：200 通过 / 401 鉴权失败 / **404 但提到模型**说明路径通、模型不对 / 404 路径级说明没这个协议。

### ② 选模型

点「从当前站点模型列表挑一个」会优先选 `gpt-6` → `gpt-5` → `gpt-4.1` → `o4`/`o3` → `gpt-4o` → `codex` 系列，
也可直接手打（比如你网关上的私有命名）。

### ③ 一键接入（可多选）

| 方式 | 做什么 |
| --- | --- |
| 写入 `~/.codex/config.toml` | 顶层写 `model` + `model_provider`，末尾追加 `[model_providers.api_probe]` 表；**只动这几个键**，你原有的 `[desktop]`、`[mcp_servers]`、`[projects.*]`、其它 provider 一律保留；改前备份，**现有文件不是合法 TOML 就拒绝写入** |
| 把 Key 写进 `~/.codex/.env` | Codex 的 `env_key` 机制就从这里取值（**明文**，改前备份；不想落盘就用下一项） |
| 生成启动脚本 | `start-codex-<站点>.bat`：运行时临时设好环境变量再拉起 codex，**Key 不落盘到配置文件** |
| 运行端到端验证 | 在隔离的 `CODEX_HOME` 里跑 `codex exec "Reply with exactly: PONG" --skip-git-repo-check --ephemeral -s read-only -o last.txt` |

**安全设计**：合并而非覆盖、改前备份、坏 TOML 拒绝写入、验证用临时 `CODEX_HOME`（不碰你正式的 `~/.codex`）、
页面里可一键「恢复上次备份」。重复执行不会产生重复块（有标记注释包裹）。

### 命令行

```bash
python api_probe.py --url https://your-gateway.com --key sk-xxx --model gpt-5.1 --codex-check
```

打印两条通道的检测结论与建议写入的 `[model_providers.*]` 片段（Key 不写进配置）；退出码 `0` = 通道可用。

### Codex 排错速查

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| 报 404 且路径级 | `base_url` 少了或多了 `/v1` | Codex 要带 `/v1`（与 Anthropic 相反），工具会自动补 |
| 明明设了 Key 却 401 | `env_key` 写的变量名与实际设置的不一致；或新终端没生效 | 核对变量名；`echo %CODEX_API_KEY%` 看有没有值；工具默认写 `~/.codex/.env` |
| 报模型不可用 | 该 Key 的可用模型里没这个 id | 在 Codex 页点「从当前站点模型列表挑一个」重选 |
| 改完配置没生效 | 有更高优先级的 config（`-p profile` 分层）或环境变量覆盖 | `codex exec` 支持 `-c key=value` 覆盖；本工具也提供「复制临时命令」 |
| 找不到 codex 命令 | 没装 CLI，或桌面版自带的 codex.exe 不在 PATH | 工具会自动去 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` 找（桌面版自带） |

---

## 7. 命令行用法

```bash
# 只做第 ① 步：列出模型，挑好了再单独测
python api_probe.py --url https://xxx.com/v1 --key sk-xxx --list-only

# 指定模型做第 ② 步（自动挑模型则省略 --model）
python api_probe.py --url https://api.deepseek.com --key sk-xxx --model deepseek-chat

# 一步到位 + JSON 输出（适合脚本/CI）
python api_probe.py --url https://xxx.com/v1 --key sk-xxx --json > out.json

python api_probe.py --list-only --json          # 第 ① 步的机器可读输出
python api_probe.py --url ... --claude-check    # 检测这个网关能不能接 Claude Code CLI
python api_probe.py --url ... --codex-check     # 检测 OpenAI 通道，判断 Codex 该用 responses 还是 chat
python api_probe.py --url ... --no-verify       # 跳过 TLS 证书校验（自签证书站点）
python api_probe.py --url ... --no-deep         # 关闭深度检测，少发一次请求
python api_probe.py --selftest                  # 核心逻辑自检（159 项）
python api_probe.py --uitest                    # 界面 + 站点管理 + 两段式 + 两个接入页（127 项）
```

退出码：`0` 成功 / `2` 失败（可直接用于脚本巡检、CI 里做连通性卡点）。

---

## 8. 配置持久化（存了什么、存在哪、怎么加密）

配置文件固定为 **`~/.api_probe.json`**（Windows 上是 `C:\Users\<你>\.api_probe.json`，当前 schema 版本 4），界面右上角也会显示实际路径。

| 内容 | 是否保存 | 形式 |
| --- | --- | --- |
| 站点列表：名称、地址、默认模型、各把 Key 的名称与格式标签、当前选中哪把 | 总是保存 | 明文（不是敏感信息） |
| 「自定义（临时）」那一格的地址与模型 | 总是保存 | 明文 |
| 上次选中的站点、超时、并发数、TLS 校验、深度检测、只看主流、窗口大小 | 总是保存 | 明文 |
| **各站点的每一把 API Key**（含名称与格式标签） | 仅勾选「记住 Key」时 | **加密**（Windows DPAPI，按当前用户），Key 名称与格式明文 |
| **批量测试列表**（含里面各站的 Key） | 仅勾选「记住 Key」时 | **加密**（同上） |

几个要点：

- **默认不保存 Key**。不勾选的话，Key 只活在内存里，关掉窗口就没了，文件里也不会出现。
- **勾选后不是明文**。用 Windows 的 DPAPI 按「当前用户」加密，密钥由系统托管。
  文件里长这样：`"key": "AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA..."`，`"key_encrypted": true`。
- **换机器/换用户就解不开**。DPAPI 绑定当前 Windows 用户 + 本机，把配置文件拷到别的电脑上，
  站点名称/地址照常生效，Key 会读不出来（此时程序不会报错，只是 Key 框为空，重填一次并重勾即可）。
- **随时可清除**：点「清除已存 Key」按钮会一次清掉所有站点与当前输入框里的 Key；取消勾选「记住 Key」也会同时清空文件里的凭据。
- **保存时机**：改动后约 1.2 秒自动落盘（避免每敲一键就写文件），切换/增删站点、点第 ① 步、开始批量测试、正常关闭时再各存一次。
- **自动迁移**：v1/v2 的老配置会自动升级到 v3（地址变成「默认站点」）；v3 升级到 v4 时，单个 Key 会变成一把名为「默认」的 Key。都不会丢数据。
- 顺带一提：`--selftest` / `--uitest` 全程把配置指向临时目录，**不会碰你真实的配置文件**。

---

## 9. 常见问题

**Q：为什么要点两次，不能一键搞定？**
因为「能列出的模型」和「你真要用的模型」经常不是同一个。一键模式只能替你猜（批量模式仍在用猜的），
两段式把选择权交回给你。命令行想一步到位就用 `--model` 或 `--json`。

**Q：切换站点后模型列表怎么空了？**
列表属于上一个站点，留着容易误测（比如拿 A 站的模型名去 B 站发请求）。切换会自动作废结果，
重新点第 ① 步即可；地址/Key 不需要重填。

**Q：Key 会被上传到别处吗？**
不会。所有请求由本机 socket 直接发往你填的地址。落盘的只有上面那张表里的内容，
且 Key 是加密的；不勾「记住 Key」则完全不落盘。

**Q：站点能连上，但列表为空 / 404？**
部分中转站关了 `/v1/models`。这时直接在上方「指定模型」里手写一个模型名，点第 ② 步即可测。

**Q：报 SSL 证书错误？**
自建站常用自签证书，取消勾选「校验 TLS 证书」即可（**仅对你信任的地址这么做**）。

**Q：双击 exe 报 `Could not create temporary directory!`？**
PyInstaller 单文件包运行时要把内容解包到 `%TEMP%`，而 **Low 完整性进程无权在 Medium 的 `%TEMP%` 下建目录**。
最常见的原因是 **exe 所在目录被打了「低完整性级别（Low integrity）」标签**——Windows 会让该目录下的
可执行文件也以 Low 完整性启动。沙箱类工具、部分加固过的目录会造成这种情况。

处置：把 exe 移到普通目录（如下载文件夹）再运行；或原地改标签：

```powershell
icacls "路径\ApiProbe.exe" /setintegritylevel Medium
```

`打包exe.bat` 结尾已自动执行这一步，重新打包不会再踩到。

**Q：这个工具能帮我规避风险吗？**
不能。它只能告诉你"这个 Key 现在能不能用、返回的东西像不像真的"。跑路、突然涨价、
数据留存这类风险，工具测不出来——遵循老规矩：小额充值试水、多站互为备份、
商业机密和密钥不要过任何第三方中转。

---

## 10. 典型用法

**场景一：新买了一个中转站，想确认是不是真货**
1. ＋ 添加站点（名称随便起，比如 `中转站A`），把地址和 Key 填进去
2. 点第 ① 步：看模型列表和站点指纹 —— 有没有你要的模型、是不是 New API 系
3. 在模型列表里挑**你实际要用的那个模型**，双击直接测
4. 看第 ② 步结果：返回模型是否一致、首字多少 ms、有没有 usage、有没有 ⛔/⚠

**场景二：手上有五六个站，选一个最靠谱的**
1. 全部添加进站点列表
2. 切到「批量测试」页 → 点「导入站点列表」→ 并发调到 3~4 → 开始
3. 结果表里横向比 首字延迟 / 请求→返回模型 / 指纹 / 结论
4. 导出 CSV，挑出延迟低且没有 ⛔ 的，再用场景一单独复测一次确认

**场景三：怀疑某站把 Opus 换成了便宜模型**
拿**同一个模型名**在"该站"和"你信得过的官转站/官方"各测一次，对比：请求→返回模型、
首字延迟是否符合真实推理的量级、usage 的 token 结构、以及多次测试的答案质量。
工具只负责把这几项证据摆出来，结论得你下。

**场景四：日常巡检**

```bash
python api_probe.py --url https://your-relay.com/v1 --key sk-xxx --list-only --json > check.json
```

退出码 `0` 可用 / `2` 不可用，直接接进 Windows 计划任务或 CI，某站挂了就告警。

---

## 11. 能力边界（做不到什么）

| 做不到 | 说明 |
| --- | --- |
| ❌ 判定"注水"的定论 | 只能给线索。要坐实，得自己拿问题对比答案质量，或用官转 Key 做 A/B |
| ❌ 预测跑路 / 涨价风险 | 站长明天关站、突然改价、留存你的 prompt——工具测不出来 |
| ❌ 代理 / 缓存 / 改写请求 | 它不是中转站，只是个客户端探测器，不转发也不保存内容 |
| ❌ 查余额 / 额度 | 各家中转站余额接口五花八门，目前只看"能不能通" |
| ❌ 并发压测 | 单次最小请求，反映连通性与模型真实性，不代表吞吐能力 |
| ❌ 非 Windows 平台的加密存储 | DPAPI 是 Windows 专有；其他平台会退回明文存储（配置里 `key_encrypted: false`） |
| ❌ 让 Claude Code 接上只支持 OpenAI 协议的站 | 那种站必须先有协议转换层（cc-switch / claude-code-router 之类），本工具只能明确告诉你"接不上"；反过来这个站通常可以直接接 Codex |
| ❌ 替你安装 CLI | 只做检测与配置；Claude Code 用官方安装器或 `npm i -g @anthropic-ai/claude-code`，Codex 用官方 CLI 或桌面版自带的 `codex.exe` |
| ❌ 让 Codex 用上非 GPT 命名的模型 | 只要网关的 `/v1/responses` 或 `/v1/chat/completions` 认这个 id 就能用，工具不限制名字；但 Codex 自身的 agent 行为按 OpenAI 模型调优，换别的模型效果不保证 |

---

## 12. 文件清单

| 文件 | 说明 |
| --- | --- |
| `api_probe.py` | 全部功能（核心探测 + 站点管理 + Claude Code / Codex 接入 + 图形界面 + 自检），单文件 |
| `启动.bat` | 双击启动图形界面（纯 ASCII，勿加中文） |
| `自检.bat` | 跑 159 项核心逻辑自检（纯 ASCII）；界面/流程自检用 `python api_probe.py --uitest`（127 项） |
| `打包exe.bat` | 用 PyInstaller 打成单文件 exe（纯 ASCII） |
| `README.md` | 本文档 |

运行时另外会产生（都在用户目录，不在本仓库）：

| 路径 | 内容 |
| --- | --- |
| `~/.api_probe.json` | 站点列表与设置（Key 加密） |
| `~/.claude/settings.json` | Claude Code 配置（本工具只合并 `ANTHROPIC_*` 键，改前自动备份） |
| `~/.codex/config.toml` | Codex 配置（本工具只改 `model`/`model_provider` 与 `[model_providers.api_probe]`，改前自动备份） |
| `~/.codex/.env` | Codex 的 `env_key` 取值处（勾选时才会写，明文） |
| `~/.api_probe/claude-code-<站点>/`、`~/.api_probe/codex-<站点>/` | 生成的启动脚本与隔离配置目录 |

版本 v1.7.0（一个站点可存多把 Key，各带格式标签；Claude Code 与 Codex 两页各自认领对应格式的 Key）
