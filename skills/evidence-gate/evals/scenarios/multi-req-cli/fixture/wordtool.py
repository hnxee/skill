#!/usr/bin/env python3
"""Count words in a text file."""

import argparse


def count_words(text: str) -> int:
    return len(text.split())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Count words in a file.")
    parser.add_argument("path")
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8") as fh:
        text = fh.read()
    print("words: {}".format(count_words(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
