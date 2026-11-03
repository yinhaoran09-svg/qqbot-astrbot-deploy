"""把一份人设文本写进 AstrBot 的数据库。

用法:
    python set_persona.py <persona_id> <人设txt文件>
    python set_persona.py xiaoman persona.txt --reset   # 顺便清空所有会话历史

例:
    python set_persona.py xiaoman persona.txt --reset

注意:
  - persona_id 必须已经存在（先在面板里新建一个人设，或者用 --create 自动建）
  - 改完要重启 AstrBot 才生效
  - 换人设/换名字后务必 --reset 清历史，否则旧语气会被对话记录锚回来
"""

import sqlite3
import sys
import time


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reset = "--reset" in sys.argv
    create = "--create" in sys.argv

    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    persona_id, txt_path = args[0], args[1]

    try:
        with open(txt_path, encoding="utf-8") as f:
            prompt = f.read().strip()
    except FileNotFoundError:
        print(f"人设文件不存在: {txt_path}")
        sys.exit(1)

    db = args[2] if len(args) > 2 else r"D:/qqbot/AstrBot/data/data_v4.db"

    con = sqlite3.connect(db)
    cur = con.cursor()

    row = cur.execute(
        "select persona_id from personas where persona_id=?", (persona_id,)
    ).fetchone()

    if row:
        cur.execute(
            "update personas set system_prompt=?, updated_at=datetime('now') where persona_id=?",
            (prompt, persona_id),
        )
        print(f"已更新人设: {persona_id}")
    elif create:
        # sort_order 是 NOT NULL，必须自己算一个；id 是 INTEGER PRIMARY KEY，省略即自增
        nxt = (cur.execute("select coalesce(max(sort_order), 0) from personas").fetchone()[0]) + 1
        cur.execute(
            "insert into personas (persona_id, system_prompt, begin_dialogs, tools, skills, "
            "folder_id, sort_order, created_at, updated_at) "
            "values (?, ?, '[]', '[]', '[]', NULL, ?, datetime('now'), datetime('now'))",
            (persona_id, prompt, nxt),
        )
        print(f"已新建人设: {persona_id} (sort_order={nxt})")
    else:
        print(f"人设 {persona_id} 不存在，加 --create 自动新建")
        con.close()
        sys.exit(1)

    con.commit()

    if reset:
        n = cur.execute("select count(*) from conversations").fetchone()[0]
        cur.execute("update conversations set content='[]'")
        con.commit()
        print(f"已清空 {n} 条会话历史")

    print(f"字数: {len(prompt)}")
    con.close()
    print("记得重启 AstrBot 生效。")


if __name__ == "__main__":
    main()
