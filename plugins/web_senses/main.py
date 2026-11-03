"""web_senses —— 让她「有手机可刷」。

三件事：
1. 后台每半小时偷偷刷一遍今日头条热榜和百度热搜，存在本地缓存里。
2. 每次要回话时，把「你今天刷到的几条」塞进上下文，这样她能自然地聊起，
   而不是每句话都现去查、也不至于被人问「今天有啥新闻」时哑口无言。
3. 给她三个工具：查热榜、搜东西、读链接。用户丢过来一个链接，她真的能看。

数据源都是公开页面，不登录、不碰 cookie。抓失败就静默降级：
宁可说「我还没刷到」，也不编一条假新闻。
"""

import asyncio
import ipaddress
import json
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# ============ 配置 ============

# 热榜缓存多久刷一次（秒）
CACHE_TTL = 1800

# 首次启动若缓存为空，延后多久去抓（秒），别和启动流程抢资源
WARMUP_DELAY = 8

# 注入上下文的热榜条数
INJECT_TOP = 7

# 注入内容的总字数上限，超了会挤占人设和历史
MAX_INJECT_CHARS = 620

# 单次抓取超时
TIMEOUT = 12

# 读网页正文时最多返回多少字
READ_LIMIT = 900

CACHE_FILE = Path(__file__).with_name("cache.json")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 不允许抓的地址（SSRF 防护：内网/回环/保留段）
_BLOCKED_HOST_EXACT = ("localhost", "0.0.0.0")

# 时政/军事/外交类过滤词。
# 她是个普通大二女生，聊涉台、外交、中东局势会立刻穿帮；
# 这类内容也不适合让她主动聊起来，所以热榜里直接跳过，往后找能聊的。
SENSITIVE_WORDS = (
    # 政治与政策
    "习近平", "总书记", "党中央", "政治局", "国务院", "中央", "人大", "政协",
    "全会", "党建", "党的领导", "乡村振兴", "乡村全面振兴", "新质生产力",
    "高质量发展", "共同富裕", "脱贫攻坚", "十四五", "十五五", "规划纲要",
    "反腐", "纪委", "监委", "落马", "双规", "巡视",
    # 外交与军事
    "中方", "外交部", "国台办", "国防部", "商务部", "涉台", "台独", "台湾",
    "两岸", "香港", "澳门", "新疆", "西藏", "南海", "钓鱼岛", "主权", "领土",
    "军委", "解放军", "军演", "军事", "军队", "导弹", "军舰", "战机", "航母",
    "领海", "领空", "长蛇阵", "石垣岛",
    "制裁", "封锁", "关税", "贸易战", "外交", "使馆", "领馆",
    # 外国政要
    "特朗普", "普京", "内塔尼亚胡", "拜登", "泽连斯基", "马克龙", "邱毅",
    "以色列", "巴勒斯坦", "乌克兰", "俄罗斯", "美国", "日本", "朝鲜", "美联储",
    "选举", "议席", "议会", "民调", "执政", "政变", "内阁",
    # 官媒与党建宣传
    "人民日报", "新华社", "央视", "环球时报", "浙江宣传", "钧正平",
    "红军", "长征", "党史",
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 别让系统代理把本地请求也劫走
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def _decode(r: requests.Response) -> str:
    """按 header -> meta -> 猜测的顺序定编码，中文站点经常报错。"""
    enc = (r.encoding or "").lower()
    if not enc or enc in ("iso-8859-1", "ascii"):
        m = re.search(rb'charset=["\']?([\w-]+)', r.content[:4096], re.I)
        r.encoding = m.group(1).decode("ascii", "ignore") if m else r.apparent_encoding
    return r.text


# ============ 各数据源 ============


def fetch_toutiao(limit: int = 20) -> list[dict]:
    """今日头条热榜。返回 [{title, url, hot}]"""
    s = _session()
    r = s.get(
        "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    out = []
    for it in data[:limit]:
        title = (it.get("Title") or "").strip()
        url = (it.get("Url") or "").strip()
        if not title or not url:
            continue
        out.append({"title": title, "url": url, "hot": it.get("HotValue") or 0})
    return out


def fetch_baidu(limit: int = 15) -> list[dict]:
    """百度热搜。返回 [{title, url, summary}]"""
    s = _session()
    r = s.get("https://top.baidu.com/board?tab=realtime", timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(_decode(r), "lxml")
    out = []
    for block in soup.select("div[class*=category-wrap]"):
        t = block.select_one("div[class*=c-single-text]")
        if not t:
            continue
        title = t.get_text(" ", strip=True)
        if not title:
            continue
        a = block.select_one("a[href]")
        url = a["href"] if a else ""
        if url.startswith("/"):
            url = "https://top.baidu.com" + url
        d = block.select_one("div[class*=hot-desc]")
        summary = d.get_text(" ", strip=True) if d else ""
        out.append({"title": title, "url": url, "summary": summary})
        if len(out) >= limit:
            break
    return out


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def is_talkable(item: dict) -> bool:
    """这条适不适合让她聊。命中时政/军事词就跳过。"""
    blob = (item.get("title") or "") + " " + (item.get("summary") or "")
    return not any(w in blob for w in SENSITIVE_WORDS)


def search_toutiao(keyword: str, limit: int = 6) -> list[dict]:
    """头条搜索。标题可靠，链接给搜索页兜底（真实跳转链带 token，会过期）。"""
    s = _session()
    r = s.get(
        "https://so.toutiao.com/search",
        params={"keyword": keyword, "pd": "synthesis"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    html = _decode(r)

    # 页面里内联了 JSON，标题和摘要都在里面，比解析 DOM 稳
    titles = re.findall(r'"title"\s*:\s*"((?:[^"\\]|\\.){6,90})"', html)
    abstracts = re.findall(r'"(?:abstract|summary|content)"\s*:\s*"((?:[^"\\]|\\.){20,300})"', html)

    def unesc(raw: str) -> str:
        # 抓到的其实是 JSON 字符串里面的内容，交给 json 处理 \u 和 \" 才安全。
        # 用 encode().decode('unicode_escape') 会把正常中文也弄坏。
        try:
            x = json.loads('"' + raw + '"')
        except Exception:
            x = raw
        return strip_html(x)

    out, seen = [], set()
    for raw in titles:
        t = unesc(raw)
        if not t or len(t) < 6 or t in seen:
            continue
        seen.add(t)
        out.append({"title": t, "summary": ""})
        if len(out) >= limit:
            break

    for i, raw in enumerate(abstracts[: len(out)]):
        out[i]["summary"] = unesc(raw)[:120]

    page = "https://so.toutiao.com/search?keyword=" + quote(keyword)
    for it in out:
        it["url"] = page
    return out


def read_page(url: str, limit: int = READ_LIMIT) -> str:
    """抓网页正文。取不到正文就退回 meta description。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return ""
    except ValueError:
        if host in ("localhost", "0.0.0.0") or host.endswith(".local"):
            return ""

    s = _session()
    r = s.get(url, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(_decode(r), "lxml")

    title = (soup.title.get_text(strip=True) if soup.title else "") or ""

    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    body = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in body.find_all(["p", "h1", "h2", "h3"])]
    paras = [p for p in paras if len(p) >= 18]

    text = "\n".join(paras)
    if len(text) < 80:
        meta = soup.find("meta", attrs={"name": "description"})
        text = (meta.get("content") if meta else "") or text
    text = text[:limit]

    return (title + "\n" + text).strip() if title else text


# ============ 插件 ============


@register(
    "web_senses",
    "local",
    "给她一部手机：会刷热榜、会搜东西、会读链接",
    "1.0.0",
)
class WebSenses(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._cache: dict = {"toutiao": [], "baidu": [], "updated": 0}
        self._lock = asyncio.Lock()
        self._load_cache()
        self._task = asyncio.create_task(self._refresh_loop())

    # ---------- 缓存 ----------

    def _load_cache(self):
        try:
            if CACHE_FILE.exists():
                self._cache.update(json.loads(CACHE_FILE.read_text(encoding="utf-8")))
                age = time.time() - (self._cache.get("updated") or 0)
                logger.info(
                    f"[web_senses] 缓存载入：头条 {len(self._cache.get('toutiao', []))} 条"
                    f" / 百度 {len(self._cache.get('baidu', []))} 条，{int(age)}s 前"
                )
        except Exception as e:
            logger.warning(f"[web_senses] 读缓存失败: {e}")

    def _save_cache(self):
        try:
            CACHE_FILE.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[web_senses] 写缓存失败: {e}")

    async def _refresh(self):
        """抓一轮。两个源独立 try，一个挂了不影响另一个。"""
        loop = asyncio.get_event_loop()
        got = {}
        try:
            got["toutiao"] = await loop.run_in_executor(None, fetch_toutiao, 20)
        except Exception as e:
            logger.warning(f"[web_senses] 头条热榜抓取失败: {type(e).__name__}: {e}")
        try:
            got["baidu"] = await loop.run_in_executor(None, fetch_baidu, 15)
        except Exception as e:
            logger.warning(f"[web_senses] 百度热搜抓取失败: {type(e).__name__}: {e}")

        async with self._lock:
            for k, v in got.items():
                if v:
                    self._cache[k] = v
            if got:
                self._cache["updated"] = time.time()
                self._save_cache()
                logger.info(
                    f"[web_senses] 刷新完成：头条 {len(self._cache['toutiao'])} 条"
                    f" / 百度 {len(self._cache['baidu'])} 条"
                )

    async def _refresh_loop(self):
        await asyncio.sleep(WARMUP_DELAY)
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[web_senses] 刷新循环异常: {e}")
            await asyncio.sleep(CACHE_TTL)

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject_what_i_saw(self, event: AstrMessageEvent, req):
        """把「今天刷到的」塞进系统提示，让她有话题可聊。"""
        if time.time() - (self._cache.get("updated") or 0) > CACHE_TTL * 3:
            return  # 缓存太旧，宁可不说，也不聊三天前的新闻

        lines, total = [], 0
        pool = [it for it in self._cache.get("toutiao", []) if is_talkable(it)]
        for it in pool[:INJECT_TOP]:
            line = "- " + it["title"]
            if total + len(line) > MAX_INJECT_CHARS:
                break
            lines.append(line)
            total += len(line)

        if not lines:
            return

        block = (
            "\n\n# 你今天刷到过的\n"
            "（你刚才刷手机看到的头条热榜，不用主动汇报，也别硬往话题上拐。\n"
            " 对方聊到相关的事，你可以自然接一句；被问到就说你刷到了什么。\n"
            " 只聊这上面的，细节记不清就说不记得，不许自己补细节编故事。）\n"
            + "\n".join(lines)
        )
        req.system_prompt = (req.system_prompt or "") + block
        logger.info(f"[web_senses] 注入 {len(lines)} 条热榜（{total} 字）")

    # ---------- 工具 ----------

    @filter.llm_tool(name="check_hot_board")
    async def check_hot_board(
        self, event: AstrMessageEvent, source: str = "toutiao", limit: int = 10
    ):
        """查看当前网络热榜，拿到热门话题标题和链接。

        Args:
            source(string): 榜单来源，toutiao 为今日头条热榜，baidu 为百度热搜
            limit(number): 返回条数，1 到 20，默认 10
        """
        try:
            limit = max(1, min(int(limit or 10), 20))
        except Exception:
            limit = 10
        source = (source or "toutiao").lower().strip()
        if source not in ("toutiao", "baidu"):
            source = "toutiao"

        # 缓存过期就先抓一次
        if time.time() - (self._cache.get("updated") or 0) > CACHE_TTL:
            await self._refresh()

        pool = [it for it in (self._cache.get(source) or []) if is_talkable(it)]
        items = pool[:limit]
        if not items:
            if self._cache.get(source):
                return "榜上这会儿全是些你不看的事（时政、外国那些），别的一条没有。"
            return "没刷出来，热榜这会儿打不开。别编，就说你现在没看到。"

        label = "今日头条热榜" if source == "toutiao" else "百度热搜"
        rows = [f"{i + 1}. {it['title']}\n   {it.get('url', '')}" for i, it in enumerate(items)]
        return (
            f"{label}（当前，已跳过时政类）:\n" + "\n".join(rows)
            + "\n（这些是能聊的。政治军事那些你本来也不看。）"
        )

    @filter.llm_tool(name="search_web")
    async def search_web(self, event: AstrMessageEvent, keyword: str, limit: int = 6):
        """上网搜一个关键词，看看大家都在说什么。

        Args:
            keyword(string): 搜索关键词
            limit(number): 返回条数，默认 6
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return "关键词是空的，没法搜。"
        try:
            limit = max(1, min(int(limit or 6), 10))
        except Exception:
            limit = 6

        loop = asyncio.get_event_loop()
        try:
            items = await loop.run_in_executor(None, search_toutiao, keyword, limit)
        except Exception as e:
            logger.warning(f"[web_senses] 搜索失败: {e}")
            return f"搜「{keyword}」的时候没连上，你先凭印象聊，别说你查过了。"

        if not items:
            return f"搜「{keyword}」没搜到东西。"

        rows = []
        for i, it in enumerate(items):
            row = f"{i + 1}. {it['title']}"
            if it.get("summary"):
                row += f"\n   {it['summary']}"
            rows.append(row)
        rows.append(f"\n搜索页: https://so.toutiao.com/search?keyword={quote(keyword)}")
        return f"关于「{keyword}」:\n" + "\n".join(rows)

    @filter.llm_tool(name="read_link")
    async def read_link(self, event: AstrMessageEvent, url: str):
        """打开一个网页链接，读里面的内容。

        Args:
            url(string): 完整的网页地址，必须以 http 开头
        """
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return "这个不是网页地址，打不开。"

        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, read_page, url)
        except Exception as e:
            logger.warning(f"[web_senses] 读链接失败 {url}: {type(e).__name__}: {e}")
            return "这个页面打不开，可能被墙了或者要登录。跟他说你点进去是空的。"

        if not text or len(text) < 60:
            return "页面打开了但没读到正文，可能全是图和脚本。老实说没看清。"

        return f"页面内容（原文，前 {len(text)} 字）:\n{text}"

    async def terminate(self):
        try:
            self._task.cancel()
        except Exception:
            pass
