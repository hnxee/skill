import unittest

import notify


class TestPayload(unittest.TestCase):
    def test_channel_default(self):
        self.assertEqual(notify.build_payload("hi")["channel"], "general")
