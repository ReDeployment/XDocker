"""Local evidence triage. No network, code execution, repository changes or publishing."""
from datetime import datetime, timezone
import json
import re
import secrets
import time

from github_watch import BAD, PROJECTS, WatchError, digest, safe_text, sha, stamp

STATES = {'new', 'investigating', 'resolved', 'dismissed'}
CI = {'check', 'workflow', 'check_run', 'check_suite', 'workflow_run', 'workflow_job', 'status'}
REVIEWS = {'review', 'comment', 'review-comment', 'pull_request_review', 'pull_request_review_comment', 'issue_comment', 'commit_comment'}


def moment(value):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return date.timestamp() if date.tzinfo else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def number(value):
    return value if type(value) is int and 0 < value < 2**63 else None


def source(project, payload, at):
    base = 'https://github.com/' + PROJECTS[project]
    url = payload.get('url', '')
    url = url if isinstance(url,str) and (url == base or url.startswith(base+'/')) else base
    event = payload.get('event') or payload.get('kind', '')
    pr = number(payload.get('number')) or number(payload.get('pr'))
    match = re.search(r'/pull/(\d+)(?:[#/?]|$)', url)
    if not pr and match: pr = number(int(match[1]))
    key = payload.get('observation_key','')
    match = re.search(r':(\d+)$',key) if isinstance(key,str) else None
    identifier = number(payload.get('object_id')) or number(payload.get('run_id')) or number(payload.get('id')) or (number(int(match[1])) if match else None)
    title = safe_text(payload.get('title'),limit=500)
    name = title.split(' · ',2)[-1] if payload.get('source') == 'signed-webhook' else title
    return {'event':event,'title':title,'name':name,'url':url,'pr':pr,'sha':sha(payload.get('sha')),
            'body':safe_text(payload.get('body'),limit=8000),'actor':safe_text(payload.get('actor'),limit=100),
            'conclusion':safe_text(payload.get('conclusion') or payload.get('state'),limit=100),
            'status':safe_text(payload.get('status'),limit=100),'action':safe_text(payload.get('action'),limit=100),
            'path':safe_text(payload.get('path'),limit=300),'line':number(payload.get('line')),
            'object_id':identifier,'attempt':number(payload.get('attempt')) or 1,'at':at,
            'source':safe_text(payload.get('source'),limit=100)}


class GitHubTriage:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS github_issue_workflow(
                issue_key TEXT PRIMARY KEY, state TEXT NOT NULL, note TEXT NOT NULL,
                evidence_digest TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS github_diagnostics(
                id TEXT PRIMARY KEY, issue_key TEXT NOT NULL, project TEXT NOT NULL,
                at TEXT NOT NULL, evidence_digest TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS github_issue_archive(issue_key TEXT PRIMARY KEY,payload TEXT NOT NULL);''')

    def read(self, db):
        notifications = db.execute('SELECT project,at,payload FROM notifications ORDER BY id DESC LIMIT 2000').fetchall()
        projects = {r['id']:dict(r) for r in db.execute('SELECT * FROM projects')}
        workflows = {r['issue_key']:dict(r) for r in db.execute('SELECT * FROM github_issue_workflow')}
        groups = {}; releases = {}
        for row in reversed(notifications):
            project = row['project']
            if project not in PROJECTS: continue
            data = source(project,json.loads(row['payload']),row['at'])
            event = data['event']
            if event == 'release':
                key = project+':'+str(data['object_id'] or data['url'])
                old = releases.get(key)
                if not old or moment(data['at']) >= moment(old['at']): releases[key] = dict(data,project=project)
                continue
            category = 'ci' if event in CI else 'review' if event in REVIEWS else None
            if not category or (category=='review' and not data['body'] and data['conclusion'].lower()!='changes_requested'): continue
            target = ('pr:'+str(data['pr'])) if category=='review' and data['pr'] else 'sha:'+str(data['sha']) if data['sha'] else 'unbound'
            identifier = digest([project,category,target])[:32]
            group = groups.setdefault(identifier,{'id':identifier,'project':project,'category':category,'target':target,'rows':[]})
            group['rows'].append(data)
        # A first polling baseline does not create historical notifications, but its release snapshot remains visible.
        for project, config in projects.items():
            snapshot=json.loads(config['snapshot']) if config['snapshot'] else {}
            for payload in snapshot.get('releases',[]):
                item=source(project,dict(payload,event='release',source='api-snapshot'),payload.get('published_at') or snapshot.get('checked_at'))
                key=project+':'+str(item['object_id'] or item['url'])
                if key not in releases:releases[key]=dict(item,project=project)
        issues = []
        for group in groups.values():
            rows = group.pop('rows'); category=group['category']; project=group['project']
            # Explicit run/check identity and attempt dominate delivery order. API and hook observations share families.
            latest = {}
            families={'check':'check_run','workflow':'workflow_run'}
            for item in rows:
                identity=(families.get(item['event'],item['event']),item['name'])
                old=latest.get(identity)
                order=lambda r:(r['object_id'] or 0,r['attempt'],r['status']=='completed' or bool(r['conclusion']),moment(r['at']))
                if not old or order(item)>=order(old):latest[identity]=item
            bad=[r for r in latest.values() if r['conclusion'].lower() in BAD]
            if category=='ci' and not any(r['conclusion'].lower() in BAD for r in rows):continue
            # Deduplicate identical comment evidence across polling, explicit backfill and signed delivery.
            unique={digest([r['event'],r['object_id'],r['url'],r['body'],r['conclusion'],r['sha']]):r for r in rows}
            evidence=sorted(unique.values(),key=lambda r:moment(r['at']),reverse=True)
            evidence_hash=digest([{k:r[k] for k in ('event','object_id','body','conclusion','sha','status','attempt')} for r in evidence])
            workflow=workflows.get(group['id'],{})
            changed=bool(workflow and workflow['evidence_digest']!=evidence_hash)
            state=workflow.get('state','new')
            if changed and state in ('resolved','dismissed'):state='new'
            row=projects[project]; snapshot=json.loads(row['snapshot']) if row['snapshot'] else {}
            fresh=row['status']=='collected' and 0<=time.time()-moment(row['success'])<=600
            tracked={h.get('sha') for h in snapshot.get('heads',[])} | {p.get('sha') for p in snapshot.get('pulls',[]) if p.get('state')=='open'}
            issue_sha=next((r['sha'] for r in evidence if r['sha']),None)
            binding='current-observed' if fresh and issue_sha in tracked else 'historical' if fresh and issue_sha else 'unconfirmed'
            current_ci=next((c for c in snapshot.get('ci',[]) if c['sha']==issue_sha),None) if fresh else None
            ci_state=current_ci.get('state') if current_ci else 'unknown'
            requested=fresh and any(p.get('number')==next((r['pr'] for r in evidence if r['pr']),None) and p.get('sha')==issue_sha and p.get('review')=='changes-requested' for p in snapshot.get('pulls',[]))
            recovered=category=='ci' and ((not bad and all(r['conclusion']=='success' for r in latest.values())) or (current_ci and ci_state=='observed-pass'))
            group.update(sha=issue_sha,pr=next((r['pr'] for r in evidence if r['pr']),None),
                title='CI 失败待处理' if category=='ci' and not recovered else '检查已恢复，待确认' if category=='ci' else '审查意见待核对',
                evidence_state='recovered-observed' if recovered else 'failed-observed' if category=='ci' else 'review-unconfirmed',
                severity='failure' if category=='ci' and not recovered else 'review',
                publish_signal='blocked' if binding=='current-observed' and ((category=='ci' and ci_state=='blocked') or requested) else 'needs-verification',
                head_binding=binding,current_ci=ci_state,evidence=evidence[:20],evidence_count=len(evidence),
                evidence_digest=evidence_hash,latest_at=evidence[0]['at'],workflow_state=state,
                note=workflow.get('note',''),updated_at=workflow.get('updated_at'),evidence_changed=changed,
                version=digest([evidence_hash,workflow.get('revision',0),binding,ci_state]))
            issues.append(group)
        live={i['id'] for i in issues}
        for saved in db.execute('SELECT * FROM github_issue_archive'):
            if saved['issue_key'] in live:continue
            item=json.loads(saved['payload']);workflow=workflows.get(item['id'],{})
            item.update(workflow_state=workflow.get('state','new'),note=workflow.get('note',''),updated_at=workflow.get('updated_at'),
                head_binding='unconfirmed',current_ci='unknown',publish_signal='needs-verification',archived=True,
                version=digest([item['evidence_digest'],workflow.get('revision',0),'unconfirmed','unknown']))
            issues.append(item)
        issues.sort(key=lambda r:moment(r['latest_at']),reverse=True)
        reports=[]
        for row in db.execute('SELECT * FROM github_diagnostics ORDER BY at DESC LIMIT 50'):
            report=json.loads(row['payload']);current=next((i for i in issues if i['id']==row['issue_key']),None)
            reports.append(dict(report,stale=not current or current['evidence_digest']!=row['evidence_digest']))
        return {'issues':issues,'releases':sorted(releases.values(),key=lambda r:moment(r['at']),reverse=True)[:100],
                'diagnostics':reports,'window':2000,'pending':sum(i['workflow_state'] in ('new','investigating') for i in issues),
                'github_writes':False,'code_execution':False,'publish_authorized':False}

    def listing(self):
        with self.store.db() as db:return self.read(db)

    def selected(self,db,identifier,version,confirmation):
        if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{32}',identifier):raise WatchError('invalid-issue')
        item=next((i for i in self.read(db)['issues'] if i['id']==identifier),None)
        if not item or confirmation!=item['project']:raise WatchError('issue-target-unconfirmed')
        if version!=item['version']:raise WatchError('issue-evidence-changed')
        return item

    def update(self,identifier,version,state,note,confirmation):
        if state not in STATES or not isinstance(note,str) or not 8<=len(note.strip())<=2000:raise WatchError('issue-note-required')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE');item=self.selected(db,identifier,version,confirmation)
            old=db.execute('SELECT revision FROM github_issue_workflow WHERE issue_key=?',(identifier,)).fetchone()
            if not old and db.execute('SELECT count(*) FROM github_issue_workflow').fetchone()[0]>=5000:raise WatchError('issue-history-full')
            db.execute('INSERT OR REPLACE INTO github_issue_archive VALUES(?,?)',(identifier,json.dumps(item,ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO github_issue_workflow VALUES(?,?,?,?,?,?)',
                (identifier,state,safe_text(note,limit=2000),item['evidence_digest'],stamp(),(old['revision'] if old else 0)+1))
        return {'saved':True,'github_writes':False,'publish_authorized':False}

    def diagnose(self,identifier,version,confirmation):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE');item=self.selected(db,identifier,version,confirmation)
            if db.execute('SELECT count(*) FROM github_diagnostics').fetchone()[0]>=500:raise WatchError('diagnostic-history-full')
            snapshot=self.store.settings(item['project'])['snapshot'] or {}
            failures=[j for j in snapshot.get('failed_jobs',[]) if j.get('sha')==item['sha']]
            annotations=[]
            for c in snapshot.get('ci',[]):
                if c.get('sha')!=item['sha']:continue
                for check in c.get('checks',[]):annotations.extend(check.get('annotations',[]))
            locations=[{'path':r['path'],'line':r['line']} for r in item['evidence'] if r['path']]
            locations.extend({'path':safe_text(a.get('path'),limit=300),'line':number(a.get('line')),'message':safe_text(a.get('message'),limit=1000)} for a in annotations[:30])
            report={'id':secrets.token_hex(16),'issue':identifier,'project':item['project'],'sha':item['sha'],'pr':item['pr'],
                'at':stamp(),'evidence_digest':item['evidence_digest'],'head_binding':item['head_binding'],
                'summary':item['title'],'root_cause':'尚未确认代码根因；以下是 GitHub 事件和采集快照提供的证据。',
                'failed_jobs':failures[:30],'locations':locations[:30],'evidence':item['evidence'],
                'next_steps':['核对当前 PR／分支 SHA；未绑定评论和过期快照不能作为当前提交结论。',
                    '在来源链接中核对失败步骤、代码注解或机器人评论，并在对应 SHA 检查相关文件。',
                    '把问题、文件位置和验收要求交给对应项目负责人；本报告不会修改代码或重跑 CI。',
                    '修复后重新采集当前提交检查，再人工确认此问题；已读或已解决标记不授权镜像发布。'],
                'method':'stored-evidence-only','network_requests':0,'code_execution':False,'github_writes':False,'publish_authorized':False}
            db.execute('INSERT OR REPLACE INTO github_issue_archive VALUES(?,?)',(identifier,json.dumps(item,ensure_ascii=False)))
            db.execute('INSERT INTO github_diagnostics VALUES(?,?,?,?,?,?)',
                (report['id'],identifier,item['project'],report['at'],item['evidence_digest'],json.dumps(report,ensure_ascii=False)))
        return report
