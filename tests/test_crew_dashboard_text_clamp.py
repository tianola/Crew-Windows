#!/usr/bin/env python3
"""The dashboard's short lines must wrap instead of being cut, and stay bounded.

The panel subtitle carries the card's size sentence (`first run on this card; backend-engineer's runs
usually ~16m · read_file`), which is a sentence the owner has to be able to read; measured on a live card
page it needed two lines inside a 255 px column and an ellipsis ate the part that matters. Same finding for
the graph node's title, its status line, and the brief node's text. So the rule these tests pin is: no
`white-space:nowrap` on those four, and a line clamp so a pathological value cannot grow a card forever -
the rows stack on the measured height (card.js lays out `y += maxH + GY`), so bounded is what keeps the
graph tidy. Pure text assertions on the shipped stylesheet: no browser in the suite.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
CSS = os.path.join(SCRIPTS, "crew_dashboard", "crew.css")

SUBJECTS = {
    "panel subtitle": ".page-card #panel .ps",
    "node title": ".page-card .name",
    "node status line": ".page-card .desc",
    "brief text": ".page-card .bline",
}


def rules(css):
    """Every (selector, body) pair in the stylesheet, in order, single-line bodies joined."""
    joined = re.sub(r"\s*\n\s*", " ", css)
    return [(m.group(1).strip(), m.group(2)) for m in
            re.finditer(r"([^{}]+)\{([^{}]*)\}", joined)]


def body_for(selector):
    """The last rule that declares exactly this selector - what the browser would apply."""
    hits = [b for (sel, b) in RULES if sel == selector]
    return hits[-1] if hits else None


with open(CSS, encoding="utf-8") as fh:
    CSS_TEXT = fh.read()
RULES = rules(CSS_TEXT)


class ShortLinesWrap(unittest.TestCase):
    def test_the_stylesheet_is_readable(self):
        self.assertTrue(RULES, "no css rules parsed from %s" % CSS)

    def test_no_subject_line_is_forced_to_one_line(self):
        for what, sel in sorted(SUBJECTS.items()):
            body = body_for(sel)
            self.assertIsNotNone(body, "%s: rule %r missing" % (what, sel))
            self.assertNotIn("white-space:nowrap", body.replace(" ", ""),
                             "%s is still clipped to one line" % what)

    def test_every_wrapped_line_is_bounded(self):
        for what, sel in sorted(SUBJECTS.items()):
            body = body_for(sel).replace(" ", "")
            m = re.search(r"-webkit-line-clamp:(\d+)", body)
            self.assertIsNotNone(m, "%s wraps without a clamp, so a card can grow forever" % what)
            self.assertTrue(2 <= int(m.group(1)) <= 4,
                            "%s clamps at %s lines" % (what, m.group(1)))

    def test_a_clamped_line_still_overflows_hidden(self):
        for what, sel in sorted(SUBJECTS.items()):
            self.assertIn("overflow:hidden", body_for(sel).replace(" ", ""),
                          "%s clamps but never hides, so the extra line escapes the box" % what)

    def test_lines_that_can_carry_a_path_may_break_mid_token(self):
        # measured on a live brief node: the longest token was
        # C:/DATA/Flax_1.12/Binaries/Editor/Win64/Development/FlaxEngine.CSharp.dll, 340 px inside a 223 px box
        for sel in (".page-card .desc", ".page-card .bline"):
            self.assertIn("overflow-wrap:anywhere", body_for(sel).replace(" ", ""),
                          "%s wraps, but one long path still cannot break and silently clips" % sel)


if __name__ == "__main__":
    unittest.main()
