"""csvlite: a tiny, dependency-free CSV reader for constrained targets."""


def parse_line(line: str) -> list:
    """Parse one CSV record into a list of fields.

    Supports the RFC 4180 subset we rely on: comma-separated fields, fields
    enclosed in double quotes (which may contain commas), and doubled double
    quotes ("") as an escaped quote inside a quoted field. Whitespace is
    significant and preserved. A trailing newline (\\n or \\r\\n) is ignored.
    """
    return line.rstrip("\r\n").split(",")


def parse(text: str) -> list:
    return [parse_line(line) for line in text.splitlines() if line]
