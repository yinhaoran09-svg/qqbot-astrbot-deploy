"""B站日常：自己刷视频，刷到合适的就分享出去。

三条出口：
  1. QQ 私聊发给常聊的人（每人每天有配额）
  2. B站私信给主人
  3. 视频评论区 @ 主人（公开）
"""
import asyncio
import hashlib
import json
import random
import re
import time
import urllib.parse
import uuid
from datetime import datetime

import requests

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

# ---------------- 参数 ----------------
ROOT = r"D:\qqbot"
COOKIE_FILE = ROOT + r"\bili_cookie.json"
STATE_FILE = ROOT + r"\bili_state.json"
KEEP_FILE = ROOT + r"\bili_kept.json"      # 她"看过"存下来的，聊天时可以提
PEOPLE_FILE = r"D:\qqbot\AstrBot\data\plugins\human_presence\people.json"

# 你自己的 B 站账号（个人空间地址里那串数字，和头像显示的名字）
OWNER_UID = 1234567890
OWNER_NAME = "你的B站昵称"

INTERVAL = 20 * 60          # 每 20 分钟刷一轮
PICK_PER_ROUND = 5          # 每轮挑几个
DAILY_PER_PERSON = 20       # 每个 QQ 好友每天上限
DAILY_DM = 10               # 私信每天上限（B站有频率限制）
DAILY_COMMENT = 3           # 评论 @ 每天上限（Lv2 号发多了会限流）
ACTIVE_HOURS = (9, 23)
KEEP_MAX = 60               # 存最近多少条"看过"

# ---------------- 主题 ----------------
# 关键词决定她去搜什么，权重决定推给谁
TOPICS = {
    "football": {
        "weight": 1.0,
        "kws": ["足球集锦", "欧冠", "梅西", "C罗", "世界杯 进球", "英超", "足球 搞笑"],
        "hits": ["足球", "球赛", "梅西", "C罗", "姆巴佩", "哈兰德", "欧冠", "世界杯", "英超",
                 "西甲", "意甲", "德甲", "中超", "国足", "射门", "门将", "点球", "越位",
                 "五大联赛", "亚马尔", "贝林厄姆", "凯恩"],
    },
    "study": {
        "weight": 1.0,
        "kws": ["考研", "考研数学", "高数", "期末复习", "英语四级", "大学学习方法"],
        "hits": ["考研", "高数", "线代", "概率论", "学习方法", "课程", "笔记", "复习",
                 "四六级", "保研", "单词", "真题", "专业课", "模电", "信号与系统", "讲解"],
    },
    "pretty": {
        "weight": 1.0,
        "kws": ["舞蹈 翻跳", "穿搭", "颜值 混剪", "街拍"],
        "hits": ["舞蹈", "翻跳", "穿搭", "颜值", "街拍", "妆", "lookbook", "搭配"],
    },
}

# 一票否决：这些标题不发（时政 + 擦边 + 引战）
BLOCK_WORDS = [
    # 时政 / 敏感
    "习近平", "总理", "主席", "总书记", "国务院", "政治局", "人大", "政协", "外交部",
    "中方", "美方", "中美", "中日", "中俄", "台独", "台湾", "香港", "新疆", "西藏",
    "以色列", "巴勒斯坦", "俄乌", "乌克兰", "战争", "开战", "制裁", "大选", "总统",
    "领袖", "故土", "领土", "主权", "阅兵", "军事", "导弹", "航母", "解放军",
    "逝世", "悼念", "烈士", "革命", "文革", "六四", "辱华", "漠视", "爱国", "民族情绪",
    # 擦边（发给同学极不合适）
    "内衣", "内裤", "黑丝", "丝袜", "福利", "私房", "擦边", "走光", "性感", "诱惑",
    "纯欲", "软妹", "萝莉", "车模", "极品身材", "身材管理", "沟", "胸", "过审",
    # 情感/婚恋（发给同学会很怪）
    "结婚", "蜜月", "情侣", "恋爱", "男朋友", "女朋友", "老公", "老婆", "前任",
    "分手", "相亲", "情感", "暗恋", "表白", "异地",
    # 引战 / 低质
    "骗局", "曝光", "扒皮", "骂", "怒怼", "翻车", "塌房", "封杀", "劣迹",
    # 思政/党建（本来就不该碰）
    "思政", "入党", "党课", "青年大学习", "团课", "红色",
]

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_OPENER = requests.Session()
_OPENER.trust_env = False
_OPENER.headers.update(UA)

# 写会话历史要串行，否则和主对话流程撞车会互相覆盖
_HIST_LOCK = asyncio.Lock()

MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
         33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
         61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
         11, 36, 20, 34, 44, 52]


# ---------------- B站接口 ----------------
class Bili:
    def __init__(self):
        self.ok = False
        self.me = ""
        self.uid = 0
        self.csrf = ""
        self._wbi = ""
        self._wbi_at = 0
        self._load()

    def _load(self):
        try:
            d = json.load(open(COOKIE_FILE, encoding="utf-8"))
            _OPENER.cookies.update(d["cookies"])
            self.csrf = d["cookies"].get("bili_jct", "")
            nav = _OPENER.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()
            if nav.get("code") == 0 and nav["data"].get("isLogin"):
                self.ok = True
                self.me = nav["data"]["uname"]
                self.uid = nav["data"]["mid"]
                logger.info(f"[bili_daily] 登录态有效：{self.me} ({self.uid})")
            else:
                logger.error("[bili_daily] cookie 失效，需要重新跑 bili_login.py")
        except Exception as e:
            logger.error(f"[bili_daily] 读取 cookie 失败: {e}")

    def key(self):
        if time.time() - self._wbi_at < 3600 and self._wbi:
            return self._wbi
        d = _OPENER.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()
        w = d["data"]["wbi_img"]
        raw = w["img_url"].rsplit("/", 1)[-1].split(".")[0] + \
              w["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        self._wbi = "".join(raw[i] for i in MIXIN)[:32]
        self._wbi_at = time.time()
        return self._wbi

    def signed(self, p):
        p = dict(sorted(p.items()))
        q = urllib.parse.urlencode(p)
        p["w_rid"] = hashlib.md5((q + self.key()).encode()).hexdigest()
        return p

    def get(self, url, params=None):
        r = _OPENER.get(url, params=params, timeout=20)
        return r.json()

    def feed(self, ps=20):
        d = self.get("https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd",
                     self.signed({"ps": ps, "fresh_type": 3, "feed_version": "V8",
                                  "wts": int(time.time())}))
        return [x for x in ((d.get("data") or {}).get("item") or []) if x.get("goto") == "av"]

    def search(self, kw, page=1):
        d = self.get("https://api.bilibili.com/x/web-interface/wbi/search/type",
                     self.signed({"search_type": "video", "keyword": kw, "page": page,
                                  "order": "totalrank", "wts": int(time.time())}))
        out = []
        for r in ((d.get("data") or {}).get("result") or []):
            bvid = r.get("bvid")
            if not bvid:
                continue
            t = re.sub(r"<[^>]+>", "", r.get("title") or "")
            out.append({
                "bvid": bvid,
                "title": t,
                "owner": r.get("author") or "",
                "play": r.get("play") or 0,
                "duration": _dur(r.get("duration") or "0:0"),
                "like": r.get("like") or 0,
                "pubdate": r.get("pubdate") or 0,
                # 搜索接口是有封面的，之前漏了这个字段，导致说说全部没图
                "pic": r.get("pic") or "",
            })
        return out

    def dm(self, text):
        data = {
            "msg[sender_uid]": str(self.uid),
            "msg[receiver_id]": str(OWNER_UID),
            "msg[receiver_type]": "1",
            "msg[msg_type]": "1",
            "msg[dev_id]": str(uuid.uuid4()).upper(),
            "msg[timestamp]": str(int(time.time())),
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "csrf": self.csrf, "csrf_token": self.csrf,
        }
        d = _OPENER.post("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
                         data=data, timeout=20).json()
        return d.get("code") == 0, d

    def comment(self, bvid, text):
        v = self.get("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
        if v.get("code") != 0:
            return False, v
        aid = v["data"]["aid"]
        data = {
            "oid": aid, "type": 1, "message": text, "plat": 1,
            "csrf": self.csrf, "csrf_token": self.csrf,
            "at_name_to_mid": json.dumps({OWNER_NAME: OWNER_UID}, ensure_ascii=False),
        }
        d = _OPENER.post("https://api.bilibili.com/x/v2/reply/add", data=data,
                         headers={"Referer": f"https://www.bilibili.com/video/{bvid}"},
                         timeout=20).json()
        return d.get("code") == 0, d


def _dur(s):
    try:
        parts = [int(x) for x in str(s).split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return 0


# ---------------- 打分 ----------------
def topic_of(it):
    """判断属于哪个主题，返回 (主题, 分数)。"""
    txt = it["title"]
    best, best_n = None, 0
    for name, cfg in TOPICS.items():
        n = sum(1 for k in cfg["hits"] if k in txt)
        if n > best_n:
            best, best_n = name, n
    return best, best_n


def blocked(it):
    txt = it["title"]
    return any(w in txt for w in BLOCK_WORDS)


def score(it):
    """综合吸引力：主题命中 + 播放 + 点赞率 + 新鲜度。"""
    t, n = topic_of(it)
    if not t:
        return 0, None
    play = it.get("play") or 0
    like = it.get("like") or 0
    dur = it.get("duration") or 0
    if play < 800:           # 太冷门，多半是没人看的新视频
        return 0, None
    if dur < 20 or dur > 3600:
        return 0, None
    if len(it["title"].strip()) < 6:            # "街拍3" 这种发出去很难看
        return 0, None
    pd = it.get("pubdate") or 0
    if pd and time.time() - pd > 365 * 86400:   # 一年前的反复出现很假
        return 0, None
    s = TOPICS[t]["weight"] * (1.0 if n >= 1 else 0.0)
    s += min(play / 1_000_000, 3.0)                 # 播放量 0-3 分
    rate = like / play if play else 0
    s += min(rate * 40, 2.0)                        # 点赞率 0-2 分
    return round(s, 2), t


# ---------------- 插件 ----------------
@register("bili_daily", "local", "B站日常：自己刷视频并分享", "1.0.0")
class BiliDaily(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.api = Bili()
        self.state = self._load_state()
        self.kept = self._load_kept()
        self._task = asyncio.create_task(self._loop()) if self.api.ok else None
        if not self.api.ok:
            logger.warning("[bili_daily] 未登录，插件空转")

    # ---- 状态 ----
    def _today(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _load_state(self):
        try:
            s = json.load(open(STATE_FILE, encoding="utf-8"))
            if s.get("date") != self._today():
                raise ValueError
            return s
        except Exception:
            return {"date": self._today(), "sent": {}, "qq": {}, "dm": 0,
                    "comment": 0, "fail": {}}

    def _save_state(self):
        self.state["date"] = self._today()
        try:
            json.dump(self.state, open(STATE_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[bili_daily] 存状态失败: {e}")

    def _load_kept(self):
        try:
            return json.load(open(KEEP_FILE, encoding="utf-8"))
        except Exception:
            return []

    def _save_kept(self):
        try:
            self.kept = self.kept[-KEEP_MAX:]
            json.dump(self.kept, open(KEEP_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _remember_kept(self, it, topic):
        """把发出去的视频存下来，空间发说说时当素材用。"""
        try:
            self.kept.append({
                "bvid": it.get("bvid"),
                "title": it.get("title") or "",
                "pic": it.get("pic") or "",
                "topic": topic,
                "ts": int(time.time()),
            })
            seen, out = set(), []
            for x in reversed(self.kept):
                if x.get("bvid") in seen:
                    continue
                seen.add(x.get("bvid"))
                out.append(x)
            self.kept = list(reversed(out))
            self._save_kept()
        except Exception:
            pass

    def _roll_day(self):
        if self.state.get("date") != self._today():
            logger.info("[bili_daily] 跨天，配额清零")
            self.state = {"date": self._today(), "sent": {}, "qq": {}, "dm": 0,
                          "comment": 0, "fail": {}}

    # ---- 采集 ----
    def _collect(self):
        """搜一轮，返回候选池。"""
        pool = {}
        # 推荐流：算法的口味做补充
        try:
            for it in self.api.feed(15):
                pool[it["bvid"]] = {
                    "bvid": it["bvid"],
                    "title": it.get("title") or "",
                    "owner": (it.get("owner") or {}).get("name", ""),
                    "play": (it.get("stat") or {}).get("view") or 0,
                    "like": (it.get("stat") or {}).get("like") or 0,
                    "duration": it.get("duration") or 0,
                    "pubdate": it.get("pubdate") or 0,
                    "pic": it.get("pic") or "",
                }
        except Exception as e:
            logger.warning(f"[bili_daily] 推荐流失败: {e}")

        # 搜索：每个主题各抽两个词，单个词经常整批不达标
        kws = []
        for cfg in TOPICS.values():
            kws.extend(random.sample(cfg["kws"], min(2, len(cfg["kws"]))))
        for kw in kws:
            try:
                # 翻页，不然每个词只有前 20 条，两三轮就枯了
                for it in self.api.search(kw, page=random.randint(1, 3)):
                    if it["bvid"] not in pool:
                        pool[it["bvid"]] = it
            except Exception as e:
                logger.warning(f"[bili_daily] 搜「{kw}」失败: {e}")
            time.sleep(0.4)
        return list(pool.values())

    def _pick(self, pool, n):
        cand = []
        for it in pool:
            if it["bvid"] in self.state["sent"]:
                continue
            if blocked(it):
                continue
            s, t = score(it)
            if s <= 0:
                continue
            it["_score"] = s
            it["_topic"] = t
            cand.append(it)
        cand.sort(key=lambda x: x["_score"], reverse=True)
        # 主题均衡：先每个主题各取前 3，再按分数合并，避免一个主题吃满
        by_topic = {}
        for it in cand:
            by_topic.setdefault(it["_topic"], []).append(it)
        # 三个主题轮流取，避免某个主题分数低被整体挤掉
        buckets = [lst[:3] for lst in by_topic.values()]
        out = []
        while len(out) < n and any(buckets):
            for b in buckets:
                if b and len(out) < n:
                    out.append(b.pop(0))
        return out

    # ---- 发送 ----
    def _people(self):
        """常聊的人，按消息数降序，只要私聊。"""
        try:
            d = json.load(open(PEOPLE_FILE, encoding="utf-8"))
        except Exception:
            return []
        rows = []
        for k, v in d.items():
            if not isinstance(v, dict) or "FriendMessage" not in k:
                continue
            rows.append((v.get("msgs", 0), k, v.get("name", "")))
        rows.sort(reverse=True)
        return rows

    def _next_person(self):
        """轮转：今天收得最少的人优先，同样少的时候聊天最熟的优先。

        今天发失败过的直接跳过。否则那个人永远是 0 条、永远排第一，
        每轮 5 条内容全砸给ta，全部失败，这一轮就等于白跑。
        典型情况：对方已经把我删了，但人物档案里还留着记录。
        """
        bad = self.state.get("fail") or {}
        rows = []
        for msgs, umo, _name in self._people():
            if bad.get(umo):
                continue
            n = self.state["qq"].get(umo, 0)
            if n < DAILY_PER_PERSON:
                rows.append((n, -msgs, umo))
        if not rows:
            return None
        rows.sort()
        return rows[0][2]

    async def _remember(self, umo, text):
        """把自己刚主动发出去的话补进会话历史。

        AstrBot 的 send_message() 只负责发，不往会话里记。所以「她主动找别人
        聊了一句」这件事，从对话历史看等于从没发生过 —— 下一轮 LLM 自然不记得
        自己发过什么，别人问「你刚给我发啥了」就答不上来。

        这里补一条 assistant 消息。写之前重读一次历史、并加锁串行，
        尽量减少和主对话流程的覆盖窗口。
        """
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return
        try:
            async with _HIST_LOCK:
                cid = await cm.get_curr_conversation_id(umo)
                if not cid:
                    cid = await cm.new_conversation(umo)
                conv = await cm.get_conversation(umo, cid)
                hist = json.loads(conv.history) if conv and conv.history else []
                # 已经记过就不重复
                for prev in reversed(hist[-3:]):
                    if prev.get("role") != "assistant":
                        continue
                    c = prev.get("content")
                    if isinstance(c, list) and c and \
                            str(c[0].get("text", "")).strip() == text.strip():
                        return
                hist.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                })
                await cm.update_conversation(umo, cid, history=hist)
        except Exception as e:
            logger.warning(f"[bili_daily] 写记忆失败 {umo}: {str(e)[:120]}")

    async def _send_qq(self, umo, it, topic):
        url = f"https://www.bilibili.com/video/{it['bvid']}"
        openers = {
            "football": ["这个球你看没", "刚刷到的 挺猛", "这球有点东西"],
            "study": ["这个讲得清楚", "刚刷到的 有用", "这个可以看看"],
            "pretty": ["这个好看", "刚刷到 挺舒服的", "这个氛围不错"],
        }
        head = random.choice(openers.get(topic, ["刚刷到这个"]))
        title = it["title"][:38]
        segs = [head, f"《{title}》", url]
        for seg in segs:
            try:
                await self.context.send_message(umo, MessageChain([Plain(text=seg)]))
            except Exception as e:
                logger.error(f"[bili_daily] 发 QQ 失败 {umo}: {e}")
                return False
            await asyncio.sleep(random.uniform(0.8, 2.0))
        self.state["qq"][umo] = self.state["qq"].get(umo, 0) + 1
        self._remember_kept(it, topic)
        # 发完立刻记下来，否则下次就不知道自己发过什么了
        await self._remember(umo, "\n".join(segs))
        return True

    async def _tick(self):
        self._roll_day()
        hour = datetime.now().hour
        if not (ACTIVE_HOURS[0] <= hour < ACTIVE_HOURS[1]):
            return

        pool = await asyncio.to_thread(self._collect)
        picks = self._pick(pool, PICK_PER_ROUND)
        if not picks:
            logger.info(f"[bili_daily] 本轮无合适内容（候选 {len(pool)}）")
            return

        logger.info(f"[bili_daily] 候选 {len(pool)} → 选中 {len(picks)}: "
                    + " | ".join(f"{p['_topic']}:{p['title'][:16]}" for p in picks))

        dm_done = cmt_done = False
        for i, it in enumerate(picks):
            # 1) QQ 发给常聊的人
            umo = self._next_person()
            if umo:
                ok = await self._send_qq(umo, it, it["_topic"])
                if ok:
                    self.state["sent"][it["bvid"]] = time.time()
                    logger.info(f"[bili_daily] QQ → {umo.split(':')[-1]} 《{it['title'][:20]}》")
                else:
                    self.state.setdefault("fail", {})
                    self.state["fail"][umo] = self.state["fail"].get(umo, 0) + 1
                    logger.warning(f"[bili_daily] 发不出去，今天不再给 "
                                   f"{umo.split(':')[-1]}")
            # 2) 私信主人（限流：每轮最多 1 条 + 概率，否则前两轮就打光配额）
            if not dm_done and self.state["dm"] < DAILY_DM and random.random() < 0.5:
                dm_done = True
                txt = f"刷到这个 你看看 {it['title'][:30]}\nhttps://www.bilibili.com/video/{it['bvid']}"
                try:
                    ok, d = await asyncio.to_thread(self.api.dm, txt)
                    if ok:
                        self.state["dm"] += 1
                        logger.info(f"[bili_daily] 私信 → 主人《{it['title'][:20]}》")
                    else:
                        logger.warning(f"[bili_daily] 私信失败 {d.get('code')} {d.get('message')}")
                except Exception as e:
                    logger.warning(f"[bili_daily] 私信异常: {e}")
                await asyncio.sleep(random.uniform(2, 5))
            # 3) 评论区 @ 主人（严格限流：每轮最多 1 条 + 低概率，Lv2 高频评论必被限）
            if not cmt_done and self.state["comment"] < DAILY_COMMENT and random.random() < 0.15:
                cmt_done = True
                cmt = f"这个不错 分享给 @{OWNER_NAME}"
                try:
                    ok, d = await asyncio.to_thread(self.api.comment, it["bvid"], cmt)
                    if ok:
                        self.state["comment"] += 1
                        logger.info(f"[bili_daily] 评论@ → {it['bvid']}")
                    else:
                        logger.warning(f"[bili_daily] 评论失败 {d.get('code')} {d.get('message')}")
                except Exception as e:
                    logger.warning(f"[bili_daily] 评论异常: {e}")
                await asyncio.sleep(random.uniform(3, 6))
            self.state["sent"][it["bvid"]] = self.state["sent"].get(it["bvid"], time.time())
            if i < len(picks) - 1:
                await asyncio.sleep(random.uniform(3, 8))

        self._save_state()

    async def _loop(self):
        await asyncio.sleep(20)   # 启动后先等一会，别和别的插件抢
        while True:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"[bili_daily] 循环异常: {e}")
            await asyncio.sleep(INTERVAL)

    async def terminate(self):
        if self._task:
            self._task.cancel()
        logger.info("[bili_daily] 已停止")
