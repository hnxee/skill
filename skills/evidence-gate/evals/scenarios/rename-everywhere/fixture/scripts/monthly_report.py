"""Print the monthly total. Run from the project root: python3 scripts/monthly_report.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pkg  # noqa: E402

ITEMS = [(2, 10.0), (1, 22.5)]

if __name__ == "__main__":
    print("Total: {:.2f}".format(pkg.calc_total(items=ITEMS)))
