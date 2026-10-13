"""The card page's size line: what runs of this card cost, read off the card's own finished runs.

This layer reports history and refuses to forecast, so the tests are mostly about what it must NOT say. The
pure helpers are pinned directly: which basis a size picks (the card's earlier runs, else the role's, else
nothing), the two-minute floor that keeps a re-spawn out of the history, the `over_s` mark for a run already
past its usual length, and the three formatters - each of which must name a *usual total* and never a
remaining time. `role_durations` is pinned against a throwaway sqlite file. The guards are pinned so a done
run - or a card with no live run - yields no size at all, never a zero and never the role's number.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import crew_graph as cg  # noqa: E402

DURS = [300, 400, 500, 600, 700, 800, 900, 1000]   # 8 samples; crew_card._pct(0.5) -> vals[4] = 700


def run_row(run_id=1, profile="worker", status="done", outcome="completed",
            started=1000, ended=1700):
    """One row in load_runs' column order: id, profile, status, outcome, summary, error, started, ended, hb."""
    return (run_id, profile, status, outcome, "", "", started, ended, ended)


class OneRun(unittest.TestCase):
    def test_a_finished_success_counts(self):
        self.assertEqual(cg.run_length_seconds(1000, 1700, "done", "completed"), 700)
        # the outcome alone is enough: the status column is not the only place success can appear
        self.assertEqual(cg.run_length_seconds(1000, 1700, "running", "completed"), 700)

    def test_a_run_still_going_is_not_history(self):
        self.assertIsNone(cg.run_length_seconds(1000, None, "running", None))

    def test_a_failed_run_is_not_history(self):
        self.assertIsNone(cg.run_length_seconds(1000, 1700, "blocked", "blocked"))
        self.assertIsNone(cg.run_length_seconds(1000, 1700, "crashed", "failed"))

    def test_the_two_minute_floor_rejects_a_re_spawn(self):
        self.assertIsNone(cg.run_length_seconds(1000, 1100, "done", "completed"))
        self.assertEqual(cg.run_length_seconds(1000, 1100, "done", "completed", min_s=0), 100)

    def test_junk_typed_times_are_not_history(self):
        self.assertIsNone(cg.run_length_seconds("x", "y", "done", "completed"))

    def test_prior_runs_keeps_only_the_ones_that_count(self):
        runs = [run_row(1, ended=1600),                      # 600s: history
                run_row(2, status="running", outcome=None, started=2000, ended=None),   # live: not history
                run_row(3, started=3000, ended=3060),         # 60s re-spawn: not history
                run_row(4, status="blocked", outcome="blocked", started=4000, ended=4900)]
        self.assertEqual(cg.prior_run_seconds(runs), [600.0])

    def test_no_runs_is_an_empty_history_not_an_error(self):
        self.assertEqual(cg.prior_run_seconds([]), [])
        self.assertEqual(cg.prior_run_seconds(None), [])


class SizeBasis(unittest.TestCase):
    def test_the_card_owns_the_answer_when_it_has_history(self):
        s = cg.run_size(DURS, [50, 60, 70], 100)
        self.assertEqual(s["basis"], "card")
        self.assertEqual(s["usual_s"], 700)
        self.assertEqual(s["n_prior"], len(DURS))

    def test_only_the_last_three_runs_are_quoted(self):
        s = cg.run_size(DURS, [], 0)
        self.assertEqual(s["prior_s"], [800, 900, 1000])

    def test_the_role_fills_in_for_a_first_run(self):
        s = cg.run_size([], DURS, 100)
        self.assertEqual(s["basis"], "role")
        self.assertEqual(s["usual_s"], 700)
        self.assertEqual(s["n_role"], len(DURS))

    def test_a_thin_role_history_is_not_a_number(self):
        s = cg.run_size([], [600, 700], 100)
        self.assertEqual(s["basis"], "none")
        self.assertIsNone(s["usual_s"])
        self.assertEqual(s["n_role"], 2)

    def test_a_thin_role_history_cannot_be_bootstrapped_by_re_spawns(self):
        # only 130 and 200 clear the two-minute floor, and two samples are below SIZE_MIN_N
        s = cg.run_size([], [130, 200, 60, 90], 100)
        self.assertEqual(s["basis"], "none")
        self.assertEqual(s["n_role"], 2)

    def test_no_history_at_all_is_not_a_guess(self):
        s = cg.run_size([], [], 0)
        self.assertEqual(s["basis"], "none")
        self.assertIsNone(s["usual_s"])
        self.assertEqual(s["prior_s"], [])

    def test_past_usual_is_marked_by_how_far_past(self):
        s = cg.run_size(DURS, [], 900)
        self.assertEqual(s["over_s"], 200)    # 900 elapsed against a 700 usual

    def test_under_usual_marks_nothing(self):
        self.assertIsNone(cg.run_size(DURS, [], 300)["over_s"])

    def test_elapsed_is_never_negative(self):
        self.assertEqual(cg.run_size(DURS, [], -50)["elapsed_s"], 0)


class Formatters(unittest.TestCase):
    def setUp(self):
        self.card = cg.run_size(DURS, [], 300)
        self.over = cg.run_size(DURS, [], 900)
        self.role = cg.run_size([], DURS, 100)
        self.role["profile"] = "backend-engineer"
        self.none = cg.run_size([], [], 100)

    def test_chip_names_the_card_history_and_says_nothing_about_remaining(self):
        text = cg.chip_size(self.card)
        self.assertEqual(text, "earlier runs of this card: 13m, 15m, 16m")
        for word in ("remaining", "left", "ETA", "eta"):
            self.assertNotIn(word, text)

    def test_chip_adds_the_past_usual_tail_only_when_past(self):
        self.assertNotIn("past", cg.chip_size(self.card))
        self.assertIn("past its usual 11m", cg.chip_size(self.over))

    def test_chip_says_it_is_a_first_run_for_the_role_basis(self):
        text = cg.chip_size(self.role)
        self.assertIn("first run on this card", text)
        self.assertIn("backend-engineer", text)
        self.assertIn("usually ~11m", text)

    def test_no_history_is_silence_not_a_zero(self):
        self.assertEqual(cg.chip_size(self.none), "")
        self.assertEqual(cg.tile_size(self.none), "")
        self.assertEqual(cg.chip_size(None), "")

    def test_tile_is_the_one_breath_form_and_still_calls_it_usual(self):
        self.assertEqual(cg.tile_size(self.card), "its usual ~11m")
        self.assertEqual(cg.tile_size(self.over), "past its usual ~11m")
        self.assertEqual(cg.tile_size(self.role), "this role's usual ~11m")

    def test_tile_never_promises_a_time(self):
        for size in (self.card, self.over, self.role):
            text = cg.tile_size(size)
            self.assertIn("usual", text)
            for word in ("remaining", "left", "ETA"):
                self.assertNotIn(word, text)

    def test_fmt_size_says_what_was_measured(self):
        text = cg.fmt_size(self.card)
        self.assertIn("8 earlier runs of this card", text)
        self.assertIn("median 11m", text)
        self.assertIn("16m", text)                    # the run list, not just the median

    def test_fmt_size_marks_the_overrun_by_amount(self):
        self.assertIn("already 3m past that", cg.fmt_size(self.over))

    def test_fmt_size_names_the_sample_count_for_a_first_run(self):
        text = cg.fmt_size(self.role)
        self.assertIn("8 finished runs of the role", text)

    def test_fmt_size_admits_when_there_is_nothing_to_quote(self):
        self.assertIn("no size to quote", cg.fmt_size(self.none))
        self.assertIn("role 0", cg.fmt_size(self.none))
        self.assertEqual(cg.fmt_size(None), "")

    def test_fmt_dur_str_units(self):
        self.assertEqual(cg.fmt_dur_str(45), "45s")
        self.assertEqual(cg.fmt_dur_str(300), "5m")
        self.assertEqual(cg.fmt_dur_str(3900), "1h05m")
        self.assertEqual(cg.fmt_dur_str(None), "-")


class Gatherer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _board(self):
        path = os.path.join(self.tmp.name, "kanban.db")
        conn = sqlite3.connect(path)
        cols = ("id integer primary key, task_id int, profile text, status text, outcome text, "
                "summary text, error text, started_at int, ended_at int, last_heartbeat_at int")
        conn.execute("create table task_runs (%s)" % cols)
        conn.executemany("insert into task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                         "values (?,?,?,?,?,?)", [
            (7, "worker", "done", "completed", 1000, 1600),      # counts: 600s
            (7, "worker", "running", "completed", 2000, 2750),   # counts by outcome: 750s
            (7, "worker", "blocked", "blocked", 3000, 3900),     # not a success -> ignored
            (7, "worker", "done", "completed", 4000, None),      # no end -> ignored
            (8, "worker", "done", "completed", 5000, 5100),      # 100s re-spawn -> still a duration here
            (7, "other", "done", "completed", 6000, 10000),      # other role -> ignored
        ])
        conn.commit()
        conn.close()
        return path

    def test_role_durations_counts_successes_only(self):
        self.assertEqual(sorted(cg.role_durations(self._board(), "worker")),
                         [100.0, 600.0, 750.0])

    def test_role_durations_empty_for_unknown_role(self):
        self.assertEqual(cg.role_durations(self._board(), "ghost"), [])

    def test_role_durations_needs_both_db_and_profile(self):
        self.assertEqual(cg.role_durations(None, "worker"), [])
        self.assertEqual(cg.role_durations(self._board(), None), [])

    def test_load_runs_then_prior_seconds_reads_the_whole_card(self):
        db = self._board()
        runs = cg.load_runs(db, 7)
        self.assertEqual([r[0] for r in runs], sorted(r[0] for r in runs))
        # a card's history is every finished run on it, whatever role ran it - that is the set the 15% error
        # was measured on - while a 100s re-spawn on another card never reaches it.
        self.assertEqual(cg.prior_run_seconds(runs), [600.0, 750.0, 4000.0])


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_no_live_run_is_no_size(self):
        self.assertIsNone(cg.live_run_size(None, None, None))

    def test_a_finished_run_is_no_size(self):
        done = run_row(1, ended=1600)
        self.assertIsNone(cg.live_run_size(None, None, done, runs=[]))

    def test_a_live_run_gets_the_card_history_and_its_profile(self):
        live = run_row(3, status="running", outcome=None, started=int(time.time()) - 300, ended=None)
        s = cg.live_run_size(None, None, live, runs=[run_row(1, ended=1700)])
        self.assertEqual(s["basis"], "card")
        self.assertEqual(s["profile"], "worker")
        self.assertAlmostEqual(s["elapsed_s"], 300, delta=30)

    def test_a_live_run_with_no_where_to_look_is_a_none_basis_not_a_crash(self):
        live = run_row(1, status="running", outcome=None, started=int(time.time()) - 60, ended=None)
        self.assertEqual(cg.live_run_size(None, None, live, runs=[])["basis"], "none")

    def test_a_live_run_loads_the_card_runs_when_the_caller_has_none(self):
        db = self._db()
        live = run_row(9, profile="worker", status="running", outcome=None,
                       started=int(time.time()) - 400, ended=None)
        s = cg.live_run_size(db, (7, "t", "running", "", "", None), live)
        self.assertEqual(s["basis"], "card")
        self.assertEqual(s["prior_s"], [600])

    def _db(self):
        path = os.path.join(self.tmp.name, "board.db")
        conn = sqlite3.connect(path)
        conn.execute("create table task_runs (id integer primary key, task_id int, profile text, "
                     "status text, outcome text, summary text, error text, started_at int, ended_at int, "
                     "last_heartbeat_at int)")
        conn.execute("insert into task_runs (id, task_id, profile, status, outcome, started_at, ended_at) "
                     "values (1, 7, 'worker', 'done', 'completed', 1000, 1600)")
        conn.commit()
        conn.close()
        return path


if __name__ == "__main__":
    unittest.main()
