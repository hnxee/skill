import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

import wordtool


class TestWordtool(unittest.TestCase):
    def test_count_words(self):
        self.assertEqual(wordtool.count_words("a b  c\nd"), 4)

    def test_cli_default_output(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("one two three\n")
        self.addCleanup(os.unlink, fh.name)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(wordtool.main([fh.name]), 0)
        self.assertEqual(out.getvalue().strip(), "words: 3")
