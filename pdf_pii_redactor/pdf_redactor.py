#!/usr/bin/env python3
"""Entry point: python pdf_redactor.py --help"""
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 or newer is required")

from redactor.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
