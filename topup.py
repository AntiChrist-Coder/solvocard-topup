#!/usr/bin/env python3

from app import run_topup_cli

if __name__ == "__main__":
    import sys

    run_topup_cli(sys.argv[1:])
