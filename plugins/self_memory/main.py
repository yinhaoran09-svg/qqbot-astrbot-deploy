"""把她自己说出去的话记回会话历史。

问题出在哪：
  AstrBot 的 send_message() 只负责把消息推出去，不往 conversations 表里写。
  所以你主动找别人说话这件事，从对话历史看等于从没发生过 —— 下一轮 LLM
  读到的是"用户在自言自语"，别人问"你刚给我发了啥"就答不上来。

两种来源：
  1. 插件主动推送（bili_daily）—— 那边自己也会写，这里负责兜底去重
  2. 主人手动用同一个号发的 —— OneBot 的 message_sent 事件

第 2 种的坑：AstrBot 的 aiocqhttp 适配器只处理 post_type 为 message/notice/request
的事件，message_sent 会被直接丢掉（convert_message 返回 None）。所以这里直接挂到
aiocqhttp 底层的事件总线（bot._bus），绕过适配器过滤。
前提：NapCat 的 onebot11_*.json 里 reportSelfMessage 必须为 true。

第二个坑（踩过）：AstrBot 开了分段回复，一条回复会被拆成好几段、间隔 0.6~1.4 秒
依次发出。每段都触发一次 message_sent，如果来一条记一条，历史里就会塞满"行 那我先
撤会儿" / "你听着" 这种碎片，和完整的 assistant 回复并存，上下文直接变垃圾。

所以这里做两件事：
  a. 攒 10 秒再合并（debounce），把同一轮分段回复拼回一条
  b. 合并后先查历史：如果最近的 assistant 消息里已经包含这段话，说明是 AstrBot
     自己发的、已经记过了，跳过。用户手动发的不会出现在历史里，才会被记下。
"""
import asyncio
import json
import re

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

_LOCK = asyncio.Lock()

FLUSH_DELAY = 10.0     # 等分段发完再合并（AstrBot 分段间隔 0.6~1.4s，10 秒足够）
NEEDLE = 20            # 取前 20 个字做包含判断，太短容易误判


def extract_text(msg):
    """从 OneBot 的 message 段数组里抠出纯文字。"""
    if isinstance(msg, str):
        return msg.strip()
    if not isinstance(msg, list):
        return ""
    out = []
    for seg in msg:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            out.append((seg.get("data") or {}).get("text", ""))
    return "".join(out).strip()


def norm(s):
    """去掉所有空白再比，因为分段拼接和原文的空格位置会对不上。"""
    return re.sub(r"\s+", "", str(s or ""))


@register("self_memory", "local", "把她自己发出去的话记回会话", "1.1.0")
class SelfMemory(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.platform_id = "qq-personal"
        self._buf = {}          # umo -> {"parts": [...], "task": Task}
        self._task = asyncio.create_task(self._attach())

    async def _attach(self):
        """插件比平台先加载，所以要轮询等平台实例起来再挂事件。"""
        for _ in range(90):
            try:
                for p in self.context.platform_manager.platform_insts:
                    getter = getattr(p, "get_client", None)
                    if not getter:
                        continue
                    bot = getter()
                    bus = getattr(bot, "_bus", None)
                    if bus is None:
                        continue
                    bus.subscribe("message_sent", self._on_sent)
                    try:
                        self.platform_id = p.meta().id
                    except Exception:
                        pass
                    logger.info(
                        f"[self_memory] 已挂上 {self.platform_id} 的 message_sent 事件")
                    return
            except Exception as e:
                logger.warning(f"[self_memory] 挂载重试: {str(e)[:80]}")
            await asyncio.sleep(2)
        logger.warning("[self_memory] 等不到平台实例，message_sent 没挂上")

    def _umo(self, ev):
        mt = ev.get("message_type") or ev.get("message_sent_type")
        if mt == "group":
            gid = ev.get("group_id")
            return f"{self.platform_id}:GroupMessage:{gid}" if gid else None
        target = ev.get("target_id") or ev.get("user_id")
        return f"{self.platform_id}:FriendMessage:{target}" if target else None

    # ---- 接收：先攒着，等分段发完 ----
    async def _on_sent(self, ev):
        try:
            text = extract_text(ev.get("message"))
            if not text:
                return
            umo = self._umo(ev)
            if not umo:
                return
            slot = self._buf.setdefault(umo, {"parts": [], "task": None})
            slot["parts"].append(text)
            # 每来一段就重置计时，直到安静 10 秒
            if slot["task"] and not slot["task"].done():
                slot["task"].cancel()
            slot["task"] = asyncio.create_task(self._flush(umo))
        except Exception as e:
            logger.warning(f"[self_memory] 收 message_sent 失败: {str(e)[:120]}")

    async def _flush(self, umo):
        try:
            await asyncio.sleep(FLUSH_DELAY)
        except asyncio.CancelledError:
            return
        slot = self._buf.pop(umo, None)
        if not slot or not slot["parts"]:
            return
        merged = " ".join(slot["parts"]).strip()
        if not merged:
            return
        try:
            if await self._in_history(umo, merged):
                return      # AstrBot 自己发的，历史里已经有了
            if await self._remember(umo, merged):
                logger.info(f"[self_memory] 记下自发消息 → {umo.split(':')[-1]} "
                            f"「{merged[:40]}」")
        except Exception as e:
            logger.warning(f"[self_memory] 落库失败: {str(e)[:120]}")

    # ---- 判断：是不是 AstrBot 已经记过的话 ----
    async def _in_history(self, umo, text):
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return False
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            return False
        conv = await cm.get_conversation(umo, cid)
        hist = json.loads(conv.history) if conv and conv.history else []
        target = norm(text)[:NEEDLE]
        if not target:
            return False
        for prev in reversed(hist[-6:]):
            if prev.get("role") != "assistant":
                continue
            c = prev.get("content")
            if not isinstance(c, list):
                continue
            body = norm("".join(str(x.get("text", "")) for x in c
                                if isinstance(x, dict)))
            if target in body:
                return True
        return False

    # ---- 写入 ----
    async def _remember(self, umo, text):
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return False
        async with _LOCK:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                cid = await cm.new_conversation(umo)
            conv = await cm.get_conversation(umo, cid)
            hist = json.loads(conv.history) if conv and conv.history else []
            # 完全重复就不写
            for prev in reversed(hist[-3:]):
                if prev.get("role") != "assistant":
                    continue
                c = prev.get("content")
                if isinstance(c, list) and c and \
                        str(c[0].get("text", "")).strip() == text.strip():
                    return False
            hist.append({
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            })
            await cm.update_conversation(umo, cid, history=hist)
        return True

    async def terminate(self):
        for slot in self._buf.values():
            if slot["task"] and not slot["task"].done():
                slot["task"].cancel()
        self._task.cancel()
        logger.info("[self_memory] 已停止")
