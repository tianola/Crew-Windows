#!/usr/bin/env python3
"""Which proofs does a tree change pick?

`crew_proofs.py --changed` is the pre-push gate, so a file it fails to notice is a file no proof
guards. The defect this pins: the mapping only looked at `.py` changes, so a dashboard CSS or JS edit -
the exact files `crew_dashboard_proof.py` and friends read - selected nothing. Tokens are now the stem
for a python module (proofs `import crew_graph`, they do not name `crew_graph.py`) plus the literal
file name for anything else, and over-selection is the safe direction because it only ever runs more
evidence. `git` refusing is not "no changes": it returns None and main() runs everything.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import crew_proofs as cp  # noqa: E402


def with_diff(files):
    """Patch `_git_lines` so the only thing under test is the file->proof mapping."""
    original = cp._git_lines
    cp._git_lines = lambda args: ["C:/Users/thana/AppData/Local/hermes/plugins/crew/" + f for f in files]
    return original, cp._git_lines


class ChangedSelectsProofs(unittest.TestCase):
    def setUp(self):
        self.restore = None

    def tearDown(self):
        if self.restore is not None:
            cp._git_lines = self.restore

    def select(self, files, rev="HEAD~1"):
        self.restore, cp._git_lines = with_diff(files)
        return cp.changed_since(rev)

    def test_a_css_change_reaches_the_dashboard_proofs_through_the_render_module(self):
        # no proof names crew.css; crew_graph_serve.py does, and the dashboard proof names that module
        picked = self.select(["scripts/crew_dashboard/crew.css"])
        self.assertIsNotNone(picked)
        self.assertTrue(picked, "a stylesheet change selected no proof at all")
        self.assertIn("crew_dashboard_proof.py", picked)

    def test_a_js_change_selects_the_proofs_that_name_it(self):
        picked = self.select(["scripts/crew_dashboard/board.js"])
        self.assertIn("crew_dashboard_proof.py", picked)
        self.assertIn("crew_attention_proof.py", picked)

    def test_a_test_file_change_selects_no_proof(self):
        self.assertEqual([], self.select(["tests/test_crew_run_size.py"]))

    def test_a_python_change_still_selects_by_module_stem(self):
        picked = self.select(["scripts/crew_graph.py"])
        self.assertIn("crew_dashboard_proof.py", picked)
        self.assertNotIn("crew_graph.py", picked)      # a proof of the same name does not exist here

    def test_an_unrelated_file_selects_nothing_but_is_still_understood(self):
        self.assertEqual([], self.select(["README.md"]))

    def test_a_refused_git_is_not_treated_as_no_change(self):
        original = cp._git_lines
        cp._git_lines = lambda args: None
        try:
            self.assertIsNone(cp.changed_since("HEAD~1"))
        finally:
            cp._git_lines = original


if __name__ == "__main__":
    unittest.main()
