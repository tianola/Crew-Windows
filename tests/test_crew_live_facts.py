"""The live tile's known facts: how many tools a running worker has called and the tool it is in.

Option 1 of issue #9 - facts, never a forecast - so the tests pin exactly that: the numbers come from the
worker's own session record, a run whose session is not recorded yet reports nothing rather than a step 0
that reads as a stall, and the tool name comes from the newest call, not the oldest. No Hermes needed:
both stores are throwaway sqlite files and the profile scan is patched.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import crew_graph as cg  # noqa: E402

SESSION_COLS = ("id text primary key, parent_session_id text, source text, model text, title text, "
                "started_at real, ended_at real, message_count int, tool_call_count int, input_tokens int, "
                "output_tokens int, profile_name text")


def board_db(directory, events=(), name="kanban.db"):
    """A board file holding the row the worker's router hook writes per run: (card_id, run_id, session_id)."""
    path = os.path.join(directory, name)
    conn = sqlite3.connect(path)
    conn.execute("create table task_events (id integer primary key, task_id text, run_id int, kind text, "
                 "payload text)")
    for card_id, run_id, session_id in events:
        conn.execute("insert into task_events (task_id, run_id, kind, payload) values (?,?,?,?)",
                     (card_id, run_id, "session", json.dumps({"session": session_id})))
    conn.commit()
    conn.close()
    return path


def state_db(directory, session_id, steps, turns):
    """A profile state db holding one worker session and the tool calls of its assistant turns, in order."""
    path = os.path.join(directory, "state_%s.db" % session_id)
    conn = sqlite3.connect(path)
    conn.execute("create table sessions (%s)" % SESSION_COLS)
    conn.execute("insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (session_id, None, "kanban", "m", "t", 1000.0, None, 4, steps, 0, 0, "worker"))
    conn.execute("create table messages (id integer primary key, session_id text, role text, "
                 "tool_calls text, timestamp real)")
    for i, names in enumerate(turns):
        payload = json.dumps([{"id": "c%d" % i, "function": {"name": n}} for n in names])
        conn.execute("insert into messages (session_id, role, tool_calls, timestamp) values (?,?,?,?)",
                     (session_id, "assistant", payload, 1000.0 + i))
    conn.commit()
    conn.close()
    return path


def scan(state_path):
    return mock.patch.object(cg, "discover_state_dbs", return_value=[("worker", state_path)])


class LiveRunFacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = state_db(self.tmp.name, "s1", steps=23, turns=[["read"], ["terminal"], ["read", "write"]])
        self.board = board_db(self.tmp.name, [("t_1", 7, "s1")])

    def test_steps_and_last_tool_come_from_the_workers_own_session(self):
        with scan(self.state):
            self.assertEqual(cg.live_run_facts(self.board, "t_1", 7), {"steps": 23, "last_tool": "write"})

    def test_no_recorded_session_is_no_facts_not_a_zero(self):
        with scan(self.state):
            self.assertEqual(cg.live_run_facts(self.board, "t_1", 99), {})

    def test_session_that_named_no_tool_yields_a_count_without_a_tool(self):
        empty = state_db(self.tmp.name, "s2", steps=0, turns=[])
        board = board_db(self.tmp.name, [("t_2", 8, "s2")], name="kanban_empty.db")
        with scan(empty):
            self.assertEqual(cg.live_run_facts(board, "t_2", 8), {"steps": 0})

    def test_unreadable_tool_calls_row_is_a_count_not_a_crash(self):
        conn = sqlite3.connect(self.state)
        conn.execute("insert into messages (session_id, role, tool_calls, timestamp) values (?,?,?,?)",
                     ("s1", "assistant", "{not json", 2000.0))
        conn.commit()
        conn.close()
        with scan(self.state):
            self.assertEqual(cg.live_run_facts(self.board, "t_1", 7), {"steps": 23})

    def test_the_runs_own_profile_store_is_read_without_a_scan(self):
        # the board polls this for every live card, so naming the profile must read that store only
        with mock.patch.object(cg, "state_db_for", return_value=self.state) as sdb, \
             mock.patch.object(cg, "discover_state_dbs",
                               side_effect=AssertionError("scanned every profile store")):
            self.assertEqual(cg.live_run_facts(self.board, "t_1", 7, "worker"),
                             {"steps": 23, "last_tool": "write"})
        sdb.assert_called_once_with("worker")


class LastTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = state_db(self.tmp.name, "s1", steps=3, turns=[["read"], ["search", "terminal"]])

    def test_newest_turn_last_call_wins(self):
        self.assertEqual(cg.last_tool_of(self.state, "s1"), "terminal")

    def test_session_without_calls_has_no_tool(self):
        self.assertIsNone(cg.last_tool_of(self.state, "ghost"))

    def test_no_database_is_no_tool(self):
        self.assertIsNone(cg.last_tool_of(None, "s1"))


class TileCarriesFacts(unittest.TestCase):
    """The tile's active block is the json board.js reads: the run's steps and tool ride on it, and a card
    with no live run carries neither. The board file is hand-made here (only the tables board_data reads) so
    the test runs without Hermes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = state_db(self.tmp.name, "s1", steps=23, turns=[["read"], ["terminal"]])
        self.board = os.path.join(self.tmp.name, "board_tiles.db")
        conn = sqlite3.connect(self.board)
        conn.execute("create table tasks (id text, title text, status text, assignee text, created_at int, "
                     "started_at int, completed_at int, body text, skills text, last_failure_error text)")
        conn.execute("insert into tasks values (?,?,?,?,?,?,?,?,?,?)",
                     ("t_1", "live card", "running", "crew-worker", 1000, 1000, None, "", "", ""))
        conn.execute("insert into tasks values (?,?,?,?,?,?,?,?,?,?)",
                     ("t_2", "queued card", "ready", "crew-worker", 1000, None, None, "", "", ""))
        conn.execute("create table task_runs (id integer primary key, task_id text, profile text, "
                     "status text, started_at int, ended_at int, last_heartbeat_at int, outcome text, "
                     "worker_pid text)")
        # a heartbeat far in the future keeps the run live without needing a real pid on this box
        conn.execute("insert into task_runs (id, task_id, profile, status, started_at, last_heartbeat_at, "
                     "outcome) values (?,?,?,?,?,?,?)", (1, "t_1", "worker", "running", 1000, 4000000000, ""))
        # one finished earlier run of the same card by the same profile: the tile's size half has to come from
        # it, so the merged tile (facts + size) is tested against a real history rather than an empty one
        conn.execute("insert into task_runs (id, task_id, profile, status, started_at, ended_at, "
                     "last_heartbeat_at, outcome) values (?,?,?,?,?,?,?,?)",
                     (0, "t_1", "worker", "done", 1000, 1900, 1900, "completed"))
        conn.execute("create table task_events (id integer primary key, task_id text, run_id int, kind text, "
                     "payload text)")
        conn.execute("insert into task_events (task_id, run_id, kind, payload) values (?,?,?,?)",
                     ("t_1", 1, "session", json.dumps({"session": "s1"})))
        conn.commit()
        conn.close()
        spec = importlib.util.spec_from_file_location("cgs_facts", os.path.join(SCRIPTS, "crew_graph_serve.py"))
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def tiles(self):
        with mock.patch.object(self.mod.CG, "kanban_db_path", return_value=self.board), \
             mock.patch.object(self.mod.CG, "discover_state_dbs", return_value=[("worker", self.state)]):
            out = {}
            for lane in self.mod.board_data()["lanes"]:
                out.update({t["id"]: t for t in lane["tiles"]})
            return out

    def test_live_tile_carries_the_runs_steps_and_tool(self):
        tile = self.tiles()["t_1"]
        self.assertEqual(23, tile["active"]["steps"])
        self.assertEqual("terminal", tile["active"]["last_tool"])

    def test_a_card_with_no_live_run_carries_no_facts(self):
        self.assertIsNone(self.tiles()["t_2"]["active"])

    def test_the_same_tile_carries_the_facts_and_the_card_size(self):
        """0.9.8 added the step count and the tool; the size report is the card's own earlier run lengths.
        They answer different questions - how far this run has got, and what this card has cost before - so
        both ride on one tile, and neither of them is a countdown."""
        tile = self.tiles()["t_1"]["active"]
        self.assertEqual(23, tile["steps"])
        self.assertEqual("terminal", tile["last_tool"])
        self.assertEqual("past its usual ~15m", tile["size"])
        for word in ("remaining", "left", "ETA"):
            self.assertNotIn(word, tile["size"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
