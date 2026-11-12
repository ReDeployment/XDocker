"""Bounded Git snapshots and fast-forward copies; source worktrees are read-only."""
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

SHA=re.compile(r'^[a-f0-9]{40}$')
KEY=re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')
REPOSITORY=re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
MAX_FILES=10000
MAX_BYTES=64*1024*1024
MAX_FILE=8*1024*1024
DENY_PARTS={'.git','.ssh','.venv','venv','node_modules','__pycache__','.cache','.nuxt','.output'}
RUNTIME_ROOTS={'data','logs','backups','uploads'}
DENY_SUFFIXES=('.pem','.key','.p12','.pfx','.sqlite','.sqlite3','.db','.dump','.tar','.gz','.zip','.log','.pyc')


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


def backup_branch(value):
    require(isinstance(value,str) and re.fullmatch(r'xdocker-backup/[a-z0-9][a-z0-9._/-]{0,120}',value)
            and '..' not in value and '//' not in value and not value.endswith(('/','.','.lock')),'XD_SYNC_BACKUP_BRANCH_REQUIRED')
    return value


def safe_path(value,roots):
    require(isinstance(value,str) and value.startswith('/') and not any(c in value for c in '\n\r\0') and '..' not in Path(value).parts,'XD_SYNC_PATH_INVALID')
    p=Path(value)
    require(not any(x.is_symlink() for x in (p,*p.parents)),'XD_SYNC_SYMLINK_DENIED')
    require(any(p!=Path(root) and p.is_relative_to(Path(root)) for root in roots),'XD_SYNC_PATH_NOT_GRANTED')
    return p


def run(command,*,env=None,input=None,timeout=60,limit=4*1024*1024,allowed=(0,)):
    environment={k:os.environ[k] for k in ('PATH','HOME','USER','LOGNAME','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS') if k in os.environ}
    environment.update({'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_GLOBAL':os.devnull,'GIT_CONFIG_NOSYSTEM':'1','GIT_OPTIONAL_LOCKS':'0','LC_ALL':'C','GIT_LFS_SKIP_SMUDGE':'1'})
    environment.update(env or {})
    with tempfile.TemporaryFile() as incoming:
        if input is not None:incoming.write(input);incoming.seek(0)
        child=subprocess.Popen(command,stdin=incoming,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=environment,start_new_session=True)
        output=bytearray();deadline=time.monotonic()+timeout;poll=selectors.DefaultSelector();poll.register(child.stdout,selectors.EVENT_READ)
        try:
            while poll.get_map():
                require(time.monotonic()<deadline,'XD_SYNC_COMMAND_TIMEOUT')
                for key,_ in poll.select(.1):
                    block=os.read(key.fd,65536)
                    if not block:poll.unregister(key.fileobj);continue
                    output.extend(block);require(len(output)<=limit,'XD_SYNC_OUTPUT_LIMIT')
            try:child.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:raise SyncError('XD_SYNC_COMMAND_TIMEOUT') from None
            require(child.returncode in allowed,'XD_SYNC_GIT_REJECTED' if command[0]=='git' else 'XD_SYNC_TRANSPORT_REJECTED')
            return child.returncode,bytes(output)
        finally:
            poll.close();child.stdout.close()
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL);child.wait(timeout=5)


def git(directory,*arguments,env=None,input=None,allowed=(0,),timeout=60):
    return run(['git','-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false','-c','commit.gpgsign=false',
                '-c','credential.helper=','-c','gc.auto=0','-C',str(directory),*arguments],env=env,input=input,allowed=allowed,timeout=timeout)


def text_git(directory,*args,**kwargs):return git(directory,*args,**kwargs)[1].decode('utf-8','strict').strip()


def source_branch(root):
    code,raw=git(root,'symbolic-ref','--quiet','--short','HEAD',allowed=(0,1))
    if code==0:return raw.decode('utf-8','strict').strip()
    sha=text_git(root,'rev-parse','HEAD');require(SHA.fullmatch(sha),'XD_SYNC_SOURCE_HEAD_INVALID')
    return 'HEAD@'+sha


def allowed_file(name,includes,excludes):
    p=Path(name)
    require(not p.is_absolute() and '..' not in p.parts and '\0' not in name,'XD_SYNC_FILE_INVALID')
    if p.parts[0] in RUNTIME_ROOTS or any(v.lower() in DENY_PARTS or v.lower().startswith('.env') for v in p.parts) or name.lower().endswith(DENY_SUFFIXES):return False
    if any(fnmatch.fnmatchcase(name,v) or any(fnmatch.fnmatchcase(part,v) for part in p.parts) for v in excludes):return False
    return any(v=='.' or name==v or name.startswith(v.rstrip('/')+'/') for v in includes)


def scan_source(profile,roots,copy_to=None):
    root=safe_path(profile['directory'],roots);require(root.is_dir(),'XD_SYNC_SOURCE_MISSING')
    require(Path(text_git(root,'rev-parse','--show-toplevel')).resolve()==root,'XD_SYNC_REPOSITORY_ROOT_REQUIRED')
    branch=source_branch(root);require(branch==profile['branch'],'XD_SYNC_SOURCE_BRANCH_CHANGED')
    head=text_git(root,'rev-parse','HEAD');require(SHA.fullmatch(head),'XD_SYNC_SOURCE_HEAD_INVALID')
    require(canonical(text_git(root,'remote','get-url','origin')).lower()==profile['repository'].lower(),'XD_SYNC_SOURCE_REMOTE_MISMATCH')
    selection=['.'] if '.' in profile['includes'] else [':(literal)'+v for v in profile['includes']]
    names=set(git(root,'ls-files','-z','--cached','--others','--exclude-standard','--',*selection)[1].decode('utf-8','strict').split('\0'))-{''}
    require(len(names)<=MAX_FILES*2,'XD_SYNC_FILE_LIMIT')
    manifest=[];total=0;excluded=0
    for name in sorted(names):
        require(len(name)<=1024,'XD_SYNC_FILE_PATH_LIMIT')
        if not allowed_file(name,profile['includes'],profile['excludes']):excluded+=1;continue
        p=root/name
        if not p.exists() and not p.is_symlink():continue
        require(not any(x.is_symlink() for x in (p,*list(p.parents)[:len(p.relative_to(root).parts)])),'XD_SYNC_SYMLINK_DENIED')
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno());require(stat.S_ISREG(info.st_mode) and info.st_size<=MAX_FILE,'XD_SYNC_FILE_LIMIT')
            body=source.read(MAX_FILE+1);after=os.fstat(source.fileno())
        require(len(body)<=MAX_FILE and (info.st_size,info.st_mtime_ns)==(after.st_size,after.st_mtime_ns),'XD_SYNC_SOURCE_CHANGED')
        total+=len(body);require(total<=MAX_BYTES and len(manifest)<MAX_FILES,'XD_SYNC_SNAPSHOT_LIMIT')
        mode=0o755 if info.st_mode&0o111 else 0o644
        manifest.append([name,hashlib.sha256(body).hexdigest(),mode,len(body)])
        if copy_to:
            target=Path(copy_to)/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(body);target.chmod(mode)
    require(manifest,'XD_SYNC_NO_SELECTED_FILES')
    require(text_git(root,'rev-parse','HEAD')==head and source_branch(root)==branch,'XD_SYNC_SOURCE_CHANGED')
    return {'head':head,'branch':branch,'files':len(manifest),'bytes':total,'excluded':excluded,
            'fingerprint':digest({'head':head,'branch':branch,'manifest':manifest}),'selected':[r[0] for r in manifest[:30]]}


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
        code,raw=git(directory,'ls-remote','--exit-code',self.remotes(profile['repository']),'refs/heads/'+profile['backup_branch'],env=env,allowed=(0,2))
        if code==2:return None
        rows=raw.decode().strip().splitlines();require(len(rows)==1,'XD_SYNC_REMOTE_HEAD_INVALID')
        sha,ref=rows[0].split();require(SHA.fullmatch(sha) and ref=='refs/heads/'+profile['backup_branch'],'XD_SYNC_REMOTE_HEAD_INVALID');return sha

    def publish(self,profile,roots,env,before_push):
        cache=self.root/(profile['id']+'.git')
        if not cache.exists():git(self.root,'init','--bare',str(cache))
        require(not cache.is_symlink(),'XD_SYNC_SYMLINK_DENIED')
        size=sum(p.stat().st_size for p in cache.rglob('*') if p.is_file());require(size<=512*1024*1024,'XD_SYNC_CACHE_CAPACITY')
        with tempfile.TemporaryDirectory(prefix='snapshot-',dir=self.root) as temporary:
            copied=Path(temporary)/'files';copied.mkdir();snapshot=scan_source(profile,roots,copied)
            index_env={**env,'GIT_INDEX_FILE':str(Path(temporary)/'index')}
            command=['git','--git-dir',str(cache),'--work-tree',str(copied)]
            run([*command,'add','--force','--all'],env=index_env)
            # The index lives outside the work tree so it can never become a source file.
            tree=run([*command,'write-tree'],env=index_env)[1].decode().strip()
            parent=self.remote_head(cache,profile,env)
            if parent:
                git(cache,'fetch','--no-tags',self.remotes(profile['repository']),'refs/heads/'+profile['backup_branch'],env=env)
                require(text_git(cache,'rev-parse','FETCH_HEAD')==parent,'XD_SYNC_REMOTE_MOVED')
                message=text_git(cache,'show','-s','--format=%B',parent)
                require('XDocker-Profile: '+profile['uid'] in message,'XD_SYNC_UNMANAGED_BACKUP_BRANCH')
                if text_git(cache,'rev-parse',parent+'^{tree}')==tree:return {'sha':parent,'changed':False,'snapshot':snapshot}
            message=f'XDocker snapshot: {profile["id"]}\n\nSource-Commit: {snapshot["head"]}\nXDocker-Profile: {profile["uid"]}\n'
            author={**env,'GIT_AUTHOR_NAME':'XDocker Backup','GIT_AUTHOR_EMAIL':'backup@xdocker.invalid','GIT_COMMITTER_NAME':'XDocker Backup','GIT_COMMITTER_EMAIL':'backup@xdocker.invalid'}
            sha=text_git(cache,'commit-tree',tree,*(['-p',parent] if parent else []),env=author,input=message.encode())
            require(scan_source(profile,roots)['fingerprint']==snapshot['fingerprint'],'XD_SYNC_SOURCE_CHANGED')
            before_push(sha,snapshot)
            try:git(cache,'push',self.remotes(profile['repository']),sha+':refs/heads/'+profile['backup_branch'],env=env)
            except SyncError:
                try:confirmed=self.remote_head(cache,profile,env)==sha
                except SyncError:confirmed=False
                require(confirmed,'XD_SYNC_UPLOAD_UNCONFIRMED')
            require(self.remote_head(cache,profile,env)==sha,'XD_SYNC_UPLOAD_UNCONFIRMED')
            git(cache,'update-ref','refs/heads/'+profile['backup_branch'],sha)
            return {'sha':sha,'changed':True,'snapshot':snapshot}

    def verify(self,profile,sha,env):
        return self.remote_head(self.root,profile,env)==sha


def inspect_copy(grant,roots):
    path=safe_path(grant['directory'],roots)
    if not path.exists():return {'exists':False,'head':None,'clean':True,'branch':grant['backup_branch']}
    require(path.is_dir(),'XD_SYNC_TARGET_NOT_DIRECTORY')
    if not any(path.iterdir()):return {'exists':False,'head':None,'clean':True,'branch':grant['backup_branch']}
    require(Path(text_git(path,'rev-parse','--show-toplevel')).resolve()==path,'XD_SYNC_TARGET_ROOT_MISMATCH')
    require(canonical(text_git(path,'remote','get-url','origin')).lower()==grant['repository'].lower(),'XD_SYNC_TARGET_REMOTE_MISMATCH')
    branch=text_git(path,'symbolic-ref','--short','HEAD');require(branch==grant['backup_branch'],'XD_SYNC_TARGET_BRANCH_MISMATCH')
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
            git(path.parent,'clone','--single-branch','--no-tags','--branch',grant['backup_branch'],remote,str(staged),env=env)
            require(text_git(staged,'rev-parse','HEAD')==sha,'XD_SYNC_REMOTE_MOVED')
            # Pin the canonical origin even when a fixture resolver supplied a local transport.
            git(staged,'remote','set-url','origin','https://github.com/'+grant['repository']+'.git')
            if path.exists():path.rmdir()  # refuses non-empty concurrent user state
            staged.rename(path)
    else:
        git(path,'fetch','--no-tags',remote,'refs/heads/'+grant['backup_branch'],env=env)
        require(text_git(path,'rev-parse','FETCH_HEAD')==sha,'XD_SYNC_REMOTE_MOVED')
        require(inspect_copy(grant,roots)==before,'XD_SYNC_TARGET_CHANGED')
        git(path,'merge-base','--is-ancestor',before['head'],sha)
        incoming=text_git(path,'ls-tree','-r','--name-only',sha).splitlines()
        ignored=git(path,'ls-files','--others','--ignored','--exclude-standard','-z')[1].decode().split('\0')
        require(not any(a==b or a.startswith(b+'/') or b.startswith(a+'/') for a in incoming for b in ignored if b),'XD_SYNC_IGNORED_FILE_COLLISION')
        if updated:git(path,'merge','--ff-only','--no-edit',sha)
    require(text_git(path,'rev-parse','HEAD')==sha,'XD_SYNC_TARGET_HEAD_UNCONFIRMED')
    return {'sha':sha,'updated':updated,'clean':inspect_copy(grant,roots)['clean']}
