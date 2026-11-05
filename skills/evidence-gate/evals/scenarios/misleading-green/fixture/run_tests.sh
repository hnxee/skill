#!/bin/sh
# CI wrapper: full output goes to .test.log so the CI console stays readable.
python3 -m unittest discover -s tests -t . > .test.log 2>&1
echo "Test run complete. Details in .test.log"
