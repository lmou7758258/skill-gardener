#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill-gardener — Hermes 技能园艺师。

扫描 Hermes 的 state.db + skills/ + memories/，产出「技能库健康报告」。

硬约束（不可违背）：
  * 只读分析，绝不修改任何 skill / memory / 配置。
  * 无 LLM 依赖，纯 sqlite + 文本统计，可在 cron 里安全、零成本运行。
  * 产出的是「线索与清单」；最终的沉淀/清理动作由 Hermes agent 在会话中确认后执行。

用法：
  python gardener.py [--home PATH] [--stale-days N] [--top N] [--sediment-kw "a,b,c"]
输出：
  * stdout 打印完整报告（cron no_agent 模式可直接用）
  * 同时写入 $HERMES_HOME/.skill-gardener/report.md
"""

import os, sys, json, re, glob, sqlite3, argparse
from collections import defaultdict, Counter
from datetime import datetime
from difflib import SequenceMatcher


def detect_home():
    """探测 HERMES_HOME：$HERMES_HOME -> ~/.hermes -> $LOCALAPPDATA/hermes -> $APPDATA/hermes"""
    cands = []
    if os.environ.get("HERMES_HOME"):
        cands.append(os.environ["HERMES_HOME"])
    cands.append(os.path.expanduser("~/.hermes"))
    for k in ("LOCALAPPDATA", "APPDATA"):
        v = os.environ.get(k)
        if v:
            cands.append(os.path.join(v, "hermes"))
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "skills")):
            return c
    return cands[0] if cands else os.path.expanduser("~/.hermes")


def ts_str(t):
    if not t:
        return "?"
    try:
        return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(t)


def read_state_db(db_path):
    """以只读模式打开 state.db；若不存在返回 None。"""
    if not os.path.isfile(db_path):
        return None
    try:
        return sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    except Exception:
        return None


def check_schema(conn):
    """校验 state.db 的必需列。返回 {表名: 缺失列清单}；空 dict = 全部就绪。

    这是 fail-closed 的关键：schema 一旦漂移（Hermes 迁移改名），
    报告必须显式标红缺失列，而不是让各 reader 静默吞异常出空表。
    """
    if conn is None:
        return {"state.db": ["<文件不存在或不可读>"]}
    missing = {}
    for table, cols in REQUIRED_COLS.items():
        try:
            have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        except Exception as e:
            missing[table] = ["<读取失败: %s>" % e]
            continue
        miss = [c for c in cols if c not in have]
        if miss:
            missing[table] = miss
    return missing


def skill_usage(conn):
    """从 skill_view 工具返回里解析「哪个 skill 被加载了几次 + 最后时间」。"""
    usage = defaultdict(lambda: {"views": 0, "last": None})
    try:
        rows = conn.execute(
            "SELECT content, timestamp FROM messages WHERE tool_name='skill_view' AND role='tool'"
        )
        for content, ts in rows:
            try:
                d = json.loads(content or "{}")
            except Exception:
                continue
            name = d.get("name")
            if not name:
                continue
            usage[name]["views"] += 1
            if usage[name]["last"] is None or (ts and ts > usage[name]["last"]):
                usage[name]["last"] = ts
    except Exception as e:
        sys.stderr.write("skill_usage error: %s\n" % e)
    return usage


def skill_changes(conn):
    """从 skill_manage 工具返回里解析技能修改历史。"""
    changes = []
    try:
        rows = conn.execute(
            "SELECT content, timestamp FROM messages WHERE tool_name='skill_manage' AND role='tool' ORDER BY id"
        )
        for content, ts in rows:
            try:
                d = json.loads(content or "{}")
            except Exception:
                continue
            msg = d.get("message") or d.get("note") or "(no message)"
            changes.append({"ts": ts, "msg": str(msg)[:200]})
    except Exception as e:
        sys.stderr.write("skill_changes error: %s\n" % e)
    return changes


def disk_skills(skills_root):
    """扫描 skills/ 下所有 SKILL.md，返回 {name: {path, desc}}。"""
    skills = {}
    for f in glob.glob(os.path.join(skills_root, "**", "SKILL.md"), recursive=True):
        try:
            txt = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        m = re.search(r"^name:\s*(.+)$", txt, re.M)
        if not m:
            continue
        name = m.group(1).strip().strip('"').strip("'")
        d = re.search(r"^description:\s*(.+)$", txt, re.M)
        desc = d.group(1).strip().strip('"').strip("'") if d else ""
        skills[name] = {"path": os.path.relpath(f, skills_root).replace("\\", "/"), "desc": desc}
    return skills


def find_dupes(skills, threshold=0.62):
    """基于 description 相似度找疑似重复对。"""
    pairs = []
    names = list(skills.keys())
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = skills[names[i]]["desc"]
            b = skills[names[j]]["desc"]
            if not a or not b:
                continue
            r = SequenceMatcher(None, a.lower(), b.lower()).ratio()
            if r >= threshold:
                pairs.append((names[i], names[j], round(r, 3)))
    pairs.sort(key=lambda x: -x[2])
    return pairs


SEDIMENT_KW_DEFAULT = ["记住", "下次", "别忘了", "以后遇到", "存成 skill", "存成skill", "要不要存", "每次都要"]

# state.db 里 gardener 依赖的必需列。缺失任一时，对应数据 section 显式报警并跳过，
# 绝不静默出空表（fail-closed：监测工具不可假装"一切正常"）。
REQUIRED_COLS = {
    "messages": ["id", "session_id", "role", "content", "tool_name", "timestamp"],
    "sessions": ["id", "title", "started_at", "message_count",
                 "tool_call_count", "estimated_cost_usd", "actual_cost_usd"],
}


def sediment_candidates(conn, kw_list):
    """从 user/assistant 消息里找「值得沉淀成 skill」的线索。"""
    out = []
    try:
        rows = conn.execute(
            "SELECT session_id, role, content, timestamp FROM messages "
            "WHERE role IN ('user','assistant') AND content IS NOT NULL ORDER BY id"
        )
        for sid, role, content, ts in rows:
            c = content or ""
            hit = next((kw for kw in kw_list if kw in c), None)
            if hit:
                snippet = re.sub(r"\s+", " ", c).strip()[:140]
                out.append({"session": sid, "role": role, "kw": hit, "ts": ts, "snippet": snippet})
    except Exception as e:
        sys.stderr.write("sediment error: %s\n" % e)
    return out


def memory_overview(mem_dir):
    """读 memories/ 下 MEMORY.md 与 USER.md，返回条目数 + 字符量 + 最近改动。"""
    overview = []
    for fn in ("MEMORY.md", "USER.md"):
        p = os.path.join(mem_dir, fn)
        if not os.path.isfile(p):
            overview.append({"file": fn, "exists": False})
            continue
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        entries = len(re.findall(r"^§", txt, re.M))
        mtime = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")
        overview.append(
            {"file": fn, "exists": True, "chars": len(txt), "entries": entries, "mtime": mtime}
        )
    return overview


def session_overview(conn):
    """从 sessions 表汇总会话信息。"""
    out = []
    try:
        rows = conn.execute(
            "SELECT id, title, started_at, message_count, tool_call_count, "
            "estimated_cost_usd, actual_cost_usd FROM sessions ORDER BY started_at"
        )
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "title": (r[1] or "")[:60],
                    "started": ts_str(r[2]),
                    "msgs": r[3] or 0,
                    "tools": r[4] or 0,
                    "cost": (r[6] if r[6] is not None else r[5]),
                }
            )
    except Exception as e:
        sys.stderr.write("session_overview error: %s\n" % e)
    return out


def load_snapshot(run_dir):
    """读取上次快照（不存在/损坏返回 None）。"""
    p = os.path.join(run_dir, "snapshot.json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def save_snapshot(run_dir, snap):
    """原子写当前快照。"""
    p = os.path.join(run_dir, "snapshot.json")
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        sys.stderr.write("save_snapshot error: %s\n" % e)


def build_report(args, sediment_kw=None):
    H = args.home or detect_home()
    db = os.path.join(H, "state.db")
    skills_root = os.path.join(H, "skills")
    mem_dir = os.path.join(H, "memories")

    sediment_kw = sediment_kw or SEDIMENT_KW_DEFAULT

    conn = read_state_db(db)
    schema_issues = check_schema(conn)
    msgs_ok = not schema_issues.get("messages")
    sess_ok = not schema_issues.get("sessions")
    disk = disk_skills(skills_root)
    usage = skill_usage(conn) if msgs_ok else {}
    changes = skill_changes(conn) if msgs_ok else []
    dupes = find_dupes(disk)
    cands = sediment_candidates(conn, sediment_kw) if msgs_ok else []
    mem = memory_overview(mem_dir)
    sess = session_overview(conn) if sess_ok else []
    run_dir = os.path.join(H, ".skill-gardener")
    prev = load_snapshot(run_dir)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    L = []
    add = L.append
    add("# Skill Gardener 报告")
    add("")
    add(f"生成时间：{now}")
    add(f"Hermes home：`{H}`")
    add(f"技能总数：{len(disk)}（磁盘 SKILL.md） | 会话数：{len(sess)}")
    add("")
    add("---")
    add("")

    # ⚠️ Schema 自检（fail-closed：缺列必须显式报警，不许静默出空表）
    add("## ⚠️ Schema 自检")
    add("")
    if schema_issues:
        for table, cols in schema_issues.items():
            if cols and isinstance(cols[0], str) and cols[0].startswith("<"):
                add(f"- ❌ `{table}`：{cols[0]}")
            else:
                add(f"- ❌ `{table}` 缺列：{', '.join(cols)}")
        add("")
        add("> 依赖缺失列的数据 section（§1/§5/§6/§6b/§8）已跳过，本报告**不完整**。")
        add("> 请核对 Hermes 版本，state.db 结构可能已迁移。")
    else:
        add("- ✅ 所有必需列就绪，数据 section 完整。")
    add("")

    # 0. 较上次变化（趋势）
    add("## 0. 较上次变化（趋势）")
    add("")
    if prev:
        never = [n for n in disk if n not in usage and n != "skill-gardener"]
        p_count = prev.get("skill_count")
        p_sess = prev.get("session_count")
        p_usage = prev.get("usage", {})
        p_never = set(prev.get("never_loaded", []))
        if p_count is not None:
            d = len(disk) - p_count
            add(f"- 技能总数：{p_count} → {len(disk)}（{'新增 %d' % d if d > 0 else '减少 %d' % -d if d < 0 else '无变化'}）")
        if p_sess is not None:
            d = len(sess) - p_sess
            add(f"- 会话数：{p_sess} → {len(sess)}（{'+%d' % d if d >= 0 else '%d' % d}）")
        moved = []
        for n, u in usage.items():
            pv = 0
            pu = p_usage.get(n)
            if isinstance(pu, dict):
                pv = pu.get("views", 0)
            if u["views"] != pv:
                moved.append((n, pv, u["views"]))
        moved.sort(key=lambda x: -(x[2] - x[1]))
        if moved:
            add("- 加载次数变化：" + "、".join(f"{n} {a}→{b}" for n, a, b in moved[:5]))
        cur_never = set(never)
        newly = p_never - cur_never
        d = len(cur_never) - len(p_never)
        if newly:
            add(f"- 从未加载：{len(p_never)} → {len(cur_never)}，开始被使用：{'、'.join(sorted(newly))}")
        elif d != 0:
            add(f"- 从未加载：{len(p_never)} → {len(cur_never)}（{'+%d' % d if d > 0 else '%d' % d}）")
        else:
            add(f"- 从未加载：{len(p_never)} → {len(cur_never)}（无变化）")
        add(f"  _上次快照：{prev.get('ts', '?')}_")
    else:
        add("（首次运行，无历史对比；下次起显示「较上次变化」）")
    add("")

    # 1. 热点技能
    if usage:
        hot = sorted(usage.items(), key=lambda kv: (-kv[1]["views"], kv[0]))
        add("## 1. 热点技能（按被加载次数）")
        add("")
        add("| 技能 | 加载次数 | 最后加载 |")
        add("|---|---|---|")
        for name, u in hot[: args.top]:
            add(f"| {name} | {u['views']} | {ts_str(u['last'])} |")
        add("")

    # 2. 长期未用（曾经用过但超过阈值）
    stale = []
    for name, u in usage.items():
        if u["last"] is not None:
            days = (datetime.now() - datetime.fromtimestamp(float(u["last"]))).days
            if days >= args.stale_days:
                stale.append((name, u["views"], days, u["last"]))
    stale.sort(key=lambda x: -x[2])
    add(f"## 2. 长期未用（曾加载但 ≥ {args.stale_days} 天没碰）")
    add("")
    if stale:
        add("| 技能 | 历史加载 | 闲置天数 | 最后加载 |")
        add("|---|---|---|---|")
        for name, v, days, last in stale:
            add(f"| {name} | {v} | {days} | {ts_str(last)} |")
    else:
        add("（无）")
    add("")

    # 3. 疑似重复
    add("## 3. 疑似重复（description 相似度 ≥ 0.62，需人工确认）")
    add("")
    if dupes:
        add("| 技能 A | 技能 B | 相似度 |")
        add("|---|---|---|")
        for a, b, r in dupes[: args.top]:
            add(f"| {a} | {b} | {r} |")
    else:
        add("（无）")
    add("")

    # 4. 从未使用（磁盘有、state.db 无加载记录）
    never = [n for n in disk if n not in usage and n != "skill-gardener"]
    add(f"## 4. 从未加载过的技能（{len(never)} 个，多为预装/领域不相关）")
    add("")
    if never:
        by_cat = defaultdict(list)
        for n in sorted(never):
            cat = disk[n]["path"].split("/")[0]
            by_cat[cat].append(n)
        for cat in sorted(by_cat):
            add(f"- **{cat}**：{'、'.join(by_cat[cat])}")
    else:
        add("（无）")
    add("")

    # 5. 技能修改历史
    add("## 5. 技能修改历史（skill_manage）")
    add("")
    if changes:
        for c in changes:
            add(f"- `{ts_str(c['ts'])}` — {c['msg']}")
    else:
        add("（无）")
    add("")

    # 6. 沉淀候选
    add("## 6. 沉淀候选（会话里的「以后记住/下次…」线索）")
    add("")
    if cands:
        seen = set()
        for c in cands:
            key = (c["session"], c["kw"], c["snippet"][:60])
            if key in seen:
                continue
            seen.add(key)
            add(f"- [{c['role']}] `{c['kw']}`（{ts_str(c['ts'])}）：{c['snippet']}")
    else:
        add("（无）")
    add("")

    # 6b. 难任务会话（高工具调用 = 可复用流程矿脉）
    add("## 6b. 难任务会话（高工具调用，可能是可复用流程的矿脉）")
    add("")
    hard = sorted([s for s in sess if s["tools"]], key=lambda s: -s["tools"])[:5]
    if hard:
        add("| 会话 | 标题 | 工具调用 | 消息 |")
        add("|---|---|---|---|")
        for s in hard:
            add(f"| {s['id']} | {s['title']} | {s['tools']} | {s['msgs']} |")
    else:
        add("（无）")
    add("")

    # 7. Memory 概览
    add("## 7. Memory 概览")
    add("")
    for m in mem:
        if m["exists"]:
            add(f"- `{m['file']}`：{m['chars']} 字符，{m['entries']} 条（`§` 分隔），最后改动 {m['mtime']}")
        else:
            add(f"- `{m['file']}`：不存在")
    add("")

    # 8. 会话概览
    add("## 8. 会话概览")
    add("")
    add("| 会话 | 标题 | 开始 | 消息 | 工具调用 | 成本(USD) |")
    add("|---|---|---|---|---|---|")
    for s in sess:
        cost = f"${s['cost']:.4f}" if s["cost"] is not None else "?"
        add(f"| {s['id']} | {s['title']} | {s['started']} | {s['msgs']} | {s['tools']} | {cost} |")
    add("")

    # 行动建议
    add("---")
    add("")
    add("## 行动建议（给 agent）")
    add("")
    add("- 疑似重复（§3）→ 逐一确认后合并/删除。")
    add("- 长期未用（§2）→ 结合用户实际工作判断是否归档。")
    add("- 沉淀候选（§6）→ 挑确有可复用流程的，生成 SKILL.md 草稿到 inbox，交用户确认。")
    add("- 本报告只读，绝不自动改动任何 skill / memory。")

    report = "\n".join(L)
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(os.path.join(run_dir, "inbox"), exist_ok=True)
    rp = os.path.join(run_dir, "report.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(report)
    save_snapshot(run_dir, {
        "ts": now,
        "skill_count": len(disk),
        "usage": {n: {"views": u["views"]} for n, u in usage.items()},
        "never_loaded": [n for n in disk if n not in usage and n != "skill-gardener"],
        "session_count": len(sess),
    })
    return report, rp


def main():
    ap = argparse.ArgumentParser(description="Skill Gardener 报告生成器")
    ap.add_argument("--home", default=None, help="Hermes home 路径（默认自动探测）")
    ap.add_argument("--stale-days", type=int, default=30, help="长期未用阈值（天）")
    ap.add_argument("--top", type=int, default=15, help="每类最多列出的条数")
    ap.add_argument("--sediment-kw", default=None,
                    help="逗号分隔的沉淀关键词，覆盖默认中文表（例：'remember,下次别忘了'）")
    args = ap.parse_args()
    if args.sediment_kw:
        sediment_kw = [k.strip() for k in args.sediment_kw.split(",") if k.strip()]
    else:
        sediment_kw = SEDIMENT_KW_DEFAULT
    report, rp = build_report(args, sediment_kw)
    print(report)
    sys.stderr.write("\n[report saved] %s\n" % rp)


if __name__ == "__main__":
    main()
