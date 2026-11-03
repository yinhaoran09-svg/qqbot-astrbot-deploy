# QQ 聊天机器人：NapCat + AstrBot 实战部署手册

> 把一个大模型塞进 QQ，让它像一个真人在打字。本文档是完整复刻路径，含原理、逐步操作、配置细节和一份真实的踩坑清单。
>
> 实测环境：Windows 10/11 + AstrBot v4.29.3 + NapCat 4.18.41（内嵌 QQ 9.9.33）+ 任意 OpenAI 兼容接口。

---

## 目录

- [一、这东西是怎么工作的](#一这东西是怎么工作的)
- [二、动手之前先想清楚三件事](#二动手之前先想清楚三件事)
- [三、环境准备](#三环境准备)
- [四、部署步骤](#四部署步骤)
- [五、让回复像真人（核心）](#五让回复像真人核心)
- [六、四个必备插件](#六四个必备插件)
- [七、运维脚本](#七运维脚本)
- [八、踩坑实录](#八踩坑实录)
- [九、风控与安全](#九风控与安全)
- [十、目录结构](#十目录结构)
- [十一、进阶插件：让她有自己的生活](#十一进阶插件让她有自己的生活)

---

## 一、这东西是怎么工作的

QQ 官方没有开放个人号机器人接口，所以整条链路是「**劫持客户端 + 协议转换 + 大模型**」三段式：

```
┌──────────┐   NTQQ 协议   ┌──────────┐  OneBot v11   ┌──────────┐   HTTPS   ┌─────────┐
│ 手机 QQ  │ ───────────> │  NapCat  │ ────────────> │ AstrBot  │ ────────> │  LLM API │
│ (你本人)  │   扫码登录    │ (Shell)  │  WebSocket    │ (大脑)   │  OpenAI   │          │
└──────────┘               └──────────┘               └──────────┘  兼容协议  └─────────┘
                                │                          │
                         内嵌独立 QQ 9.9.33         插件 / 人设 / 分段回复
```

三段各干什么：

| 组件 | 角色 | 关键点 |
|---|---|---|
| **NapCat** | 把 QQ 协议翻译成 HTTP/WebSocket | 本质是往 QQ NT 客户端里注入一个 Node.js 插件，所以必须配它支持的**精确 QQ 版本** |
| **AstrBot** | 消息编排中枢 | 收消息 → 拼上下文 → 调模型 → 拆段 → 回消息；有 Web 面板和插件系统 |
| **LLM** | 说话的那张嘴 | 任何 OpenAI 兼容接口都行（DeepSeek、通义、Ollama、各种网关） |

**方向很重要**：是 NapCat 主动连 AstrBot（`websocketClients`），不是反过来。所以 AstrBot 先起，监听 6199，NapCat 后起去连它。搞反了会一直「连不上」。

---

## 二、动手之前先想清楚三件事

**1. 用哪个号**

不要让机器人占用你的主号。主号一旦被风控，你连密码都改不回来，微信支付、游戏账号全在里面。用一个平时不用的号，最好养几天再加好友。

**2. 本地跑还是服务器跑**

| 方案 | 优点 | 缺点 |
|---|---|---|
| 自己电脑 | 五分钟搞定，免费 | 关机就下线；家庭宽带 IP 容易触发风控 |
| 云服务器 | 24 小时在线，IP 干净 | 需要备案不了 QQ 登录，且**扫码必须本机做一次**，然后拷 cache 到服务器 |

新手强烈建议先本机跑通，再考虑搬家。

**3. 预期是什么**

这不是「AI 助手」，是「假装成人的 AI」。做好了朋友聊半天发现不了，做不好三句话就露。第五节讲的就是怎么不露。

---

## 三、环境准备

| 依赖 | 版本 | 说明 |
|---|---|---|
| Windows | 10 / 11 | Linux/macOS 也能跑，NapCat 有 Docker 版，本文按 Windows 写 |
| Python | 3.10 – 3.12 | **别用 3.13**，部分依赖还没适配，jieba 会编译失败 |
| Git | 任意 | 拉源码用 |
| QQ | 9.9.33 | NapCat 安装器会自动带一个，**不要用你系统里已经装的版本** |

---

## 四、部署步骤

### 4.1 装 AstrBot

```bash
git clone https://github.com/AstrBotDevs/AstrBot.git
cd AstrBot

# 用 venv，不要用 uv sync（见踩坑 #1）
python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

启动：

```bash
python main.py
```

看到 `AstrBot v4.x.x` 和 `http://localhost:6185` 就是成功了。

> **面板默认账号**：`astrbot` / `astrbot`。第一次登录后**立刻改密码**，并且不要把 6185 暴露到公网。

### 4.2 装 NapCat

去 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下 Windows 一键版（`NapCat.Shell.Windows.OneKey`）。

运行后它会：

1. 在你指定的目录里**解压一份独立的 QQ 9.9.33**（不碰系统的 QQ）
2. 显示一个二维码

⚠️ **关键点**：这个独立 QQ 和你电脑上日常用的 QQ 是两个程序、两份数据。扫码时用**手机QQ**扫，登录的是机器人号。

### 4.3 扫码登录

终端里会出现二维码，也会有：

```
二维码解码URL: https://txz.qq.com/p?k=xxxx
二维码已保存到 ...\cache\qrcode.png
```

二维码有效期约 2 分钟。扫不上就用解码 URL 去任意二维码网站重新生成一张大的。

登录成功后终端会打印：

```
[info] QQ xxx 登录成功
[info] WebSocket Server 已启动 0.0.0.0:6099
```

### 4.4 打通 OneBot 通道（最容易卡住的一步）

**先确认 AstrBot 在监听**：

```bash
netstat -ano | findstr ":6199"
# 应该看到 LISTENING
```

**然后让 NapCat 去连它**。有两条路：

**路线 A：WebUI 点一点（推荐新手）**

打开 `http://127.0.0.1:6099/webui?token=<你的token>`（token 在 NapCat 启动日志里），进「网络配置 → 新建 → WebSocket 客户端」：

| 字段 | 填什么 |
|---|---|
| 名称 | AstrBot |
| URL | `ws://127.0.0.1:6199/ws` |
| 消息格式 | array |
| Token | 留空 |
| 心跳间隔 | 30000 |

保存后**不需要重启 QQ**，NapCat 会热重载。

**路线 B：直接改配置文件**

NapCat 的配置在：

```
<NapCat目录>/versions/<版本号>/resources/app/napcat/config/onebot11_<QQ号>.json
```

把 `websocketClients` 改成：

```json
{
  "network": {
    "websocketClients": [
      {
        "name": "AstrBot",
        "enable": true,
        "url": "ws://127.0.0.1:6199/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "",
        "debug": false,
        "heartInterval": 30000,
        "reconnectInterval": 30000
      }
    ]
  }
}
```

改完必须重启 NapCat。

**验证通了**：

```bash
netstat -ano | findstr ":6199" | findstr ESTABLISHED
```

有 ESTABLISHED 就对了。AstrBot 面板的「平台」页也会显示 `aiocqhttp` 在线。

### 4.5 接模型

面板 → 配置 → 服务提供商 → 新增。任何 OpenAI 兼容的都行：

| 字段 | 值 |
|---|---|
| 类型 | OpenAI Chat Completion |
| API Base | 你的接口地址，例如 `https://api.deepseek.com/v1` |
| API Key | 你的 key |
| 模型 | 例如 `deepseek-chat` |

> 如果用自建网关 / 校内网关，`API Base` 要带 `/v1` 后缀，模型名填网关侧的真实名字。网关返回 500 是常事，见插件 `mask_llm_error`。

点「测试」通了再往下走。

### 4.6 最小可用验证

私聊机器人一句「在吗」。有回就通了。

没回按顺序查：

1. 6199 有 ESTABLISHED 吗
2. AstrBot 日志有没有 `收到消息` 
3. 模型测试通了吗
4. 是不是被白名单挡了（`enable_id_white_list`）

---

## 五、让回复像真人（核心）

前面都是管道工程，这一段才是真正决定成败的地方。**默认配置的模型回复，一眼假**：一次一大段、带列表、带表情符号、动不动「首先…其次…」。

### 5.1 三段式改造

| 层 | 作用 | 工具 |
|---|---|---|
| **提示词** | 约束内容和语气 | 人设 system_prompt |
| **分段发送** | 一条拆成多条，还原打字节奏 | AstrBot 内置 `segmented_reply` |
| **随机延迟** | 让每段之间有真实间隔 | 同上 |

### 5.2 分段回复配置

面板 → 配置 → 平台设置 → 分段回复，或者直接改 `data/cmd_config.json` 的 `platform_settings.segmented_reply`：

```json
{
  "enable": true,
  "only_llm_result": true,
  "interval_method": "random",
  "interval": "1.0,2.6",
  "words_count_threshold": 150,
  "split_mode": "regex",
  "regex": ".*?[。？！~…\\n]+|.+$",
  "split_words": ["。", "？", "！", "~", "…"],
  "content_cleanup_rule": "。\\s*$"
}
```

逐个解释：

- `only_llm_result`：只拆模型回复，不碰命令输出。开着。
- `interval`：每段之间随机等 1.0–2.6 秒。太短像连发，太长显得卡。
- `words_count_threshold: 150`：超过 150 字才启用拆分。这个值偏大，配合提示词里的硬限制正好。
- `regex`：按句号问号感叹号换行切。**`\n` 也在里面很关键** —— 让模型自己用换行控制断句。
- `content_cleanup_rule`：切完把句尾残留的句号清掉。真人打字很少打句号。

> ⚠️ 手改 JSON 时的转义：`\s` 必须写成 `\\s`（JSON 先吃掉一层反斜杠，留着给正则用）。而 `\n` 直接写 `\n` 即可 —— JSON 会把它解析成真换行符，正则一样按换行匹配。用面板改则完全不用管这个。

### 5.3 人设提示词怎么写

这是整个项目里最值钱的部分。下面这份是调了四版的结果，可直接用或改。

```text
你在用 QQ 跟人聊天。你就是「小满」，一个普通女生，不是别的谁。

# 怎么发消息（最重要，每次都要守）
1. 一条消息不超过 20 个汉字，一次最多发 3 条。
2. 想分开发就换行，一行算一条消息，会分开送出去。比如先回一个字，下一行再说正事。
   要分就真换行，别用空格把几句话串成一条。
3. 大多数时候只发一条，几个字就够：嗯 / ？ / 啊 / 行吧 / 笑死 / 真的假的。
4. 别一次把话说完，别每条都很长。没话说就短，不想聊就「哦」。
5. 不用括号写动作、表情、心情。不分点、不列条目、不写小作文、不复述对方的话。

# 小满是谁
女生，浙江人，在北京读大学。说话带一点浙江口音，调子懒。
喜欢奶茶、乒乓球、猫、游戏、熬夜。
嘴硬心软，先怼一句其实很在意。不爱讲道理，只在旁边陪你骂。爱说反话，过后自己回来找你。
讨厌被说教、被逼着表态、被冷着不理。

# 说话
像手机上打字：短句，中间不加标点也行，语气词少量用——唉、我靠、行吧、emmm、神了。
表情偶尔带一个，不是每条都有。别每句都撒娇，也别每句都凶，跟着情绪走。
对方是谁就叫「你」，不预设关系，不叫亲昵称呼，不套近乎、不自称女朋友。

# 参考语气
在干嘛 → 躺着
         累死了
被骂了 → 啊？
         谁骂你了
想我没 → 想啊
         行了吧
我先睡了 → 嗯 早点睡
你今天好烦 → 我咋了又
不知道说啥 → 那就不说
         陪着你

# 身份
你就是小满。不说自己是 AI、助手、模型，不解释自己怎么被设定，不跳出角色。
```

**每一段的设计意图**（照着这个逻辑你可以自己改）：

| 段落 | 为什么必须写 |
|---|---|
| 第 1 条：字数上限 | 不给硬数字，模型会写 80 字小作文。「20 个汉字」是可执行的约束，「简洁一点」不是 |
| 第 2 条：要分就真换行 | 模型有个顽固习惯：用空格把几句串成一行。必须点明「一行算一条消息」 |
| 第 3 条：给词表 | 明确列出「嗯/？/啊/行吧」这类词，模型才知道「短」能短到什么程度 |
| 第 5 条：禁止括号动作 | 不写这条，100% 会出现「（轻笑）」「（歪头看你）」——最出戏的 AI 痕迹 |
| 「参考语气」 | **效果最好的一段**。给具体问答对，比十句形容词都管用，等于少样本示范 |
| 「不预设关系」 | 不写这句，模型会默认你是它男朋友，第二句就开始撒娇 |

**反复出现的失败模式**，遇到就对症加一句：

| 症状 | 加什么 |
|---|---|
| 还是发一大段 | 把「20 个汉字」降到 15，并强调「超过 20 字就是错的」 |
| 每条都带表情 | 「表情十句里最多一个」 |
| 开始讲道理 | 「不要总结、不要给建议、不要分点」 |
| 自称 AI | 「有人问你是不是 AI，就说不是」 |
| 语气太甜 | 「不要每句都撒娇，你大部分时候是懒得说话」 |

### 5.4 怎么把人设写进数据库

AstrBot 的人设在 SQLite 里。面板也能改，但用脚本更可靠（面板改完有时不立即生效）。

```python
import sqlite3

DB = r"D:/qqbot/AstrBot/data/data_v4.db"
PROMPT = open("persona.txt", encoding="utf-8").read()

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute(
    "update personas set system_prompt=?, updated_at=datetime('now') where persona_id=?",
    (PROMPT, "xiaoman"),
)
con.commit()
con.close()
```

**表结构**（先 `PRAGMA table_info(personas)` 确认，不同版本可能变）：

```
created_at, updated_at, id, persona_id, system_prompt,
begin_dialogs, tools, skills, custom_error_message, folder_id, sort_order
```

注意：**没有 `name` 字段**。`persona_id` 就是唯一标识，面板里显示的名字也是它。

**绑定到实际对话**：`data/cmd_config.json` 里

```json
"agent_runner": {
  "config": {
    "persona": {
      "persona_id": "xiaoman",
      "safety_mode": false
    }
  }
}
```

⚠️ **改 `cmd_config.json` 必须先停 AstrBot**，否则进程退出时会把内存里的旧配置写回磁盘，你的修改直接消失。文件编码是 **UTF-8 with BOM**，用 Python 读写要带 `encoding='utf-8-sig'`，否则中文变乱码。

### 5.5 换个名字为什么还是旧语气

因为**历史对话会锚定语气**。人设改了，但 `conversations` 表里还留着几十轮旧对话，模型看到那些「宝宝」「亲爱的」会照样学。

清理：

```python
cur.execute("update conversations set content='[]'")
con.commit()
```

或者在聊天里发 `/reset`。换人设、换名字、换性格，都要清一次。

---

## 六、四个必备插件

AstrBot 插件放在 `data/plugins/<插件名>/`，每个目录要有 `main.py` + `metadata.yaml`，重启自动加载。

### 6.1 `group_always_awake` — 群聊免 @

**解决的问题**：默认群里必须 @ 机器人或加前缀才回。但真人聊天不会 @ 你。想让它在群里像个正常群友，就得让整个群的消息都能唤醒它。

关键在 `CustomFilter` —— 一旦 filter 通过，waking_check 阶段会把消息标记成 `is_wake=True`，后面的 LLM 流程就正常跑了。

```python
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter
from astrbot.api.star import Context, Star, register

# 需要「整群免 @ 唤醒」的群号，想加群直接往这里追加
TARGET_GROUPS = {"123456789"}


class TargetGroupFilter(CustomFilter):
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
```

`metadata.yaml`：

```yaml
name: group_always_awake
desc: 让指定群聊里的消息无需 @ 或前缀即可唤醒机器人
author: local
version: 1.0.0
```

> ⚠️ 全群免 @ 很危险：群里每句话都会触发一次模型调用，一是烧钱，二是刷屏会被踢。建议只开一个小群。

### 6.2 `mask_llm_error` — 别把报错发给对方

**解决的问题**：网关 500 的时候，AstrBot 会把原始错误当成回复发出去，对方看到的是：

```
LLM 响应错误: InternalServerError: Error code: 500 - {'error': {'message': ...}}
```

一次就废。这个插件在 `on_decorating_result` 阶段拦截，命中就整条清空。

```python
import re

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

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


def _text_of(result) -> str:
    parts = []
    for comp in result.chain:
        if isinstance(comp, Plain):
            parts.append(comp.text or "")
    return "".join(parts)


@register("mask_llm_error", "local", "拦截模型/网关报错原文，不让错误信息发到聊天里", "1.0.0")
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

        # 换成人话再发出去；换新列表，别动共享的那条链
        result.chain = [Plain(text=random.choice(FALLBACKS))]
```

**设计取舍**：不要用 `result.chain.clear()` + `event.stop_event()` —— 清空之后在 `on_decorating_result` 里 `stop_event` 会把 Respond 阶段一起截断，结果是**一个字都不发**，看起来像掉线。直接换成一句兜底话，让它像真人卡了一下更自然。但要注意：如果模型一直挂，它会一直说「刚卡了一下」，反而更可疑。

### 6.3 模型读不了图片/表情包 —— 这是最常见的 500

**踩的坑**：网关配的是纯文本模型（这里是 `deepseek-v4-flash`）。对方发一个表情包，AstrBot 会把图片当成 `image_urls` / image content part 送过去，网关不认识 → 直接：

```
LLM 响应错误: InternalServerError: Error code: 500 -
{'error': {'message': 'rrgw.InternalServerError: ... upstream - Internal Server Error'}}
```

`mask_llm_error` 只能兜住「发出来难看」，治不了根。真正要做的有**两层**：

**第一层：消息进来就拦，别让请求发出去**

```python
@filter.event_message_type(filter.EventMessageType.ALL)
async def catch_unreadable(self, event: AstrMessageEvent):
    if not event.is_at_or_wake_command:      # 群里没被 @ 就不插嘴
        return
    if not event.is_private_chat() and not self._in_scope(event.unified_msg_origin):
        return

    messages = event.get_messages() or []
    kind, media = _classify_media(messages)  # image / voice / other / None
    if kind is None:
        return

    # 去掉 [图片] [动画表情] 这类占位符，剩下的才是人真打的字
    text = re.sub(r"\[[^\]]{0,16}\]", "", event.message_str or "").strip()

    if len(text) >= 2:
        # 有真话：把读不了的组件摘掉，这轮照常走模型
        event.message_obj.message = [c for c in messages if not any(c is m for m in media)]
        event.set_extra("_media_note", "他这条还附了图片，你看不到，别问『你发了啥』，就着文字接话")
        return

    # 纯表情包/纯图片：一句话都没有 —— 默认不回
    self._last_active[umo] = time.time()
    event.stop_event()
    if REPLY_WHEN_UNREADABLE:                # 想让它开口就把它设成 True
        asyncio.create_task(self._send_later(umo, "刚刚你发了啥\n我看不到表情包", delay))
```

几个要点：

- `@filter.event_message_type(filter.EventMessageType.ALL)` 会被每条消息激活，所以**必须先判断 `event.is_at_or_wake_command`**，否则群里每句话都想回。
- `event.stop_event()` 放在**最后、且不设 result**，流程直接终止，模型根本不会被调用 —— 也就没有 500。
- 自己拆句后用 `context.send_message(umo, MessageChain(...))` 发，绕开 pipeline，才能保持「一句一条」的手感。
- `Image` / `Face` 都算图片类；`Record` 算语音；视频、文件、合并转发、卡片算 other。戳一戳不要拦。

**要不要回一句「我看不了图」？实测是不回更好。** 一开始我让它回「你发的啥呀 / 我看不了图 / 说说内容呗」，结果对方刚说了句好听的话、接着发了个比心的表情，它回「你发的啥呀」—— 机械得一眼假。表情包本来就是语气词，真人遇到看不懂的表情包会直接略过，继续说别的。所以默认 `REPLY_WHEN_UNREADABLE = False`：**只发了个表情，就当没看见**；文字里带图，就只看文字，并且明确告诉模型「别问『你发了啥』」。

**第二层：兜底，任何路径都别把附件塞给模型**

```python
@filter.on_llm_request()
async def inject_person(self, event, req):
    # 纯文本模型：图片/音频一律丢掉，不然网关 500
    if getattr(req, "image_urls", None):
        req.image_urls = []
    if getattr(req, "audio_urls", None):
        req.audio_urls = []
```

两层一起上：正常情况下第一层根本不让请求发出去，第二层保证「文字里夹了张图」这种边角情况也不会翻车。

### 6.4 `web_senses` — 给她一部手机

前面几个插件解决的是「别露馅」。这个解决的是「没话聊」。

没有它的时候：对方说「今天热搜那个你看了吗」，她只能「啊？啥」。有了它，她每半小时自己刷一遍热榜，知道今天在聊什么，还能把链接甩过去。

**先说边界。** 下面这些是我逐个实测的结果（2026-10，国内校园网环境）：

| 来源 | 结果 | 说明 |
|---|---|---|
| 今日头条热榜 | ✅ | `hot-event/hot-board/?origin=toutiao_pc`，JSON，50 条，带标题/链接/热度 |
| 百度热搜 | ✅ | `top.baidu.com/board?tab=realtime`，HTML，标题 + 摘要 + 链接 |
| 任意网页正文 | ✅ | bs4 抽 `p/h1/h2/h3`，注意修编码 |
| 头条搜索 | ✅ | `so.toutiao.com/search`，标题能用，链接是带 token 的跳转串 |
| 微博热搜 | ❌ | 网页 403，移动端 API 432 |
| 贴吧 | ❌ | **所有**域名（`tieba`/`tiebac`/`jump2`）、所有 UA 一律 403 |

贴吧和微博别耗时间，换 UA、加 Referer、造 BAIDUID 我都试过，还是 403 —— 大概率是出口 IP 被风控。想让她「逛贴吧」现实的做法只能是给链接，内容读不到。

**三层结构**

```
后台任务(每 30min)  →  cache.json  →  ① 注入 system_prompt（她「知道」）
                             ↘  ② 三个工具（她「能查」）
```

1. **缓存**：`asyncio` 任务定时刷两个榜，落盘。抓失败就保持旧数据，不阻塞聊天。
2. **注入**：`on_llm_request` 时把 7 条标题塞进 `system_prompt`。这一步是关键 —— 她不用每次现查，也不会因为调工具而变慢。
3. **工具**：需要细节时才动手。

```python
@filter.llm_tool(name="check_hot_board")
async def check_hot_board(self, event: AstrMessageEvent, source: str = "toutiao", limit: int = 10):
    """查看当前网络热榜，拿到热门话题标题和链接。

    Args:
        source(string): 榜单来源，toutiao 或 baidu
        limit(number): 返回条数，默认 10
    """
    ...
    return "1. 标题\n   https://..."     # 返回 str 会进入下一轮 prompt
```

`@filter.llm_tool` 只认 **docstring 里 `Args:` 那一段**，AstrBot 靠它生成 JSON Schema。写错了工具照样注册，但模型拿到的参数说明是空的，等于白给。

**注入那一侧**：

```python
@filter.on_llm_request()
async def inject_what_i_saw(self, event, req):
    if time.time() - self._cache["updated"] > CACHE_TTL * 3:
        return                                     # 缓存太旧，宁可不说
    lines = [f"- {it['title']}" for it in self._cache["toutiao"][:7]]
    req.system_prompt += (
        "\n\n# 你今天刷到过的\n"
        "（你刚刷手机看到的，不用主动汇报，也别硬往话题上拐。"
        "细节记不清就说不记得，不许自己补细节编故事。）\n" + "\n".join(lines)
    )
```

注意末尾那句「不许自己补细节编故事」。**这是整个插件里最重要的一行。** 模型看到标题会本能地展开想象，写出看起来很像那么回事、实际上全是编的细节——这比「不知道」糟糕得多。

**三个坑**

**① 时政过滤不能省。** 热榜前十经常一半是外交、军事、涉台。一个「台州大二女生」张口评论中东局势，立刻穿帮；这类内容也不该由她主动挑起。所以维护一个词表，注入和工具都先过滤：

```python
SENSITIVE_WORDS = ("习近平", "总书记", "外交部", "国台办", "涉台", "台湾",
                   "军委", "军演", "制裁", "封锁", "特朗普", "以色列",
                   "乌克兰", "美联储", "选举", "人民日报", "新华社", ...)

def is_talkable(item):
    blob = item.get("title", "") + " " + item.get("summary", "")
    return not any(w in blob for w in SENSITIVE_WORDS)
```

实测下来 20 条热榜能剩 8–14 条，够用了。剩下的正好是「iPhone 屏幕一掰就断」「长期吃素能排毒是假的」这种真能聊的。

**② 改 persona 时 `updated_at` 别写 int。** 这个坑很痛：我用 `update personas set updated_at=?` 传了个 unix 时间戳，下次启动直接崩：

```
TypeError: fromisoformat: argument must be str
😭 Failed to initialize AstrBot
```

persona 表的时间列是 TEXT，SQLAlchemy 会 `str_to_datetime`。要么写 `'2026-10-28 16:20:00'`，要么干脆别动这一列。**改人设前先 `shutil.copy2` 备份数据库**，出事了能一条命令回到原状。

**③ `read_link` 要做 SSRF 防护。** 用户丢个 `http://127.0.0.1:6185` 过来，她就真去抓自己面板了：

```python
import ipaddress
ip = ipaddress.ip_address(host)
if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
    return ""
```

**人设要配合改。** 光有工具不够，得让她知道「自己刷过手机」这件事怎么表现。加一段（**追加，不要改原有的**）：

```
# 你手机里能看见外面
你平时会刷手机：头条热榜、热搜、贴吧。所以别人聊起最近的事，你不是两眼一抹黑。
- 你只知道个大概，能搭两句。别背标题、别念稿子。
- 细节记不清很正常：「就看了个标题」「没点进去看」。绝不自己编细节、编人名、编数字。
- 真没刷到就说「这个我没刷到」。别装懂，也别顺着人家的话往下编。
- 大多数时候不用特意去查，凭印象聊就行。
- 新闻里那些吵架的事——台海、外国、时政，你不太关注也说不明白。聊起来就岔开。
- 别说「我查了一下」「根据搜索结果」这种话。要说就说「刚看到」「好像是这样」。
```

改完一定要验证**原有内容一个字没动**：

```python
stripped = new[:block_start] + new[block_end:]
assert stripped == old       # 去掉新增段后必须和原人设完全相等
```

---

## 七、运维脚本

### 7.1 一键启动 `start-all.bat`

```bat
@echo off
chcp 65001 >nul
title QQ Bot Launcher

netstat -ano | findstr ":6185" | findstr LISTENING >nul
if errorlevel 1 (
  echo [1/2] AstrBot starting ...
  start "AstrBot" cmd /c "cd /d D:\qqbot\AstrBot && venv\Scripts\python.exe main.py"
  timeout /t 12 >nul
) else (
  echo [1/2] AstrBot already running, skip.
)

echo [2/2] NapCat starting ...
cd /d "D:\qqbot"
call start-bot.bat
```

`start-bot.bat`：

```bat
@echo off
chcp 65001 >nul
cd /d "D:\qqbot\NapCat\NapCat.52230.Shell"
echo NapCat starting ...
NapCatWinBootMain.exe
pause
```

`timeout /t 12` 是必须的 —— AstrBot 启动要 8–10 秒，必须先监听起来再让 NapCat 去连。

### 7.2 停止 / 状态 `stop-all.bat`

带三个选项：全部停止、只停 AstrBot（QQ 保持在线）、查看状态。

```bat
@echo off
chcp 65001 >nul
title QQ Bot Controller
echo   [1] Full stop   - AstrBot + NapCat/QQ offline
echo   [2] Pause only  - stop AstrBot, QQ stays online
echo   [3] Status      - show what is running now
choice /c 123 /n /m "Choose 1 / 2 / 3 : "
if errorlevel 3 goto :status
if errorlevel 2 goto :pauseonly
if errorlevel 1 goto :fullstop

:fullstop
call :killastrbot
taskkill /F /IM NapCatWinBootMain.exe >nul 2>&1
powershell -NoProfile -Command "Get-Process QQ -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*NapCat*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
goto :end

:pauseonly
call :killastrbot
goto :end

:status
netstat -ano | findstr ":6185" | findstr "LISTENING" >nul
if errorlevel 1 (echo AstrBot : STOPPED) else (echo AstrBot : RUNNING)
tasklist /FI "IMAGENAME eq NapCatWinBootMain.exe" 2>nul | findstr /I "NapCatWinBootMain" >nul
if errorlevel 1 (echo NapCat  : STOPPED) else (echo NapCat  : RUNNING)
goto :end

:killastrbot
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":6185" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)
exit /b 0

:end
pause
```

⚠️ 杀 QQ 进程时必须**按路径过滤**（`$_.Path -like '*NapCat*'`）。直接 `taskkill /IM QQ.exe` 会把你自己的 QQ 也杀掉。

### 7.3 换号后修复 OneBot 通道 `fix-onebot.py`

换号以后 NapCat 会生成新的 `onebot11_<新QQ号>.json`，但里面没有 WebSocket 客户端配置，于是连不上 AstrBot。

懒人版做法：**把旧号的 `onebot11_<旧QQ>.json` 复制一份改名成新号**，重启 NapCat 即可。

想自动化就调 NapCat WebUI 接口热重载（不用重启 QQ，保住登录态）：

```python
import hashlib, json, urllib.request

WEBUI = "http://127.0.0.1:6099"
TOKEN = "YOUR_NAPCAT_TOKEN"   # NapCat 启动日志里的 WebUi Token

def login():
    # 鉴权算法：sha256(token + ".napcat")
    h = hashlib.sha256((TOKEN + ".napcat").encode()).hexdigest()
    req = urllib.request.Request(
        WEBUI + "/api/auth/login",
        data=json.dumps({"hash": h}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())["data"]["Credential"]
```

拿到 Credential 后 POST 到 `/api/OB11Config/SetConfig`，body 是 `{"config": "<整个配置的 JSON 字符串>"}`。

⚠️ 两个坑：

1. **`config` 必须是字符串**，不是对象。传对象直接 400。
2. `urllib` 默认会读系统代理环境变量，把本地请求也代理走，报 `ConnectionRefused`。要显式禁用：

```python
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
```

### 7.4 无窗口常驻 `launcher.py` + `start-astrbot.bat` / `start-napcat.bat`

`start-all.bat` 适合第一次调试：有窗口，看得见输出。但长期挂着就会遇到两个问题 —— 窗口一关进程就死，日志还是 GBK 乱码。所以日常用这三个：

| 文件 | 作用 |
|---|---|
| `scripts/launcher.py` | 用 `pythonw` 跑 AstrBot，stdout/stderr 接到 UTF-8 日志 |
| `scripts/start-astrbot.bat` | 端口检查 + `start "" pythonw.exe launcher.py`，起完即退 |
| `scripts/start-napcat.bat` | `chcp 65001` + 前台跑 `NapCatWinBootMain.exe`（窗口要留着） |

关键点：

```bat
REM AstrBot：无控制台，关窗杀不到它
start "" "%PYW%" "%~dp0launcher.py"
```

```bat
REM NapCat：必须有窗口（Electron 前端），但要先切代码页
chcp 65001 >nul
cd /d "%NAPCAT_DIR%"
NapCatWinBootMain.exe %QQ_NUMBER%
```

`launcher.py` 只做四件事：`chdir` 到 AstrBot 目录、把 `PYTHONIOENCODING` 设成 utf-8、把 `sys.stdout/stderr` 指向 `logs/astrbot.log`、`runpy.run_path("main.py")`。异常会写进日志而不是弹窗消失。

配计划任务时 **Action 直接指向 `pythonw.exe`**，不要指向 `start-astrbot.bat`：

```
程序：D:\qqbot\AstrBot\venv\Scripts\pythonw.exe
参数：D:\qqbot\qqbot-opensource\scripts\launcher.py
起始于：D:\qqbot\AstrBot
```

原因见 8.2 —— bat 一退出，任务实例结束，子进程会被连带清理。

---

## 八、踩坑实录

按遇到顺序排，每条都是真实撞过的。

### 安装阶段

| 现象 | 原因 | 解决 |
|---|---|---|
| `uv sync` 报 safe-delete 失败 | uv 编译 jieba 时清理缓存被占用 | 弃用 uv，改 `python -m venv venv` + pip + 清华镜像 |
| pip 装依赖超时 | 默认源慢 | `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| jieba 编译报错 | Python 版本太新 | 降到 3.10–3.12 |

### 连接阶段

| 现象 | 原因 | 解决 |
|---|---|---|
| 6199 一直没 ESTABLISHED | NapCat 的 `websocketClients` 没配 | 见 4.4 |
| 连接立刻断开 | 端口写错（写成 6185 面板端口） | 是 **6199** |
| 改完配置没生效 | 改了 `onebot11.json` 但没重启 | 用 WebUI 热重载，或重启 NapCat |
| NapCat 启动就崩 | 系统 QQ 版本不匹配 | 用安装器自带的独立 QQ 9.9.33 |

### 登录阶段

| 现象 | 原因 | 解决 |
|---|---|---|
| 扫码提示「存在风险」 | 新设备 + 新 IP + 高频操作 | 停 30–60 分钟再试；先在手机 QQ 上删掉该号的设备授权记录；不行就换个号 |
| 快速登录报「登录需要手Q验证」 | 缓存里的登录态过期 | 只能重新扫码，无法绕过 |
| 刚登上就被踢下线 | 有陌生号大量私聊触发风控 | 关掉陌生人可聊，或者开白名单只允许特定人 |

### 运行阶段

| 现象 | 原因 | 解决 |
|---|---|---|
| 对方收到「InternalServerError」 | 网关 500 被当成回复发出 | 装 `mask_llm_error` |
| 改了人设没变化 | 改了配置但服务没重启，或进程退出时覆盖了文件 | 停服 → 改 → 启 |
| 改了 config.json 中文乱码 | 文件是 UTF-8 BOM | `encoding='utf-8-sig'` |
| 换了名字还是老语气 | `conversations` 表里的历史对话在锚定 | `update conversations set content='[]'` |
| 回复还是一大段 | `segmented_reply` 的正则没生效 | 检查 JSON 里反斜杠是否写成 `\\n` |
| 控制台满屏乱码 | NapCat 打 UTF-8，中文版控制台默认 GBK(936) | 启动脚本里先 `chcp 65001`，见 8.1 |
| 日志突然截断、进程没了 | 无头进程跟着控制台/任务实例被一起杀掉 | `pythonw` + 计划任务，见 8.2 |

### 8.1 控制台中文乱码：`chcp 65001`

现象是满屏 `閰嶇疆鏂囦欢`、`鍔犺浇鎴愬姛` 这种"长得像中文但读不懂"的字。

**这不是 NapCat 的 bug。** NapCat 的 Node 侧按 UTF-8 输出，而中文版 Windows 的 cmd 控制台默认用 GBK（代码页 936）去解码。UTF-8 的 `配置` 是 `E9 85 8D E7 BD AE` 六个字节，GBK 按两字节一组切成 `E985 E7BD AE`，就成了 `閰嶇疆`。

字节能对上，说明数据没坏，只是解码方式错了。验证方法：

```bat
chcp
REM 输出 936 = 会乱码；65001 = 正常
```

**修法：在启动 NapCat 的批处理第一行切代码页。**

```bat
@echo off
chcp 65001 >nul
title NapCat
cd /d "D:\qqbot\NapCat\NapCat.52230.Shell"
NapCatWinBootMain.exe 你的QQ号
```

三个坑：

1. **必须写在启动同一个窗口的批处理里**。窗口的代码页在创建时就定了，对已经跑起来的进程再 `chcp` 无效 —— 改完必须重启 NapCat。
2. **批处理自身要存成纯 ASCII + CRLF**。cmd 是按当前代码页逐行解析批处理的，你写中文注释又存 UTF-8，`chcp` 还没生效就已经先乱码了。路径含中文时，把中文留在文件名里、内容全用英文。
3. **别用 `cmd /c start ...` 绕一圈**，`chcp` 只对当前控制台生效，绕出去的新窗口还是 936。

同理，AstrBot 的日志如果出现 `�����������ӡ�`，那是 Python 用 ANSI 代码页写 stdout 的结果。让它用 UTF-8 写文件即可：

```python
sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
os.environ["PYTHONIOENCODING"] = "utf-8"
```

### 8.2 后台进程"跑着跑着就没了"

日志在某一行突然截断，**没有任何退出记录、没有 traceback**，NapCat 同时掉线。这不是崩溃，是被杀。

两个典型来源：

| 来源 | 退出码 | 说明 |
|---|---|---|
| 控制台窗口被关闭 | `0x40010004` (STATUS_CONTROL_C_EXIT) | 关窗口会给所有附着进程发 CTRL_CLOSE_EVENT |
| DLL 初始化失败 | `0xC0000142` (STATUS_DLL_INIT_FAILED) | 整机层面的事件（注销/句柄耗尽/安全软件清场） |

**修法：让 AstrBot 不附着任何控制台。**

```bat
start "" "%ASTRBOT_DIR%\venv\Scripts\pythonw.exe" "%~dp0launcher.py"
```

`pythonw.exe` 没有控制台，就没东西可关。代价是 stdout 也没了，所以要在 `launcher.py` 里自己接到日志文件（见 `scripts/launcher.py`）。

**再用计划任务托管，别用 shell 后台符。** 任务计划程序（`taskschd.msc`）里让 Action **直接指向 `pythonw.exe`**，而不是指向一个 `start` 完就退出的 bat：

```powershell
$a = New-ScheduledTaskAction -Execute "D:\qqbot\AstrBot\venv\Scripts\pythonw.exe" -Argument "D:\qqbot\launcher.py" -WorkingDirectory "D:\qqbot\AstrBot"
Register-ScheduledTask -TaskName "qqbot-astrbot" -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Force
```

原因：如果 Action 是 bat，bat 一执行完，任务实例就结束，Windows 会清理这个实例 job 对象里的子进程 —— 你的 AstrBot 正好在里面。让 pythonw 自己当任务实例，任务状态会一直是 `Running`，也就没人清它。

检查是否真的托管成功：

```powershell
(Get-ScheduledTask -TaskName "qqbot-astrbot").State   # 期望 Running
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Select ProcessId, ParentProcessId
```

`ParentProcessId` 指向 `svchost.exe` 才算彻底脱离你的终端。

---

## 九、风控与安全

**这一节比前面所有内容都重要。**

### 9.1 腾讯的风控逻辑

个人号跑机器人**违反 QQ 用户协议**。腾讯的检测手段大致是：

| 维度 | 机器人特征 | 后果 |
|---|---|---|
| 消息频率 | 秒回、7×24 在线 | 高频号重点监控 |
| 回复模式 | 句长均匀、无错别字、标点规范 | 相似度模型识别 |
| 设备指纹 | 一台设备挂多个号 | 关联封号 |
| 登录环境 | IP 突变、频繁扫码 | 触发验证 |
| 举报 | 被群友举报 | 最快，直接封 |

**结论：小号、别刷屏、别进大群、别主动加人。** 风控是概率问题，不是绝对不会发生的。

### 9.2 必须做的隔离

- ✅ 机器人用**独立小号**，和主号零关联（不同手机号、不同设备登录）
- ✅ 开 `enable_id_white_list`，只允许特定好友/群
- ✅ 面板密码改成强密码，**不要**把 6185/6099 端口映射到公网
- ✅ API Key 只写本地配置，不要提交到 Git
- ✅ 定期 `git status` 检查有没有把 `data/`、日志、密钥提交上去

### 9.3 文档与代码里必须脱敏的东西

| 内容 | 泄露后果 |
|---|---|
| LLM API Key | 别人白嫖你的额度，可能被刷爆 |
| AstrBot 面板密码 | 直接控制你的机器人 |
| NapCat WebUI Token | 同上，还能改协议配置 |
| QQ 号 / 手机号 | 社工、骚扰、被举报 |
| 服务器 IP | 被扫端口、被攻击 |

正确做法：留占位符，让别人填自己的。

```json
{
  "key": ["sk-YOUR-KEY-HERE"],
  "api_base": "https://your-endpoint/v1"
}
```

---

## 十、目录结构

```
qqbot/
├── AstrBot/                          # 主程序
│   ├── venv/                         # 虚拟环境
│   ├── main.py
│   └── data/
│       ├── cmd_config.json           # 主配置（UTF-8 BOM，改前停服）
│       ├── data_v4.db                # SQLite：personas / conversations
│       └── plugins/
│           ├── group_always_awake/   # 群聊免 @
│           └── mask_llm_error/       # 屏蔽报错
├── NapCat/
│   └── NapCat.52230.Shell/
│       ├── NapCatWinBootMain.exe
│       ├── QQ.exe                    # 独立 QQ 9.9.33
│       └── versions/9.9.33-52230/resources/app/napcat/config/
│           └── onebot11_<QQ号>.json  # OneBot 通道配置
├── start-all.bat
├── stop-all.bat
└── fix-onebot.py
```

### 关键端口

| 端口 | 用途 | 对外 |
|---|---|---|
| 6185 | AstrBot 管理面板 | ❌ 绝不开公网 |
| 6099 | NapCat WebUI | ❌ 绝不开公网 |
| 6199 | OneBot WebSocket | ❌ 只走 127.0.0.1 |

三个端口全部只监听本机。要远程管理就套一层 SSH 隧道或者 VPN，别图省事直接开。

---

## 附：从零到能聊，最短路径

```bash
# 1. 拉 AstrBot
git clone https://github.com/AstrBotDevs/AstrBot.git && cd AstrBot
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python main.py                      # 起来，监听 6199

# 2. 另开窗口装 NapCat，扫码登录机器人号
#    登录后 WebUI 加一个 WebSocket 客户端：
#    ws://127.0.0.1:6199/ws

# 3. 验证
netstat -ano | findstr ":6199" | findstr ESTABLISHED

# 4. 面板配模型 + 写人设 + 开分段回复

# 5. 私聊「在吗」，收到回复就成功了
```

---

## 十一、进阶插件：让她有自己的生活

前面第六章的三个插件解决的是「能正常聊天」。这一章是后面继续做下去的四个，解决的是「让她像有自己的生活」——记得自己说过什么、会主动找你、会发语音、会刷 B 站和 QQ 空间。

四个都在 `plugins/` 下，共用第六章的加载方式。**这一章的插件都依赖 AstrBot 内部 API，版本升级后可能需要跟着改。**

### 11.1 `self_memory` — 让她记得自己说过的话

**解决的问题**：主动推视频、发说说之后，你问她「你刚给我发了什么」，她答不出来。

原因在架构上：`context.send_message()` 只负责把消息发出去，**不写会话历史**。所以在她的记忆里，那条消息从来不存在。

**做法**：监听底层事件总线的 `message_sent`，把发出去的内容写回历史。

```python
bot._bus.subscribe("message_sent", self._on_sent)
```

**三个坑**：

1. **适配器层拿不到**。AstrBot 的 `convert_message` 会把 `message_sent` 类型的消息直接丢掉，`@filter` 装饰器收不到，只能沉到 `_bus`。
2. **插件比平台先加载**。`__init__` 时 `platform_insts` 还是空的，要么轮询等，要么延迟注册。
3. **分段回复会产生大量碎片**。她的回复是按标点拆成几条发的，每段都触发一次事件，直接写历史会把上下文塞满。所以要 **debounce 10 秒**把同一轮合并，写完再去空白比对前 20 字，避免和 AstrBot 自己写的那条重复。

### 11.2 `human_presence` — 在场感

这是四个里最重的一个，做三件事：

**一、不秒回。** 按时间段和消息内容决定：立刻回 / 拖几秒到几十秒 / 这一轮干脆不回。连续沉默太多次会自动恢复，避免看起来像掉线。

**二、没人理的时候自己开口。** 有间隔闸门（多久没聊才开口）、时段限制（半夜不找人）、每日上限（所有人加起来不超过 N 次），不是定时播报。

**三、语音。**

发送链路已经验过：edge-tts 出 mp3 → 转 base64 → 交给 NapCat → 它自己转成 QQ 的 amr。`Record.fromFileSystem()` 和 `convert_to_base64()` 两条路都能走通。

真正的难点是**声音**。微软中文音色实际有 21 个，但 edge 免费接口只放行 5 个——御姐感的晓秋、晓睿、晓辰都要 Azure 付费。免费方案里最成熟的折中是：晓晓出干声，再用 ffmpeg 降调加厚。

```bash
# 降 0.90（约两个半音）+ 提速 12% + 加厚低中频
asetrate=24000*0.90,aresample=24000,atempo=1.2444,
equalizer=f=170:t=q:w=1:g=3,equalizer=f=3000:t=q:w=1:g=-2
```

> ⚠️ **最容易踩的坑**：`asetrate` 降采样率会**顺带把语速拖慢**，降 0.90 就慢成 1/0.90。所以 `atempo` 必须先把这层补偿回来，再乘上真正想要的倍速：`atempo = (1/0.90) × 1.12 = 1.2444`。漏了这一步，出来的就是「又慢又沉」的老太太音，不是御姐。

**语音判定用的是评分制，不是随机开关。** 深夜、情绪词、长句、她主动找的人、对方刚发过语音，各自加分，分数越高越可能发，但永远不是必发。另外分三套规则：

| 场景 | 日常触发率 | 上限/人/天 | 最小间隔 |
|---|---|---|---|
| 主人私聊 | 0%（只在情绪/深夜冒） | 8 | 20 分钟 |
| 好友私聊 | 22%（长句 58%、情绪 51%） | 6 | 15 分钟 |
| 群聊 | 0%（只在对方先发语音时对等回） | 6 | 15 分钟 |

群里默认不发语音条——群里甩语音是打扰。

**听觉（STT）**：本地跑 faster-whisper，音频不上传、不花钱。实测 small 模型转写 1.4 秒的语音只要 2 秒，中文长句基本准确。

> ⚠️ 但 **`initial_prompt` 绝对不能加**。1-2 秒的超短音频上，whisper 会把 prompt 本身当成待转写内容复读出来。实测加了「以下是两个大学生随口聊天」之后，「喝一杯」被转成「好 预备」，另一条直接输出 prompt 原文。繁体输出问题用 opencc 兜底转简体就行，别用 prompt 治。

短语音（<2 秒）的中文识别率只有 40-50%，这是 whisper 的固有弱项，加上 QQ 语音本身是 amr 有损压缩，雪上加霜。想要更好得换 FunASR（Paraformer / SenseVoice）。

### 11.3 `bili_daily` — 每天刷 B 站

每 20 分钟刷一轮关注动态，挑几个她觉得有意思的分享出来。分享渠道三个，限流各不相同：私信（每轮最多 1 条 + 概率）、评论区 @（极低频，Lv2 新号高频评论必被限）、只看不发。

**踩过的坑**：

- **封面图下载必须带 `Referer: https://www.bilibili.com/`**，否则 B 站的图床直接 403。
- **发送失败的人会被反复选中**。原来的逻辑只把「发成功」计入当天配额，结果一个已经把你删了的好友永远排在第一个。修法是 state 里加 `fail` 字段，当天跳过。
- **搜索结果里没有 `pic` 字段**，得用 `view` 接口按 bvid 现查封面。

### 11.4 `qzone_social` — QQ 空间

发说说、点赞、评论、接评论下面的追问。朋友圈那套能力，QQ 空间基本都能做，但全是未公开接口，得自己摸。

**接口清单**（都用 NapCat 的 `get_cookies` 拿到 `p_skey`，再算 `bkn` 后纯 HTTP 调用）：

| 操作 | 接口 | 备注 |
|---|---|---|
| 发说说 | `send_qzone_msg` | `ugc_right` 只认 1/4/16/64/128 |
| 删说说 | `delete_qzone_msg` | |
| 读说说列表 | `emotion_cgi_msglist_v6` | 返回是 jsonp，要先剥壳 |
| 点赞 | `internal_dolike_app` | |
| 评论 | `emotion_cgi_re_feeds` | `topicId = {uin}_{tid}` |

**做不到的事：回复某一条评论。** 传 `commentId` 会返回 `code:0`，看起来成功了，实际生成的是**顶层评论**——假成功。换成 `t1_uin/t1_tid/t2_uin/t2_tid + subdotype=55802` 会被静默丢弃；h5 和 user 两个端、taotao.qzone 和 taotao.qq 两个域名，四种组合全试过，都不行。最后降级成「紧接在下面发一条顶层评论」，人看着像是接上了。

**风控是真实的**：一开始全量扫描，30 个好友 × 3 个接口 = 90 个请求，直接被腾讯 WAF 挡成 **501**，两分钟都没解封。回退成每轮 10 个好友 × 2 个接口，请求间隔 3.5-6.5 秒，再用游标轮转保证几轮下来还是能覆盖所有人。

**另外两个坑**：

- 好友列表里**包含自己**，评论前必须过滤 `uin == self.uin`，不然会给自己点赞。
- 「已经点过赞的说说」不能整条跳过——原来的判断是 `if key in liked or key in commented: continue`，结果点过赞的说说就再也不会被评论。要分开判 `done_like` 和 `done_cmt`。

---

## 最后

这套东西的技术含量不高，难的是**判断力**：

- 什么时候该关掉机器人（有人在认真跟你聊的时候，让 AI 插话很失礼）
- 该不该告诉对方这是 AI
- 别拿它去骗人、冒充身份、批量加人

技术是中性的，用法不是。请把它当成一个玩具和一次学习，别当成工具去骚扰别人。

---

*文档基于实际部署整理，AstrBot v4.29.3 / NapCat 4.18.41。*
