import random
import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

# 命中这些片段的回复不发给用户（模型/网关报错原文）
PATTERNS = [
    "LLM 响应错误",
    "All chat models failed",
    "InternalServerError",
    "Internal Server Error",
    "Error code: 500",
    "Error code: 502",
    "Error code: 503",
    "Traceback",
    "openai.",
]

# 报错时随机回的「兜底话」，让它像真人卡了一下，而不是暴露技术细节
FALLBACKS = ["刚走神了 你再说一遍", "啊 刚没看清", "等等 刚卡了一下", "啥 再说一遍"]


def _text_of(result) -> str:
    parts = []
    for comp in result.chain:
        if isinstance(comp, Plain):
            parts.append(comp.text or "")
    return "".join(parts)


@register(
    "mask_llm_error",
    "local",
    "拦截模型/网关报错原文，换成一句人话再发出去",
    "1.0.1",
)
class MaskLlmError(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.on_decorating_result()
    async def mask_error(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is None or not result.chain:
            return

        text = _text_of(result)
        if not text:
            return

        hit = any(p.lower() in text.lower() for p in PATTERNS)
        # 长报错里常带换行和 stack，正则再兜一层
        if not hit:
            hit = bool(re.search(r"(?i)(error code|traceback|failed:|503|502)", text))

        if not hit:
            return

        # 换成人话，别把报错发出去；换新列表，别动共享的那条链
        result.chain = [Plain(text=random.choice(FALLBACKS))]
