"""QQ空间日常：自己发说说、刷好友动态、点赞评论、回复留言。

三条工作流互相独立：
  1. 发说说   —— 每天 3~5 条，时间随机，素材来自 B站/头条/聊天记录
  2. 刷空间   —— 每 45 分钟看一轮好友动态，点赞，偶尔对熟人评论
  3. 回留言   —— 回自己说说下的评论，以及她评论过的说说下别人的追问

技术底座：
  - 发/删说说走 NapCat 内置的 send_qzone_msg / delete_qzone_msg
  - 读动态、点赞、评论走 QZone 老 Web 接口，cookie 由 NapCat 的 get_cookies 提供
  - 评论定位靠「评论在列表里的序号」，所以读完必须立刻发，中间不能插队
"""
import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import time
from datetime import datetime, date

import requests

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

# ---------------- 路径与参数 ----------------
ROOT = r"D:\qqbot"
NAPCAT = "http://127.0.0.1:6700"
STATE_FILE = ROOT + r"\qzone_state.json"
PIC_DIR = ROOT + r"\_qzone_pics"
DB_FILE = r"D:\qqbot\AstrBot\data\data_v4.db"
PEOPLE_FILE = r"D:\qqbot\AstrBot\data\plugins\human_presence\people.json"
TOUTIAO_FILE = r"D:\qqbot\AstrBot\data\plugins\web_senses\cache.json"
KEEP_FILE = ROOT + r"\bili_kept.json"

POST_CHECK = 5 * 60          # 每 5 分钟看一眼该不该发说说
SOCIAL_TICK = 45 * 60        # 每 45 分钟刷一次空间
ACTIVE_HOURS = (9, 24)       # 只在这个时段活动（24 = 到午夜）

POSTS_PER_DAY = (3, 5)       # 每天发几条说说
DAILY_LIKE = 80              # 每天最多点几个赞（看到的基本都点）
DAILY_COMMENT = 30           # 每天最多评论几条（防封顶，别指望全用满）
COMMENT_PER_ROUND = 4        # 每轮最多评论 4 条
LIKE_PER_ROUND = 12          # 每轮最多点赞 12 条（基本等于当轮全部）
REPLY_PER_ROUND = 4          # 每轮最多回 4 条留言
REPLY_MAX_AGE = 3 * 86400    # 只回 3 天内的说说
REPLY_PER_FEED = 6           # 同一条说说每天最多回 6 条
REPLY_IN_THREADS = True      # 别人在我评论下面的追问，继续接
SCAN_FRIENDS = 10            # 每轮扫 10 个，游标轮转，3 轮覆盖全部好友
FEED_MAX_AGE = 36 * 3600     # 只看 36 小时内的动态
KEEP_MAX = 80

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 熟人：这几个人可以被评论（空间里的评论是实名的，宁可少）
# 换成你自己确定关系够近的人。空间里的评论是实名的，宁可少放几个。
FAMILIAR = {
    1234567890,   # 示例：自己
    1000000001,   # 示例：熟人 A
}

# 绝不能碰的词：时政、丧事、擦边、广告
BAD_WORDS = [
    # 时政
    "习近平", "总书记", "国务院", "主席", "总理", "书记", "部长", "省长", "市长",
    "两会", "人大", "政协", "外交", "外交部", "美方", "美国", "日本", "台独",
    "港独", "疆独", "藏独", "六四", "法轮", "游行", "示威", "罢工", "维权",
    "纪委", "落马", "双规", "被捕", "判刑", "审判", "间谍", "制裁", "军事",
    "战争", "俄乌", "以色列", "巴勒斯坦", "核武", "政变", "选举", "民调",
    "中央", "党", "政府", "政策", "改革", "财政", "经济下行", "失业率",
    # 丧事
    "逝世", "去世", "追悼", "葬礼", "悼念", "逝世", "离世", "病逝", "讣告",
    # 擦边
    "擦边", "福利", "诱惑", "湿身", "内衣", "泳装", "性感", "丝袜", "情色",
    "裸", "床照", "约炮", "一夜情", "包养", "足浴", "按摩",
    # 广告营销
    "加微信", "私信我", "限时", "优惠券", "秒杀", "代理", "加盟", "刷单",
    "兼职日结", "点击链接", "扫码", "免费领",
    # 负面情绪
    "自杀", "轻生", "跳楼", "猝死", "得癌", "绝症",
]


# ---------------- 小工具 ----------------
def _pic_url(u: str) -> str:
    """B站封面常是 //i0.hdslb.com/... 或 http://，统一成 https。"""
    if not u:
        return ""
    u = str(u).strip()
    if u.startswith("//"):
        u = "https:" + u
    return u.replace("http://", "https://")

def clean(txt) -> str:
    """去掉富文本里的标签、表情代码、多余空白。"""
    s = str(txt or "")
    s = re.sub(r"\[em\]e\d+\[/em\]", "", s)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    s = re.sub(r"@\{uin:\d+[^}]*\}", "", s)      # @占位符
    return re.sub(r"\s+", " ", s).strip()


def blocked(text: str) -> bool:
    return any(w in (text or "") for w in BAD_WORDS)


def hkey(*parts) -> str:
    return hashlib.md5("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def jsonp(txt: str, cb: str = "_preloadCallback"):
    """剥掉 JSONP 外壳。"""
    txt = (txt or "").strip()
    m = re.search(rf"^{cb}\((.*)\);?\s*$", txt, re.S)
    return json.loads(m.group(1) if m else txt)


def iframe_json(txt: str) -> dict:
    """老接口返回的是 iframe 里包一段 JS，把里面的 JSON 抠出来。"""
    m = re.search(r"frameElement\.callback\(\s*(\{.*?\})\s*\);", txt or "", re.S)
    if not m:
        m = re.search(r"(\{[^{}]*\"code\"[^{}]*\})", txt or "", re.S)
    return json.loads(m.group(1)) if m else {}


# ---------------- 空间接口 ----------------
class Qzone:
    def __init__(self):
        self.s = requests.Session()
        self.s.trust_env = False
        self.uin = None
        self._ck = None
        self._bkn = None
        self._at = 0

    # --- 鉴权（bkn 会随 cookie 变，10 分钟刷新一次）---
    def auth(self, force=False):
        if force or not self._ck or time.time() - self._at > 600:
            r = self.s.post(f"{NAPCAT}/get_cookies",
                            json={"domain": "qzone.qq.com"}, timeout=20).json()
            self._ck = r["data"]["cookies"]
            self._bkn = str(r["data"]["bkn"])
            self._at = time.time()
        return self._ck, self._bkn

    def ready(self) -> bool:
        try:
            r = self.s.post(f"{NAPCAT}/get_login_info", json={}, timeout=10).json()
            if r.get("status") != "ok":
                return False
            self.uin = str(r["data"]["user_id"])
            self.auth()
            return True
        except Exception as e:
            logger.warning(f"[qzone] NapCat 不可用: {e}")
            return False

    def _h(self, ref: str):
        ck, _ = self.auth()
        return {"Cookie": ck, "Referer": ref, "User-Agent": UA,
                "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}

    # --- 好友 ---
    def friends(self):
        try:
            r = self.s.post(f"{NAPCAT}/get_friend_list", json={}, timeout=20).json()
            out = []
            for f in r.get("data") or []:
                out.append({
                    "uin": str(f.get("user_id")),
                    "name": f.get("remark") or f.get("nickname") or "",
                    "nick": f.get("nickname") or "",
                })
            return out
        except Exception as e:
            logger.warning(f"[qzone] 取好友列表失败: {e}")
            return []

    # --- 读某人的说说 ---
    def feeds(self, uin: str, num: int = 3):
        _, bkn = self.auth()
        url = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
               "emotion_cgi_msglist_v6"
               f"?uin={uin}&ftype=0&sort=0&pos=0&num={num}&replynum=20&g_tk={bkn}"
               f"&callback=_preloadCallback&code_version=1&format=jsonp"
               f"&need_private_comment=1&hostUin={uin}")
        try:
            r = self.s.get(url, headers=self._h(f"https://user.qzone.qq.com/{uin}"),
                           timeout=25)
            d = jsonp(r.text)
            if d.get("code") != 0:
                if d.get("code") == -10031:      # 对方设了隐私
                    return []
                logger.info(f"[qzone] 读 {uin} 的说说返回 code={d.get('code')} "
                            f"{d.get('message')}")
                return []
            out = []
            for it in d.get("msglist") or []:
                txt = it.get("content") or ""
                if not txt:
                    for c in (it.get("conlist") or []):
                        txt += str(c.get("con") or "")
                cmts = []
                for idx, c in enumerate(it.get("commentlist") or [], 1):
                    subs = [{
                        "idx": idx, "uin": str(c.get("uin")),
                        "name": c.get("name") or "",
                        "text": clean(c.get("content")),
                    }]
                    for s in (c.get("list_3") or []):
                        subs.append({
                            "idx": idx, "uin": str(s.get("uin")),
                            "name": s.get("name") or "",
                            "text": clean(s.get("content")),
                            "sub": True,
                        })
                    cmts.append(subs)
                out.append({
                    "uin": str(it.get("uin") or uin),
                    "tid": str(it.get("tid")),
                    "text": clean(txt),
                    "raw": txt,
                    "time": int(it.get("created_time") or 0),
                    "cmtnum": int(it.get("cmtnum") or 0),
                    "likes": len(it.get("like") or []),
                    "appid": str(it.get("source_appid") or "311"),
                    "comments": cmts,
                })
            return out
        except Exception as e:
            logger.warning(f"[qzone] 读 {uin} 说说异常: {e}")
            return []

    # --- 点赞 ---
    def like(self, uin: str, tid: str, abstime: int) -> bool:
        _, bkn = self.auth()
        unikey = f"http://user.qzone.qq.com/{uin}/mood/{tid}"
        body = {
            "opuin": self.uin, "unikey": unikey, "curkey": unikey, "from": "1",
            "appid": "311", "typeid": "0", "abstime": str(abstime), "fid": tid,
            "active": "0", "fupdate": "1",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }
        try:
            r = self.s.post(
                "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/"
                f"likes/internal_dolike_app?g_tk={bkn}",
                data=body, headers=self._h(f"https://user.qzone.qq.com/{uin}"), timeout=20)
            d = iframe_json(r.text)
            return d.get("code") == 0
        except Exception as e:
            logger.warning(f"[qzone] 点赞失败 {uin}/{tid}: {e}")
            return False

    # --- 评论 / 回复评论 ---
    def comment(self, uin: str, tid: str, text: str, reply_idx: int | None = None) -> bool:
        """reply_idx 不为空时，表示这是对第 N 条评论的回复。"""
        _, bkn = self.auth()
        body = {
            "topicId": f"{uin}_{tid}", "content": text, "hostUin": uin,
            "uin": self.uin, "format": "json", "plat": "qzone",
            "inCharset": "utf-8", "outCharset": "utf-8", "ref": "feeds",
            "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        }
        if reply_idx:
            body["commentId"] = str(reply_idx)
            body["subdotype"] = "55702"
            body["sceneid"] = "100"
            body["t1_uin"] = uin
            body["t1_tid"] = tid
        try:
            r = self.s.post(
                "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/"
                f"emotion_cgi_re_feeds?g_tk={bkn}",
                data=body, headers=self._h(f"https://user.qzone.qq.com/{uin}"), timeout=20)
            d = iframe_json(r.text)
            if not d:
                d = json.loads(r.text) if r.text.strip().startswith("{") else {}
            ok = d.get("code") == 0
            if not ok:
                logger.info(f"[qzone] 评论返回 {r.text[:120]}")
            return ok
        except Exception as e:
            logger.warning(f"[qzone] 评论失败 {uin}/{tid}: {e}")
            return False

    # --- 发说说（走 NapCat）---
    def publish(self, content: str, images: list | None = None) -> str | None:
        body = {
            "content": content,
            "images": images or [],
            "ugc_right": 1,          # 1 = 所有人可见
            "target_uins": [],
        }
        try:
            r = self.s.post(f"{NAPCAT}/send_qzone_msg", json=body, timeout=90).json()
            if r.get("status") == "ok":
                return str((r.get("data") or {}).get("tid") or "")
            logger.warning(f"[qzone] 发说说失败: {str(r)[:200]}")
            return None
        except Exception as e:
            logger.warning(f"[qzone] 发说说异常: {e}")
            return None


# ---------------- 插件 ----------------
@register("qzone_social", "local", "QQ空间日常：发说说/刷空间/点赞评论/回留言", "1.0.0")
class QzoneSocial(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.api = Qzone()
        self.state = self._load_state()
        self._people = self._load_people()
        self._busy = False
        self._tasks = []
        os.makedirs(PIC_DIR, exist_ok=True)
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self._boot())
        except Exception as e:
            logger.error(f"[qzone] 初始化失败: {e}")

    async def _boot(self):
        await asyncio.sleep(150)          # 让 bili_daily 先跑，别抢
        if not self.api.ready():
            logger.warning("[qzone] 启动时 NapCat 还没准备好，稍后自动重试")
        else:
            logger.info(f"[qzone] 已挂上，账号 {self.api.uin}")
        self._tasks.append(asyncio.create_task(self._post_loop()))
        self._tasks.append(asyncio.create_task(self._social_loop()))

    # ---------- 状态 ----------
    def _today(self):
        return date.today().isoformat()

    def _fresh(self):
        return {"date": self._today(), "counts": {"post": 0, "like": 0, "comment": 0},
                "plan": [], "posted": [], "liked": {}, "commented": {},
                "replied": {}, "quota": {"like": 0, "comment": 0}}

    def _load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                s = json.load(f)
            for k, v in self._fresh().items():
                s.setdefault(k, v)
            return s
        except Exception:
            return self._fresh()

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[qzone] 存状态失败: {e}")

    def _roll(self):
        if self.state.get("date") != self._today():
            logger.info("[qzone] 跨天，重置配额")
            old_liked = self.state.get("liked") or {}
            old_cmt = self.state.get("commented") or {}
            self.state = self._fresh()
            # 只保留最近两天的记录用于防重复
            cutoff = time.time() - 2 * 86400
            self.state["liked"] = {k: v for k, v in old_liked.items() if v > cutoff}
            self.state["commented"] = {k: v for k, v in old_cmt.items() if v > cutoff}
            self._save_state()

    def _load_people(self):
        try:
            with open(PEOPLE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            out = {}
            for umo, v in d.items():
                if not isinstance(v, dict):
                    continue
                m = re.search(r"FriendMessage:(\d+)", umo)
                if m:
                    out[m.group(1)] = {"name": v.get("name") or "",
                                       "msgs": v.get("msgs") or 0}
            return out
        except Exception:
            return {}

    def _is_familiar(self, uin: str) -> bool:
        if int(uin) in FAMILIAR:
            return True
        p = self._people.get(uin) or {}
        return (p.get("msgs") or 0) >= 30

    def _active(self):
        h = datetime.now().hour
        return ACTIVE_HOURS[0] <= h < ACTIVE_HOURS[1]

    # ---------- 人设 ----------
    def _persona(self):
        try:
            cfg = self.context.get_config()
            pid = (cfg.get("agent_runner", {}).get("config", {})
                   .get("persona", {}).get("persona_id"))
            if not pid:
                return None
            p = self.context.persona_manager.get_persona_v3_by_id(pid)
            if not p:
                return None
            return p.get("prompt") if isinstance(p, dict) else getattr(p, "prompt", None)
        except Exception as e:
            logger.warning(f"[qzone] 读人设失败: {e}")
            return None

    async def _llm(self, prompt: str, system: str | None = None,
                   max_try: int = 2) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("[qzone] 没有可用模型")
            return ""
        sys_p = system if system is not None else self._persona()
        for i in range(max_try):
            try:
                resp = await provider.text_chat(
                    prompt=prompt, contexts=None, system_prompt=sys_p)
                txt = (resp.completion_text or "").strip()
                txt = txt.strip("「」\"'“”‘’")
                if txt:
                    return txt
            except Exception as e:
                logger.warning(f"[qzone] 模型调用失败({i+1}): {e}")
                await asyncio.sleep(3)
        return ""

    # ================= 工作流 1：发说说 =================
    def _plan_today(self):
        """给今天排 3~5 个随机时刻，全部落在未来，避免一次性补发好几条。"""
        if self.state.get("plan") and self.state.get("date") == self._today():
            return
        lo, hi = ACTIVE_HOURS
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        start = max(lo * 60, cur + 10)
        end = hi * 60

        if end - start < 30:
            self.state["plan"] = []
            self.state["date"] = self._today()
            self._save_state()
            logger.info("[qzone] 今天剩下的时间不多，不发说说了")
            return

        want = random.randint(*POSTS_PER_DAY)
        room = max(1, (end - start) // 90)          # 每条之间至少隔一个半小时
        n = min(want, room)
        slots = sorted(random.sample(range(start, end), n))
        self.state["plan"] = slots
        self.state["date"] = self._today()
        self._save_state()
        logger.info(f"[qzone] 今天的发说说计划："
                    f"{[f'{s//60:02d}:{s%60:02d}' for s in slots]}")

    def _gather_material(self):
        """凑素材，返回 (类型, 标题/正文, 封面图URL)。"""
        pools = []

        # 1) B站：她刚推过的视频
        try:
            with open(KEEP_FILE, encoding="utf-8") as f:
                kept = json.load(f)
            fresh = [x for x in kept
                     if time.time() - (x.get("ts") or 0) < 36 * 3600
                     and x.get("title") and not blocked(x["title"])]
            if fresh:
                pools.append(("bili", fresh))
        except Exception:
            pass

        # 2) 头条：过滤掉时政和丧事之后剩下的
        try:
            with open(TOUTIAO_FILE, encoding="utf-8") as f:
                d = json.load(f)
            tt = [x for x in (d.get("toutiao") or [])
                  if x.get("title") and not blocked(x["title"])
                  and len(x["title"]) >= 8]
            if tt:
                pools.append(("toutiao", tt))
        except Exception:
            pass

        # 3) 聊天记录：今天聊过什么
        try:
            chats = self._recent_chats()
            if chats:
                pools.append(("chat", chats))
        except Exception:
            pass

        if not pools:
            return None
        # B站最自然（能配图），头条次之，聊天记录只当补充
        weights = [{"bili": 3, "toutiao": 2, "chat": 1}[k] for k, _ in pools]
        kind, items = random.choices(pools, weights=weights, k=1)[0]
        it = random.choice(items)

        if kind == "bili":
            pic = _pic_url(it.get("pic") or "")
            if not pic and it.get("bvid"):
                pic = self._cover(it["bvid"])
            return ("bili", it.get("title", ""), pic)
        if kind == "toutiao":
            return ("toutiao", it.get("title", ""), "")
        return ("chat", it, "")

    def _recent_chats(self):
        """捞今天聊到的内容当素材。

        只留对方说的话，并且必须剔掉三类垃圾：
          - human_presence 注入的时间标记（Current datetime: ...）
          - 她自己推视频的固定话术和链接
          - 任何链接
        这些一旦进了说说，等于把她私下的推送记录摊在空间里。
        """
        BAD_MARKS = ("Current datetime:", "http://", "https://", "www.",
                     "刚刷到", "这个讲得清楚", "这球有点东西", "这个可以看看",
                     "这个好看", "这个氛围不错", "《", "》",
                     "（我刚在QQ空间", "接口自检")
        con = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT user_id, content FROM conversations "
                "ORDER BY updated_at DESC LIMIT 8").fetchall()
        finally:
            con.close()

        out = []
        for umo, content in rows:
            if "qzone-self" in str(umo):
                continue
            try:
                hist = json.loads(content) if isinstance(content, str) else content
            except Exception:
                continue
            if not isinstance(hist, list) or len(hist) < 4:
                continue

            lines = []
            for msg in hist[-10:]:
                if msg.get("role") == "assistant":
                    continue                      # 她自己说的，跳过
                c = msg.get("content")
                if isinstance(c, list):
                    t = "".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
                else:
                    t = str(c)
                t = clean(t)
                t = re.sub(r"Current datetime:.*$", "", t).strip()
                t = re.sub(r"^（?\d{4}-\d{2}-\d{2}[^）]*）?", "", t).strip()
                if any(b in t for b in BAD_MARKS):
                    continue
                if not (6 <= len(t) <= 60):
                    continue
                if blocked(t):
                    continue
                lines.append(t)
            if len(lines) >= 2:
                out.append(" / ".join(lines[-3:]))
        return out

    async def _compose_post(self, kind: str, source: str):
        hint = {
            "bili": f"你刚刷到一个视频，标题是「{source}」。",
            "toutiao": f"你刚看到一条新闻/热点：「{source}」。",
            "chat": (f"今天有人跟你提到：{source[:240]}\n"
                     "（这只是给你一个想法的由头。说说里绝对不能提到这段对话，"
                     "不能出现「今天跟人聊天」「有人说」这类说法，就当是你自己想到的。）"),
        }.get(kind, "")

        prompt = (
            f"{hint}\n"
            "现在你想在 QQ 空间发一条说说。要求：\n"
            "1. 一到两句话，口语，像随手打的，别像作文；\n"
            "2. 总长 15~45 字，不要分点、不要表情符号方括号；\n"
            "3. 只说自己的想法或感受，不要复述素材原文，不要报标题；\n"
            "4. 如果是聊天记录，绝对不能提到对方说了什么，只说你自己的感受；\n"
            "5. 不碰时政、不谈疾病死亡、不带任何广告和链接；\n"
            "6. 直接给说说正文，不要加引号，不要解释。"
        )
        txt = await self._llm(prompt)
        if not txt:
            return None
        txt = txt.split("\n")[0].strip()
        txt = re.sub(r"^[「『\"']|[」』\"']$", "", txt).strip()
        if len(txt) < 8 or len(txt) > 90:
            return None
        if blocked(txt):
            logger.info(f"[qzone] 生成内容含敏感词，丢弃：{txt[:40]}")
            return None
        return txt

    async def _safe_to_post(self, txt: str) -> bool:
        """发布前再自检一次：这句话贴到好友都能看见的空间里，会不会出事。"""
        prompt = (
            "下面这句话准备公开发在你的 QQ 空间，好友都能看到。判断有没有风险。\n"
            f"「{txt}」\n\n"
            "算风险的有：透露别人隐私（姓名、身份、住址、聊天内容）、"
            "像在复述私人对话、涉及政治、涉及他人生病死亡、像广告营销、"
            "会让别人误会或不舒服、读完不知道在说什么。\n"
            "安全就只回答 YES，有风险就只回答 NO。不要解释。"
        )
        r = await self._llm(prompt, system="你是一个谨慎的内容审核员，只回答 YES 或 NO。",
                            max_try=1)
        return r.strip().upper().startswith("Y")

    def _cover(self, bvid: str) -> str:
        """搜索接口不返回封面时，用 view 接口现查一张。"""
        try:
            sess = requests.Session()
            sess.trust_env = False
            r = sess.get("https://api.bilibili.com/x/web-interface/view",
                         params={"bvid": bvid},
                         headers={"User-Agent": UA,
                                  "Referer": "https://www.bilibili.com/"},
                         timeout=15)
            return _pic_url(((r.json().get("data") or {}).get("pic")) or "")
        except Exception as e:
            logger.info(f"[qzone] 查封面失败 {bvid}: {e}")
            return ""

    def _download_pic(self, url: str, tag: str):
        """下 B站封面（带 Referer，不然 403）。"""
        url = _pic_url(url)
        if not url:
            return None
        try:
            p = os.path.join(PIC_DIR, f"{tag}.jpg")
            r = requests.get(url, headers={"Referer": "https://www.bilibili.com/",
                                           "User-Agent": UA},
                             timeout=20, stream=True)
            if r.status_code != 200:
                return None
            with open(p, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            if os.path.getsize(p) < 3000:
                os.remove(p)
                return None
            return p
        except Exception:
            return None

    async def _do_post(self):
        if self.state["counts"]["post"] >= POSTS_PER_DAY[1]:
            return
        # 一条素材被自检否掉就换下一条，最多试 3 次，别因为挑到烂素材就整轮跳过
        mat = txt = None
        used = set()
        for attempt in range(6):
            m = self._gather_material()
            if not m:
                logger.info("[qzone] 今天没凑到素材，这轮跳过")
                return
            kind, src, pic = m
            if src in used:
                continue                      # 挑重了，重新抽
            used.add(src)
            t = await self._compose_post(kind, src)
            if not t:
                continue
            if not await self._safe_to_post(t):
                logger.info(f"[qzone] 自检否掉：{t[:40]}，换素材")
                await asyncio.sleep(2)
                continue
            mat, txt = m, t
            break
        if not txt:
            logger.info("[qzone] 三条素材都没过自检，这轮先不发")
            return
        kind, src, pic = mat

        images = []
        if pic:
            p = self._download_pic(pic, hkey(txt, time.time()))
            if p:
                images.append(p)

        tid = self.api.publish(txt, images)
        if tid is None:
            return
        self.state["counts"]["post"] += 1
        self.state["posted"].append({"tid": tid, "text": txt,
                                     "time": int(time.time()), "kind": kind})
        self.state["posted"] = self.state["posted"][-30:]
        self._save_state()
        logger.info(f"[qzone] 发了说说（{kind}，{self.state['counts']['post']} 条）：{txt}")
        await self._remember_post(txt)

    async def _remember_post(self, text: str):
        """让她记得自己发过这条，聊天时能提。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id("qzone-self")
            if not cid:
                cid = await cm.new_conversation("qzone-self", "主人")
            conv = await cm.get_conversation("qzone-self", cid)
            hist = conv.content if conv else []
            if isinstance(hist, str):
                hist = json.loads(hist)
            hist = hist or []
            hist.append({"role": "assistant", "content": f"（我刚在QQ空间发了条说说：{text}）"})
            await cm.update_conversation("qzone-self", cid, hist[-80:])
        except Exception as e:
            logger.warning(f"[qzone] 记录说说失败: {e}")

    async def _post_loop(self):
        while True:
            try:
                if not self._active():
                    await asyncio.sleep(POST_CHECK)
                    continue
                self._roll()
                self._plan_today()
                now = datetime.now()
                cur = now.hour * 60 + now.minute
                for slot in list(self.state.get("plan") or []):
                    if cur < slot:
                        continue
                    if self.state["counts"]["post"] >= POSTS_PER_DAY[1]:
                        break
                    self.state["plan"].remove(slot)
                    self._save_state()
                    logger.info(f"[qzone] 到点了（计划 {slot//60:02d}:{slot%60:02d}），准备发说说")
                    await self._do_post()
                    await asyncio.sleep(random.uniform(30, 90))
                    break
            except Exception as e:
                logger.error(f"[qzone] 发说说循环异常: {e}")
            await asyncio.sleep(POST_CHECK)

    # ================= 工作流 2：刷空间 =================
    async def _scroll(self):
        friends = self.api.friends()
        # 好友列表里会包含自己，不加这行她会给自己点赞
        friends = [f for f in friends if f["uin"] != str(self.api.uin)]
        if not friends:
            return
        # 游标轮转：每轮扫一小批，保证几轮之内把所有人都覆盖到。
        # 之前是随机抽取，导致很多好友的动态永远轮不到；
        # 但一次性全量扫描会触发腾讯 WAF（HTTP 501），所以只能这样折中。
        friends.sort(key=lambda x: x["uin"])
        pos = self.state.get("scan_pos") or 0
        n = min(SCAN_FRIENDS, len(friends))
        batch = [friends[(pos + i) % len(friends)] for i in range(n)]
        self.state["scan_pos"] = (pos + n) % len(friends)
        cands = []
        for f in batch:
            feeds = self.api.feeds(f["uin"], num=2)
            for fd in feeds:
                if time.time() - fd["time"] > FEED_MAX_AGE:
                    continue
                if not fd["text"] or len(fd["text"]) < 6:
                    continue
                if blocked(fd["text"]):
                    continue
                key = hkey(fd["uin"], fd["tid"])
                # 点赞和评论分别记，不然点过赞的说说就再也没机会被评论
                done_like = key in self.state["liked"]
                done_cmt = key in self.state["commented"]
                if done_like and done_cmt:
                    continue
                fd["_name"] = f["name"] or f["nick"]
                fd["_key"] = key
                fd["_liked"] = done_like
                fd["_commented"] = done_cmt
                fd["_fam"] = self._is_familiar(fd["uin"])
                cands.append(fd)
            await asyncio.sleep(random.uniform(3.5, 6.5))

        if not cands:
            logger.info("[qzone] 这轮没有值得互动的新动态")
            return

        # 点赞：这一轮看到的、还没赞过的，全赞
        todo_like = [c for c in cands if not c["_liked"]]
        random.shuffle(todo_like)
        likes = 0
        for fd in todo_like:
            if likes >= min(LIKE_PER_ROUND, DAILY_LIKE - self.state["quota"]["like"]):
                break
            if self.api.like(fd["uin"], fd["tid"], fd["time"]):
                self.state["liked"][fd["_key"]] = int(time.time())
                self.state["quota"]["like"] += 1
                likes += 1
                logger.info(f"[qzone] 赞了 {fd['_name']}：{fd['text'][:30]}")
            await asyncio.sleep(random.uniform(4, 10))
        self._save_state()

        # 评论：这一轮看到的、还没评过的，都评（不再只看熟人）
        if self.state["quota"]["comment"] >= DAILY_COMMENT:
            return
        todo_cmt = [c for c in cands if not c["_commented"]]
        if not todo_cmt:
            return
        random.shuffle(todo_cmt)
        for fd in todo_cmt[:COMMENT_PER_ROUND]:
            if self.state["quota"]["comment"] >= DAILY_COMMENT:
                break
            txt = await self._compose_comment(fd)
            if not txt:
                continue
            if self.api.comment(fd["uin"], fd["tid"], txt):
                self.state["commented"][fd["_key"]] = int(time.time())
                self.state["quota"]["comment"] += 1
                self.state["commented"][fd["_key"] + "_ctx"] = {
                    "uin": fd["uin"], "tid": fd["tid"], "name": fd["_name"],
                    "feed": fd["text"][:80], "my": txt, "t": int(time.time()),
                }
                self._save_state()
                logger.info(f"[qzone] 评论了 {fd['_name']}：{txt}")
            await asyncio.sleep(random.uniform(15, 30))

    async def _compose_comment(self, fd):
        prompt = (
            f"你朋友「{fd['_name']}」在 QQ 空间发了一条：\n「{fd['text'][:150]}」\n\n"
            "你看到了，想留一句评论。要求：\n"
            "1. 一句话，8~20 字，像熟人随口搭一句，可以带一点调侃或共鸣；\n"
            "2. 不要问「怎么了」「还好吗」这种客套，不要用感叹号堆情绪；\n"
            "3. 不评论对方的外貌、身材、感情状态；不碰时政；不提疾病死亡；\n"
            "4. 不许提动态里没出现过的东西，不要编造对方做过什么事；\n"
            "5. 不要提你是 AI，不要解释；\n"
            "6. 直接给评论内容，不加引号。如果这条动态你不知道该说什么，"
            "就只回一个「SKIP」。"
        )
        txt = await self._llm(prompt)
        txt = (txt or "").split("\n")[0].strip()
        if not txt or "SKIP" in txt.upper():
            return None
        txt = re.sub(r"^[「『\"']|[」』\"']$", "", txt).strip()
        if len(txt) < 3 or len(txt) > 40 or blocked(txt):
            return None
        return txt

    async def _social_loop(self):
        while True:
            await asyncio.sleep(random.uniform(20, 60))
            try:
                if not self._active():
                    await asyncio.sleep(SOCIAL_TICK)
                    continue
                if not self.api.ready():
                    await asyncio.sleep(SOCIAL_TICK)
                    continue
                self._roll()
                await self._scroll()
                await self._reply()
            except Exception as e:
                logger.error(f"[qzone] 刷空间异常: {e}")
            await asyncio.sleep(SOCIAL_TICK + random.uniform(-300, 300))

    # ================= 工作流 3：回留言 =================
    async def _reply(self):
        done = 0

        # A. 自己说说下的评论
        mine = self.api.feeds(self.api.uin, num=5)
        today = self._today()
        for fd in mine:
            if done >= REPLY_PER_ROUND:
                break
            if not fd["comments"]:
                continue
            if time.time() - fd["time"] > REPLY_MAX_AGE:
                continue                      # 三天前的旧说说不翻出来回
            feed_key = hkey("feed", today, fd["tid"])
            if self.state["replied"].get(feed_key, 0) >= REPLY_PER_FEED:
                continue                      # 同一条说说今天回满了
            # 所有还没回过的留言，按先后顺序逐个回（老的先回，读起来才连贯）
            pend = []
            for group in fd["comments"]:
                head = group[0]
                if head["uin"] == self.api.uin:
                    continue                    # 我自己发的顶层评论
                if any(s["uin"] == self.api.uin for s in group[1:]):
                    continue                    # 我已经在这人下面接过话了
                k = hkey("r", fd["tid"], head["uin"], head["text"])
                if k in self.state["replied"]:
                    continue
                pend.append((head, k))
            for head, k in pend:
                if done >= REPLY_PER_ROUND:
                    break
                if self.state["replied"].get(feed_key, 0) >= REPLY_PER_FEED:
                    break
                txt = await self._compose_reply(head["name"] or head["uin"],
                                                fd["text"], head["text"])
                if not txt:
                    continue
                # 这个接口只能发顶层评论，造不出层级回复，所以紧接着往下接一句
                ok = self.api.comment(self.api.uin, fd["tid"], txt)
                if ok:
                    self.state["replied"][k] = int(time.time())
                    self.state["replied"][feed_key] = \
                        self.state["replied"].get(feed_key, 0) + 1
                    done += 1
                    logger.info(f"[qzone] 接了 {head['name']} 的话：{txt}")
                    self._save_state()
                await asyncio.sleep(random.uniform(10, 25))

        # B. 我评论过的说说，别人又回了我
        # 只发顶层评论的话，在别人的说说下发一句没头没脑的话最招人烦，
        # 所以这条链路默认关掉，要开自己改 REPLY_IN_THREADS。
        if not REPLY_IN_THREADS:
            return
        ctxs = {k[:-4]: v for k, v in self.state["commented"].items()
                if isinstance(v, dict) and k.endswith("_ctx")}
        # 按评论时间倒序，先看最近的
        ctxs = dict(sorted(ctxs.items(), key=lambda kv: kv[1].get("t") or 0,
                           reverse=True))
        for key, ctx in list(ctxs.items())[:8]:
            if done >= REPLY_PER_ROUND:
                break
            if time.time() - (ctx.get("t") or 0) > REPLY_MAX_AGE:
                continue                      # 太久远的追问不追了
            feeds = self.api.feeds(ctx["uin"], num=3)
            target = next((x for x in feeds if x["tid"] == ctx["tid"]), None)
            if not target:
                continue
            for gi, g in enumerate(target["comments"], 1):
                if done >= REPLY_PER_ROUND:
                    break
                # 找别人在我那条评论下面的追问
                mine_idx = None
                for i, c in enumerate(g):
                    if c["uin"] == self.api.uin:
                        mine_idx = i
                if mine_idx is None:
                    continue
                later = [c for c in g[mine_idx + 1:] if c["uin"] != self.api.uin]
                if not later:
                    continue
                last = later[-1]
                k = hkey("f", ctx["tid"], gi, last["uin"], last["text"])
                if k in self.state["replied"]:
                    continue
                txt = await self._compose_reply(last["name"] or last["uin"],
                                                target["text"], last["text"])
                if not txt:
                    continue
                # 层级回复造不出来（接口假成功），所以在对方说说下发一句带 @ 的
                body = f"@{last['name'] or last['uin']} {txt}"
                if self.api.comment(ctx["uin"], ctx["tid"], body):
                    self.state["replied"][k] = int(time.time())
                    done += 1
                    logger.info(f"[qzone] 回了 {last['name']} 的追问：{txt}")
                    self._save_state()
                await asyncio.sleep(random.uniform(10, 25))

    async def _compose_reply(self, who: str, feed: str, their: str):
        prompt = (
            f"你的 QQ 空间说说（或评论）是：「{feed[:120]}」\n"
            f"「{who}」在这条下面留了一句：「{their[:100]}」\n\n"
            "你想接着说一句，会作为一条新评论紧跟在对方后面发出去。要求：\n"
            "1. 一句话，6~25 字，像朋友之间随口接话，可以直接回答对方；\n"
            "2. 必须能接住对方那句话的意思，因为界面上看不到你在回谁，"
            "读起来要像顺着往下说；\n"
            "3. 绝对不要重复对方刚说的话，也不要只回「嗯」「哈哈」「好」这种；\n"
            "4. 不许编造正文里没提过的东西：你养了什么、去了哪、认识谁、"
            "做过什么，正文里没有就别编；\n"
            "5. 不碰时政，不提疾病死亡，不谈钱；\n"
            "6. 直接给内容，不加引号。没把握怎么接就回「SKIP」。"
        )
        txt = await self._llm(prompt)
        txt = (txt or "").split("\n")[0].strip()
        if not txt or "SKIP" in txt.upper():
            return None
        txt = re.sub(r"^[「『\"']|[」』\"']$", "", txt).strip()
        if len(txt) < 2 or len(txt) > 50 or blocked(txt):
            return None
        return txt

    async def terminate(self):
        for t in self._tasks:
            t.cancel()
        self._save_state()
        logger.info("[qzone] 已停止")
