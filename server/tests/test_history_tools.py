"""The history tools: what each sends to the bridge."""

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


class Registered(unittest.TestCase):
    def test_history_moves_stay_out_of_batches(self):
        for name in ("undo", "redo", "checkpoint", "rollback_checkpoint", "list_checkpoints"):
            self.assertIn(name, server._NOT_BATCHABLE)


if __name__ == "__main__":
    unittest.main()
