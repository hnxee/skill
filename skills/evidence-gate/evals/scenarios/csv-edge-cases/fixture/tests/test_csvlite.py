import unittest

from csvlite import parse, parse_line


class TestParseLine(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse_line("a,b,c"), ["a", "b", "c"])

    def test_newline_stripped(self):
        self.assertEqual(parse_line("1,2\n"), ["1", "2"])


class TestParse(unittest.TestCase):
    def test_rows(self):
        self.assertEqual(parse("a,b\nc,d\n"), [["a", "b"], ["c", "d"]])
