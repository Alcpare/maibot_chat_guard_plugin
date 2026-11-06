# 聊天护栏（Alcpare.chat_guard）

给麦麦的回复加一层**可开关、可观察**的护栏。

四个独立模块，每个都能单独启用/关闭，都能先切成 `log` 模式**只观察不改行为**——先看看它到底会拦什么，确认没误伤了再改成真正拦截。

- 当前版本：**0.7.0**（测试版）
- 兼容：MaiBot **1.3.19 ~ 1.3.99** / 插件 SDK **2.9.0 ~ 2.21.99**
- 联系方式：**1373558257@qq.com**

---

## 一分钟上手

### 方式一：WebUI 配置页（推荐）

1. 在 WebUI 插件页确认本插件已加载
2. 打开「聊天护栏」的配置
3. 按需开关各模块

**保存即生效，不用重启** ✓

### 方式二：直接改配置

```toml
[plugin]
enabled = true

[rhythm]
enabled = true
action = "force"

[inject]
enabled = true
action = "force"
```

---

## 四个模块

### 1️⃣ `rhythm` — 发送节奏

**解决什么问题**：麦麦回得太快、太长，一眼就是机器。

**怎么做**：只在消息长度落在 `2 ~ 24` 字之间时，才给宿主设置「正在输入」的 typing 状态，让短消息也有打字过程。

| 配置 | 默认 | 说明 |
|---|---|---|
| `action` | `force` | `force` 真的设置 typing；`log` 只记日志 |
| `min_chars` | 2 | 少于这个字数不管（表情、单字） |
| `max_chars` | 24 | 多于这个字数不管（长回复本来就有思考时间） |
| `skip_patterns` | 见配置 | 命中就不管（系统提示、斜杠命令等） |
| `log_every` | true | 每条都记日志，方便核对 |

---

### 2️⃣ `inject` — 每轮注入规则

**核心模块。** 在她回答之前，往提示词里塞两段规则：

| 字段 | 注入到 | 作用 |
|---|---|---|
| `template` | **Replyer**（写回复的模型） | 告诉她这一轮该怎么说 |
| `planner_template` | **Planner**（决定回不回、怎么回的模型） | 告诉它该怎么写「回复指引」 |

| 配置 | 默认 | 说明 |
|---|---|---|
| `action` | `force` | `force` 真的注入；`log` 只记本来会注入什么 |
| `marker` | `[用词提醒]` | 注入标记，用于同轮去重 |
| `planner_marker` | `[指引写法]` | Planner 侧的注入标记 |
| `words` | 42 个词 | 用词黑名单，写进模板里 |
| `conditional` | false | 打开后只在 `triggers` 命中时才注入 |
| `triggers` | 见配置 | 条件注入的触发词 |

**为什么两个阶段都要注入**：Planner 定方向，Replyer 写具体话。只改 Replyer 的话，Planner 给的指引可能已经是「平淡接一句」，Replyer 只能照着写，规则就白设了。

---

### 3️⃣ `system_text` — 系统文案闸门

**解决什么问题**：框架有些硬编码文案（如「我暂时失忆了」「已清空 Maisaka 历史上下文」）会在某些情况下被当成麦麦的话发出去，一看就是系统提示泄漏。

**怎么做**：在发送前扫一遍文本，命中 `patterns` 就拦掉。

| 配置 | 默认 | 说明 |
|---|---|---|
| `action` | `block` | `block` 直接中止发送；`log` 只记录 |
| `patterns` | 2 条 | 正则，一行一条 |

---

### 4️⃣ `learn` — 学习守卫

**解决什么问题**：麦麦有个表达学习机制，会把群里的说法学进表达库。但它有时候会把「固定尾缀」「行为模板」当成人话学进去，之后每句话都套同一个模板。

**怎么做**：在 `expression.learn.before_upsert` 钩子上拦一道——命中 `guard_patterns` 的**不入库**。

| 配置 | 默认 | 说明 |
|---|---|---|
| `action` | `block` | `block` 不入库；`log` 只记录 |
| `guard_patterns` | 见配置 | 正则，拦截「句尾加…」「先复述对方…」「纯引号原话」这类 |
| `vary_marks` | 见配置 | 白名单形态：描述里出现「用」「直接」「换」「每次」这类可变特征 → 允许入库 |

**设计要点**：守卫只拦「明显不能变」的模板。有可变标记的（比如「用 X 表示 Y」）照常入库，因为那是可复用的表达方式，不是死板模板。

---

## 它用了哪些 Hook

| Hook | 用在哪个模块 |
|---|---|
| `send_service.before_send` | `rhythm`（设置打字状态）、`system_text`（拦截） |
| `maisaka.replyer.before_model_request` | `inject`（给 Replyer 注入） |
| `maisaka.planner.before_request` | `inject`（给 Planner 注入） |
| `expression.learn.before_upsert` | `learn`（拦截入库） |

全部是 SDK 正式内置的 Hook，不是私有补丁。

---

## 配置项一览

```toml
[plugin]
enabled = true                # 总开关
config_version = "1.0.0"

[rhythm]
enabled = true
action = "force"              # force / log
min_chars = 2
max_chars = 24
skip_patterns = """..."""
log_every = true

[system_text]
enabled = true
action = "block"              # block / log
patterns = """..."""

[learn]
enabled = true
action = "block"              # block / log
guard_patterns = """..."""
vary_marks = """..."""

[inject]
enabled = true
action = "force"              # force / log
marker = "[用词提醒]"
planner_marker = "[指引写法]"
template = "..."
planner_template = "..."
words = """..."""
conditional = false
triggers = """..."""
log_every = false
```

---

## 排查（日志里搜 `[护栏]`）

| 日志 | 含义 |
|---|---|
| `已加载：节奏=force 字数=2~24 | 系统文案=block(2 条) | 注入=force | 入库守卫=block` | 启动状态 |
| `已注入(items/SystemMessageItem)：...` | Replyer 注入成功，括号里是注入位置 |
| `已注入 planner(items/SystemMessageItem)：...` | Planner 注入成功 |
| `已注入过，跳过（key=...）` | 同轮去重生效 |
| `已拦截系统文案：...` | `system_text` 拦了一条 |
| `[log] 本可拦截系统文案：...` | `system_text` 是 log 模式，没真拦 |
| `配置已刷新：...` | 配置热重载完成 |
| `已卸载：判断 N 条 · 强制延迟 N 条 · 系统文案 N 条` | 卸载时的统计 |

---

## 常见问题

**Q：为什么 `rhythm` 只对 2~24 字的消息生效？**

短消息（「在」「嗯」）如果秒发，特别像机器人。长消息本来就要打一会儿，不用额外延迟。而超过 24 字的，宿主的打字时间已经够长了，再叠加会让人等太久。

**Q：`inject` 的 `log` 模式和 `force` 区别？**

`log` 只打印「本轮本可以注入什么（前 70 字）」，不会真的改提示词。想先看规则长什么样、会不会误伤，就用 `log` 跑一段时间。

**Q：`learn` 会不会太严，把好的表达也拦掉？**

`guard_patterns` 只拦明确不能变的形态：固定尾缀、行为模板、纯引号原话。`vary_marks` 是白名单——描述里出现「用」「换」「每次」这类可变标记就允许入库。真被误拦了，把对应正则从 `guard_patterns` 里删掉即可。

**Q：改了配置要重启吗？**

不用。保存后热重载，日志会打一条 `配置已刷新`。只有改 `plugin.py` 本身才需要重载插件。

---

## 已知限制

- `system_text` 的 `patterns` 是正则，写错了会导致该条规则失效（启动时会打 warning）
- `inject` 依赖宿主的 `SystemMessageItem`，如果宿主改了这个结构，注入会失败（会打 `注入失败`）
- `learn` 只拦「新入库」，已经入库的旧表达不会自动清理，需要手动删

---

## 更新日志

### v0.7.0（测试版）
- 精简为四个模块，删掉 `duel`（怼人句式监控）、`dedupe`（重复文本防抖）、`literary`（文学腔监控）
  - `literary` 的词表合并进 `inject.words`（18 → 42 词）
  - `rhythm.skip_patterns` 去掉与 `system_text.patterns` 重复的规则
- 兼容区间改为 MaiBot 1.3.19 ~ 1.3.99 / SDK 2.9.0 ~ 2.21.99

### v0.6.0
- 新增 `inject` 的 Planner 侧注入（`planner_template`）

### v0.5.0
- 新增 `learn` 学习守卫

---

## 许可

MIT
