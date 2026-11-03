"""换号后一键修复 OneBot 连接。

用法:
    python fix-onebot.py <QQ号> <NapCat配置文件目录>
    python fix-onebot.py <QQ号> <NapCat配置文件目录> --token <WebUiToken>

例:
    python fix-onebot.py 123456789 "D:/qqbot/NapCat/NapCat.52230.Shell/versions/9.9.33-52230/resources/app/napcat/config"

做的事:
  1. 为指定 QQ 号写/修 onebot11_<uin>.json, websocketClients 指向 AstrBot
  2. 调 NapCat WebUI 接口热重载, 不重启 QQ, 保住登录态

NapCat WebUi Token 在哪看: NapCat 启动日志里 "[WebUi] WebUi Token: xxxx"
"""

import glob
import hashlib
import json
import os
import sys
import urllib.request

WEBUI = "http://127.0.0.1:6099"
WS_URL = "ws://127.0.0.1:6199/ws"

WS_CLIENT = {
    "name": "AstrBot",
    "enable": True,
    "url": WS_URL,
    "messagePostFormat": "array",
    "reportSelfMessage": False,
    "token": "",
    "debug": False,
    "heartInterval": 30000,
    "reconnectInterval": 30000,
}


def api(method, path, payload=None, cred=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(WEBUI + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if cred:
        req.add_header("Authorization", "Bearer " + cred)
    # 关键: 显式禁用系统代理, 否则本地请求会被代理走导致连接失败
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read().decode())


def login(token):
    # NapCat WebUI 鉴权算法: sha256(token + ".napcat")
    h = hashlib.sha256((token + ".napcat").encode()).hexdigest()
    res = api("POST", "/api/auth/login", {"hash": h})
    return res["data"]["Credential"]


def build_config(cfg_dir, uin):
    path = os.path.join(cfg_dir, f"onebot11_{uin}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        # 没有就照抄任意一个已有的 onebot11_*.json 当模板
        tpl = glob.glob(os.path.join(cfg_dir, "onebot11_*.json"))
        if tpl:
            with open(tpl[0], encoding="utf-8") as f:
                cfg = json.load(f)
        else:
            cfg = {"network": {"httpServers": [], "httpClients": [],
                               "websocketServers": [], "websocketClients": []}}
    cfg.setdefault("network", {})
    cfg["network"]["websocketClients"] = [dict(WS_CLIENT)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path, cfg


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    token = None
    for i, a in enumerate(sys.argv):
        if a == "--token" and i + 1 < len(sys.argv):
            token = sys.argv[i + 1]

    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    uin, cfg_dir = args[0].strip(), args[1].strip()
    if not uin.isdigit():
        print("QQ号必须是纯数字")
        sys.exit(1)
    if not os.path.isdir(cfg_dir):
        print(f"配置目录不存在: {cfg_dir}")
        sys.exit(1)

    path, cfg = build_config(cfg_dir, uin)
    print(f"[1/2] wrote {os.path.basename(path)}  wsClients={len(cfg['network']['websocketClients'])}")

    token = token or os.environ.get("NAPCAT_TOKEN")
    if not token:
        print("[2/2] 没提供 WebUi Token, 跳过热重载 (配置已落盘, 手动重启 NapCat 也会生效)")
        print("      -> 补上参数 --token <token> 可以免重启热加载")
        sys.exit(0)

    try:
        cred = login(token)
    except Exception as e:
        print(f"[2/2] NapCat WebUI 登录失败: {e}")
        print("      -> 配置文件已写好, 手动重启 NapCat 也会生效")
        sys.exit(2)

    # 注意: config 字段必须是 JSON 字符串, 传对象会 400
    body = {"config": json.dumps(cfg, ensure_ascii=False)}
    try:
        r = api("POST", "/api/OB11Config/SetConfig", body, cred)
        print(f"[2/2] hot reload -> {r}")
    except Exception as e:
        print(f"[2/2] 热重载失败: {e} (配置已落盘, 重启 NapCat 生效)")
        sys.exit(2)


if __name__ == "__main__":
    main()
