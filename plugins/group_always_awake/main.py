from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register

# 需要「整群免 @ 唤醒」的群号，想加群直接往这里追加
TARGET_GROUPS = {"123456789"}


class TargetGroupFilter(CustomFilter):
    """只对目标群放行。

    这个 handler 一旦通过 filter，waking_check 阶段就会把消息标记为
    is_wake=True，于是该群里的普通聊天不再需要 @ 机器人或加前缀。
    """

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        gid = event.get_group_id()
        return bool(gid) and str(gid) in TARGET_GROUPS


@register("group_always_awake", "local", "让指定群聊里的消息无需 @ 也能唤醒机器人", "1.0.0")
class GroupAlwaysAwake(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

    @filter.custom_filter(TargetGroupFilter)
    async def keep_awake(self, event: AstrMessageEvent):
        # 不发消息、不 stop_event，只负责让 pipeline 认为该消息已被唤醒，
        # 真正的回复交给后续 LLM 流程。
        return
