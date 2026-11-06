# -*- coding: utf-8 -*-
"""mai输出拟人强化（Alcpare.chat_guard）— 让麦麦说话更像人。

背景（实测数据）：
    同一个人连发两条的间隔中位数 —— 小萤 0.28 秒 ✗ / 真人 5.00 秒 ✓ / 其他 AI 3.00 秒。
    她 0.27 秒就接上第二条，这是"最物理、最难辩解的 AI 味"。

根因（读框架源码得到，不是猜的）：
    src/chat/utils/utils.py:686   calculate_typing_time()  中文 0.3s/字，算得贴合真人 ✓
    src/services/send_service.py:671   if typing:  → 677 calculate_typing_time() → 682 sleep()
    src/services/send_service.py:864   typing = _coerce_bool(before_kwargs.get("typing"), typing)
    → 框架**本来就有**打字延迟，但默认 typing=False（回复链路的默认值），所以永不生效。

本插件的做法（不睡、不改核心、复用框架公式）：
    在 send_service.before_send 钩子里，把 typing 改成 True —— 之后框架自己会按
    「中文 0.3 秒/字 × response_post_process.typing_speed」计算并 sleep。
    → 不用在钩子里 sleep（钩子超时只有 5 秒 ✗），也不会被升级覆盖（插件目录不在覆盖范围 ✓）。

默认 action = "log"：只记录"本来应该等多久"，不改任何行为。观察满意后再改成 "force"。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# ── 常量 ──────────────────────────────────────────────────────────────

_CHINESE_TIME = 0.3
"""框架 calculate_typing_time 的中文单字时长（秒），仅用于日志估算。"""

_ENGLISH_TIME = 0.15
"""框架的英文单字时长（秒），仅用于日志估算。"""

_CJK_OR_LETTER = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
"""用来判断"这条消息里到底有没有可打字的文字"（纯表情/纯图 → 交给框架处理）。"""

_INJECT_DEDUPE_TTL = 30.0
"""注入去重记录的有效期（秒）；同一轮在此时限内只注入一次。"""

_MSG_ID_PATTERNS = [
    re.compile(r'msg_id="([^"]+)"'),
    re.compile(r"\[msg_id:(\d+)\]"),
]
"""从上下文文本里提取消息 ID 的两种格式（planner 与 replyer 各一种）。"""

_DEFAULT_SKIP = "\n".join(
    [
        "你没有权限使用此命令",
        "已清空.*Maisaka 历史上下文",
        "我暂时失忆了",
        "^/",
    ]
)
"""默认跳过的文本（正则，每行一条）。这些是系统回执/命令，不该套打字延迟。"""

_DEFAULT_SYSTEM_TEXTS = "\n".join(
    [
        "^我暂时失忆了$",
        "^已清空.*Maisaka 历史上下文",
    ]
)
"""框架硬编码、且【没有开关】的系统文案（P2'）。

来源：src/chat/message_receive/bot.py:509 与 /clear 回执 —— 它们冒用她的账号发进群里，
并且 storage_message=False（不入库），她自己也"不记得说过"。
（"你没有权限使用此命令。" 已有官方开关 silent_permission_denied ✓，不需要这里管。）
"""

_DEFAULT_DUEL_PATTERNS = "\n".join(
    [
        "# 怼人句式（regex）：命中即为一条怼",
        "你这是[^。！？]{0,12}还是",
        "对得起",
        "装得|挺像那么回事",
        "算什么意思|算什么|算哪门子",
        "你是真没记住|记不住|没记住",
        "你倒是|你这也|你也就|你就这",
        "^那你|^不也|^难道|^你才|那你[^。！？]{0,10}$",
        "你以为|谁让你|怪谁|都怪",
        "就这[？?！!。]?$",
        "还不如|倒不如",
        "少来|免了|没劲|出息|不认",
        "别[^。！？]{0,6}(?:了|吧)[？?]?$",
    ]
)
"""怼人检测句式（P8 兜底）。实测：她单条不多，但一进互怼模式就连续 5 条 ✗。"""


_DEFAULT_VARY_MARKS = "\n".join(
    [
        "# A 白名单形态：风格描述里出现这些「可变特征」→ 允许入库",
        "用",
        "直接",
        "换",
        "每次",
        "可以",
        "先",
        "再",
        "按",
        "提出",
        "回应",
        "接梗",
        "复述",
        "引用",
        "堆叠",
        "反问",
        "换个说法",
        "说法",
        "几种",
        "多个",
        "X",
        "N",
        "…",
    ]
)
"""A 白名单形态的可变特征词。

为什么用白名单而不是黑名单：
  2026-10-17 实测 —— 黑名单永远在追（禁了「入伙」→ 出现「投名状」✗）。
  但口癖的**形态**是稳定的：它一定是"一句写死的原话"（每次复用同一句）。
  所以只放"可变的表达"入库，新口癖在结构上就进不来。
"""

_DEFAULT_GUARD_PATTERNS = "\n".join(
    [
        "# 守卫 1：固定尾缀 / 行为模板（虽含方向词，输出仍是固定的一句）",
        "句尾加|结尾加|都以.*结尾",
        "先复述对方|复述对方原句|复述对方原话",
        "# 守卫 2：确认过的标志词（黑名单降级为补充，只留 0 误伤的）",
        "属于是",
        "投名状|了却|何妨|承让|讨教",
        "# 守卫 3：纯引号原话",
        "^[\"\u201c].{0,18}[\"\u201d]$",
    ]
)
"""A 的守卫规则：这两类虽然不是"纯原话"，但一样是固定输出。"""


def judge_learned_style(style: str, situation: str, vary_marks: list, guards: list) -> Tuple[bool, str]:
    """A 白名单形态判定：允许入库？返回 (是否允许, 原因)。

    只回答一个问题：**这条表达是可变的，还是每次都用同一句？**
    """

    text = f"{situation} {style}".strip()
    if not text:
        return False, "空内容"
    for guard in guards:
        if guard.search(text):
            return False, f"固定输出形态（{guard.pattern[:14]}）"
    for mark in vary_marks:
        if mark in text:
            return True, f"可变（含「{mark}」）"
    if len(style.strip()) <= 4:
        return False, f"写死的短词（{style.strip()[:8]}）"
    return False, "写死的原话（无方向、无占位）"
_DEFAULT_LITERARY_WORDS = "\n".join(
    [
        "投名状",
        "江湖",
        "恩怨",
        "交手",
        "了却",
        "何妨",
        "承让",
        "讨教",
        "搁浅",
        "怎么看都像",
        "这套",
        "那套",
        "招式",
        "路数",
        "章法",
        "手法",
        "与其说",
        "竟有种",
        "不免",
        "反倒",
        "谈不上",
    ]
)
"""书面/文学腔词表（P3-B 检测用）。

现实来源：2026-10-16 两次实测 —— "我这儿不收投名状"（江湖语域）、
"怎么看都像条搁浅的大带鱼"（planner 的书面句式漏进了她的回复）。
提示词里已经写了规则，这里是**监控**：命中就记日志，用来验证规则到底管不管用。
"""


# ── 配置模型 ──────────────────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    """基础设置：插件开关和配置版本。"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "settings"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="总开关。关掉后所有模块都不工作",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置结构版本，用于升级时自动迁移。不要手改",
        json_schema_extra={"label": "配置版本（勿改）"},
    )


class RhythmSectionConfig(PluginConfigBase):
    """发送节奏：让她「打一会儿字」再发，别秒回。

    只对 2~24 字的消息生效。太短的（「在」「嗯」）本来就该立刻发，
    太长的宿主自己会等，再叠加就让人等太久。
    """

    __ui_label__ = "发送节奏"
    __ui_icon__ = "clock"
    __ui_order__ = 2

    enabled: bool = Field(
        default=True,
        description="是否启用发送节奏控制",
        json_schema_extra={"label": "启用"},
    )
    max_chars: int = Field(
        default=24,
        ge=4,
        le=120,
        description="超过这个字数的消息不强制打字延迟（避免等太久：24 字 ≈ 7 秒）",
        json_schema_extra={"label": "字数上限", "x-widget": "slider", "step": 1, "hint": "单条消息超过这个字数就不套打字延迟"},
    )
    min_chars: int = Field(
        default=2,
        ge=1,
        le=20,
        description="短于这个字数的消息不强制（一两个字本来就该立刻发出）",
        json_schema_extra={"label": "字数下限", "x-widget": "slider", "step": 1, "hint": "短于这个字数的消息立刻发出，不等待"},
    )
    skip_patterns: str = Field(
        default=_DEFAULT_SKIP,
        description="命中的消息不套打字延迟（正则，每行一条）。默认已排除斜杠命令和系统回执，没有特殊需要不用改",
        json_schema_extra={"label": "跳过的消息（正则，每行一条）", "x-widget": "textarea"},
    )


class SystemTextSectionConfig(PluginConfigBase):
    """系统文案拦截：挡住框架提示语被当成她说的话发出去。

    像「我暂时失忆了」「已清空 Maisaka 历史上下文」这种，
    是框架内部的东西，出现在聊天里一看就穿帮。
    """

    __ui_label__ = "系统文案拦截"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    enabled: bool = Field(
        default=True,
        description="是否启用系统文案拦截",
        json_schema_extra={"label": "启用"},
    )
    patterns: str = Field(
        default=_DEFAULT_SYSTEM_TEXTS,
        description="要拦截的系统话术（正则，每行一条）。这些是框架硬编码的提示语，正常不该出现在聊天里",
        json_schema_extra={"label": "拦截规则（正则，每行一条）", "x-widget": "textarea"},
    )


class LearnSectionConfig(PluginConfigBase):
    """学习守卫：防止她把固定台词学进表达库。

    她有个表达学习机制，会把群里的说法学进去复用。但它有时候会把
    「句尾加个感叹号」这种死板模板也学进去，之后每句都套同一个尾巴。

    两道闸：白名单（有可变特征就放行）+ 黑名单（固定输出就拦掉）。
    """

    __ui_label__ = "学习守卫"
    __ui_icon__ = "filter"
    __ui_order__ = 4

    enabled: bool = Field(
        default=True,
        description="是否启用入库闸门（防止她把固定台词学进表达库）",
        json_schema_extra={"label": "启用"},
    )
    vary_marks: str = Field(
        default=_DEFAULT_VARY_MARKS,
        description="白名单：描述里出现这些「可变特征」就放行。比如有「用」「换」「每次」说明是可复用的表达，不是死板模板",
        json_schema_extra={"label": "可变特征词（白名单，每行一条）", "x-widget": "textarea"},
    )
    guard_patterns: str = Field(
        default=_DEFAULT_GUARD_PATTERNS,
        description="黑名单：命中就不入库。拦的是固定尾缀、行为模板、纯引号原话这类「每句都一样」的东西",
        json_schema_extra={"label": "拦截规则（正则，每行一条）", "x-widget": "textarea"},
    )

    def vary_marks_list(self) -> list:
        """把多行特征词解析成列表。"""

        return parse_words(self.vary_marks)


class InjectSectionConfig(PluginConfigBase):
    """回复规则：每轮往提示词里写规则，管她这一轮该怎么说。

    两个模板分别管两个阶段：
    · template 给 Replyer（写具体回复的模型）
    · planner_template 给 Planner（决定回不回、怎么回的模型）

    只改一边没用——Planner 定方向、Replyer 写话，方向错了话再好也白搭。
    """

    __ui_label__ = "回复规则"
    __ui_icon__ = "message"
    __ui_order__ = 1

    enabled: bool = Field(
        default=True,
        description="是否启用每轮注入",
        json_schema_extra={"label": "启用"},
    )

    # ── 给 Replyer 的规则 ──────────────────────────────
    template: str = Field(
        default=(
            "[这一轮怎么说话]\n"
            "像在微信上随手打字，不像写文章、不像台词。\n"
            "\n"
            "· 短。一两句就够，别铺开。\n"
            "· 大白话。把要说的事直接说出来，别绕着说。\n"
            "· 别照抄参考：\n"
            "  【回复信息参考】只是告诉你要说什么事，不是让你抄句子。\n"
            "  换成你自己的说法再开口，别沿用里面的词和句式。\n"
            "· 别跟着入戏：群里有人在演、在编段子、在写剧本时，\n"
            "  你还是用平时说话的方式接，不要跟着换成戏里的腔调。\n"
            "· 总判据：把这句话念一遍——\n"
            "  像【电视剧台词 / 作文】就改写；像【微信里随手打的】才对。\n"
            "\n"
            "[别用这些词]\n"
            "{words}"
        ),
        description="给 Replyer 的规则：管她这一轮该怎么说话。{words} 会替换成下面的用词黑名单",
        json_schema_extra={"label": "① 回复规则（{words} 会替换成词表）", "x-widget": "textarea"},
    )
    words: str = Field(
        default=_DEFAULT_LITERARY_WORDS,
        description="用词黑名单（每行一条）。会替换进上面规则的 {words}。加词就写在这，不用改模板",
        json_schema_extra={"label": "② 用词黑名单（每行一条）", "x-widget": "textarea"},
    )
    marker: str = Field(
        default="[这一轮怎么说话]",
        description="内部去重标记。上面规则的开头必须包含它，否则同一轮会重复注入——不要改",
        json_schema_extra={"label": "去重标记（勿改）", "hidden": True},
    )

    # ── 给 Planner 的规则 ──────────────────────────────
    planner_template: str = Field(
        default=(
            "[指引写法]\n"
            "你要给后面的模型写「这一轮该怎么回」的方向，不是写台词。\n"
            "\n"
            "格式：【对象】＋【点】｜【回复方向】｜【语气】\n"
            "\n"
            "【回复方向】只能选一个：\n"
            "接话 / 接梗 / 给结论 / 平淡接 / 拒绝 / 不接 / 撩\n"
            "\n"
            "【点】要具体：某个词、某个细节，不超过 6 个字。\n"
            "【语气】用一两个词：随口 / 嘴硬 / 平淡 / 好奇\n"
            "\n"
            "写指引时：\n"
            "· 只写方向和点，别写成一整句打磨过的台词。\n"
            "  错：「图里那只趴着不动，怎么看都不像可爱」← 这是台词\n"
            "  对：「拆台｜平淡接｜随口」← 这是方向\n"
            "· 别用书面语和文学腔（投名状、江湖、了却、怎么看都像、与其说）。\n"
            "· 场景再「戏」也让台词说人话——像在宿舍里随口聊天。\n"
            "\n"
            "示例（只示范格式，内容别照抄）：\n"
            "✓ 【对象】A｜【点】某个词｜【回复方向】玩笑接｜【语气】随口\n"
            "✓ 【对象】B｜【点】某个细节｜【回复方向】撩｜【语气】嘴硬"
        ),
        description="给 Planner 的规则：管它该怎么写「回复指引」。只写方向，不要写成能直接发的整句台词",
        json_schema_extra={"label": "③ Planner 指引写法", "x-widget": "textarea"},
    )
    planner_marker: str = Field(
        default="[指引写法]",
        description="内部去重标记。Planner 规则的开头必须包含它——不要改",
        json_schema_extra={"label": "Planner 去重标记（勿改）", "hidden": True},
    )

    def word_list(self) -> list:
        """词表。"""

        return parse_words(self.words)


class ChatGuardConfig(PluginConfigBase):
    """mai输出拟人强化 —— 配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    rhythm: RhythmSectionConfig = Field(default_factory=RhythmSectionConfig)
    system_text: SystemTextSectionConfig = Field(default_factory=SystemTextSectionConfig)
    learn: LearnSectionConfig = Field(default_factory=LearnSectionConfig)
    inject: InjectSectionConfig = Field(default_factory=InjectSectionConfig)


# ── 纯函数（可离线测试）────────────────────────────────────────────────


def extract_text(message: Any) -> str:
    """从钩子给的序列化消息里尽量取出"要发出去的文本"。"""

    if not isinstance(message, dict):
        return ""
    for key in ("processed_plain_text", "plain_text", "text"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw = message.get("raw_message")
    if isinstance(raw, dict):
        parts = raw.get("components") or raw.get("message") or []
        if isinstance(parts, list):
            chunks = []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    data = part.get("data")
                    if isinstance(data, str):
                        chunks.append(data)
            if chunks:
                return " ".join(chunks).strip()
    return ""


def typing_seconds(text: str, typing_speed: float = 1.0) -> float:
    """按框架同一套公式估算打字时间（只用于日志，不用于真正等待）。"""

    total = 0.0
    for ch in text:
        total += _CHINESE_TIME if "\u4e00" <= ch <= "\u9fff" else _ENGLISH_TIME
    if typing_speed <= 0:
        return 0.0
    return total * typing_speed


def parse_patterns(raw: str) -> list:
    """把多行正则文本解析成编译好的正则列表；坏行跳过但不影响其它行。"""

    out = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(re.compile(line))
        except re.error:
            continue
    return out


def match_words(text: str, words: list) -> list:
    """返回文本里命中的词（P3-B 监控用）。"""

    return [w for w in words if w and w in text]


def parse_words(raw: str) -> list:
    """把多行词表解析成列表（跳空行与 # 注释）。"""

    out = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def decide(
    text: str,
    typing_now: bool,
    *,
    enabled: bool,
    max_chars: int,
    min_chars: int,
    patterns: list,
) -> Tuple[bool, str]:
    """判断这条消息要不要强制 typing=True。

    返回 ``(是否强制, 原因)``；原因是给日志看的，便于核对。
    """

    if not enabled:
        return False, "模块未启用"
    if typing_now:
        return False, "框架已经在打字（typing 本来就是 True）"
    stripped = (text or "").strip()
    if not stripped:
        return False, "取不到文本"
    if not _CJK_OR_LETTER.search(stripped):
        return False, "没有可打字的文字（表情/图片，交给框架）"
    if len(stripped) < max(int(min_chars), 0):
        return False, f"太短（{len(stripped)} 字）"
    if len(stripped) > max(int(max_chars), 1):
        return False, f"太长（{len(stripped)} 字 > {max_chars}）"
    for pattern in patterns:
        if pattern.search(stripped):
            return False, f"命中跳过规则（{pattern.pattern[:24]}）"
    return True, f"{len(stripped)} 字"


# ── 插件主体 ──────────────────────────────────────────────────────────


class ChatGuardPlugin(MaiBotPlugin):
    """mai输出拟人强化：让她的回复像人在打字。"""

    config_model = ChatGuardConfig

    def __init__(self) -> None:
        super().__init__()
        self._patterns: list = []
        self._sys_patterns: list = []
        self._guard_patterns: list = []
        self._strict_patterns: list = []
        self._typing_speed = 1.0
        self._decided = 0
        self._forced = 0
        self._sys_seen = 0
        self._learn_seen = 0
        self._learn_blocked = 0
        self._inject_seen = 0
        self._inject_hits = 0
        self._injected_keys: Dict[str, float] = {}
        self._inject_planner = 0

    async def on_load(self) -> None:
        """载入配置并编译各模块的规则。"""

        self._refresh()
        self.ctx.logger.info(
            f"[拟人强化] 已加载：打字节奏 {self.config.rhythm.min_chars}~{self.config.rhythm.max_chars} 字 "
            f"| 系统文案拦截 {len(self._sys_patterns)} 条 "
            f"| 学习守卫 {len(self._guard_patterns)} 条 "
            f"| 用词黑名单 {len(self.config.inject.word_list())} 词 "
            f"| typing_speed={self._typing_speed}"
        )

    async def on_config_update(self, *args: Any, **kwargs: Any) -> None:
        """配置热更新（平台会传 old/new 等参数 → 用 *args/**kwargs 兜住，否则报 takes 1 positional argument but 4 were given）。"""

        try:
            if args and isinstance(args[0], dict):
                pass  # 平台可能传旧配置；我们只读 self.config，无需处理
        except Exception:  # noqa: BLE001
            pass
        self._refresh()
        self.ctx.logger.info(
            f"[拟人强化] 配置已刷新：打字节奏 {self.config.rhythm.min_chars}~{self.config.rhythm.max_chars} 字 "
            f"| 系统文案拦截 {len(self._sys_patterns)} 条 "
            f"| 学习守卫 {len(self._guard_patterns)} 条 "
            f"| 用词黑名单 {len(self.config.inject.word_list())} 词"
        )

    async def on_unload(self) -> None:
        """卸载/重载时输出统计，便于核对各模块到底盯到了什么。"""

        self.ctx.logger.info(
            f"[拟人强化] 已卸载：判断 {self._decided} 条 · 强制延迟 {self._forced} 条 · "
            f"系统文案 {self._sys_seen} 条"
        )

    def _refresh(self) -> None:
        cfg = self.config
        self._patterns = parse_patterns(cfg.rhythm.skip_patterns)
        self._sys_patterns = parse_patterns(cfg.system_text.patterns)
        self._guard_patterns = parse_patterns(cfg.learn.guard_patterns)
        try:
            from src.config.config import global_config  # type: ignore

            self._typing_speed = float(global_config.response_post_process.typing_speed)
        except Exception:  # noqa: BLE001
            self._typing_speed = 1.0

    # ── 钩子 ──────────────────────────────────────────────────────────

    @HookHandler(
        "send_service.before_send",
        name="chat_guard_before_send",
        description="发送前：系统文案拦截 / 重复防抖 / 文学腔监控 / 打字节奏",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_before_send(self, **kwargs: Any) -> Dict[str, Any]:
        """发送前处理：系统文案拦截 + 打字节奏。

        顺序：先拦（系统话术不该发出去），再改 typing。
        不 sleep、不改文本、只在必要时改 ``typing`` 或中止本次发送。
        """

        base: Dict[str, Any] = {"action": "continue", "modified_kwargs": kwargs}
        try:
            if not self.config.plugin.enabled:
                return base

            message = kwargs.get("message")
            text = extract_text(message)
            self._decided += 1

            # ① 系统文案拦截：框架提示语不该出现在聊天里
            cfg_sys = self.config.system_text
            if cfg_sys.enabled and text:
                for pattern in self._sys_patterns:
                    if not pattern.search(text):
                        continue
                    self._sys_seen += 1
                    self.ctx.logger.info(f"[拟人强化] 已拦截系统文案：{text[:34]}")
                    return {"action": "abort"}

            # ② 打字节奏：让短回复也「打一会儿字」
            cfg_rh = self.config.rhythm
            typing_now = bool(kwargs.get("typing"))
            force, why = decide(
                text,
                typing_now,
                enabled=cfg_rh.enabled,
                max_chars=cfg_rh.max_chars,
                min_chars=cfg_rh.min_chars,
                patterns=self._patterns,
            )
            if not force:
                self.ctx.logger.debug(f"[拟人强化] 不改（{why}）：{text[:30]}")
                return base

            seconds = typing_seconds(text, self._typing_speed)
            kwargs["typing"] = True
            self._forced += 1
            self.ctx.logger.info(f"[拟人强化] 已启用打字延迟 {seconds:.1f} 秒（{why}）：{text[:30]}")
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"[拟人强化] 发送前处理失败，已跳过：{type(exc).__name__} {exc}")
            return base


    # ── P4 入库闸门 ───────────────────────────────────────────────────

    @HookHandler(
        "expression.learn.before_upsert",
        name="chat_guard_expression_guard",
        description="P4：表达方式入池之前判定——一级硬拦、二级放行但重点审核",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_before_upsert(self, **kwargs: Any) -> Dict[str, Any]:
        """A 白名单形态：只有"可变的表达"才允许入库。"""

        base: Dict[str, Any] = {"action": "continue", "modified_kwargs": kwargs}
        try:
            cfg = self.config.learn
            if not (self.config.plugin.enabled and cfg.enabled):
                return base
            situation = str(kwargs.get("situation") or "")
            style = str(kwargs.get("style") or "")
            self._learn_seen += 1

            allow, why = judge_learned_style(style, situation, cfg.vary_marks_list(), self._guard_patterns)
            if allow:
                self.ctx.logger.debug(f"[拟人强化] 允许入库（{why}）：{style[:34]}")
                return base
            self._learn_blocked += 1
            self.ctx.logger.info(f"[拟人强化] 已拦下入库（{why}）：{style[:40]}")
            return {"action": "abort"}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"[拟人强化] 入库判定失败，已放行：{type(exc).__name__} {exc}")
            return base


    # ── P3-A 用词注入（回复层）────────────────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="chat_guard_word_inject",
        description="P3-A：在回复器请求前注入一句短的用词提醒（替掉常驻的长规则）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_before_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """注入一句短提示 + 词表；默认只记录（action=log）。"""

        base: Dict[str, Any] = {"action": "continue", "modified_kwargs": kwargs}
        try:
            cfg = self.config.inject
            if not (self.config.plugin.enabled and cfg.enabled):
                return base
            text = self._last_user_text(kwargs.get("items"))
            self._inject_seen += 1

            words = "|".join(cfg.word_list()[:18])
            if not words:
                return base
            note = cfg.template.replace("{words}", words)
            if self._note_present(kwargs.get("items"), cfg.marker):
                return base

            dedupe_key = self._dedupe_key("replyer", kwargs, cfg.marker)
            if self._is_duplicate(dedupe_key):
                self.ctx.logger.debug(f"[拟人强化][跳过/replyer] 本轮已注入过（{dedupe_key}）")
                return base

            self._inject_hits += 1

            target = self._append_note(kwargs, note, cfg.marker)
            if target is None:
                self.ctx.logger.warning("[拟人强化] 注入失败：钩子既无 items 也无 extra_prompt")
                return base
            self._mark_injected(dedupe_key)
            self.ctx.logger.info(f"[拟人强化] 已注入({target})：{note[:60]}")
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"[拟人强化] 注入失败，已跳过：{type(exc).__name__} {exc}")
            return base

    @HookHandler(
        "maisaka.planner.before_request",
        name="chat_guard_planner_inject",
        description="P3-A：在决策器请求前注入「指引怎么写」的规则（别写成可抄的句子）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_before_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """决策层注入：管"台词指引的写法"（回复层管不上这一层）。"""

        base: Dict[str, Any] = {"action": "continue", "modified_kwargs": kwargs}
        try:
            cfg = self.config.inject
            if not (self.config.plugin.enabled and cfg.enabled):
                return base
            note = cfg.planner_template
            if not note.strip():
                return base
            if self._note_present(kwargs.get("items"), cfg.planner_marker):
                return base

            dedupe_key = self._dedupe_key("planner", kwargs, cfg.planner_marker)
            if self._is_duplicate(dedupe_key):
                self.ctx.logger.debug(f"[拟人强化][跳过/planner] 本轮已注入过（{dedupe_key}）")
                return base

            self._inject_planner += 1
            target = self._append_note(kwargs, note, cfg.planner_marker)
            if target is None:
                return base
            self._mark_injected(dedupe_key)
            self.ctx.logger.info(f"[拟人强化] 已注入 planner({target})：{note[:56]}")
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning(f"[拟人强化] planner 注入失败，已跳过：{type(exc).__name__} {exc}")
            return base

    @staticmethod
    def _last_user_text(items: Any) -> str:
        """取 items 里最后一条含文字的文本（用于条件判断）。"""

        if not isinstance(items, list):
            return ""
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            for part in reversed(item.get("parts") or []):
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if text.strip():
                        return text
        return ""

    def _dedupe_key(self, stage: str, kwargs: dict, marker: str) -> str:
        """构造去重键：阶段 + 会话 + 最后一条消息 ID。"""

        session = str(kwargs.get("session_id") or "").strip()
        turn = ""
        items = kwargs.get("items")
        if isinstance(items, list):
            for item in reversed(items):
                if not isinstance(item, dict):
                    continue
                parts = item.get("parts") or []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    text = str(part.get("text") or "")
                    for pattern in _MSG_ID_PATTERNS:
                        m = pattern.search(text)
                        if m:
                            turn = m.group(1)
                            break
                    if turn:
                        break
                if turn:
                    break
        return f"{stage}|{session}|{turn or '-'}|{marker}"

    def _is_duplicate(self, key: str) -> bool:
        """检查同一个键是否在有效期内注入过。"""

        now = time.time()
        for old_key, ts in list(self._injected_keys.items()):
            if now - ts > _INJECT_DEDUPE_TTL:
                self._injected_keys.pop(old_key, None)
        return key in self._injected_keys

    def _mark_injected(self, key: str) -> None:
        """记录一次注入。"""

        self._injected_keys[key] = time.time()

    @staticmethod
    def _note_present(items: Any, marker: str) -> bool:
        """同一轮是否已经注入过（按标记词判断，避免重复注入）。"""

        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            for part in item.get("parts") or []:
                if isinstance(part, dict) and marker in str(part.get("text") or ""):
                    return True
        return False

    def _append_note(self, kwargs: dict, note: str, marker: str) -> Any:
        """照身份插件的做法：追加到最后一条 SystemMessageItem，没有就新建。"""

        items = kwargs.get("items")
        if isinstance(items, list):
            for item in reversed(items):
                if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem":
                    parts = item.get("parts")
                    if not isinstance(parts, list):
                        parts = []
                        item["parts"] = parts
                    parts.append({"type": "text", "text": note})
                    return "items/SystemMessageItem"
            items.insert(
                0,
                {
                    "item_type": "SystemMessageItem",
                    "meta": {
                        "item_id": f"Alcpare-chatguard-{marker}-{int(time.time())}",
                        "logical_turn_id": None,
                        "timestamp": __import__("datetime").datetime.now().astimezone().isoformat(),
                    },
                    "parts": [{"type": "text", "text": note}],
                },
            )
            return "items/SystemMessageItem(新建)"
        if "extra_prompt" in kwargs:
            kwargs["extra_prompt"] = (kwargs.get("extra_prompt") or "") + note
            return "extra_prompt"
        return None


def create_plugin() -> ChatGuardPlugin:
    """插件入口。"""

    return ChatGuardPlugin()
