#!/usr/bin/env python3
"""The crew's error reader: every failure a card carries, the cause under it, and who can resolve it.

The board records failures the crew never reads back. `task_runs.error` is a wall of text, the kernel writes
events (`crashed`, `rate_limited`, `protocol_violation`, `completion_blocked_hallucination`) that no plugin code
consumes, and the verifier's FAIL lines say only that a command exited non-zero. This reader turns all of that
into one answer per card - the named cause, the record it came from, and the next action - and with `--fix` it
takes the actions that are mechanical: the remedies crew_heal already owns, the root cause posted back onto the
card for research, and a re-run of the card's own proof followed by a check of EVERY unit of the original
contract, item by item, so a card cannot be called done on one passing command while a unit of its plan is
still open.

Usage:
  crew_errorcheck.py                     every crew card that is not done, one line each
  crew_errorcheck.py --card t_ab12cd34   one card: its records, its cause, its plan
  crew_errorcheck.py --card t_ab12cd34 --fix   apply the mechanical remedies, then re-verify the plan
  crew_errorcheck.py --json              the same result as data

Read-only unless `--fix`. Exit 0 with the report (an empty report is a result), 1 for an unknown card,
2 when the board cannot be read.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crew_card  # noqa: E402 - the card's contract, verdicts and progress ledger
import crew_heal  # noqa: E402 - the failure words and the mechanical remedies
import crew_safety  # noqa: E402 - the one runner of a proof command

# The kernel writes these; nothing in the plugin reads them back. They are evidence, not noise.
UNREAD_KINDS = ("crashed", "rate_limited", "protocol_violation", "spawn_failed", "timed_out",
                "terminal_worker_reaped", "completion_blocked_hallucination", "gave_up",
                "block_loop_detected", "changes_requested")
REASON_CHARS = 700
PROOF_CANNOT_RUN = ("command not found", "no such file or directory", "unrecognized arguments",
                    "usage:", "permission denied", "externally-managed", "modulenotfounderror",
                    "importerror", "is not a package", "no tests ran", "not a directory")


# ------------------------------------------------------------------ reading the records

def q(db, sql, args=()):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def run_records(db, card_id):
    """The card's failed runs, newest first: (status, wall kind, text).

    Only failures: a card's earlier successful runs are not evidence about why it is stuck now."""
    rows = q(db, "select id, status, outcome, error, summary, started_at, ended_at from task_runs "
                  "where task_id = ? order by id desc limit 12", (card_id,))
    out = []
    for run in rows:
        sign = crew_heal.failure_signature(run)
        if sign is None:
            continue
        out.append({"source": "run", "id": run["id"], "status": sign[0], "kind": sign[1],
                    "ts": run["ended_at"] or run["started_at"] or 0,
                    "text": " ".join(str(run.get(k) or "") for k in ("outcome", "error", "summary")).strip()})
    return out


def event_records(db, card_id):
    """The failure events no plugin code consumes, newest first, with their payload read as data."""
    marks = ",".join("?" * len(UNREAD_KINDS))
    rows = q(db, "select id, kind, payload, created_at from task_events where task_id = ? and kind in (%s) "
                  "order by id desc limit 20" % marks, (card_id,) + UNREAD_KINDS)
    out = []
    for row in rows:
        try:
            data = json.loads(row["payload"] or "{}")
        except ValueError:
            data = {}
        detail = data if isinstance(data, dict) else {}
        text = " ".join(str(detail.get(k) or "") for k in ("error", "reason", "summary_preview")) or row["kind"]
        out.append({"source": "event", "id": row["id"], "status": row["kind"],
                    "kind": crew_heal.wall_kind(text if isinstance(text, str) else row["kind"]),
                    "ts": row["created_at"] or 0, "text": str(text), "data": detail})
    return out


def verdict_records(card_id):
    """The verifier's FAIL lines, newest first - what the check actually said, not that it failed."""
    out = []
    for v in reversed(crew_card.all_verdicts(card_id)):
        if v.get("verdict") != "FAIL":
            continue
        out.append({"source": "verdict", "id": None, "status": "fail", "kind": "other",
                    "ts": v.get("ts") or 0, "command": v.get("command") or "",
                    "rc": v.get("rc"), "text": str(v.get("output_head") or "")})
    return out


def collect(card_id, db=None):
    """Everything that went wrong with a card, newest record first, from all three places."""
    db = db or crew_card.kanban_db()
    recs = run_records(db, card_id) + event_records(db, card_id) + verdict_records(card_id)
    for rec in recs:
        if rec["kind"] == "other":
            rec["kind"] = wall_kind_of(rec)
    recs.sort(key=lambda r: -(r["ts"] or 0))
    return {"card": card_id, "records": recs}


def wall_kind_of(rec):
    """A verdict line's own failure: the check broke, or the work did."""
    low = (rec.get("text") or "").lower()
    if any(w in low for w in PROOF_CANNOT_RUN):
        return "proof_broken"
    return "work_failed" if rec["source"] == "verdict" else rec["kind"]


# ------------------------------------------------------------------ the cause under the records

# Structural facts first: they are read from the plugin's own records, not guessed from text.
CAUSES = (
    # cause                owner      what it means, and who resolves it
    ("script_changed",    "owner",     "the proof script changed and the owner has not authorised it"),
    ("hallucinated",      "decide",    "the worker reported work that is not on the board"),
    ("verifier_blocked",  "decide",    "the verifier blocked the card: its judgement stands over a proof"),
    ("dead_model",        "heal",      "a quota/auth wall on a model the card is still pinned to"),
    ("spawn_failed",      "owner",     "the worker process could not start: the machine, not the work"),
    ("proof_broken",      "research",  "the check itself cannot run, so it says nothing about the work"),
    ("timeout",           "heal",      "the run was cut off, not refused"),
    ("no_heartbeat",      "heal",      "the worker stopped reporting; the kernel respawns it"),
    ("repeat_block",      "owner",     "the same block twice: the card is looping, the owner's call"),
    ("work_failed",       "research",  "the work itself failed - the error below is the thing to fix"),
    ("other",             "decide",    "no named cause: the coordinator's decision turn reads the records"),
)
CAUSE_OWNERS = dict((name, owner) for name, owner, _ in CAUSES)
CAUSE_MEANING = dict((name, meaning) for name, owner, meaning in CAUSES)

# crew_heal's wall kinds are what failed; the cause is who can act on it. proof_broken is not a wall kind:
# wall_kind_of() gives it to a verdict line whose own command could not run.
WALL_TO_CAUSE = {"quota": "dead_model", "auth": "dead_model", "spawn_failed": "spawn_failed",
                 "timeout": "timeout", "tool_error": "work_failed", "proof_failed": "work_failed",
                 "proof_broken": "proof_broken", "thrash": "repeat_block"}


def root_cause(card_id, records):
    """One named cause for the card, from the newest evidence that carries one.

    The structural checks run before the text: a card whose script revision awaits consent, or whose verifier
    blocked it, has a cause the error text cannot show, and the wrong cause sends the card down a remedy that
    cannot work."""
    if crew_card.pending_script_authorization(card_id):
        return cause("script_changed", None, records)
    halluc = [r for r in records if r["status"] == "completion_blocked_hallucination"]
    if halluc:
        return cause("hallucinated", halluc[0], records)
    blocked = crew_card.verifier_block(card_id)
    if blocked:
        # The verifier's own words are the evidence: a card it stopped is resolved against its judgement,
        # not against the error text of some earlier run.
        return cause("verifier_blocked", {"source": "run", "id": blocked.get("run"),
                                          "text": blocked.get("reason") or ""}, records)
    newest = records[0] if records else None
    if newest is None:
        return cause("other", None, records)
    mapped = WALL_TO_CAUSE.get(newest["kind"])
    if mapped:
        return cause(mapped, newest, records)
    if newest["status"] == "crashed":
        return cause("no_heartbeat", newest, records)
    if newest["status"] in ("block_loop_detected", "gave_up"):
        return cause("repeat_block", newest, records)
    if newest["source"] == "verdict":
        return cause("work_failed", newest, records)
    return cause("other", newest, records)


def cause(name, record, records):
    return {"cause": name, "owner": CAUSE_OWNERS[name], "meaning": CAUSE_MEANING[name],
            "from": (record or {}).get("source"), "record_id": (record or {}).get("id"),
            "evidence": ((record or {}).get("text") or "")[:REASON_CHARS],
            "records": len(records)}


# ------------------------------------------------------------------ the original plan, item by item

def plan_of(card_id, body=None, verdicts=None):
    """Every unit of the card's own contract, and whether it is evidenced - the plan re-checked, not assumed.

    A passing proof command says the checks the card was opened with exit 0; it does not say the units of work
    the contract listed were done. This reads the contract's `Units:` list against the progress ledger and the
    verdict log, so a card that passed one command while three units are unevidenced shows as incomplete with
    the items named.

    The verdict half is the closer's own rule, not a second opinion on it: the command is crew_card's snapshot
    and `complete` is `close_check` plus every unit evidenced. A checker that disagreed with the gate would be
    the thing the owner trusts by mistake."""
    body = body if body is not None else _body(card_id)
    contract = crew_card.parse_contract(body)
    ledger = crew_card.load_progress(card_id) or {}
    units = [u.strip() for u in (contract.get("units") or "").split("|") if u.strip()]
    rows = ledger.get("units") or []
    by_unit = {str(u.get("unit") or "").strip(): u for u in rows}
    items, gaps = [], []
    for unit in units:
        entry = by_unit.get(unit)
        state = "open" if entry is None or entry.get("pass") is None else (
            "pass" if entry.get("pass") is True else "fail")
        items.append({"unit": unit, "state": state,
                      "evidence": ((entry or {}).get("evidence") or "").strip(),
                      "by": (entry or {}).get("by") or ""})
        if state != "pass":
            gaps.append(unit if entry is not None else "%s (never recorded)" % unit)
    extra = [str(u.get("unit") or "").strip() for u in rows
             if str(u.get("unit") or "").strip() and str(u.get("unit") or "").strip() not in units]
    # The command the close rule reads - the owner-confirmed snapshot, not the body line. They diverge after a
    # rescope, and scoring the body line would call a card complete on a PASS from a command nothing runs.
    cmd = crew_card.close_proof_command(card_id)
    proof = _latest_proof(card_id, cmd, verdicts)
    proof["command"] = cmd
    ok, why = crew_card.close_check(card_id, body, crew_card.claimed_at(card_id), verdicts)
    blocked = crew_card.verifier_block(card_id)
    if blocked:
        ok, why = False, ("the verifier blocked this card: %s" % (blocked.get("reason") or "")[:200])
    proof["close_ok"], proof["close_reason"] = ok, why
    contract_cmd = contract.get("proof_cmd") or ""
    proof["contract_line"] = contract_cmd
    proof["contract_diverges"] = bool(contract_cmd) and contract_cmd != cmd
    return {"card": card_id, "units": items, "gaps": gaps, "extra_units": extra,
            "done_when": contract.get("done_when") or "", "proof": proof,
            "complete": bool(items) and not gaps and ok,
            "no_contract_units": not items}


def _body(card_id):
    row = crew_card.card_row(card_id)
    return (row[4] if row and len(row) > 4 else "") or ""


def _latest_proof(card_id, cmd, verdicts=None):
    """The newest verdict line for the card's own proof command, as {verdict, rc, ts, text}."""
    lines = [v for v in (verdicts if verdicts is not None else crew_card.all_verdicts(card_id))
             if (v.get("command") or "") == cmd] if cmd else []
    if not lines:
        return {"verdict": "none", "rc": None, "ts": None, "text": ""}
    last = lines[-1]
    return {"verdict": last.get("verdict") or "none", "rc": last.get("rc"), "ts": last.get("ts"),
            "text": str(last.get("output_head") or "")}


# ------------------------------------------------------------------ what the checker does about it

def ask(card_id, finding, plan):
    """The bounded question the resolution turn is handed: cause, evidence, and the plan items still open."""
    lines = ["crew error check: cause `%s` (%s)" % (finding["cause"], finding["meaning"])]
    if finding["evidence"]:
        lines.append("  evidence: %s" % " ".join(finding["evidence"].split())[:300])
    if plan["gaps"]:
        lines.append("  still open from the plan: %s" % "; ".join(plan["gaps"])[:300])
    lines.append("  proof command last said: %s%s" % (
        plan["proof"]["verdict"], "" if plan["proof"]["rc"] is None else " (rc=%s)" % plan["proof"]["rc"]))
    if plan["proof"].get("contract_diverges"):
        lines.append("  the card's `proof command:` line (`%s`) is not the command the close rule runs (`%s`)"
                     % (one_line(plan["proof"]["contract_line"], 70), one_line(plan["proof"]["command"], 70)))
    if not plan["proof"].get("close_ok", True):
        lines.append("  why it cannot close yet: %s" % one_line(plan["proof"].get("close_reason"), 240))
    if finding["cause"] == "proof_broken":
        lines.append("  the check could not run, so fix the check first: the work is unmeasured, not proven wrong.")
    if finding["cause"] == "work_failed":
        lines.append("  fix the failure above, then re-run: crew_errorcheck.py --card %s --fix" % card_id)
    return "\n".join(lines)


def stale_passes(plan):
    """Units the ledger calls passed while the proof last failed - a PASS the newest check denies.

    The ledger is the crew's own bookkeeping, and a unit marked passed that the re-run denies is not a judgement:
    clearing it is what makes the next verifier round re-check that item instead of trusting a stale line."""
    if plan["proof"].get("verdict") != "FAIL":
        return []
    return [u["unit"] for u in plan["units"] if u["state"] == "pass"]


def reset_passes(card_id, plan, dry):
    """Write the stale passes back to open, with the reason in the evidence line."""
    stale = set(stale_passes(plan))
    if not stale:
        return []
    cmd = plan["proof"].get("command") or ""
    why = ("re-opened by crew_errorcheck: the proof command failed after this was marked passed"
           if not cmd else
           "re-opened by crew_errorcheck: the proof command `%s` failed after this was marked passed" % cmd[:80])
    ledger = crew_card.load_progress(card_id) or {}
    units = ledger.get("units") or []
    touched = []
    for u in units:
        if str(u.get("unit") or "").strip() in stale and u.get("pass") is True:
            u["pass"] = None
            u["evidence"] = why
            touched.append(str(u.get("unit") or ""))
    if touched and not dry:
        ledger["units"] = units
        ledger["updated_at"] = time.time()
        os.makedirs(os.path.dirname(crew_card.progress_path(card_id)), exist_ok=True)
        with open(crew_card.progress_path(card_id), "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=1)
    return touched


def reprove(card_id, dry):
    """Run the card's owner-confirmed proof command again and record the verdict: the re-verify step.

    Only the sanctioned runner and the sanctioned command: the card's own proof, under the safety floor, written
    as a verdict line the close rule reads. Anything else is a different check, which is the verifier's job."""
    cmd = crew_card.close_proof_command(card_id)
    if not cmd:
        return {"ran": False, "why": "the card has no owner-confirmed proof command"}
    if dry:
        return {"ran": False, "why": "would re-run: %s" % cmd[:120]}
    t0 = time.time()
    res = crew_safety.run_proof(cmd, None, 240, crew_safety.proof_mode(card_id, cmd), card=card_id)
    if res.blocked:
        return {"ran": False, "blocked": res.blocked, "why": "refused by the safety floor, nothing ran"}
    crew_card.record_verdict(card_id, cmd, res.rc, res.out, time.time() - t0, by=crew_card.current_profile())
    return {"ran": True, "rc": res.rc, "verdict": "PASS" if res.rc == 0 else "FAIL",
            "command": cmd, "output_head": (res.out or "")[:400]}


def check_card(card_id, db=None, fix=False):
    """One card: its records, the cause, its plan item by item, and - with fix - the mechanical actions.

    The actions are ordered as a repair reads: the remedy that needs no judgement (crew_heal), then the root
    cause posted onto the card for whoever resolves it, then a fresh proof, then the plan re-checked against that
    proof. A card no remedy can move keeps its cause and its open items; that is the report, not a failure."""
    records = collect(card_id, db)["records"]
    finding = root_cause(card_id, records)
    plan = plan_of(card_id)
    out = {"card": card_id, "cause": finding["cause"], "owner": finding["owner"],
           "meaning": finding["meaning"], "evidence": finding["evidence"],
           "records": finding["records"], "plan": plan, "ask": ask(card_id, finding, plan), "did": []}
    if not fix:
        return out
    if finding["owner"] == "heal":
        row = crew_card.card_row(card_id)
        healed = crew_heal.heal_card({"id": card_id, "status": row[2] if row else "",
                                     "last_failure_error": finding["evidence"]}, False)
        if healed:
            out["did"].append("%s: %s" % (healed["class"], healed["action"]))
            if not healed.get("fixed"):
                out["did"].append("the remedy did not move the card")
        else:
            out["did"].append("no heal applies in this state")
    if finding["owner"] in ("research", "decide") and records:
        if crew_card.record_brief(card_id, out["ask"], source="errorcheck"):
            out["did"].append("posted the cause and the open items on the card")
    cleared = reset_passes(card_id, plan, dry=False)
    if cleared:
        out["did"].append("re-opened %d unit(s) the failed proof denies: %s"
                          % (len(cleared), "; ".join(cleared)[:200]))
    if records:
        again = reprove(card_id, dry=False)
        if again.get("ran"):
            out["did"].append("proof re-run: %s (rc=%s)" % (again["verdict"], again["rc"]))
        elif again.get("why"):
            out["did"].append(again["why"])
    out["plan"] = plan_of(card_id)
    out["complete"] = out["plan"]["complete"]
    return out


def crew_cards(db):
    """Crew cards that are not done, oldest first - the cards a check is about."""
    rows = q(db, "select id, body, status from tasks order by created_at asc")
    return [r["id"] for r in rows if r["body"] and crew_card.is_crew_body(r["body"])
            and r["status"] not in ("done", "archived")]


# ------------------------------------------------------------------ report

LINE = re.compile(r"\s+")


def one_line(text, limit=110):
    return LINE.sub(" ", (text or "")).strip()[:limit]


def render(results, card):
    if card:
        r = results[0]
        p = r["plan"]
        lines = ["error check: %s" % r["card"], "  cause: %s - %s (owner: %s)"
                 % (r["cause"], r["meaning"], r["owner"]),
                 "  records: %d   evidence: %s" % (r["records"], one_line(r["evidence"]) or "-"),
                 "  done when: %s" % (one_line(p["done_when"]) or "-"),
                 "  proof last: %s%s" % (p["proof"]["verdict"],
                                         "" if p["proof"]["rc"] is None else " (rc=%s)" % p["proof"]["rc"]),
                 "  plan: %d unit(s)%s" % (len(p["units"]), "" if p["units"] else " - the contract names none")]
        for u in p["units"]:
            lines.append("    [%s] %s%s" % (u["state"].upper(), one_line(u["unit"], 70),
                                            (" - " + one_line(u["evidence"], 60)) if u["evidence"] else ""))
        for did in r.get("did") or []:
            lines.append("  did: %s" % did)
        if p["gaps"]:
            lines.append("  still open: %s" % "; ".join(one_line(g, 70) for g in p["gaps"]))
        return "\n".join(lines)
    if not results:
        return "error check: 0 crew card(s) to read"
    lines = ["error check: %d crew card(s)" % len(results), ""]
    for r in results:
        p = r["plan"]
        lines.append("%s  cause: %-17s owner: %-8s plan: %s  %s" % (
            r["card"], r["cause"], r["owner"],
            "complete" if p["complete"] else ("%d open" % len(p["gaps"]) if p["gaps"] else "no units"),
            one_line(r["evidence"], 80) or "-"))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Every failure a crew card carries, its cause, its plan re-checked.")
    ap.add_argument("--card", default=None, help="one card id")
    ap.add_argument("--fix", action="store_true",
                    help="apply the mechanical remedies, post the cause on the card, re-run the proof, re-check the plan")
    ap.add_argument("--json", action="store_true", help="the same result as data")
    args = ap.parse_args(argv)

    db = crew_card.kanban_db()
    if not db:
        print("no board to read: HERMES_KANBAN_DB, KANBAN_DB and %s are all missing"
              % os.path.join(crew_card.base_home(), "kanban.db"))
        return 2
    if args.card:
        if not crew_card.card_row(args.card):
            print("no such card: %s" % args.card)
            return 1
        cards = [args.card]
    else:
        cards = crew_cards(db)
    results = [check_card(c, db, args.fix) for c in cards]
    if args.json:
        print(json.dumps({"card": args.card, "fix": args.fix, "count": len(results), "cards": results},
                         indent=1, default=str))
    else:
        print(render(results, args.card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
