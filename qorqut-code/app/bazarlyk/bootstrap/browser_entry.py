"""Prepared browser command through the persistent helper installation."""
import argparse
from .browser import main

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    selected, arguments = parser.parse_known_args()
    raise SystemExit(main(selected.workspace, arguments))
