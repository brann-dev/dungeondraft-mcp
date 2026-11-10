"""The history and co-pilot tools: what each sends to the bridge."""

import unittest
from unittest import mock

from dungeondraft_mcp import server


class Sent(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def request(cmd, **params):
            self.calls.append((cmd, params))
            return {}

        patch = mock.patch.object(server.bridge, "request", request)
        patch.start()
        self.addCleanup(patch.stop)

    def test_history_moves_leave_the_users_steps_alone_by_default(self):
        server.undo()
        server.redo()
        server.rollback_checkpoint("a")
        self.assertEqual(self.calls, [
            ("undo", {"include_user": False}),
            ("redo", {"include_user": False}),
            ("rollback_checkpoint", {"label": "a", "include_user": False}),
        ])

    def test_include_user_is_passed_on(self):
        server.undo(include_user=True)
        server.redo(include_user=True)
        server.rollback_checkpoint("a", include_user=True)
        self.assertEqual([params["include_user"] for _, params in self.calls], [True, True, True])

    def test_selection_and_cursor_are_plain_reads(self):
        server.get_selection()
        server.get_cursor()
        self.assertEqual(self.calls, [("get_selection", {}), ("get_cursor", {})])


class Registered(unittest.TestCase):
    def test_the_reads_are_tools_and_can_run_in_a_batch(self):
        for name in ("get_selection", "get_cursor"):
            self.assertIn(name, server._TOOLS)
            self.assertNotIn(name, server._NOT_BATCHABLE)

    def test_history_moves_stay_out_of_batches(self):
        for name in ("undo", "redo", "checkpoint", "rollback_checkpoint", "list_checkpoints"):
            self.assertIn(name, server._NOT_BATCHABLE)

    def test_what_clears_the_history_stays_out_of_batches(self):
        # Each of these wipes the batch's own undo group along with the history.
        for name in ("set_map_size", "crop_map", "delete_level"):
            self.assertIn(name, server._NOT_BATCHABLE)
        out = server.batch([{"tool": "delete_level", "args": {"index": 0}}])
        self.assertEqual(out["failed"], 1)
        self.assertIn("cannot be used inside a batch", out["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
