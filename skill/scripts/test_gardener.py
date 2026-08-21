#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill-gardener 冒烟测试。stdlib unittest，无第三方依赖。

覆盖 gardener.py 里依赖 state.db schema 的 reader + 去重 + schema 自检。
运行：
    python test_gardener.py
或  python -m unittest test_gardener
"""
import json
import sqlite3
import unittest

import gardener


def make_conn(messages=None, sessions=None):
    """建内存 state.db，按 gardener 的 REQUIRED_COLS 契约建表并塞 fixture。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
        "content TEXT, tool_name TEXT, timestamp INTEGER)"
    )
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at INTEGER, "
        "message_count INTEGER, tool_call_count INTEGER, "
        "estimated_cost_usd REAL, actual_cost_usd REAL)"
    )
    for m in (messages or []):
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
            "VALUES (?,?,?,?,?)",
            (m.get("session_id"), m.get("role"), m.get("content"),
             m.get("tool_name"), m.get("timestamp")),
        )
    for s in (sessions or []):
        conn.execute(
            "INSERT INTO sessions (id, title, started_at, message_count, "
            "tool_call_count, estimated_cost_usd, actual_cost_usd) "
            "VALUES (?,?,?,?,?,?,?)",
            (s.get("id"), s.get("title"), s.get("started_at"),
             s.get("message_count"), s.get("tool_call_count"),
             s.get("estimated_cost_usd"), s.get("actual_cost_usd")),
        )
    conn.commit()
    return conn


class SchemaCheckTest(unittest.TestCase):
    def test_all_present(self):
        conn = make_conn()
        self.assertEqual(gardener.check_schema(conn), {})
        conn.close()

    def test_missing_column(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT)")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        missing = gardener.check_schema(conn)
        self.assertIn("messages", missing)
        self.assertIn("tool_name", missing["messages"])
        self.assertIn("sessions", missing)
        self.assertIn("started_at", missing["sessions"])
        conn.close()

    def test_none_conn(self):
        missing = gardener.check_schema(None)
        self.assertTrue(missing)  # 非空 = 有报警


class SkillUsageTest(unittest.TestCase):
    def test_counts_views(self):
        conn = make_conn(messages=[
            {"session_id": "s1", "role": "tool", "tool_name": "skill_view",
             "content": json.dumps({"name": "xlsx"}), "timestamp": 100},
            {"session_id": "s2", "role": "tool", "tool_name": "skill_view",
             "content": json.dumps({"name": "xlsx"}), "timestamp": 200},
            {"session_id": "s3", "role": "tool", "tool_name": "skill_view",
             "content": json.dumps({"name": "pdf"}), "timestamp": 300},
        ])
        usage = gardener.skill_usage(conn)
        self.assertEqual(usage["xlsx"]["views"], 2)
        self.assertEqual(usage["xlsx"]["last"], 200)
        self.assertEqual(usage["pdf"]["views"], 1)
        conn.close()

    def test_ignores_bad_json(self):
        conn = make_conn(messages=[
            {"session_id": "s1", "role": "tool", "tool_name": "skill_view",
             "content": "not json", "timestamp": 100},
        ])
        self.assertEqual(gardener.skill_usage(conn), {})
        conn.close()


class SedimentTest(unittest.TestCase):
    def test_matches_keyword(self):
        conn = make_conn(messages=[
            {"session_id": "s1", "role": "user",
             "content": "以后遇到这个就记住", "timestamp": 100},
            {"session_id": "s1", "role": "assistant",
             "content": "没问题", "timestamp": 101},
        ])
        cands = gardener.sediment_candidates(conn, gardener.SEDIMENT_KW_DEFAULT)
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["kw"], "记住")
        conn.close()

    def test_custom_keyword_list(self):
        conn = make_conn(messages=[
            {"session_id": "s1", "role": "user",
             "content": "please remember this", "timestamp": 100},
        ])
        # 默认中文表不该命中英文
        self.assertEqual(gardener.sediment_candidates(conn, gardener.SEDIMENT_KW_DEFAULT), [])
        # 自定义英文表应该命中
        hit = gardener.sediment_candidates(conn, ["remember"])
        self.assertEqual(len(hit), 1)
        conn.close()


class FindDupesTest(unittest.TestCase):
    def test_similar_descriptions(self):
        skills = {
            "a": {"path": "x/a", "desc": "Convert files between formats from the terminal"},
            "b": {"path": "x/b", "desc": "Convert files between formats from the terminal"},
            "c": {"path": "x/c", "desc": "Manage smart home lights via API"},
        }
        names = {(x[0], x[1]) for x in gardener.find_dupes(skills)}
        self.assertIn(("a", "b"), names)

    def test_no_false_positive(self):
        skills = {
            "a": {"path": "x/a", "desc": "Convert files between formats"},
            "b": {"path": "x/b", "desc": "Control Philips Hue lights"},
        }
        self.assertEqual(gardener.find_dupes(skills), [])


class SessionOverviewTest(unittest.TestCase):
    def test_parses_rows(self):
        conn = make_conn(sessions=[
            {"id": "s1", "title": "报价单", "started_at": 1000,
             "message_count": 10, "tool_call_count": 5,
             "estimated_cost_usd": 0.1, "actual_cost_usd": 0.09},
        ])
        sess = gardener.session_overview(conn)
        self.assertEqual(len(sess), 1)
        self.assertEqual(sess[0]["title"], "报价单")
        self.assertEqual(sess[0]["cost"], 0.09)  # actual 优先
        conn.close()


if __name__ == "__main__":
    unittest.main()
