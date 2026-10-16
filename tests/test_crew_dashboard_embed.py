"""The Hermes Desktop embed routes (dashboard/plugin_api.py /embed/*): JSON envelopes around the real pages.

Built in-process from crew's own builders on a real temp kanban board; no daemon and no port. The existing routes must
answer exactly as before: the tab's pages keep their <base> and tab bridge.
"""
import base64
import html
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
CREW_ROOT = os.path.dirname(HERE)
for p in (HERE, CREW_ROOT, os.path.join(CREW_ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests import kernel_board as kbd  # noqa: E402
from dashboard import plugin_api  # noqa: E402
from dashboard.plugin_api import router  # noqa: E402
from test_crew_dashboard_customization import RealBoardCase  # noqa: E402

API = "/api/plugins/crew"


def _client():
    app = FastAPI()
    app.include_router(router, prefix=API)
    return TestClient(app)


class EmbedRouteTests(RealBoardCase):
    def setUp(self):
        self.client = _client()

    def test_embed_board_envelope_has_no_base_and_a_nonced_bridge(self):
        resp = self.client.get(API + "/embed/board")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.headers["content-type"].startswith("application/json"))
        env = resp.json()
        self.assertEqual(set(env), {"html", "nonce"})
        page, nonce = env["html"], env["nonce"]
        self.assertTrue(nonce)
        self.assertNotRegex(page, r"<base\b")
        self.assertIn('<script id="crew-desktop-bridge" nonce="%s">' % nonce, page)
        # the page's own scripts carry the same nonce, so the meta policy lets them and the bridge run
        policy = html.unescape(re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', page).group(1))
        self.assertIn("'nonce-%s'" % nonce, policy)
        self.assertNotIn("frame-ancestors", policy)
        self.assertNotIn("'self'", re.search(r"script-src[^;]*", policy).group(0))
        # the tab bridge and the standalone-board link are not in the embed
        self.assertNotIn("crew-parent-bridge", page)
        self.assertNotIn("class=tailnet", page)

    def test_embed_board_is_the_standalone_board(self):
        import crew_graph_serve
        env = self.client.get(API + "/embed/board").json()
        # anchor: lib.js faceUrl is rewritten for the bridge, everything else of board.js is the served script
        self.assertIn("window.__crewFace", env["html"])
        self.assertIn('fetch("/board.json"', env["html"])
        standalone = crew_graph_serve.board_page(False, env["nonce"])
        body = re.sub(r'<script id="crew-desktop-bridge".*?</script>', "", env["html"], flags=re.S)
        body = re.sub(r"<meta http-equiv=.*?>", "", body)
        # the same page once the three embed edits are undone
        self.assertEqual(len(body.split("<script")), len(standalone.split("<script")))

    def test_bridge_is_inert_outside_a_frame_and_only_in_embed_routes(self):
        self.assertIn("(function () {\n  if (window.parent === window) return;", plugin_api._EMBED_BRIDGE_JS)
        import crew_graph_serve
        self.assertNotIn("crew-desktop-bridge", crew_graph_serve.board_page(False, "n"))
        self.assertNotIn("__crewOpenChat = ", crew_graph_serve.board_page(False, "n"))   # standalone: no bridge, no button host
        tab = self.client.get(API + "/board")
        self.assertNotIn("crew-desktop-bridge", tab.text)
        self.assertIn('<base href="/api/plugins/crew/">', tab.text)   # the tab proxy is unchanged

    def test_embed_card_page_for_a_real_card(self):
        resp = self.client.get(API + "/embed/card/" + self.card)
        self.assertEqual(resp.status_code, 200, resp.text)
        page = resp.json()["html"]
        self.assertIn('id="crew-desktop-bridge"', page)
        self.assertNotRegex(page, r"<base\b")
        self.assertIn('window.__crewNav ? window.__crewNav("board")', page)   # the "o" shortcut goes through the bridge
        self.assertNotIn('window.location.href = "/";', page)
        self.assertIn("window.__crewOpenChat = function (profile, session)", page)
        self.assertIn("cardCoOpen", page)

    def test_embed_card_id_validation(self):
        for bad in ("t_1.json", "t_1%2Fx", "..", "t_1.html"):
            self.assertIn(self.client.get(API + "/embed/card/" + bad).status_code, (400, 404), bad)
        # a well-formed id reaches the real card page: upstream answers for the card (no graph), not the proxy
        resp = self.client.get(API + "/embed/card/t_nosuchcard")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("no graph for t_nosuchcard", resp.text)   # the builder's own answer, passed through unwrapped

    def test_embed_avatar_data_url_and_validation(self):
        resp = self.client.get(API + "/embed/avatar/role/worker.svg")
        self.assertEqual(resp.status_code, 200, resp.text)
        url = resp.json()["data_url"]
        prefix = "data:image/svg+xml;base64,"
        self.assertTrue(url.startswith(prefix))
        self.assertIn(b"<svg", base64.b64decode(url[len(prefix):]))
        for bad in ("../x.svg", "role/..%2F..%2Fx.svg", "role/worker.exe", "role/a b.svg"):
            self.assertIn(self.client.get(API + "/embed/avatar/" + bad).status_code, (400, 404), bad)

    def test_embed_ack_applies_server_side_and_wraps_the_answer(self):
        hdr = {"Origin": "http://127.0.0.1:9119"}
        resp = self.client.post(API + "/embed/ack/all?undo=1", headers=hdr)
        self.assertEqual(resp.status_code, 200)
        env = resp.json()
        self.assertEqual((env["ok"], env["status"]), (True, 200))
        self.assertIn("restored", env["text"])
        cleared = self.client.post(API + "/embed/ack/" + self.blocked, headers=hdr).json()
        self.assertEqual((cleared["ok"], cleared["status"], cleared["text"]), (True, 200, "cleared %s\n" % self.blocked))
        self.assertEqual(self.client.post(API + "/embed/ack/%s?undo=1" % self.blocked, headers=hdr).json()["status"], 200)
        busy = self.client.post(API + "/embed/ack/" + self.card, headers=hdr).json()   # ready card: 409 inside a 200
        self.assertEqual((busy["ok"], busy["status"]), (False, 409))
        # an upstream refusal is an answer inside a 200, not a transport error
        miss = self.client.post(API + "/embed/ack/t_nosuchcard", headers=hdr).json()
        self.assertEqual((miss["ok"], miss["status"]), (False, 404))
        self.assertIn("no card", miss["text"])
        self.assertEqual(self.client.post(API + "/embed/ack/a%2Fb").status_code, 404)
        self.assertEqual(self.client.get(API + "/embed/ack/all").status_code, 405)

    def test_existing_ack_route_still_plain_text(self):
        resp = self.client.post(API + "/ack/all?undo=1", headers={"Origin": "http://127.0.0.1:9119"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("restored", resp.text)
        self.assertTrue(resp.headers["content-type"].startswith("text/plain"))


class EmbedFailureTests(RealBoardCase):
    def test_a_failing_builder_is_a_500_not_a_hang_and_ack_keeps_its_envelope(self):
        import crew_graph_serve
        client = _client()
        with patch.object(crew_graph_serve, "board_page", side_effect=OSError("disk gone")), \
                patch.object(crew_graph_serve, "ack_all", side_effect=OSError("disk gone")):
            with self.assertLogs(plugin_api._log, "ERROR"):
                resp = client.get(API + "/embed/board")
                ack = client.post(API + "/embed/ack/all")
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("disk gone", resp.text)
        self.assertEqual((ack.status_code, ack.json()["status"], ack.json()["ok"]), (200, 500, False))


if __name__ == "__main__":
    unittest.main()
