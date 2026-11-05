Users report that `parse_line` in `csvlite.py` breaks on quoted fields that contain
commas, e.g. `a,"b,c",d`. Please fix it.

Note: csvlite runs on a MicroPython target, so it must stay dependency-free
(do not use the `csv` module).
