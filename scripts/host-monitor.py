#!/usr/bin/env python3
"""One bounded Linux sample; schedule with the supplied systemd timer."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'services/admin'))
from host_metrics import collect

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Private 0700 host metrics directory')
    args = parser.parse_args()
    try:
        import json
        print(json.dumps(collect(args.output)))
    except (OSError, ValueError):
        raise SystemExit('XD_METRICS_COLLECTION_FAILED: check Linux procfs and private directory permissions') from None
