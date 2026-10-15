"""A proof that seeds fixture cards must never give them an assignee the dispatcher can claim.

Measured 2026-09-30: fixtures seeded 'ready' with assignee crew-worker started 20 real crew-worker sessions
in 20 minutes. crew_card.FIXTURE_ASSIGNEE is no Hermes profile, so the dispatcher skips the card. This reads
every proof's source, so a new proof that seeds `tasks` with a real assignee fails here, not on someone's board.
"""
import glob
import os
import re
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")

# diagnose builds a scratch database of its own (no dispatcher reads it); route_floor is not shipped and
# seeds real assignees on purpose to prove the model floor.
ALLOWED = {"crew_diagnose_proof.py", "crew_route_floor_proof.py"}


class ProofFixtureTests(unittest.TestCase):
    def proofs_that_seed_tasks(self):
        for path in sorted(glob.glob(os.path.join(SCRIPTS, "crew_*_proof.py"))):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            if re.search(r"insert into tasks", src, re.I):
                yield os.path.basename(path), src

    def test_every_proof_seeds_its_cards_with_the_fixture_assignee(self):
        seen = 0
        for name, src in self.proofs_that_seed_tasks():
            if name in ALLOWED:
                continue
            seen += 1
            with self.subTest(proof=name):
                self.assertTrue("FIXTURE_ASSIGNEE" in src or "crew-probe" in src, "no fixture assignee")
                for m in re.finditer(r"insert into tasks\b.*?values[\s\"']*\((.*?)\)", src, re.I | re.S):
                    self.assertNotIn("crew-worker", m.group(1), "a claimable assignee in the VALUES list")
        self.assertGreater(seen, 10, "the scan found too few proofs: the glob is wrong")


class ProofHermesPathTests(unittest.TestCase):
    """A proof must not name Hermes's Linux home: on native Windows ~/.hermes does not exist (Hermes lives in
    %LOCALAPPDATA%\\hermes), so a proof that defaults to it dies on a missing directory. crew_card.hermes_home,
    default_home and hermes_root are the platform-aware lookups (found 2026-10-14 on a real Windows 11 box: 18
    of 31 proofs failed with WinError 267)."""

    def test_no_shipped_proof_defaults_to_the_linux_hermes_home(self):
        offenders = []
        for path in sorted(glob.glob(os.path.join(SCRIPTS, "crew_*proof*.py"))):
            name = os.path.basename(path)
            if name == "crew_route_floor_proof.py":      # not shipped; reads this host's live router state
                continue
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if re.search(r"expanduser\(\s*[\"']~/\.hermes|[\"']\.hermes[\"']", line):
                        offenders.append("%s:%d" % (name, n))
        self.assertEqual(offenders, [], "use crew_card.hermes_home()/default_home()/hermes_root()")

    def test_no_proof_looks_for_chrome_by_its_linux_names_alone(self):
        """On Windows neither chrome.exe nor msedge.exe is on PATH, so a which("google-chrome") lookup skips the
        browser check there; crew_proof_board.find_chrome also tries the install paths."""
        offenders = []
        for path in sorted(glob.glob(os.path.join(SCRIPTS, "crew_*.py"))):
            name = os.path.basename(path)
            if name == "crew_proof_board.py":
                continue
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    if re.search(r"which\(\s*[\"'](google-chrome|chromium)", line):
                        offenders.append("%s:%d" % (name, n))
        self.assertEqual(offenders, [], "use crew_proof_board.find_chrome()")


if __name__ == "__main__":
    unittest.main()
