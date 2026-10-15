#!/usr/bin/env python3
"""Manual read-only observation with local gh authentication; does not enable the server poller."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/admin'))
from github_watch import GitHubWatch, GitHubReader, PROJECTS


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',required=True,help='private local XDocker state directory; never copied into an image')
    parser.add_argument('--project',choices=['all',*PROJECTS],default='all')
    parser.add_argument('--gh',action='store_true',help='explicitly use local gh api GET and its existing credentials')
    parser.add_argument('--backfill',action='store_true',help='explicit bounded historical import; does not enable polling')
    args=parser.parse_args()
    watcher=GitHubWatch(args.state,reader_factory=(lambda s:GitHubReader(s['repository'],gh=True)) if args.gh else None)
    try:
        selected=watcher.sync(args.project,background=False,backfill=args.backfill)
        # Do not print private comments, raw bodies, credentials or the whole inbox.
        print(json.dumps({'projects':[{'project':p,**{k:watcher.store.settings(p)[k] for k in ('repository','status','success_at')}} for p in selected],
            'backfill':args.backfill,'auto_enabled':False,'github_writes':False},ensure_ascii=False))
        return 0 if all(watcher.store.settings(p)['status']=='collected' for p in selected) else 2
    except Exception:
        print(json.dumps({'error':'collection-unconfirmed','github_writes':False}));return 2


if __name__=='__main__':sys.exit(main())
