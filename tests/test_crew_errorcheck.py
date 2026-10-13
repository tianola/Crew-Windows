#!/usr/bin/env python3
"""Tests for scripts/crew_errorcheck.py - the reader that names the cause under a card's failures.

The board is the kernel's own (kernel_board), so a test sees the real task_runs and task_events the checker
reads; the verdict log and the progress ledger are the crew's files under a throwaway HERMES_HOME, so nothing
here touches the live board or the live home.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

_TMP = tempfile.mkdtemp(prefix="crew-errorcheck-")
os.environ["HERMES_HOME"] = os.path.join(_TMP, "home")
os.environ["HERMES_BIN"] = "/bin/false"
os.environ["KANBAN_DB"] = os.path.join(_TMP, "not-the-live-board.db")

import crew_card        # noqa: E402
import crew_errorcheck as EC  # noqa: E402
import crew_heal        # noqa: E402
import kernel_board as K    # noqa: E402

BODY = ("Role: backend-engineer\nCoordinator: owner/s\nBudget: 200000 tokens\n\n"
        "GOAL: g\nDone when: the tests pass\n\n"
        "Units (one entry each in $HERMES_HOME/crew/progress/<card>.json):\n"
        "  - the endpoint answers\n  - the migration applies\n\n"
        "proof command: pytest -q\n")
REASON = "Citations are mismatched: PMC7143747 is a stored-product-insects paper, not Topol."


class BoardCase(unittest.TestCase):
    """A board of the kernel's own making, with the raw rows the checker is about."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=_TMP)
        self.kb, self.conn, self.db = K.open_board(self.dir)
        self.addCleanup(self.conn.close)
        patch = mock.patch.object(crew_heal, "KANBAN_DB", self.db)
        patch.start()
        self.addCleanup(patch.stop)
        home = mock.patch.object(crew_card, "hermes_home", return_value=os.environ["HERMES_HOME"])
        home.start()
        self.addCleanup(home.stop)
        board = mock.patch.object(crew_card, "kanban_db", return_value=self.db)
        board.start()
        self.addCleanup(board.stop)

    def card(self, body=BODY, status="ready"):
        return K.add_card(self.conn, status, body=body, assignee="crew-backend-engineer")

    def ledger(self, cid, units):
        path = crew_card.progress_path(cid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"card": cid, "units": units, "created_at": 1.0, "updated_at": 1.0}, fh)
        return path

    def both_passed(self):
        return [{"unit": "the endpoint answers", "pass": True, "evidence": "curl 200", "by": "crew-verifier"},
                {"unit": "the migration applies", "pass": True, "evidence": "alembic head", "by": "crew-verifier"}]

    def add_run(self, cid, status="crashed", outcome=None, error="", summary="", ts=None, profile=None):
        ts = int(ts or time.time())
        self.conn.execute("insert into task_runs (task_id, profile, status, started_at, ended_at, outcome, "
                          "summary, error) values (?,?,?,?,?,?,?,?)",
                          (cid, profile or "crew-backend-engineer", status, ts - 60, ts, outcome, summary, error))
        self.conn.commit()
        return self.conn.execute("select id from task_runs where task_id = ? order by id desc", (cid,)).fetchone()[0]

    def add_event(self, cid, kind, payload=None, ts=None):
        self.conn.execute("insert into task_events (task_id, run_id, kind, payload, created_at) "
                          "values (?,?,?,?,?)",
                          (cid, None, kind, json.dumps(payload or {}), int(ts or time.time())))
        self.conn.commit()

    def proof(self, cid, cmd="pytest -q"):
        """The command the close rule actually runs: the snapshot the intake writes when the card opens."""
        self.add_event(cid, "origin", {"proof_cmd": cmd, "by": "crew"})
        return cmd

    def line(self, verdict="PASS", rc=0, cmd="pytest -q", by="crew-verifier", ts=None, out="11 passed"):
        return {"ts": ts or time.time(), "verdict": verdict, "rc": rc, "command": cmd, "by": by,
                "output_head": out}


class WallKindOfTests(unittest.TestCase):
    def test_a_check_that_could_not_run_is_the_check_broken_not_the_work(self):
        for text in ("bash: line 1: pytest: command not found", "python: can't open file 'x.py': "
                     "[Errno 2] No such file or directory", "usage: pytest.py [-h]", "FATAL: no tests ran"):
            with self.subTest(text=text):
                self.assertEqual("proof_broken",
                                 EC.wall_kind_of({"source": "verdict", "kind": "other", "text": text}))

    def test_a_failing_test_is_the_work_failing(self):
        rec = {"source": "verdict", "kind": "other", "text": "assert 1 == 0\nFAILED tests/test_x.py::test_y"}
        self.assertEqual("work_failed", EC.wall_kind_of(rec))


class RunAndEventRecordsTests(BoardCase):
    def test_a_failed_run_is_a_record_with_its_wall_kind(self):
        cid = self.card()
        self.add_run(cid, status="crashed", error="Traceback: rate limit exceeded", ts=1000)
        rows = EC.run_records(self.db, cid)
        self.assertEqual(1, len(rows))
        self.assertEqual("run", rows[0]["source"])
        self.assertEqual("crashed", rows[0]["status"])
        self.assertEqual("quota", rows[0]["kind"])
        self.assertIn("rate limit", rows[0]["text"])

    def test_a_successful_run_is_not_evidence_about_a_stuck_card(self):
        cid = self.card()
        self.add_run(cid, status="done", outcome="completed", summary="shipped", ts=1000)
        self.assertEqual([], EC.run_records(self.db, cid))

    def test_the_events_nothing_else_reads_come_back_with_their_payload_as_text(self):
        cid = self.card()
        self.add_event(cid, "rate_limited", {"error": "429 too many requests"}, ts=1000)
        self.add_event(cid, "completion_blocked_hallucination",
                       {"phantom_cards": ["t_zzz"], "summary_preview": "claims three cards that do not exist"},
                       ts=1001)
        rows = EC.event_records(self.db, cid)
        kinds = sorted(r["status"] for r in rows)
        self.assertEqual(["completion_blocked_hallucination", "rate_limited"], kinds)
        hall = [r for r in rows if r["status"] == "completion_blocked_hallucination"][0]
        self.assertIn("claims three cards", hall["text"])

    def test_an_unknown_event_kind_is_not_read_at_all(self):
        cid = self.card()
        self.add_event(cid, "heartbeat", {"x": 1}, ts=1000)
        self.assertEqual([], EC.event_records(self.db, cid))

    def test_a_verdict_fail_line_is_a_record_of_what_the_check_said(self):
        cid = self.card()
        crew_card.record_verdict(cid, "pytest -q", 1, "FAILED test_x::test_y - assert 0 == 1", 1.2, by="crew-verifier")
        rows = EC.verdict_records(cid)
        self.assertEqual(1, len(rows))
        self.assertEqual("verdict", rows[0]["source"])
        self.assertIn("assert 0 == 1", rows[0]["text"])
        self.assertEqual(1, rows[0]["rc"])

    def test_a_pass_line_is_not_a_failure(self):
        cid = self.card()
        crew_card.record_verdict(cid, "pytest -q", 0, "5 passed", 1.2, by="crew-verifier")
        self.assertEqual([], EC.verdict_records(cid))

    def test_collect_merges_the_three_places_newest_first_and_kind(self):
        cid = self.card()
        self.add_run(cid, status="crashed", error="boom", ts=1000)
        self.add_event(cid, "spawn_failed", {"error": "No such file or directory"}, ts=2000)
        crew_card.record_verdict(cid, "pytest -q", 1, "ModuleNotFoundError: No module named 'x'", 0.5,
                                 by="crew-verifier")
        with mock.patch.object(crew_card, "all_verdicts", side_effect=lambda c: [
                {"ts": 3000.0, "verdict": "FAIL", "rc": 1, "command": "pytest -q",
                 "output_head": "ModuleNotFoundError: No module named 'x'"}]):
            recs = EC.collect(cid, self.db)["records"]
        self.assertEqual(["verdict", "event", "run"], [r["source"] for r in recs])
        self.assertEqual("proof_broken", recs[0]["kind"])
        self.assertEqual("spawn_failed", recs[1]["kind"])


class RootCauseTests(BoardCase):
    def rec(self, source="run", status="crashed", kind="other", text="", ts=1000):
        return {"source": source, "id": 1, "status": status, "kind": kind, "ts": ts, "text": text}

    def test_a_script_awaiting_consent_outranks_every_text_clue(self):
        cid = self.card()
        recs = [self.rec(kind="quota", text="quota exceeded")]
        with mock.patch.object(crew_card, "pending_script_authorization", return_value={"why": "changed"}), \
             mock.patch.object(crew_card, "verifier_block", return_value=None):
            found = EC.root_cause(cid, recs)
        self.assertEqual("script_changed", found["cause"])
        self.assertEqual("owner", found["owner"])

    def test_a_hallucination_event_is_its_own_cause(self):
        cid = self.card()
        recs = [self.rec(source="event", status="completion_blocked_hallucination", text="phantom")]
        with mock.patch.object(crew_card, "pending_script_authorization", return_value=None), \
             mock.patch.object(crew_card, "verifier_block", return_value=None):
            found = EC.root_cause(cid, recs)
        self.assertEqual("hallucinated", found["cause"])
        self.assertEqual("decide", found["owner"])

    def test_the_verifiers_block_is_the_cause_even_when_the_proof_now_passes(self):
        cid = self.card()
        with mock.patch.object(crew_card, "pending_script_authorization", return_value=None), \
             mock.patch.object(crew_card, "verifier_block", return_value={"reason": "fabricated citations"}):
            found = EC.root_cause(cid, [self.rec(kind="proof_failed", text="tests pass")])
        self.assertEqual("verifier_blocked", found["cause"])

    def test_a_quota_wall_is_a_heal_owned_cause(self):
        found = EC.root_cause("t_x", [self.rec(kind="quota", text="429 rate limit")])
        self.assertEqual("dead_model", found["cause"])
        self.assertEqual("heal", found["owner"])

    def test_a_spawn_failure_blames_the_machine_not_the_work(self):
        found = EC.root_cause("t_x", [self.rec(status="spawn_failed", kind="spawn_failed",
                                              text="No such file or directory: hermes")])
        self.assertEqual("spawn_failed", found["cause"])
        self.assertEqual("owner", found["owner"])

    def test_a_repeated_block_goes_to_the_owner(self):
        found = EC.root_cause("t_x", [self.rec(source="event", status="block_loop_detected", text="twice")])
        self.assertEqual("repeat_block", found["cause"])
        self.assertEqual("owner", found["owner"])

    def test_a_failing_verdict_is_the_work_and_goes_for_research(self):
        found = EC.root_cause("t_x", [self.rec(source="verdict", status="fail", kind="work_failed",
                                              text="assert 0 == 1")])
        self.assertEqual("work_failed", found["cause"])
        self.assertEqual("research", found["owner"])

    def test_nothing_at_all_is_honestly_no_cause(self):
        found = EC.root_cause("t_x", [])
        self.assertEqual("other", found["cause"])
        self.assertEqual("decide", found["owner"])
        self.assertEqual(0, found["records"])

    def test_every_cause_names_an_owner_and_a_meaning(self):
        for name, owner, meaning in EC.CAUSES:
            with self.subTest(cause=name):
                self.assertIn(owner, ("heal", "research", "owner", "decide"))
                self.assertTrue(meaning)
                self.assertEqual(owner, EC.cause(name, None, [])["owner"])


class PlanTests(BoardCase):
    def card_with_proof(self, units, body=BODY, verdicts=None):
        cid = self.card(body=body)
        self.proof(cid)
        self.ledger(cid, units)
        return cid, (verdicts if verdicts is not None else [self.line()])

    def test_each_unit_of_the_contract_is_answered_for(self):
        cid = self.card()
        self.proof(cid)
        self.ledger(cid, [{"unit": "the endpoint answers", "pass": True, "evidence": "curl 200", "by": "v"},
                          {"unit": "the migration applies", "pass": None, "evidence": "", "by": ""}])
        plan = EC.plan_of(cid, verdicts=[self.line()])
        self.assertEqual(["pass", "open"], [u["state"] for u in plan["units"]])
        self.assertEqual(["the migration applies"], plan["gaps"])
        self.assertEqual("the tests pass", plan["done_when"])
        self.assertFalse(plan["complete"])

    def test_a_unit_the_worker_never_wrote_a_line_for_is_said_so(self):
        cid = self.card()
        self.proof(cid)
        self.ledger(cid, [{"unit": "the endpoint answers", "pass": True, "evidence": "", "by": "v"}])
        plan = EC.plan_of(cid, verdicts=[self.line()])
        self.assertEqual(["the migration applies (never recorded)"], plan["gaps"])

    def test_a_passing_proof_does_not_make_the_plan_complete_while_a_unit_fails(self):
        cid, lines = self.card_with_proof([{"unit": "the endpoint answers", "pass": True, "evidence": "", "by": "v"},
                                           {"unit": "the migration applies", "pass": False,
                                            "evidence": "lock held", "by": "v"}])
        plan = EC.plan_of(cid, verdicts=lines)
        self.assertEqual("PASS", plan["proof"]["verdict"])
        self.assertFalse(plan["complete"])
        self.assertEqual(["the migration applies"], plan["gaps"])

    def test_all_units_passed_and_the_proof_passing_is_complete(self):
        cid, lines = self.card_with_proof(self.both_passed())
        plan = EC.plan_of(cid, verdicts=lines)
        self.assertTrue(plan["complete"])
        self.assertEqual([], plan["gaps"])
        self.assertTrue(plan["proof"]["close_ok"])

    def test_the_plan_scores_the_command_the_close_rule_runs_not_the_body_line(self):
        """A rescope rewrites the body line; the snapshot is what runs. Scoring the body line would read a PASS
        off a command nothing executed (t_3f619c1d's rule, seen from the checker's side)."""
        cid = self.card()
        self.proof(cid, "pytest -q tests/test_real.py")
        self.ledger(cid, self.both_passed())
        plan = EC.plan_of(cid, verdicts=[self.line(cmd="pytest -q tests/test_real.py"),
                                         self.line(cmd="pytest -q", verdict="FAIL", rc=1, out="assert 0 == 1")])
        self.assertEqual("PASS", plan["proof"]["verdict"])
        self.assertEqual("pytest -q tests/test_real.py", plan["proof"]["command"])
        self.assertTrue(plan["proof"]["contract_diverges"])
        self.assertEqual("pytest -q", plan["proof"]["contract_line"])

    def test_a_body_line_that_is_not_the_snapshot_is_said_so_on_the_card(self):
        cid = self.card()
        self.proof(cid, "pytest -q -x")
        self.ledger(cid, self.both_passed())
        plan = EC.plan_of(cid, verdicts=[self.line(cmd="pytest -q -x")])
        text = EC.ask(cid, EC.cause("other", None, []), plan)
        self.assertIn("not the command the close rule runs", text)

    def test_a_pass_by_a_profile_that_may_not_close_is_not_called_complete(self):
        """The writer's own PASS on an independent card: close_check refuses it and the checker says the same,
        with the reason, rather than reporting a plan it cannot act on."""
        cid = self.card(body=BODY.replace("Budget: 200000 tokens", "Budget: 200000 tokens\nVerify: independent"))
        self.proof(cid)
        self.ledger(cid, self.both_passed())
        plan = EC.plan_of(cid, verdicts=[self.line(by="crew-backend-engineer")])
        self.assertFalse(plan["proof"]["close_ok"])
        self.assertFalse(plan["complete"])
        self.assertIn("not by", plan["proof"]["close_reason"])

    def test_a_card_with_no_owner_confirmed_proof_says_what_is_missing(self):
        cid = self.card()
        self.ledger(cid, self.both_passed())
        plan = EC.plan_of(cid, verdicts=[self.line()])
        self.assertEqual("none", plan["proof"]["verdict"])
        self.assertFalse(plan["complete"])
        self.assertIn("no owner-confirmed proof command", plan["proof"]["close_reason"])

    def test_a_check_that_failed_after_the_pass_holds_the_plan_open(self):
        cid, lines = self.card_with_proof(self.both_passed(), verdicts=[
            self.line(), self.line(verdict="FAIL", rc=1, cmd="pytest -q tests/e2e", out="e2e red")])
        plan = EC.plan_of(cid, verdicts=lines)
        self.assertFalse(plan["proof"]["close_ok"])
        self.assertIn("after the PASS line", plan["proof"]["close_reason"])

    def test_a_card_without_units_says_so_instead_of_calling_it_done(self):
        cid = self.card(body="Role: writer\nCoordinator: owner/s\nBudget: 1\nVerify: proof\n\nGOAL: g\n"
                             "Done when: d\n\nproof command: true\n")
        self.proof(cid, "true")
        plan = EC.plan_of(cid, verdicts=[self.line(cmd="true")])
        self.assertTrue(plan["no_contract_units"])
        self.assertFalse(plan["complete"])

    def test_a_ledger_unit_the_contract_never_named_is_shown_as_extra(self):
        cid, lines = self.card_with_proof(self.both_passed() +
                                          [{"unit": "a bonus refactor", "pass": True, "evidence": "", "by": "v"}])
        plan = EC.plan_of(cid, verdicts=lines)
        self.assertEqual(["a bonus refactor"], plan["extra_units"])


class StalePassTests(BoardCase):
    def plan(self, cid, verdict):
        return {"proof": {"verdict": verdict, "command": "pytest -q"},
                "units": [{"unit": "the endpoint answers", "state": "pass"}]}

    def test_a_pass_the_newest_proof_denies_is_stale(self):
        cid = self.card()
        self.assertEqual(["the endpoint answers"], EC.stale_passes(self.plan(cid, "FAIL")))

    def test_a_passing_proof_leaves_the_ledger_alone(self):
        cid = self.card()
        self.assertEqual([], EC.stale_passes(self.plan(cid, "PASS")))

    def test_reset_writes_the_pass_back_to_open_with_the_reason(self):
        cid = self.card()
        path = self.ledger(cid, [{"unit": "the endpoint answers", "pass": True, "evidence": "curl 200", "by": "v"}])
        touched = EC.reset_passes(cid, self.plan(cid, "FAIL"), dry=False)
        self.assertEqual(["the endpoint answers"], touched)
        with open(path) as fh:
            unit = json.load(fh)["units"][0]
        self.assertIsNone(unit["pass"])
        self.assertIn("`pytest -q` failed after this was marked passed", unit["evidence"])

    def test_dry_says_what_it_would_do_and_writes_nothing(self):
        cid = self.card()
        path = self.ledger(cid, [{"unit": "the endpoint answers", "pass": True, "evidence": "curl 200", "by": "v"}])
        self.assertEqual(["the endpoint answers"], EC.reset_passes(cid, self.plan(cid, "FAIL"), dry=True))
        with open(path) as fh:
            self.assertIs(True, json.load(fh)["units"][0]["pass"])


class AskTests(unittest.TestCase):
    def test_the_question_carries_the_cause_the_evidence_and_the_open_items(self):
        finding = EC.cause("work_failed", {"source": "verdict", "id": 3, "text": "assert 0 == 1"}, [{}])
        plan = {"gaps": ["the migration applies"], "proof": {"verdict": "FAIL", "rc": 1},
                "done_when": "the tests pass"}
        text = EC.ask("t_ab", finding, plan)
        self.assertIn("cause `work_failed`", text)
        self.assertIn("assert 0 == 1", text)
        self.assertIn("still open from the plan: the migration applies", text)
        self.assertIn("FAIL (rc=1)", text)
        self.assertIn("--fix", text)

    def test_a_broken_check_is_named_as_unmeasured_not_wrong(self):
        finding = EC.cause("proof_broken", {"source": "verdict", "id": 3, "text": "pytest: not found"}, [{}])
        plan = {"gaps": [], "proof": {"verdict": "FAIL", "rc": 127}, "done_when": ""}
        self.assertIn("the work is unmeasured", EC.ask("t_ab", finding, plan))


class ReproveTests(BoardCase):
    def test_a_card_without_an_owner_confirmed_proof_runs_nothing(self):
        cid = self.card()
        with mock.patch.object(crew_card, "close_proof_command", return_value=""):
            out = EC.reprove(cid, dry=False)
        self.assertFalse(out["ran"])
        self.assertIn("no owner-confirmed proof", out["why"])

    def test_dry_names_the_command_and_runs_nothing(self):
        cid = self.card()
        with mock.patch.object(crew_card, "close_proof_command", return_value="pytest -q"), \
             mock.patch.object(EC.crew_safety, "run_proof", side_effect=AssertionError("dry ran the proof")):
            out = EC.reprove(cid, dry=True)
        self.assertFalse(out["ran"])
        self.assertIn("pytest -q", out["why"])

    def test_a_passing_rerun_records_the_verdict_the_close_rule_reads(self):
        cid = self.card()
        lines = []
        with mock.patch.object(crew_card, "close_proof_command", return_value="pytest -q"), \
             mock.patch.object(EC.crew_safety, "run_proof",
                               return_value=SimpleNamespace(rc=0, out="7 passed", blocked="")), \
             mock.patch.object(EC.crew_safety, "proof_mode", return_value="plain"), \
             mock.patch.object(crew_card, "record_verdict",
                               side_effect=lambda c, cmd, rc, out, dur, by=None, for_event=None: lines.append((cmd, rc))):
            out = EC.reprove(cid, dry=False)
        self.assertTrue(out["ran"])
        self.assertEqual("PASS", out["verdict"])
        self.assertEqual([("pytest -q", 0)], lines)

    def test_a_refused_proof_records_no_verdict(self):
        cid = self.card()
        with mock.patch.object(crew_card, "close_proof_command", return_value="rm -rf /"), \
             mock.patch.object(EC.crew_safety, "run_proof",
                               return_value=SimpleNamespace(rc=None, out="", blocked="destructive")), \
             mock.patch.object(crew_card, "record_verdict", side_effect=AssertionError("recorded a verdict that never ran")):
            out = EC.reprove(cid, dry=False)
        self.assertFalse(out["ran"])
        self.assertEqual("destructive", out["blocked"])


class CheckCardTests(BoardCase):
    def test_reading_a_card_writes_nothing_at_all(self):
        cid = self.card()
        self.add_run(cid, status="crashed", error="rate limit exceeded", ts=1000)
        with mock.patch.object(crew_card, "record_brief", side_effect=AssertionError("read wrote to the card")), \
             mock.patch.object(crew_heal, "heal_card", side_effect=AssertionError("read healed")), \
             mock.patch.object(EC.crew_safety, "run_proof", side_effect=AssertionError("read ran the proof")):
            out = EC.check_card(cid, self.db, fix=False)
        self.assertEqual("dead_model", out["cause"])
        self.assertEqual("heal", out["owner"])
        self.assertEqual([], out["did"])

    def test_fixing_applies_the_remedy_posts_the_cause_and_re_checks_the_plan(self):
        cid = self.card()
        self.add_run(cid, status="crashed", error="429 quota exceeded", ts=1000)
        seen = []
        with mock.patch.object(crew_heal, "heal_card",
                               return_value={"class": "dead_model", "action": "re-pinned on anthropic/claude",
                                             "fixed": True}), \
             mock.patch.object(crew_card, "record_brief", side_effect=lambda *a, **k: seen.append("brief")), \
             mock.patch.object(crew_card, "close_proof_command", return_value="pytest -q"), \
             mock.patch.object(EC.crew_safety, "run_proof",
                               return_value=SimpleNamespace(rc=0, out="7 passed", blocked="")), \
             mock.patch.object(EC.crew_safety, "proof_mode", return_value="plain"), \
             mock.patch.object(crew_card, "record_verdict", return_value={}), \
             mock.patch.object(crew_card, "all_verdicts", return_value=[
                 {"ts": time.time(), "verdict": "PASS", "rc": 0, "command": "pytest -q", "output_head": "7 passed"}]):
            out = EC.check_card(cid, self.db, fix=True)
        self.assertIn("dead_model: re-pinned on anthropic/claude", out["did"])
        self.assertIn("proof re-run: PASS (rc=0)", out["did"])
        self.assertEqual([], seen)      # a heal-owned cause is not also posted as a question
        self.assertFalse(out["complete"])   # its units are still unevidenced

    def test_a_research_cause_is_posted_on_the_card_for_resolution(self):
        cid = self.card()
        self.conn.execute("insert into task_runs (task_id, profile, status, started_at, ended_at, outcome, summary, "
                          "error) values (?,?,?,?,?,?,?,?)",
                          (cid, "crew-backend-engineer", "failed", 1, 2, "crashed", "",
                           "Traceback: assert 0 == 1"))
        self.conn.commit()
        posted = []
        with mock.patch.object(crew_card, "record_brief",
                               side_effect=lambda c, text, source="owner": posted.append(text) or True), \
             mock.patch.object(crew_card, "close_proof_command", return_value=""), \
             mock.patch.object(crew_card, "pending_script_authorization", return_value=None), \
             mock.patch.object(crew_card, "verifier_block", return_value=None):
            out = EC.check_card(cid, self.db, fix=True)
        self.assertEqual("work_failed", out["cause"])
        self.assertEqual(1, len(posted))
        self.assertIn("cause `work_failed`", posted[0])
        self.assertIn("posted the cause and the open items on the card", out["did"])

    def test_the_checker_calls_complete_what_the_verifiers_own_close_rule_closes(self):
        """The two rules must agree, on the writers the verifier actually uses: a snapshot event, a PASS line in
        the verdict log, a ledger the worker marked. If the checker says complete and the close rule says no,
        the owner is told to trust a card the board will not shut."""
        cid = self.card()
        self.proof(cid)
        self.ledger(cid, self.both_passed())
        crew_card.record_verdict(cid, "pytest -q", 0, "11 passed", 1.0, by="crew-verifier")
        found = EC.check_card(cid, self.db)
        self.assertTrue(found["plan"]["proof"]["close_ok"], found["plan"]["proof"]["close_reason"])
        self.assertTrue(found["plan"]["complete"])
        self.assertTrue(crew_card.close_check(cid, BODY, crew_card.claimed_at(cid))[0])

    def test_a_checkers_pass_the_close_rule_refuses_is_reported_as_not_complete(self):
        cid = self.card()
        self.proof(cid)
        self.ledger(cid, self.both_passed())
        crew_card.record_verdict(cid, "pytest -q", 0, "11 passed", 1.0, by="crew-backend-engineer")
        found = EC.check_card(cid, self.db)
        self.assertFalse(found["plan"]["complete"])
        self.assertIn("not by", found["plan"]["proof"]["close_reason"])
        self.assertIn("why it cannot close yet", found["ask"])

    def test_the_verifiers_block_stands_over_a_passing_proof_and_opens_nothing(self):
        cid = self.card(body=BODY.replace("Budget: 200000 tokens",
                                          "Budget: 200000 tokens\nVerify: independent"))
        self.proof(cid)
        self.ledger(cid, self.both_passed())
        self.add_run(cid, status="blocked", outcome="blocked", error=REASON, ts=2000, profile="crew-verifier")
        self.add_event(cid, "blocked", {"reason": REASON}, ts=2001)
        self.conn.execute("update tasks set status = 'blocked' where id = ?", (cid,))
        self.conn.commit()
        with mock.patch.object(crew_heal, "heal_card", side_effect=AssertionError("heal lifted the verifier")):
            crew_card.record_verdict(cid, "pytest -q", 0, "11 passed", 1.0, by="crew-verifier")
            found = EC.check_card(cid, self.db)
        self.assertEqual("verifier_blocked", found["cause"])
        self.assertEqual("decide", found["owner"])
        self.assertFalse(found["plan"]["complete"])
        self.assertIn("the verifier blocked the card", found["meaning"])

    def test_fixing_a_verifiers_block_posts_the_reason_and_never_lifts_it(self):
        cid = self.card(body=BODY.replace("Budget: 200000 tokens",
                                          "Budget: 200000 tokens\nVerify: independent"))
        self.proof(cid)
        self.ledger(cid, self.both_passed())
        self.add_run(cid, status="blocked", outcome="blocked", error=REASON, ts=2000, profile="crew-verifier")
        self.add_event(cid, "blocked", {"reason": REASON}, ts=2001)
        self.conn.execute("update tasks set status = 'blocked' where id = ?", (cid,))
        self.conn.commit()
        posted = []
        with mock.patch.object(crew_heal, "heal_card", side_effect=AssertionError("heal lifted the verifier")), \
             mock.patch.object(crew_card, "record_brief",
                               side_effect=lambda c, text, source="owner": posted.append(text) or True):
            found = EC.check_card(cid, self.db, fix=True)
        self.assertEqual(1, len(posted))
        self.assertIn(REASON.split(":")[0], found["ask"])
        self.assertEqual("blocked", self.conn.execute("select status from tasks where id = ?", (cid,)).fetchone()[0])


class VerifierPipelineTests(BoardCase):
    """The checker against the files a verifier session really writes.

    The rest of this file hand-writes the ledger; a drifted unit name, a second copy of the file, or a
    progress path the two tools disagree about would then pass here and fail on the board. These go through
    start_progress and cmd_progress - the same entry points the verifier's own CLI uses."""

    UNITS = ("the endpoint answers", "the migration applies")

    def opened(self, body=BODY, status="ready"):
        """A card the way the crew opens one: contract units in the ledger, snapshot proof, verifier PASS."""
        cid = self.card(body=body, status=status)
        self.proof(cid)
        crew_card.start_progress(cid, [u.strip() for u in crew_card.parse_contract(body)["units"].split("|")])
        return cid

    def mark(self, cid, unit, passed=True, evidence="seen on the running box", by="crew-verifier"):
        crew_card.cmd_progress(SimpleNamespace(card=cid, unit=unit, pass_=passed, evidence=evidence, by=by))

    def test_the_units_the_verifier_records_are_the_units_the_plan_reads(self):
        cid = self.opened()
        for unit in self.UNITS:
            self.mark(cid, unit)
        crew_card.record_verdict(cid, "pytest -q", 0, "11 passed", 1.0, by="crew-verifier")
        found = EC.check_card(cid, self.db)
        self.assertEqual([], found["plan"]["gaps"])
        self.assertEqual([], found["plan"]["extra_units"])
        self.assertTrue(found["plan"]["complete"], found["plan"]["proof"]["close_reason"])

    def test_a_unit_the_verifier_left_open_is_named_and_keeps_the_card_from_looking_done(self):
        cid = self.opened()
        self.mark(cid, self.UNITS[0])
        crew_card.record_verdict(cid, "pytest -q", 0, "11 passed", 1.0, by="crew-verifier")
        found = EC.check_card(cid, self.db)
        self.assertFalse(found["plan"]["complete"])
        self.assertEqual(["the migration applies"], found["plan"]["gaps"])
        self.assertIn("the migration applies", found["ask"])

    def test_a_unit_the_verifier_failed_stays_failed_and_no_fix_clears_it(self):
        cid = self.opened()
        self.mark(cid, self.UNITS[0], passed=False, evidence="500 on the second call")
        self.mark(cid, self.UNITS[1])
        crew_card.record_verdict(cid, "pytest -q", 0, "11 passed", 1.0, by="crew-verifier")
        found = EC.check_card(cid, self.db)
        self.assertEqual("fail", found["plan"]["units"][0]["state"])
        with mock.patch.object(crew_heal, "heal_card", return_value=[]), \
             mock.patch.object(crew_card, "record_brief", return_value=True):
            fixed = EC.check_card(cid, self.db, fix=True)
        self.assertEqual("fail", fixed["plan"]["units"][0]["state"])
        self.assertNotIn("re-opened", " ".join(fixed["did"]))
        state = [u["pass"] for u in crew_card.load_progress(cid)["units"]]
        self.assertEqual([False, True], state)


class BoardStateTests(BoardCase):
    """Cards in the states a check is asked about, including the ones the scan never shows."""

    def test_a_card_back_from_review_is_read_by_its_verdict_not_its_status(self):
        cid = self.card(status="review_requested")
        self.proof(cid)
        self.ledger(cid, self.both_passed())
        self.add_run(cid, status="done", outcome="completed", ts=2000, profile="crew-verifier")
        crew_card.record_verdict(cid, "pytest -q", 1, "assertion error on the migration", 1.0, by="crew-verifier")
        found = EC.check_card(cid, self.db)
        self.assertEqual("work_failed", found["cause"])
        self.assertIn("assertion error", found["evidence"])
        self.assertFalse(found["plan"]["complete"])

    def test_a_card_still_in_triage_has_no_cause_to_name_and_no_completion_to_claim(self):
        cid = self.card(status="triage")
        found = EC.check_card(cid, self.db)
        self.assertEqual("other", found["cause"])                                        # no evidence, no invented cause
        self.assertEqual("decide", found["owner"])
        self.assertEqual("none", found["plan"]["proof"]["verdict"])                       # nothing has run
        self.assertFalse(found["plan"]["complete"])

    def test_a_card_the_board_called_done_with_a_failing_proof_is_still_read_as_failing(self):
        """The scan skips done cards, so a done-but-FAIL card only ever reaches a human through --card. The
        checker must not let the status soften what the last check said."""
        cid = self.card(status="ready")
        self.proof(cid)
        self.ledger(cid, self.both_passed())
        crew_card.record_verdict(cid, "pytest -q", 0, "11 passed", 1.0, by="crew-verifier")
        self.conn.execute("update tasks set status = 'done' where id = ?", (cid,))
        crew_card.record_verdict(cid, "pytest -q", 1, "the suite went red after the merge", 1.0, by="crew-verifier")
        self.conn.commit()
        found = EC.check_card(cid, self.db)
        self.assertEqual("FAIL", found["plan"]["proof"]["verdict"])
        self.assertEqual("work_failed", found["cause"])
        self.assertFalse(found["plan"]["complete"])

    def test_a_card_parked_by_the_owner_is_the_verifiers_words_not_a_broken_check(self):
        cid = self.card(body=BODY.replace("Budget: 200000 tokens",
                                          "Budget: 200000 tokens\nVerify: independent"))
        self.proof(cid)
        self.add_run(cid, status="blocked", outcome="blocked", error="the spec is ambiguous about the schema",
                     ts=2000, profile="crew-backend-engineer")
        self.add_event(cid, "blocked", {"reason": "the spec is ambiguous about the schema"}, ts=2001)
        self.conn.execute("update tasks set status = 'blocked' where id = ?", (cid,))
        self.conn.commit()
        found = EC.check_card(cid, self.db)
        self.assertNotIn(found["cause"], ("proof_broken", "none"))
        self.assertIn("the spec is ambiguous", found["ask"])


class ScanTests(BoardCase):
    def test_the_scan_lists_only_crew_cards_that_are_not_done(self):
        stuck = self.card()
        K.add_card(self.conn, "ready", body="a plain card with no contract", assignee="someone")
        done = self.card(status="ready")
        self.conn.execute("update tasks set status='done' where id=?", (done,))
        self.conn.execute("update tasks set status='blocked' where id=?", (stuck,))
        self.conn.commit()
        self.assertEqual([stuck], EC.crew_cards(self.db))

    def test_rendering_a_card_shows_the_cause_the_items_and_the_actions(self):
        cid = self.card()
        out = EC.check_card(cid, self.db)
        text = EC.render([out], cid)
        self.assertIn("cause: other", text)
        self.assertIn("[OPEN] the endpoint answers", text)
        self.assertIn("still open:", text)

    def test_an_empty_scan_is_a_result_not_an_error(self):
        self.assertEqual("error check: 0 crew card(s) to read", EC.render([], None))


if __name__ == "__main__":
    unittest.main()
