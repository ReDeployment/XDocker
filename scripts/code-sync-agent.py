#!/usr/bin/env python3
"""Read one bounded JSON request using a host-private destination policy."""
import argparse
import json
from pathlib import Path
import sys
import sqlite3

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from sync_agent import CopyAgent
from sync_runtime import SyncError

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--policy',required=True);args=parser.parse_args()
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:raise SyncError('XD_SYNC_AGENT_INPUT_LIMIT')
        result=CopyAgent(args.policy).handle(json.loads(raw));print(json.dumps({'result':result},ensure_ascii=False))
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        # Protocol responses deliberately contain no command output or credential paths.
        print(json.dumps({'error':str(error) if isinstance(error,SyncError) else 'XD_SYNC_AGENT_REQUEST_REJECTED'}));raise SystemExit(1)
