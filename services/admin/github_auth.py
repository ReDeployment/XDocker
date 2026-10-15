"""Private GitHub App credentials and memory-only installation tokens.

Only credential issuance uses POST. Repository APIs remain GET-only. No user OAuth,
client secret, webhook modification, repository writes or arbitrary endpoints.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import urllib.error
import urllib.request

REPOSITORIES = {
    'artisancloud': ('PowerX','PowerXPlugin','PowerXPluginSCRM','PowerXPluginECommerce'),
    'matrix-x': ('PowerXPluginMediaXStudio','PowerXPluginCRM'),
}
PERMISSIONS = dict.fromkeys(('actions','checks','statuses','contents','issues','pull_requests','metadata'),'read')
LOCAL_ERRORS = {'credentials-required','github-credential-unsafe','github-credential-invalid','github-credential-unreadable',
    'github-app-config-invalid','github-app-key-required','github-app-key-invalid','github-app-crypto-unavailable',
    'github-app-installation-required'}
_cache = {}
_lock = threading.Lock()


class AuthError(ValueError):
    def __init__(self,code,retry_at=0):super().__init__(code);self.retry_at=retry_at


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def owner_of(repo):
    parts=repo.split('/')
    if len(parts)!=2 or parts[0].lower() not in REPOSITORIES or parts[1] not in REPOSITORIES[parts[0].lower()]:
        raise AuthError('repository-not-allowlisted')
    return parts[0].lower()


def private_read(path,limit,root=None):
    path=Path(path);root=Path(root) if root is not None else path.parent
    for parent in (path,*path.parents):
        if parent.is_symlink():raise AuthError('github-credential-unsafe')
        if parent==root:break
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>limit:
                raise AuthError('github-credential-unsafe')
            data=stream.read(limit+1)
            if len(data)>limit:raise AuthError('github-credential-unsafe')
            return data
    except FileNotFoundError:return None
    except OSError:raise AuthError('github-credential-unreadable') from None


def configuration(root,repo):
    owner=owner_of(repo);directory=Path(root)/'credentials'
    raw=private_read(directory/'app.json',16384,root)
    if raw is None:return None
    try:
        def unique(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError()
                result[key]=value
            return result
        data=json.loads(raw,object_pairs_hook=unique)
        if not isinstance(data,dict) or set(data)!={'app_id','installations','key_id'}:raise ValueError()
        if type(data['app_id']) is not int or not 0<data['app_id']<2**63:raise ValueError()
        installations=data['installations']
        if not isinstance(installations,dict) or not installations or set(installations)-set(REPOSITORIES):raise ValueError()
        if any(type(i) is not int or not 0<i<2**63 for i in installations.values()):raise ValueError()
        if len(set(installations.values()))!=len(installations):raise ValueError()
        if not isinstance(data['key_id'],str) or not re.fullmatch(r'[a-f0-9]{64}',data['key_id']):raise ValueError()
    except (ValueError,TypeError,UnicodeError):raise AuthError('github-app-config-invalid') from None
    if owner not in installations:raise AuthError('github-app-installation-required')
    pem=private_read(directory/'app-keys'/(data['key_id']+'.pem'),32768,root)
    if pem is None:raise AuthError('github-app-key-required')
    if hashlib.sha256(pem).hexdigest()!=data['key_id']:raise AuthError('github-app-key-invalid')
    return dict(data,owner=owner,pem=pem,identity=hashlib.sha256(raw+pem).hexdigest())


def signing_key(pem):
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        from cryptography.exceptions import UnsupportedAlgorithm
    except ImportError:raise AuthError('github-app-crypto-unavailable') from None
    try:
        key=load_pem_private_key(pem,password=None)
        if not isinstance(key,RSAPrivateKey) or key.key_size<2048:raise ValueError()
        return key
    except (ValueError,TypeError,UnsupportedAlgorithm):raise AuthError('github-app-key-invalid') from None


def describe(root,repo):
    """Disk/crypto validation only: GET /api/github never issues access tokens."""
    owner=owner_of(repo);config=configuration(root,repo)
    if config:
        signing_key(config['pem'])
        return {'owner':owner,'configured':True,'mode':'github-app','state':'app-configured',
            'file':'data/admin/github/credentials/app.json','app_id':config['app_id'],
            'installation_id':config['installations'][owner]}
    value=legacy_token(root,repo)
    return {'owner':owner,'configured':bool(value),'mode':'personal-token','state':'file-configured' if value else 'credentials-required',
        'file':'data/admin/github/credentials/'+owner+'.token'}


def legacy_token(root,repo):
    raw=private_read(Path(root)/'credentials'/(owner_of(repo)+'.token'),1024,root)
    if raw is None:return ''
    try:
        value=raw.decode().strip()
        if not re.fullmatch(r'[A-Za-z0-9_\-.]{20,1024}',value):raise ValueError()
        return value
    except (ValueError,UnicodeError):raise AuthError('github-credential-invalid') from None


def jwt(config):
    key=signing_key(config['pem'])
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    def encode(data):return base64.urlsafe_b64encode(data).rstrip(b'=')
    now=int(time.time())
    header=encode(b'{"alg":"RS256","typ":"JWT"}')
    body=encode(json.dumps({'iss':str(config['app_id']),'iat':now-60,'exp':now+540},separators=(',',':')).encode())
    message=header+b'.'+body
    signature=key.sign(message,padding.PKCS1v15(),hashes.SHA256())
    return (message+b'.'+encode(signature)).decode()


def api(opener,path,authorization,body=None):
    # Callers generate only installation metadata and credential issuance endpoints.
    if not re.fullmatch(r'/app/installations/[1-9][0-9]*(?:/access_tokens)?',path):raise AuthError('github-app-endpoint-invalid')
    if (body is not None)!=path.endswith('/access_tokens'):raise AuthError('github-app-endpoint-invalid')
    request=urllib.request.Request('https://api.github.com'+path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Accept':'application/vnd.github+json','User-Agent':'XDocker-GitHub-App',
            'X-GitHub-Api-Version':'2022-11-28','Authorization':'Bearer '+authorization,
            'Content-Type':'application/json'},method='POST' if body is not None else 'GET')
    try:
        with opener.open(request,timeout=8) as response:
            if response.status!=(201 if body is not None else 200):raise AuthError('github-app-response-invalid')
            raw=response.read(131073)
            if len(raw)>131072:raise AuthError('github-app-response-invalid')
    except urllib.error.HTTPError as error:
        try:
            retry=0
            if error.code==429 or (error.code==403 and error.headers.get('X-RateLimit-Remaining')=='0'):
                try:retry=float(error.headers.get('X-RateLimit-Reset','0'))
                except (ValueError,TypeError):pass
                retry=min(time.time()+86400,max(time.time()+60,retry))
            if error.headers.get('Retry-After'):
                try:retry=max(retry,time.time()+min(86400,max(60,int(error.headers['Retry-After']))))
                except (ValueError,TypeError):pass
            raise AuthError('rate-limited' if retry and error.code in (403,429) else 'github-app-http-'+str(error.code),retry)
        finally:error.close()
    except (OSError,ValueError) as error:
        if isinstance(error,AuthError):raise
        raise AuthError('github-app-network-unconfirmed') from None
    try:
        data=json.loads(raw)
        if not isinstance(data,dict):raise ValueError()
        return data
    except (ValueError,UnicodeError):raise AuthError('github-app-response-invalid') from None


def token(root,repo):
    config=configuration(root,repo)
    if config is None:return legacy_token(root,repo)
    owner=config['owner'];cache_key=(str(Path(root).resolve()),owner,repo)
    with _lock:
        cached=_cache.get(cache_key)
        if cached and cached['identity']==config['identity'] and cached['expires']>time.time()+300:return cached['token']
        opener=urllib.request.build_opener(NoRedirect)
        authorization=jwt(config);installation=config['installations'][owner]
        details=api(opener,'/app/installations/'+str(installation),authorization)
        if details.get('id')!=installation or details.get('app_id')!=config['app_id'] or str(details.get('account',{}).get('login','')).lower()!=owner:
            raise AuthError('github-app-installation-mismatch')
        if details.get('suspended_at'):raise AuthError('github-app-installation-suspended')
        if any(details.get('permissions',{}).get(name) not in ('read','write') for name in PERMISSIONS):
            raise AuthError('github-app-permissions-missing')
        grant=api(opener,'/app/installations/'+str(installation)+'/access_tokens',authorization,
            {'repositories':[repo.split('/')[1]],'permissions':PERMISSIONS})
        value=grant.get('token');permissions=grant.get('permissions')
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{20,16384}',value):raise AuthError('github-app-response-invalid')
        if not isinstance(permissions,dict) or any(permissions.get(name)!='read' for name in PERMISSIONS) or any(v!='read' for v in permissions.values()):
            raise AuthError('github-app-permissions-unconfirmed')
        try:
            expiry=datetime.fromisoformat(grant['expires_at'].replace('Z','+00:00'))
            if expiry.tzinfo is None:raise ValueError()
            expires=expiry.timestamp()
            if not time.time()+300<expires<=time.time()+7200:raise ValueError()
        except (KeyError,TypeError,ValueError,AttributeError):raise AuthError('github-app-expiry-invalid') from None
        _cache[cache_key]={'identity':config['identity'],'expires':expires,'token':value}
        return value


def invalidate(root,repo):
    with _lock:_cache.pop((str(Path(root).resolve()),owner_of(repo),repo),None)
