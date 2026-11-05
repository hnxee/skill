Please improve `wordtool.py`:

1. Add a `--lines` flag that also prints the number of lines.
2. If the input file does not exist, print `error: file not found: <path>` to stderr
   and exit with status 2 (right now it crashes with a traceback).
3. Add a `--json` flag that prints the result as JSON, e.g. `{"words": 12}`
   (and `"lines"` too when `--lines` is given).
4. Update the Usage section of README.md to document the new flags.

Don't break the existing behaviour.
