"""Push existing commits and fast-forward copies; source worktrees are read-only."""
from contextlib import contextmanager
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time

from host_metrics import private_directory
from sync_scan import content_findings,safe_label,VERSION as SCAN_VERSION

SHA=re.compile(r'^[a-f0-9]{40}$')
KEY=re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
REPOSITORY=re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
MAX_FILES=10000
MAX_BYTES=64*1024*1024
MAX_FILE=8*1024*1024
DENY_PARTS={'.git','.ssh','.venv','venv','node_modules','__pycache__','.cache','.nuxt','.output'}


class SyncError(ValueError):pass


def require(ok,code):
    if not ok:raise SyncError(code)


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def identifier(value):require(isinstance(value,str) and KEY.fullmatch(value),'XD_SYNC_ID_INVALID')


def repository(value):
    require(isinstance(value,str) and REPOSITORY.fullmatch(value) and not value.endswith('.git'),'XD_SYNC_REPOSITORY_INVALID')
    return value


def canonical(value):
    for prefix in ('https://github.com/','git@github.com:','ssh://git@github.com/'):
        if value.startswith(prefix):return repository(value[len(prefix):].removesuffix('.git'))
    raise SyncError('XD_SYNC_GITHUB_REMOTE_REQUIRED')


def branch_name(value):
    require(isinstance(value,str) and 1<=len(value)<=200 and not value.startswith(('HEAD@','-')) and not any(c.isspace() or ord(c)<32 for c in value),'XD_SYNC_SOURCE_BRANCH_REQUIRED')
    require(run(['git','check-ref-format','--branch',value],allowed=(0,128),limit=4096)[0]==0,'XD_SYNC_BRANCH_INVALID')
    return value


def safe_path(value,roots):
    require(isinstance(value,str) and value.startswith('/') and not any(c in value for c in '\n\r\0') and '..' not in Path(value).parts,'XD_SYNC_PATH_INVALID')
    p=Path(value)
    require(not any(x.is_symlink() for x in (p,*p.parents)),'XD_SYNC_SYMLINK_DENIED')
    require(any(p!=Path(root) and p.is_relative_to(Path(root)) for root in roots),'XD_SYNC_PATH_NOT_GRANTED')
    return p


def run(command,*,env=None,input=None,timeout=60,limit=4*1024*1024,allowed=(0,)):
    environment={k:os.environ[k] for k in ('PATH','HOME','USER','LOGNAME','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
    environment.update({'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1','GIT_OPTIONAL_LOCKS':'0','LC_ALL':'C','GIT_LFS_SKIP_SMUDGE':'1','GIT_NO_REPLACE_OBJECTS':'1'})
    environment.update(env or {})
    with tempfile.TemporaryFile() as incoming:
        if input is not None:incoming.write(input);incoming.seek(0)
        child=subprocess.Popen(command,stdin=incoming,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment,start_new_session=True)
        output=bytearray();deadline=time.monotonic()+timeout;poll=selectors.DefaultSelector();poll.register(child.stdout,selectors.EVENT_READ,'stdout');poll.register(child.stderr,selectors.EVENT_READ,'stderr');error_size=0
        try:
            while poll.get_map():
                require(time.monotonic()<deadline,'XD_SYNC_COMMAND_TIMEOUT')
                for key,_ in poll.select(.1):
                    block=os.read(key.fd,65536)
                    if not block:poll.unregister(key.fileobj);continue
                    if key.data=='stdout':output.extend(block);require(len(output)<=limit,'XD_SYNC_OUTPUT_LIMIT')
                    else:error_size+=len(block);require(error_size<=65536,'XD_SYNC_OUTPUT_LIMIT')
            try:child.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:raise SyncError('XD_SYNC_COMMAND_TIMEOUT') from None
            require(child.returncode in allowed,'XD_SYNC_GIT_REJECTED' if command[0]=='git' else 'XD_SYNC_TRANSPORT_REJECTED')
            return child.returncode,bytes(output)
        finally:
            poll.close();child.stdout.close();child.stderr.close()
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)


def git(directory,*arguments,env=None,input=None,allowed=(0,),timeout=60,limit=4*1024*1024):
    return run(['git','--no-replace-objects','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false','-c','commit.gpgsign=false',
                '-c','credential.helper=','-c','gc.auto=0','-C',str(directory),*arguments],env=env,input=input,allowed=allowed,timeout=timeout,limit=limit)


def text_git(directory,*args,**kwargs):return git(directory,*args,**kwargs)[1].decode('utf-8','strict').strip()


def source_branch(root):
    code,raw=git(root,'symbolic-ref','--quiet','--short','HEAD',allowed=(0,1))
    if code==0:return raw.decode('utf-8','strict').strip()
    sha=text_git(root,'rev-parse','HEAD');require(SHA.fullmatch(sha),'XD_SYNC_SOURCE_HEAD_INVALID')
    return 'HEAD@'+sha


MAX_COMMITS=500
MAX_PACK=256*1024*1024


class ScanRejected(SyncError):
    def __init__(self,summary):super().__init__('XD_SYNC_SENSITIVE_CONTENT');self.summary=summary


def inspect_source(profile,roots):
    root=safe_path(profile['directory'],roots);require(root.is_dir(),'XD_SYNC_SOURCE_MISSING')
    require(Path(text_git(root,'rev-parse','--show-toplevel')).resolve()==root,'XD_SYNC_REPOSITORY_ROOT_REQUIRED')
    branch=source_branch(root);require(branch==profile['branch'],'XD_SYNC_SOURCE_BRANCH_CHANGED')
    graft=Path(text_git(root,'rev-parse','--git-path','info/grafts'));graft=graft if graft.is_absolute() else root/graft
    require(not graft.exists(),'XD_SYNC_GRAFTED_SOURCE_UNSUPPORTED')
    head=text_git(root,'rev-parse','HEAD');require(SHA.fullmatch(head),'XD_SYNC_SOURCE_HEAD_INVALID')
    require(canonical(text_git(root,'remote','get-url','origin')).lower()==profile['repository'].lower(),'XD_SYNC_SOURCE_REMOTE_MISMATCH')
    pending={}
    for name,args in [('modified',('diff','--name-only','-z')),('staged',('diff','--cached','--name-only','-z')),('untracked',('ls-files','--others','--exclude-standard','-z'))]:
        paths=git(root,*args)[1].decode('utf-8','strict').split('\0');paths=[v for v in paths if v]
        pending[name]={'count':len(paths),'paths':[safe_label(v) for v in paths[:20]]}
    return {'mode':'committed-only','head':head,'branch':branch,'pending':pending,'pushable':not branch.startswith('HEAD@'),
            'shallow':text_git(root,'rev-parse','--is-shallow-repository')=='true'}


def scan_commits(directory,commits,context,base=None,scope='outgoing-history'):
    require(len(commits)<=MAX_COMMITS,'XD_SYNC_SCAN_COMMIT_LIMIT')
    summary={**context,'scope':scope,'remote_head':base,'commit_count':len(commits),'commits':[],
             'scan_version':SCAN_VERSION,'files':0,'bytes':0,'findings':[],'findings_count':0,'blocked':False}
    seen=set();paths=set();findings=[]
    for sha in commits:
        require(SHA.fullmatch(sha),'XD_SYNC_SHA_INVALID')
        message=git(directory,'show','-s','--format=%B',sha,limit=65536)[1]
        issues=content_findings('<commit-metadata>',git(directory,'cat-file','commit',sha,limit=65536)[1])
        findings.extend({'commit':sha,'path':'<commit-metadata>',**issue} for issue in issues)
        title='提交说明含敏感信息，已隐藏' if issues else message.decode('utf-8','replace').split('\n')[0][:200]
        if len(summary['commits'])<30:summary['commits'].append({'sha':sha,'title':title})
        raw=git(directory,'diff-tree','--no-commit-id','--root','-r','-m','--no-renames','--raw','-z',sha)[1].split(b'\0')
        index=0
        while index<len(raw) and raw[index]:
            fields=raw[index].decode('ascii').split();require(len(fields)==5 and fields[0].startswith(':'),'XD_SYNC_SCAN_INVALID_TREE')
            path=raw[index+1].decode('utf-8','strict');index+=2
            mode,oid=fields[1],fields[3]
            if mode=='000000':continue
            require(SHA.fullmatch(oid) and len(path)<=1024,'XD_SYNC_SCAN_INVALID_TREE')
            paths.add(path);require(len(paths)<=MAX_FILES,'XD_SYNC_SCAN_FILE_LIMIT')
            if mode!='100644' and mode!='100755':
                findings.append({'commit':sha,'path':safe_label(path),'rule':'symlink-or-submodule','line':None});continue
            if (path,oid) in seen:continue
            seen.add((path,oid));require(len(seen)<=MAX_FILES,'XD_SYNC_SCAN_FILE_LIMIT')
            size=int(text_git(directory,'cat-file','-s',oid));require(size<=MAX_FILE,'XD_SYNC_SCAN_FILE_LIMIT')
            summary['bytes']+=size;require(summary['bytes']<=MAX_BYTES,'XD_SYNC_SCAN_BYTE_LIMIT')
            body=git(directory,'cat-file','blob',oid,limit=MAX_FILE+1024)[1]
            findings.extend({'commit':sha,'path':safe_label(path),**issue} for issue in content_findings(path,body))
    summary.update(files=len(paths),findings=findings[:30],findings_count=len(findings),blocked=bool(findings))
    return summary


def scan_source(profile,roots):
    context=inspect_source(profile,roots)
    result=scan_commits(profile['directory'],[context['head']],context,scope='local-head-only')
    require(inspect_source(profile,roots)['head']==context['head'],'XD_SYNC_SOURCE_CHANGED')
    return result


@contextmanager
def github_auth(root,token):
    require(isinstance(token,str) and 16<=len(token)<=2048 and re.fullmatch(r'[A-Za-z0-9_]+',token),'XD_SYNC_CREDENTIAL_REQUIRED')
    directory=private_directory(Path(tempfile.mkdtemp(prefix='auth-',dir=root)))
    try:
        file=directory/'token';file.write_text(token);file.chmod(0o600)
        helper=directory/'askpass';helper.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" x-access-token;; *) cat "$XD_SYNC_TOKEN_FILE";; esac\n');helper.chmod(0o700)
        yield {'GIT_ASKPASS':str(helper),'GIT_ASKPASS_REQUIRE':'force','XD_SYNC_TOKEN_FILE':str(file)}
    finally:shutil.rmtree(directory)


class GitTransfer:
    def __init__(self,root,remote_resolver=None):
        self.root=private_directory(root);self.remotes=remote_resolver or (lambda value:'https://github.com/'+repository(value)+'.git')

    def remote_head(self,directory,profile,env):
        branch_name(profile['branch'])
        code,raw=git(directory,'ls-remote','--exit-code',self.remotes(profile['repository']),'refs/heads/'+profile['branch'],env=env,allowed=(0,2))
        if code==2:return None
        rows=raw.decode().strip().splitlines();require(len(rows)==1,'XD_SYNC_REMOTE_HEAD_INVALID')
        sha,ref=rows[0].split();require(SHA.fullmatch(sha) and ref=='refs/heads/'+profile['branch'],'XD_SYNC_REMOTE_HEAD_INVALID');return sha

    def prepare(self,profile,roots,env):
        context=inspect_source(profile,roots);branch_name(context['branch'])
        require(not context['shallow'],'XD_SYNC_SHALLOW_SOURCE_UNSUPPORTED')
        source=Path(profile['directory']);cache=self.root/(profile['id']+'-commits-v2.git')
        require(not cache.is_symlink(),'XD_SYNC_SYMLINK_DENIED')
        if not cache.exists():git(self.root,'init','--bare',str(cache))
        require(sum(p.stat().st_size for p in cache.rglob('*') if p.is_file())<=512*1024*1024,'XD_SYNC_CACHE_CAPACITY')
        parent=self.remote_head(cache,profile,env);head=context['head']
        if parent:
            git(cache,'fetch','--no-tags','--no-recurse-submodules',self.remotes(profile['repository']),'refs/heads/'+profile['branch'],env=env)
            require(text_git(cache,'rev-parse','FETCH_HEAD')==parent,'XD_SYNC_REMOTE_MOVED')
        if parent!=head:
            present=bool(parent and git(source,'cat-file','-e',parent+'^{commit}',allowed=(0,1,128))[0]==0)
            if present:require(git(source,'merge-base','--is-ancestor',parent,head,allowed=(0,1))[0]==0,'XD_SYNC_REMOTE_DIVERGED')
            revisions=head+'\n'+('^'+parent+'\n' if present else '')
            # Transfer only Git objects into a private bare cache. No source index/ref writes,
            # and source-local URL rewrites or credential helpers cannot affect the push.
            outgoing=text_git(source,'rev-list','--max-count='+str(MAX_COMMITS+1),head,*(['^'+parent] if present else [])).splitlines()
            require(len(outgoing)<=MAX_COMMITS,'XD_SYNC_SCAN_COMMIT_LIMIT')
            packed=git(source,'pack-objects','--stdout','--revs','--quiet',input=revisions.encode(),limit=MAX_PACK,timeout=120)[1]
            git(cache,'index-pack','--stdin',input=packed,timeout=120)
            if parent:require(git(cache,'merge-base','--is-ancestor',parent,head,allowed=(0,1))[0]==0,'XD_SYNC_REMOTE_DIVERGED')
        commits=text_git(cache,'rev-list','--reverse','--topo-order','--max-count='+str(MAX_COMMITS+1),head,*(['^'+parent] if parent else [])).splitlines()
        summary=scan_commits(cache,commits,context,parent)
        latest=inspect_source(profile,roots);require(latest['head']==head and latest['branch']==context['branch'],'XD_SYNC_SOURCE_CHANGED')
        return cache,summary

    def preview(self,profile,roots,env):return self.prepare(profile,roots,env)[1]

    def publish(self,profile,roots,env,before_push):
        cache,summary=self.prepare(profile,roots,env);sha=summary['head']
        if summary['blocked']:raise ScanRejected(summary)
        changed=summary['remote_head']!=sha
        if changed:
            require(self.remote_head(cache,profile,env)==summary['remote_head'],'XD_SYNC_REMOTE_MOVED')
            before_push(sha,summary)
            require(inspect_source(profile,roots)['head']==sha,'XD_SYNC_SOURCE_CHANGED')
            try:git(cache,'push',self.remotes(profile['repository']),sha+':refs/heads/'+profile['branch'],env=env)
            except SyncError:
                try:confirmed=self.remote_head(cache,profile,env)==sha
                except SyncError:confirmed=False
                require(confirmed,'XD_SYNC_UPLOAD_UNCONFIRMED')
        require(self.remote_head(cache,profile,env)==sha,'XD_SYNC_UPLOAD_UNCONFIRMED')
        git(cache,'update-ref','refs/heads/'+profile['branch'],sha)
        return {'sha':sha,'changed':changed,'snapshot':summary}

    def verify(self,profile,sha,env):return self.remote_head(self.root,profile,env)==sha


def inspect_copy(grant,roots):
    path=safe_path(grant['directory'],roots)
    if not path.exists():return {'exists':False,'head':None,'clean':True,'branch':grant['branch']}
    require(path.is_dir(),'XD_SYNC_TARGET_NOT_DIRECTORY')
    if not any(path.iterdir()):return {'exists':False,'head':None,'clean':True,'branch':grant['branch']}
    require(Path(text_git(path,'rev-parse','--show-toplevel')).resolve()==path,'XD_SYNC_TARGET_ROOT_MISMATCH')
    require(canonical(text_git(path,'remote','get-url','origin')).lower()==grant['repository'].lower(),'XD_SYNC_TARGET_REMOTE_MISMATCH')
    branch=text_git(path,'symbolic-ref','--short','HEAD');require(branch==grant['branch'],'XD_SYNC_TARGET_BRANCH_MISMATCH')
    return {'exists':True,'head':text_git(path,'rev-parse','HEAD'),'clean':not bool(git(path,'status','--porcelain','--untracked-files=normal')[1]),'branch':branch}


def pull_copy(grant,roots,sha,observation,env,remote_resolver=None):
    require(SHA.fullmatch(sha or ''),'XD_SYNC_SHA_INVALID')
    before=inspect_copy(grant,roots);require(digest(before)==observation,'XD_SYNC_TARGET_CHANGED');require(before['clean'],'XD_SYNC_TARGET_DIRTY')
    path=safe_path(grant['directory'],roots);remote=(remote_resolver or (lambda r:'https://github.com/'+repository(r)+'.git'))(grant['repository'])
    updated=before['head']!=sha
    if not before['exists']:
        require(path.parent.is_dir(),'XD_SYNC_TARGET_PARENT_MISSING')
        with tempfile.TemporaryDirectory(prefix='.xdocker-copy-',dir=path.parent) as temp:
            staged=Path(temp)/'repository'
            git(path.parent,'clone','--single-branch','--no-tags','--branch',grant['branch'],remote,str(staged),env=env)
            require(text_git(staged,'rev-parse','HEAD')==sha,'XD_SYNC_REMOTE_MOVED')
            # Pin the canonical origin even when a fixture resolver supplied a local transport.
            git(staged,'remote','set-url','origin','https://github.com/'+grant['repository']+'.git')
            if path.exists():path.rmdir()  # refuses non-empty concurrent user state
            staged.rename(path)
    else:
        git(path,'fetch','--no-tags',remote,'refs/heads/'+grant['branch'],env=env)
        require(text_git(path,'rev-parse','FETCH_HEAD')==sha,'XD_SYNC_REMOTE_MOVED')
        require(inspect_copy(grant,roots)==before,'XD_SYNC_TARGET_CHANGED')
        git(path,'merge-base','--is-ancestor',before['head'],sha)
        incoming=text_git(path,'ls-tree','-r','--name-only',sha).splitlines()
        ignored=git(path,'ls-files','--others','--ignored','--exclude-standard','-z')[1].decode().split('\0')
        require(not any(a==b or a.startswith(b+'/') or b.startswith(a+'/') for a in incoming for b in ignored if b),'XD_SYNC_IGNORED_FILE_COLLISION')
        if updated:git(path,'merge','--ff-only','--no-edit',sha)
    require(text_git(path,'rev-parse','HEAD')==sha,'XD_SYNC_TARGET_HEAD_UNCONFIRMED')
    return {'sha':sha,'updated':updated,'clean':inspect_copy(grant,roots)['clean']}
