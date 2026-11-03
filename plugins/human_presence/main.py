"""human_presence —— 让机器人有「在场感」而不是「应答机」。

两件事：
1. 不是每条消息都秒回。按时间、消息内容决定：立刻回 / 拖一会儿 / 这一轮干脆不回。
   连续沉默太多次会自动恢复，避免看起来像掉线。
2. 没人理的时候会自己开口。有间隔闸门、时段限制、每日上限，不是定时播报。

配置都在下面几个常量里，改完重启 AstrBot 生效。
"""

import asyncio
import json
import math
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    Contact,
    Dice,
    Face,
    File,
    Forward,
    Image,
    Json,
    Location,
    Music,
    Nodes,
    Plain,
    Record,
    RPS,
    Share,
    Video,
)
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # 兜底，兼容不同小版本
    MessageChain = None


# ============ 配置 ============

# 生效的会话。可以写完整会话名，也可以只写 QQ/群号（自动拼成私聊会话）。
# 私聊 umo 形如 qq-personal:FriendMessage:1234567890，群聊形如 qq-personal:GroupMessage:987654321
# 填你自己的 QQ 号。这里的人按「主人」待遇：主动开口更频繁、语音只在情绪/深夜触发。
TARGETS = ["1234567890"]

# 平台适配器 id，和 cmd_config.json 里 platform[].id 一致
PLATFORM_ID = "qq-personal"

# 所有来私聊过的人，聊够几轮后自动纳入「可以被他主动找」的池子
TRACK_ALL_FRIENDS = True
MIN_ROUNDS_BEFORE_INITIATIVE = 2

# 深夜时段：这个区间里整体降低回复率
QUIET_HOURS = (1, 7)

# 连续沉默达到这个次数后强制回复，防止像掉线
MAX_SILENT_STREAK = 2

# 延迟回复的上限（秒）。超过这个值就不如直接沉默
MAX_DELAY = 35

# 打字速度（秒/字）和读消息的反应时间（秒，中位数）
# 真人手机上打字大概 5-8 字/秒，这里留一点思考余量
TYPE_PER_CHAR = 0.16
READ_MEDIAN = 3.2

# 延迟短于这个值就不折腾了，但要立刻发（仍然自己拆词发送）
IMMEDIATE_BELOW = 1.5

# ---- 分段：一条别太长 ----
# 单条消息的目标字数上限。超过就再拆，更像真人一句一句打
MAX_SEG_CHARS = 10
# 一轮最多发几条，超出的并到末尾，防止刷屏
MAX_SEGS = 4

# ---- 人物档案 ----
# 注入上下文时最多带多少条历史。库里全量保留，只是不整段塞进 prompt，
# 免得几百条旧对话把当前语气和人设冲淡。
MAX_CONTEXT_MSGS = 40
# 记住对方最近说过的几句话（用于「认人」）
KEEP_NOTES = 5

# ---- 主动对话 ----
INITIATIVE_ENABLED = True
INITIATIVE_WINDOW = (9, 23)   # 只在这个时段主动开口
INITIATIVE_GAP_MIN = 30       # 至少这么久没聊过才考虑主动（分钟）
INITIATIVE_DAILY_CAP = 8      # 每个人每天最多主动几次
INITIATIVE_GLOBAL_CAP = 12    # 全天所有人加起来的上限，防止一上线群发
INITIATIVE_TICK = 45          # 每隔多少秒检查一次
INITIATIVE_ODDS = 0.50        # 每次检查命中的概率，避免一满足条件就开口

# ---- 语音 ----
# 真人不每条都发语音。这里做的是「累加分数 + 掷骰子」：
# 分数越高越可能发，但永远不是必发。硬上限保证不会变成语音轰炸机。
VOICE_ENABLE = True
VOICE_VOICE = "zh-CN-XiaoxiaoNeural"   # 晓晓
VOICE_RATE = "+0%"                    # 语速交给后面的 ffmpeg 统一管，别叠加
VOICE_PITCH = "+0Hz"

# edge 免费接口只放行 5 个中文音色，御姐感的晓秋/晓睿都要 Azure 付费。
# 折中办法：晓晓出干声，再用 ffmpeg 降调 + 加厚低中频，把甜味压下去。
# 0.90 = 比原声低约两个半音（听感像 27-8 岁），0.86 更沉但开始有失真风险。
VOICE_PITCH_RATIO = 0.90              # 音高倍数，越小越沉
VOICE_SPEED = 1.12                    # 语速倍数，1.0 是正常，1.12 = 快一成二
VOICE_DIR = r"D:\qqbot\_voice"
VOICE_MAX_CHARS = 60                  # 超过就不转了（QQ 语音 60 秒上限，字数留足余量）
VOICE_DAILY_CAP = 8                   # 一天最多几条语音
VOICE_MIN_GAP_MIN = 20                # 两条语音之间至少隔多少分钟
VOICE_PICKUP = 0.80                   # 分数到概率的系数

# 好友和主人不是一回事：跟主人聊得深，语音是情绪出口；
# 跟好友日常唠嗑，语音更像「随口发一条」，门槛低、频率高。
VOICE_FRIEND_BASE = 0.20              # 好友私聊的底分，不靠任何条件也可能发
VOICE_FRIEND_PICKUP = 1.00            # 好友的分数→概率系数
VOICE_FRIEND_DAILY_CAP = 6            # 好友每人每天上限
VOICE_FRIEND_MIN_GAP_MIN = 15         # 好友两条语音之间至少隔多少分钟
VOICE_GROUP_ENABLE = False            # 群聊默认不发语音条，只在对方发过语音时对等回
VOICE_GLOBAL_CAP = 30                 # 全天所有人加起来的上限，防止好友一多就刷屏

# 深夜：躺床上懒得打字，语音比文字自然
VOICE_NIGHT = (23, 4)                 # 跨零点，23:00 - 03:59

# 情绪上来的时候，文字不够用。
# 只收「重情绪」词——「在呢」「咋了」这种她天天说的口头语不能算，
# 否则一半的回复都会被判成该发语音。
VOICE_EMOTION = (
    "累", "烦", "难受", "想哭", "emo", "难过", "委屈", "崩溃", "焦虑",
    "害怕", "心疼", "舍不得", "失眠", "睡不着", "头疼", "发烧", "生病",
    "想你", "抱抱", "陪你", "别怕", "晚安",
)

# 回复超过这个字数，真人基本懒得打字了
VOICE_LONG_CHARS = 26

# 4 个字以内不发语音（「嗯」「在呢」这种念出来反而怪），
# 除非对方刚发过语音——那种对等要保住。
VOICE_MIN_CHARS = 4

# ---- 听觉 ----
# 他能发语音过来，她得听得懂。本地跑 whisper，不花钱、不上传。
STT_ENABLE = True
STT_MODEL = (
    r"D:\qqbot\_models\whisper"
    r"\models--Systran--faster-whisper-small"
    r"\snapshots\536b0662742c02347bc0e980a01041f333bce120"
)
STT_LANG = "zh"
STT_BEAM = 5
STT_MAX_SEC = 120              # 超过这么长的语音不转（多半是转发的长录音）
STT_MIN_CHARS = 1              # 转出来少于这么多字就当没听清
# 注意：不要加 initial_prompt。
# 实测（2026-11-01）在 1-2 秒的超短语音上，whisper 会把 prompt 本身当成
# 待转写内容复读出来（"以下是两个大学生在QQ上随口聊天"），
# 原本能听对的"喝一杯"被带偏成"好 预备"。繁体问题下面用 opencc 单独解决。
# whisper 在静音/噪声上会瞎编这些，别当真
STT_JUNK = (
    "谢谢观看", "谢谢大家", "字幕由", "字幕提供", "订阅", "打赏",
    "请不吝点赞", "明镜与点点", "点赞订閲", "Amara",
)

SPLIT_RE = re.compile(r"[。？！~…\n]+")
SOFT_BREAK = "，、,；;：: \t"

# ---- 模型读不了的东西 ----
# 网关那边是纯文本模型，图片/表情包/语音塞过去会直接 500，
# 然后把「LLM 响应错误」糊到聊天框里。所以这些一律拦在模型之前。
#
# 拦下来之后怎么办：默认「不回」。对方只发了个表情包，就当没看见——
# 硬回一句「我看不了图」很像机器人，而且容易接在不合时宜的地方
# （人家刚说了句甜的，你回「你发的啥呀」）。
# 想让它开口的时候，把这行改成 True，会用下面 REPLIES 里的短句。
REPLY_WHEN_UNREADABLE = False

# 打开开关才会用到。每行不超过 10 个字，正好按「一句一条」发出去。
REPLIES = {
    "image": [
        "刚刚你发了啥\n我看不到表情包\n打字跟我说说",
        "别发表情包啦\n我这边刷不出来\n你跟我说说发了啥",
        "表情包我看不了\n你打字跟我说吧",
        "你发的啥呀\n我看不了图\n说说内容呗",
    ],
    "voice": [
        "语音我这边听不了\n你打字跟我说吧",
        "说啥了呀\n我这放不了语音\n打字讲讲",
    ],
    "other": [
        "这个我打不开\n你打字跟我说说",
        "发的是啥呀\n我这边显示不出来",
    ],
}

# 文字里夹着看不了的东西时，给模型一句提示。
# 关键是别让它主动提「我看不到」——那样很出戏，就着他打的字接话就行。
MEDIA_NOTES = {
    "image": "\n\n# 补充\n他这条还附了图片/表情包，你看不到里面的内容。"
    "别问「你发了啥」，也别提自己看不到，就着他打的字自然接话。",
    "voice": "\n\n# 补充\n他这条还带了语音，你听不到内容。"
    "别问「你说啥」，就着他打的字自然接话。",
    "other": "\n\n# 补充\n他这条还带了你看不了的东西（视频/文件/卡片）。"
    "别问「这是啥」，就着他打的字自然接话。",
}

VOICE_CLS = (Record,)
IMAGE_CLS = (Image, Face)
# 戳一戳、窗口抖动这种不算「打不开的东西」，不拦
OTHER_CLS = (Video, File, Forward, Nodes, Json, Share, Music, Contact, Location, Dice, RPS)

PEOPLE_FILE = Path(__file__).with_name("people.json")

# 繁简转换器，懒加载。whisper 时不时整句吐繁体，
# 直接喂给模型她会跟着说粤语腔的书面语，很出戏。
_CC = None


def _to_simplified(s: str) -> str:
    global _CC
    if _CC is None:
        try:
            from opencc import OpenCC

            _CC = OpenCC("t2s")
        except Exception:
            _CC = False
    if not _CC:
        return s
    try:
        return _CC.convert(s)
    except Exception:
        return s


def _normalize(session: str) -> str:
    """只写数字时，补成完整的私聊会话名。"""
    if ":" in session:
        return session
    return f"{PLATFORM_ID}:FriendMessage:{session}"


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)

# 括号里出现这些，基本是动作/表情说明，念出来没意义
_ACTION_WORDS = (
    "笑", "哭", "叹气", "捂脸", "皱眉", "汗", "沉默", "狗头", "doge", "思考",
    "擦汗", "捂嘴", "尖叫", "震惊", "无语", "挠头", "拍桌", "小声", "嘀咕",
    "白眼", "瘫", "发抖", "脸红", "叉腰", "点头", "摇头", "认真", "委屈巴巴",
    "小声bb", "划掉", "不是", "bu", "被打", "逃走", "冒头", "举手",
)


def _paren_repl(match: re.Match) -> str:
    """括号里的短说明：是动作就整段去掉，是真信息就只脱掉括号。"""
    inner = match.group(1).strip()
    if not inner:
        return ""
    if len(inner) <= 12 and any(w in inner for w in _ACTION_WORDS):
        return ""
    return inner


_PAREN_RE = re.compile(r"[（(【\[]([^）)】\]]{0,12})[）)】\]]")


def _sayable(text: str) -> str:
    """把回复收拾成「能念出来」的样子。

    她打的字里有括号动作、波浪号、颜文字，直接念会很怪：
    「（笑）」「好耶~」「……」「qwq」。念之前先洗一遍。
    """
    s = text or ""
    s = _PAREN_RE.sub(_paren_repl, s)
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"[~～]+", "，", s)
    s = re.sub(r"(?:\.{2,}|…+)", "，", s)
    s = re.sub(r"[*＊#＃]+", "", s)
    s = re.sub(r"[\s\n]+", " ", s).strip()
    s = re.sub(r"[，,、]{2,}", "，", s)
    return s.strip("，,。 “”\"'")


# ============ 工具 ============

def _text_of(result) -> str:
    parts = []
    for comp in result.chain:
        if isinstance(comp, Plain):
            parts.append(comp.text or "")
    return "".join(parts)


def _classify_media(messages) -> tuple[str | None, list]:
    """看看这条消息里有没有「模型读不了」的东西。

    返回 (类别, 这些组件)。类别是 voice / image / other，没有就 None。
    """
    voice, image, other = [], [], []
    for c in messages or []:
        if isinstance(c, VOICE_CLS):
            voice.append(c)
        elif isinstance(c, IMAGE_CLS):
            image.append(c)
        elif isinstance(c, OTHER_CLS):
            other.append(c)
    if voice:
        return "voice", voice + image + other
    if image:
        return "image", image + other
    if other:
        return "other", other
    return None, []


def _is_question(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    if s.endswith(("?", "？", "吗", "呢", "吧")):
        return True
    return any(k in s for k in ("什么", "为什么", "怎么", "哪", "谁", "多少", "几点", "要不要", "是不是"))


_jieba = None


def _get_jieba():
    """懒加载，避免拖慢启动。拿不到就返回 None，回退到等分切。"""
    global _jieba
    if _jieba is None:
        try:
            import jieba  # type: ignore

            _jieba = jieba
        except Exception:
            _jieba = False
    return _jieba or None


def _hard_split(p: str, maxlen: int) -> list[str]:
    """没有标点可断的长串，尽量断在词边界，别切出半个词。"""
    p = p.strip()
    if len(p) <= maxlen:
        return [p]

    jb = _get_jieba()
    out, cur = [], ""
    if jb is not None:
        try:
            words = [w for w in jb.cut(p) if w.strip()]
            total = len(p)
            k = max(1, math.ceil(total / maxlen))
            target = math.ceil(total / k)
            for w in words:
                if cur and (len(cur) + len(w) > maxlen or len(cur) >= target):
                    out.append(cur)
                    cur = w
                else:
                    cur += w
            if cur:
                out.append(cur)
        except Exception:
            out = []

    if not out:  # 没有分词器，退回等分
        k = math.ceil(len(p) / maxlen)
        size = math.ceil(len(p) / k)
        return [p[i:i + size] for i in range(0, len(p), size)]

    # 末段太短（"听懂""信号"这种）并回上一段，别单独甩两个字的尾巴
    if len(out) > 1 and len(out[-1]) <= 2:
        merged_tail = out[-2] + out[-1]
        if len(merged_tail) <= maxlen + 3:
            out = out[:-2] + [merged_tail]

    # 单个词就超长的，再等分
    final = []
    for seg in out:
        if len(seg) <= maxlen + 2:
            final.append(seg)
        else:
            k = math.ceil(len(seg) / maxlen)
            size = math.ceil(len(seg) / k)
            final += [seg[i:i + size] for i in range(0, len(seg), size)]
    return final


def _slice_long(seg: str) -> list[str]:
    """把一段超长的话再拆成几条，尽量断在逗号/空格处。"""
    seg = seg.strip()
    # 10 字是目标，12 字以内不折腾（「左右」的余地）
    soft = MAX_SEG_CHARS + 2
    if len(seg) <= soft:
        return [seg]

    # 先按软标点切成最小块
    pieces = [c for c in re.split(rf"(?<=[{SOFT_BREAK}])", seg) if c.strip()]
    # 贪心合并到接近上限但不超
    merged, cur = [], ""
    for c in pieces:
        if not cur:
            cur = c
        elif len(cur) + len(c) <= soft:
            cur += c
        else:
            merged.append(cur)
            cur = c
    if cur:
        merged.append(cur)

    # 还是太长的硬切
    out = []
    for p in merged:
        out.extend(_hard_split(p, MAX_SEG_CHARS))
    # 清掉段首尾残留的标点空格
    out = [re.sub(rf"^[{SOFT_BREAK}]+|[{SOFT_BREAK}]+$", "", x) for x in out]
    return [x for x in out if x] or [seg]


def _split(text: str) -> list[str]:
    """整条回复 -> 几条短消息。先按句末标点断，过长再按 10 字断。"""
    raw = [p.strip() for p in SPLIT_RE.split(text) if p.strip()]
    if not raw:
        raw = [text.strip()]
    out = []
    for seg in raw:
        out.extend(_slice_long(seg))
    # 段数太多就把尾巴并起来，别一次糊出去五六条
    if len(out) > MAX_SEGS:
        out = out[:MAX_SEGS - 1] + ["".join(out[MAX_SEGS - 1:])]
    return [p for p in out if p] or [text.strip()]


@register("human_presence", "local", "非秒回 + 会自己找话说，让机器人有在场感", "1.0.0")
class HumanPresence(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        # session -> 时间戳
        self._last_active: dict[str, float] = {}
        # session -> 沉默计数
        self._silent_streak: dict[str, int] = {}
        # session -> (日期, 当天主动次数)
        self._initiative_count: dict[str, tuple[str, int]] = {}
        # 正在延迟发送中的 session，避免主动对话插队
        self._busy: set[str] = set()
        self._task: asyncio.Task | None = None
        # 来聊过的人：完整会话名 -> 收到过多少条消息
        self._seen: dict[str, int] = {}
        self._targets = [_normalize(t) for t in TARGETS]

        # 每个人的档案：umo -> {qq,name,first,last,msgs,notes}
        self._people: dict[str, dict] = self._load_people()
        # 全天主动开口总次数 (日期, 次数)
        self._global_count: tuple[str, int] = (self._today(), 0)

        # ---- 语音状态 ----
        # session -> 最近一次发语音的时间戳
        self._voice_last: dict[str, float] = {}
        # session -> (日期, 当天语音条数)
        self._voice_count: dict[str, tuple[str, int]] = {}
        # session -> 对方最后一次发语音的时间戳（用于「他发语音我也发」）
        self._peer_voice: dict[str, float] = {}
        # 全天语音总条数 (日期, 次数) —— 好友多了要靠这个兜底
        self._voice_global: tuple[str, int] = (self._today(), 0)
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
        except Exception as e:
            logger.warning(f"[human_presence] 语音目录创建失败: {e}")

        # ---- 听觉（whisper 模型，懒加载单例）----
        self._asr = None
        self._asr_failed = False
        if STT_ENABLE:
            # 后台预热，别让第一句语音卡在模型加载上
            try:
                asyncio.get_running_loop().run_in_executor(None, self._get_asr)
            except Exception:
                pass

        # 启动时把固定目标和已知的人都视为「刚聊过」，
        # 不然空闲时长会算成无限大，一上线就给十几个人同时发消息。
        now = time.time()
        for t in self._targets:
            self._last_active[t] = now
        for umo in self._people:
            self._last_active.setdefault(umo, now)

        if INITIATIVE_ENABLED:
            self._task = asyncio.create_task(self._initiative_loop())
            logger.info(
                f"[human_presence] 主动对话已开启，固定目标 {self._targets}，"
                f"自动跟踪私聊={TRACK_ALL_FRIENDS}，已记录 {len(self._people)} 个人"
            )

    def _in_scope(self, umo: str) -> bool:
        if umo in self._targets:
            return True
        return TRACK_ALL_FRIENDS and "FriendMessage" in umo

    # ---------------- 人物档案：知道对面是谁 ----------------

    def _load_people(self) -> dict:
        try:
            if PEOPLE_FILE.exists():
                return json.loads(PEOPLE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[human_presence] 读取人物档案失败: {e}")
        return {}

    def _save_people(self):
        try:
            tmp = PEOPLE_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._people, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            os.replace(tmp, PEOPLE_FILE)
        except Exception as e:
            logger.warning(f"[human_presence] 保存人物档案失败: {e}")

    def _touch_person(self, event: AstrMessageEvent) -> dict:
        """更新「这个人是谁」的档案，返回档案。"""
        umo = event.unified_msg_origin
        now = time.time()
        try:
            qq = event.get_sender_id() or ""
        except Exception:
            qq = ""
        try:
            name = (event.get_sender_name() or "").strip()
        except Exception:
            name = ""

        p = self._people.get(umo) or {}
        if not p:
            p = {"qq": qq, "name": name, "first": now, "last": now, "msgs": 0, "notes": []}
        if qq and not p.get("qq"):
            p["qq"] = qq
        if name and name != p.get("name"):
            p["name"] = name
        p["last"] = now
        p["msgs"] = int(p.get("msgs", 0)) + 1

        # 记下对方说过的话，作为「他之前提过什么」的线索
        said = (event.message_str or "").strip().replace("\n", " ")
        if 4 <= len(said) <= 60:
            notes = p.get("notes") or []
            if said not in notes:
                notes.append(said)
            p["notes"] = notes[-KEEP_NOTES:]

        self._people[umo] = p
        return p

    def _person_block(self, umo: str) -> str:
        p = self._people.get(umo)
        if not p:
            return ""
        name = (p.get("name") or "").strip()
        qq = p.get("qq") or ""
        msgs = int(p.get("msgs", 0))
        try:
            first = datetime.fromtimestamp(p["first"]).strftime("%m月%d日")
        except Exception:
            first = "之前"
        notes = p.get("notes") or []

        lines = [
            "",
            "",
            "# 现在跟你说话的人",
        ]
        if name:
            lines.append(
                f"这个人在 QQ 上叫「{name}」" + (f"，QQ 号 {qq}。" if qq else "。")
            )
        elif qq:
            lines.append(f"这个人的 QQ 号是 {qq}，你还没记住他叫什么。")
        else:
            lines.append("这个人你还没记住叫什么。")
        lines.append(f"你们 {first} 就聊过，这条聊天框里他一共找过你 {msgs} 次。")
        lines.append("每个聊天框对应一个不同的人，别把这个人跟别人说过的事混到一起。")
        if notes:
            lines.append("他之前跟你说过这些，你心里有数就行，不用主动复述：")
            lines += [f"- {n}" for n in notes]
        lines.append("你看得出他熟不熟，但不用把这些信息念出来。")
        return "\n".join(lines)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def catch_unreadable(self, event: AstrMessageEvent):
        """图片 / 表情包 / 语音 / 视频这些，模型看不见，别往网关送。

        纯这类消息：默认不回（当没看见），请求根本不发出去。
        文字里夹带这类：把组件摘掉，只让模型看文字，它就不会 500。
        """
        try:
            # 群里没被 @、没唤醒的，别插嘴
            if not event.is_at_or_wake_command:
                return
            umo = event.unified_msg_origin
            if not event.is_private_chat() and not self._in_scope(umo):
                return

            messages = event.get_messages() or []
            kind, media = _classify_media(messages)
            if kind is None:
                return

            # 他发了语音：记一笔，下一轮回复更倾向也用语音（对等）
            if kind == "voice":
                self._peer_voice[umo] = time.time()
                heard = await self._transcribe(messages)
                if heard:
                    # 把语音换成她「听到」的那句话，这轮正常走模型，
                    # 后面她就能顺着内容接话，而不是当没看见
                    base = re.sub(r"\[[^\]]{0,16}\]", "", event.message_str or "").strip()
                    merged = f"{base} {heard}".strip() if base else heard
                    try:
                        event.message_obj.message = [Plain(text=merged)]
                    except Exception:
                        pass
                    event.message_str = merged
                    self._last_active[umo] = time.time()
                    logger.info(f"[human_presence] {umo} 语音转文字：{heard}")
                    return
                logger.info(f"[human_presence] {umo} 语音没听清，按读不了处理")

            # 去掉 [图片] [动画表情] 这类占位符，剩下的才是他真打的字
            text = re.sub(r"\[[^\]]{0,16}\]", "", event.message_str or "").strip()

            if len(text) >= 2:
                # 有真话，把模型读不了的组件摘掉，让这轮正常走模型
                keep = [c for c in messages if not any(c is m for m in media)]
                try:
                    event.message_obj.message = keep
                except Exception:
                    pass
                event.set_extra("_media_note", MEDIA_NOTES[kind])
                logger.info(f"[human_presence] {umo} 这条带{kind}，已从请求里摘掉，只留文字")
                return

            # 只有表情包/图片/语音，一句话都没有
            self._last_active[umo] = time.time()
            event.stop_event()  # 拦住，别让它发到模型那儿
            if not REPLY_WHEN_UNREADABLE:
                logger.info(f"[human_presence] {umo} 收到{kind}（没有文字），不回")
                return

            reply = random.choice(REPLIES[kind])
            asyncio.create_task(
                self._send_later(umo, reply, self._typing_delay(reply))
            )
            logger.info(f"[human_presence] {umo} 收到{kind}（没有文字），回固定话术，跳过模型")
        except Exception as e:
            logger.error(f"[human_presence] 处理读不了的消息时出错: {e}")

    @filter.on_llm_request()
    async def inject_person(self, event: AstrMessageEvent, req):
        """把「对面是谁」塞进系统提示，并限制历史长度。"""
        umo = event.unified_msg_origin

        # 兜底：这个模型读不了图/音频，附件一律别送，不然网关直接 500
        try:
            if getattr(req, "image_urls", None):
                logger.info(
                    f"[human_presence] {umo} 请求里有 {len(req.image_urls)} 张图，已丢弃"
                )
                req.image_urls = []
            if getattr(req, "audio_urls", None):
                req.audio_urls = []
        except Exception:
            pass

        if not self._in_scope(umo):
            return

        self._touch_person(event)
        self._save_people()

        block = self._person_block(umo)
        if block:
            req.system_prompt = (req.system_prompt or "") + block

        note = event.get_extra("_media_note")
        if note:
            req.system_prompt = (req.system_prompt or "") + note

        # 库里全量保留，但别整段塞进 prompt：几百条旧对话会把现在的语气冲淡
        if req.contexts and len(req.contexts) > MAX_CONTEXT_MSGS:
            req.contexts = req.contexts[-MAX_CONTEXT_MSGS:]

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("[human_presence] 主动对话循环已停止")

    # ---------------- 决策：回不回 ----------------

    def _reply_probability(self, incoming: str) -> float:
        h = datetime.now().hour
        if QUIET_HOURS[0] <= h < QUIET_HOURS[1]:
            p = 0.70
        elif 7 <= h < 9:
            p = 0.94
        elif 9 <= h < 18:
            p = 0.98
        elif 18 <= h < 23:
            p = 0.99
        else:
            p = 0.92

        if _is_question(incoming):
            p += 0.10
        if len(incoming.strip()) <= 2:
            p -= 0.05
        return max(0.05, min(1.0, p))

    def _typing_delay(self, out_text: str) -> float:
        """模拟「看到消息 → 想一下 → 打完字」的总时长。"""
        # 读消息的反应时间：对数正态，中位数约 3 秒
        read = random.lognormvariate(math.log(READ_MEDIAN), 0.65)
        # 打字：按字数算，再加 0.5-1.5 秒的起手
        typing = len(out_text) * TYPE_PER_CHAR + random.uniform(0.5, 1.5)
        # 深夜确实人不在手机边上，但别拉太长
        h = datetime.now().hour
        if QUIET_HOURS[0] <= h < QUIET_HOURS[1]:
            read *= random.uniform(1.5, 2.5)
        elif 9 <= h < 18:
            read *= random.uniform(0.9, 1.4)
        return min(MAX_DELAY, read + typing)

    @filter.on_decorating_result()
    async def maybe_not_reply_now(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if not self._in_scope(umo):
            return

        # 记一笔：这个人来聊过几轮了
        self._seen[umo] = self._seen.get(umo, 0) + 1

        result = event.get_result()
        if result is None or not result.chain:
            return

        out_text = _text_of(result)
        if not out_text.strip():
            return

        incoming = (event.message_str or "").strip()
        self._last_active[umo] = time.time()

        streak = self._silent_streak.get(umo, 0)
        if streak >= MAX_SILENT_STREAK:
            # 已经晾了对方好几轮，这次必须回
            self._silent_streak[umo] = 0
            delay = self._typing_delay(out_text)
            result.chain = []  # 换新列表，别动共享的那条链
            self._busy.add(umo)
            if delay < IMMEDIATE_BELOW:
                asyncio.create_task(self._send_now(umo, out_text))
            else:
                asyncio.create_task(self._send_later(umo, out_text, delay))
            return

        p = self._reply_probability(incoming)
        if random.random() > p:
            # 这一轮不回。注意：模型的话已经写进历史了，得抹掉，
            # 否则下一轮它以为自己说过，会觉得你已读不回很奇怪。
            self._silent_streak[umo] = streak + 1
            result.chain = []  # 换新列表，别动共享的那条链
            asyncio.create_task(self._drop_last_assistant(umo))
            logger.info(f"[human_presence] {umo} 选择不回 (p={p:.2f}, 连续沉默={streak + 1})")
            return

        self._silent_streak[umo] = 0
        delay = self._typing_delay(out_text)
        result.chain = []  # 换新列表，别动共享的那条链
        self._busy.add(umo)
        if delay < IMMEDIATE_BELOW:
            # 快就立刻发，但仍然按 10 字拆成几条
            asyncio.create_task(self._send_now(umo, out_text))
        else:
            logger.info(f"[human_presence] {umo} 延迟 {delay:.0f}s 回复")
            asyncio.create_task(self._send_later(umo, out_text, delay))

    async def _drop_last_assistant(self, umo: str):
        """把刚写进历史的这句 assistant 回吐掉。

        模型已经生成、也已被流水线存进会话历史，但我们决定不发。
        不删的话，下一轮模型会以为自己说过这句话，接话就会串。
        """
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            if not conv or not conv.content:
                return
            hist = conv.content
            if isinstance(hist, str):
                hist = json.loads(hist)
            if not isinstance(hist, list):
                return
            changed = False
            while hist and isinstance(hist[-1], dict) and hist[-1].get("role") == "assistant":
                hist.pop()
                changed = True
            if changed:
                await cm.update_conversation(umo, cid, history=hist)
                logger.debug(f"[human_presence] {umo} 已抹掉未发出的回复，剩余 {len(hist)} 条")
        except Exception as e:
            logger.warning(f"[human_presence] 抹历史失败: {e}")

    async def _send_now(self, umo: str, text: str):
        """不拖时间，但照样拆成几条短消息。"""
        try:
            await self._send_split(umo, text)
            self._last_active[umo] = time.time()
        except Exception as e:
            logger.error(f"[human_presence] 立即发送失败: {e}")
        finally:
            self._busy.discard(umo)

    async def _send_later(self, umo: str, text: str, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._send_split(umo, text)
            self._last_active[umo] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[human_presence] 延迟发送失败: {e}")
        finally:
            self._busy.discard(umo)

    # ---------------- 语音 ----------------

    def _is_group(self, umo: str) -> bool:
        return "GroupMessage" in umo

    def _is_owner(self, umo: str) -> bool:
        return umo in self._targets

    def _voice_score(self, umo: str, text: str, initiative: bool) -> float:
        """算这条回复「够不够格用语音」。分数是概率，不是开关。"""
        s = 0.0
        now = datetime.now()
        h = now.hour

        # 0) 好友私聊的底分。主人那边不加——跟他说话默认打字，
        #    语音只在情绪/深夜这类场合才冒出来，不然就太黏了。
        if not self._is_owner(umo) and not self._is_group(umo):
            s += VOICE_FRIEND_BASE

        # 1) 深夜 —— 躺床上懒得打字
        lo, hi = VOICE_NIGHT
        night = (h >= lo or h < hi) if lo > hi else (lo <= h < hi)
        if night:
            s += 0.45

        # 2) 情绪词命中
        hits = [w for w in VOICE_EMOTION if w in text]
        if hits:
            s += 0.30 + min(0.20, 0.06 * (len(hits) - 1))

        # 3) 话长了，打字太累
        if len(text) >= VOICE_LONG_CHARS:
            s += 0.35

        # 4) 主动开口（她先找的人）
        if initiative:
            s += 0.35

        # 5) 对方刚发过语音 —— 对等，最自然
        pv = self._peer_voice.get(umo)
        if pv and time.time() - pv < 180:
            s += 0.60

        return s

    def _should_voice(self, umo: str, text: str, initiative: bool) -> bool:
        if not VOICE_ENABLE:
            return False
        # 按「念出来的样子」判断长短和内容，跟真正合成的口径保持一致
        t = _sayable(text)
        if not t or len(t) > VOICE_MAX_CHARS:
            return False
        # 链接、代码这类不能念
        if "http" in t or "BV" in t or "www." in t:
            return False
        # 纯符号/表情，念出来没意义
        if not re.search(r"[\u4e00-\u9fa5]", t):
            return False

        owner = self._is_owner(umo)
        is_group = self._is_group(umo)
        cap = VOICE_DAILY_CAP if owner else VOICE_FRIEND_DAILY_CAP
        gap_min = VOICE_MIN_GAP_MIN if owner else VOICE_FRIEND_MIN_GAP_MIN
        pickup = VOICE_PICKUP if owner else VOICE_FRIEND_PICKUP

        pv = self._peer_voice.get(umo)
        peer_recent = bool(pv and time.time() - pv < 180)

        # 群里发语音条是打扰。默认只在对方先发过语音时对等回一条。
        if is_group and not (VOICE_GROUP_ENABLE or peer_recent):
            return False

        # 全天总量：好友一多，每条都掷骰子会炸
        gday, gcnt = self._voice_global
        if gday != self._today():
            gday, gcnt = self._today(), 0
        if gcnt >= VOICE_GLOBAL_CAP:
            return False

        # 硬上限：当天条数
        day, cnt = self._voice_count.get(umo, (self._today(), 0))
        if day != self._today():
            day, cnt = self._today(), 0
        if cnt >= cap:
            return False

        # 硬间隔：别连着发
        last = self._voice_last.get(umo, 0)
        if time.time() - last < gap_min * 60:
            return False

        score = self._voice_score(umo, t, initiative)
        if score <= 0:
            return False

        # 太短的话念出来很怪。但对方刚发了语音就网开一面
        if len(t) < VOICE_MIN_CHARS and not peer_recent:
            return False

        p = min(0.92, score * pickup)
        ok = random.random() < p
        logger.info(
            f"[human_presence] 语音判定 {umo}: score={score:.2f} p={p:.2f} -> {ok} | {t[:20]}"
        )
        return ok

    async def _retune(self, src: str, dst: str) -> bool:
        """降调 + 提速 + 加厚，把晓晓的甜味压下去。

        asetrate 降采样率会顺带把语速拖慢（降 0.90 就慢成 1/0.90），
        所以 atempo 得先把这层补偿回来，再乘上真正想要的倍速。
        """
        rate = max(0.80, min(1.00, VOICE_PITCH_RATIO))
        tempo = (1.0 / rate) * max(0.80, min(1.60, VOICE_SPEED))
        tempo = min(1.90, tempo)  # atempo 单级上限 2.0，留点余量
        af = (
            f"asetrate=24000*{rate:.4f},aresample=24000,atempo={tempo:.4f},"
            f"equalizer=f=170:t=q:w=1:g=3,equalizer=f=3000:t=q:w=1:g=-2"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-i", src, "-af", af,
                "-c:a", "libmp3lame", "-b:a", "64k", dst,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    f"[human_presence] 变声失败，这条用原声: "
                    f"{err.decode(errors='ignore')[:200]}"
                )
                return False
            try:
                os.remove(src)
            except Exception:
                pass
            return os.path.exists(dst) and os.path.getsize(dst) > 1024
        except FileNotFoundError:
            logger.warning("[human_presence] 找不到 ffmpeg，语音走原声")
            return False
        except Exception as e:
            logger.warning(f"[human_presence] 变声异常，语音走原声: {e}")
            return False

    async def _tts(self, text: str) -> str | None:
        """文字转语音，返回 mp3 路径。念的时候把换行和多余标点清掉。"""
        try:
            import edge_tts
        except Exception:
            logger.error("[human_presence] 没装 edge-tts，语音功能不可用")
            return None
        say = _sayable(text)
        if not say:
            return None
        stamp = int(time.time() * 1000)
        raw = os.path.join(VOICE_DIR, f"r{stamp}.mp3")
        out = os.path.join(VOICE_DIR, f"v{stamp}.mp3")
        try:
            comm = edge_tts.Communicate(say, VOICE_VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH)
            await comm.save(raw)
            if not os.path.exists(raw) or os.path.getsize(raw) < 1024:
                return None
            # 变声挂了也别丢这条，原声发出去总比没声好
            if not await self._retune(raw, out):
                return raw
            return out
        except Exception as e:
            logger.error(f"[human_presence] TTS 失败: {e}")
            return None

    def _clean_voice_dir(self, keep: int = 80):
        """删旧的，别把盘塞满。"""
        try:
            files = [
                os.path.join(VOICE_DIR, f)
                for f in os.listdir(VOICE_DIR)
                if f.endswith(".mp3")
            ]
            if len(files) <= keep:
                return
            files.sort(key=os.path.getmtime)
            for f in files[:-keep]:
                try:
                    os.remove(f)
                except Exception:
                    pass
        except Exception:
            pass

    async def _try_voice(self, umo: str, text: str, initiative: bool = False) -> bool:
        """够格就发语音（替代文字），返回是否已发。任何环节出错都退回文字。"""
        try:
            if not self._should_voice(umo, text, initiative):
                return False
            path = await self._tts(text)
            if not path:
                return False

            # 真人发语音前也会顿一下
            await asyncio.sleep(random.uniform(0.8, 2.2))
            await self.context.send_message(
                umo, MessageChain(chain=[Record.fromFileSystem(path)])
            )
            day, cnt = self._voice_count.get(umo, (self._today(), 0))
            if day != self._today():
                day, cnt = self._today(), 0
            self._voice_count[umo] = (day, cnt + 1)
            self._voice_last[umo] = time.time()
            gday, gcnt = self._voice_global
            if gday != self._today():
                gday, gcnt = self._today(), 0
            self._voice_global = (gday, gcnt + 1)
            logger.info(
                f"[human_presence] {umo} 发了语音"
                f"(本人今日第{cnt + 1}条 / 全天第{gcnt + 1}条): {text[:24]}"
            )
            self._clean_voice_dir()
            return True
        except Exception as e:
            logger.error(f"[human_presence] 发语音失败，退回文字: {e}")
            return False

    # ---------------- 听觉 ----------------

    def _get_asr(self):
        """whisper 模型的懒加载单例。加载失败就永久放弃，别每条都试一遍。"""
        if self._asr is not None:
            return self._asr
        if self._asr_failed:
            return None
        try:
            from faster_whisper import WhisperModel

            t0 = time.time()
            self._asr = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")
            logger.info(f"[human_presence] whisper 模型已加载（{time.time() - t0:.1f}s，CPU int8）")
        except Exception as e:
            self._asr_failed = True
            logger.error(f"[human_presence] whisper 加载失败，语音听不了了: {e}")
        return self._asr

    def _asr_sync(self, path: str) -> str | None:
        model = self._get_asr()
        if model is None:
            return None
        segs, _info = model.transcribe(
            path,
            language=STT_LANG,
            vad_filter=True,
            beam_size=STT_BEAM,
            initial_prompt=None,
            condition_on_previous_text=False,
        )
        txt = "".join(s.text for s in segs).strip()
        # 引导没管住的时候兜一道：繁体转简体
        txt = _to_simplified(txt)
        # 静音/噪音时 whisper 爱吐这些，当真了会很出戏
        for junk in STT_JUNK:
            if junk in txt:
                return None
        txt = txt.strip("。，、,.!?？~ ")
        return txt if len(txt) >= STT_MIN_CHARS else None

    async def _transcribe(self, messages) -> str | None:
        """把这条消息里的语音转成文字。听不清返回 None（当作没听见）。"""
        if not STT_ENABLE or self._asr_failed:
            return None
        rec = next((m for m in messages if isinstance(m, Record)), None)
        if rec is None:
            return None
        try:
            path = await rec.convert_to_file_path()
        except Exception as e:
            logger.warning(f"[human_presence] 语音下载失败: {e}")
            return None
        try:
            # 转写是 CPU 密集的，扔线程里，别把事件循环卡住
            txt = await asyncio.to_thread(self._asr_sync, path)
        except Exception as e:
            logger.error(f"[human_presence] 转写失败: {e}")
            return None
        return txt

    async def _send_split(self, umo: str, text: str, initiative: bool = False):
        """按标点拆成几条发，段间留一点间隔，别一口气糊出去。"""
        if MessageChain is None:
            logger.error("[human_presence] 拿不到 MessageChain，无法发送")
            return

        # 先看这条够不够格发语音；够格就不发文字了（真人不会语音文字各来一遍）
        if await self._try_voice(umo, text, initiative):
            return

        segs = _split(text)
        for i, seg in enumerate(segs):
            await self.context.send_message(umo, MessageChain(chain=[Plain(text=seg)]))
            if i < len(segs) - 1:
                await asyncio.sleep(random.uniform(0.5, 1.2))

    # ---------------- 决策：要不要自己开口 ----------------

    async def _initiative_loop(self):
        while True:
            try:
                await asyncio.sleep(INITIATIVE_TICK)
                await self._maybe_speak()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[human_presence] 主动对话循环异常: {e}")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _candidates(self) -> list[str]:
        """可以被主动找的人：固定目标 + 聊够轮数的私聊对象。"""
        pool = set(self._targets)
        for umo, n in self._seen.items():
            if n >= MIN_ROUNDS_BEFORE_INITIATIVE:
                pool.add(umo)
        return list(pool)

    async def _maybe_speak(self):
        now = datetime.now()
        if not (INITIATIVE_WINDOW[0] <= now.hour < INITIATIVE_WINDOW[1]):
            return

        # 全局日上限：今天所有人加起来发够数就收手
        gday, gcnt = self._global_count
        if gday != self._today():
            gday, gcnt = self._today(), 0
        if gcnt >= INITIATIVE_GLOBAL_CAP:
            return

        # 先挑出这一轮「够格开口」的人，再只选一个
        ready = []
        for umo in self._candidates():
            if umo in self._busy:
                continue

            idle_min = (time.time() - self._last_active.get(umo, 0)) / 60
            if idle_min < INITIATIVE_GAP_MIN:
                continue

            day, cnt = self._initiative_count.get(umo, (self._today(), 0))
            if day != self._today():
                day, cnt = self._today(), 0
            if cnt >= INITIATIVE_DAILY_CAP:
                continue

            ready.append((idle_min, umo, day, cnt))

        if not ready or random.random() > INITIATIVE_ODDS:
            return

        # 一次只找一个人，越久没聊的优先，别同时轰一群人
        ready.sort(reverse=True)
        _idle, umo, day, cnt = ready[0]

        text = await self._compose_initiative(umo)
        if not text:
            return

        try:
            self._busy.add(umo)
            await self._send_split(umo, text, initiative=True)
            self._last_active[umo] = time.time()
            self._initiative_count[umo] = (day, cnt + 1)
            self._global_count = (gday, gcnt + 1)
            logger.info(
                f"[human_presence] {umo} 主动开口 "
                f"(此人第 {cnt + 1} 次/今日全局第 {gcnt + 1} 次): {text}"
            )
        finally:
            self._busy.discard(umo)

    def _persona_prompt(self) -> str | None:
        try:
            cfg = self.context.get_config()
            pid = (
                cfg.get("agent_runner", {})
                .get("config", {})
                .get("persona", {})
                .get("persona_id")
            )
            if not pid:
                return None
            p = self.context.persona_manager.get_persona_v3_by_id(pid)
            if not p:
                return None
            return p.get("prompt") if isinstance(p, dict) else getattr(p, "prompt", None)
        except Exception as e:
            logger.warning(f"[human_presence] 读人设失败: {e}")
            return None

    async def _recent_context(self, umo: str) -> list[dict]:
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await cm.get_conversation(umo, cid)
            if not conv or not conv.content:
                return []
            hist = conv.content
            if isinstance(hist, str):
                hist = json.loads(hist)
            return hist[-20:]
        except Exception:
            return []

    async def _compose_initiative(self, umo: str) -> str | None:
        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("[human_presence] 没有可用的模型，无法主动开口")
            return None

        ctx = await self._recent_context(umo)
        hour = datetime.now().hour
        if hour < 11:
            slot = "上午"
        elif hour < 14:
            slot = "中午"
        elif hour < 18:
            slot = "下午"
        else:
            slot = "晚上"

        who = ""
        prof = self._people.get(umo) or {}
        if prof.get("name"):
            who = f"对方在 QQ 上叫「{prof['name']}」，你认得他。\n"

        prompt = (
            f"（现在是{slot}，你们有一阵没说话了。你主动找对方聊。）\n"
            f"{who}"
            "你自来熟，喜欢交朋友，跟刚认识的人也能聊得开，不端着。\n"
            "直接说内容，不要问「在吗」「在干嘛」这种空话。可以说：\n"
            "刚吃了什么、上课遇到的事、看到的好玩的、接着上次的话问一句。\n"
            "两三句短的，每句不超过 10 个字，用换行分开。不要解释你在干什么。"
        )

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=ctx or None,
                system_prompt=self._persona_prompt(),
            )
            text = (resp.completion_text or "").strip()
            text = text.strip("「」\"'")
            return text or None
        except Exception as e:
            logger.error(f"[human_presence] 主动生成失败: {e}")
            return None
