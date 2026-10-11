"""PowerX image inventory and read-only upgrade previews; never pull or apply changes."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
from urllib.parse import quote

from preflight import image_parts, read_env, isolated_probe, http_read

SHA=re.compile(r'^[a-f0-9]{40}$')
DIGEST=re.compile(r'^sha256:[a-f0-9]{64}$')
KEY=re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
VERSION=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}$')
RELEASE=re.compile(r'^v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\+[A-Za-z0-9.-]+)?$')
REPOS={'backend':'ghcr.io/artisancloud/powerx-backend','web-admin':'ghcr.io/artisancloud/powerx-web-admin'}
ENV_KEYS={'backend':'POWERX_BACKEND_IMAGE','web-admin':'POWERX_WEB_IMAGE'}
STEPS={'Build backend','Build web admin','Fresh database, login, frontend and API smoke test'}


def now():return datetime.now(timezone.utc).isoformat()


def evidence_result(state,detail,**fields):return {'state':state,'detail':detail,**fields}


def valid_reference(reference):
    registry,name,tag=image_parts(reference)
    return registry+'/'+name,tag


def reference_fields(reference):
    try:
        repository,tag=valid_reference(reference)
        return {'reference':reference,'pin':'digest' if DIGEST.fullmatch(tag) else 'sha-tag' if re.fullmatch(r'sha-[a-f0-9]{40}',tag) else 'mutable',
                'tag_revision':tag[4:] if re.fullmatch(r'sha-[a-f0-9]{40}',tag) else None,'repository':repository}
    except (ValueError,TypeError):return {'reference':None,'pin':'invalid','tag_revision':None,'repository':None}


def validate_candidate(raw):
    required={'id','channel','version','source_revision','images','evidence','migration'}
    if not isinstance(raw,dict) or set(raw)!=required:raise ValueError('Invalid candidate fields')
    if not isinstance(raw['id'],str) or not KEY.fullmatch(raw['id']):raise ValueError('Invalid candidate identity')
    if raw['channel'] not in ('dev','stable') or not isinstance(raw['version'],str) or not VERSION.fullmatch(raw['version']):raise ValueError('Invalid version/channel')
    if not isinstance(raw['source_revision'],str) or not SHA.fullmatch(raw['source_revision']):raise ValueError('Full source revision required')
    if not isinstance(raw['images'],dict) or set(raw['images'])!=set(REPOS):raise ValueError('Both images required')
    for role,reference in raw['images'].items():
        repository,tag=valid_reference(reference)
        if repository!=REPOS[role] or '@' not in reference or not DIGEST.fullmatch(tag):raise ValueError('Candidate images must be role-matched and digest-pinned')
    evidence=raw['evidence']
    if not isinstance(evidence,dict) or set(evidence)!={'run_id','release_tag'}:raise ValueError('Invalid evidence fields')
    if type(evidence['run_id']) is not int or not 0<evidence['run_id']<10**20:raise ValueError('Invalid workflow run')
    if raw['channel']=='stable':
        if evidence['release_tag']!=raw['version'] or not RELEASE.fullmatch(raw['version']):raise ValueError('Stable channel requires a final semver release tag')
    elif evidence['release_tag'] is not None:raise ValueError('Development channel does not declare a stable release')
    migration=raw['migration']
    if not isinstance(migration,dict) or set(migration)!={'status','from_revisions','notes','rollback'}:raise ValueError('Invalid migration fields')
    if migration['status'] not in ('none','required','unknown') or migration['rollback'] not in ('compatible','restore-required','unknown'):raise ValueError('Invalid migration declaration')
    revisions=migration['from_revisions']
    if not isinstance(revisions,list) or len(revisions)>30 or any(not isinstance(r,str) or not SHA.fullmatch(r) for r in revisions):raise ValueError('Invalid migration source list')
    if not isinstance(migration['notes'],str) or len(migration['notes'])>2000:raise ValueError('Invalid migration notes')
    # Return only supported data; catalog declarations are not signed producer attestations.
    return json.loads(json.dumps(raw))


def github_json(path):
    code,_,body=http_read('https://api.github.com/repos/ArtisanCloud/PowerX/'+path,
                         {'Accept':'application/vnd.github+json','User-Agent':'XDocker-ReadOnly'})
    if code!=200:return code,None
    data=json.loads(body)
    if not isinstance(data,dict):raise ValueError('Invalid GitHub response')
    return code,data


def verify_evidence(run_id,revision,channel,release_tag=None):
    if type(run_id) is not int or not 0<run_id<10**20 or not SHA.fullmatch(revision):raise ValueError('Invalid evidence identity')
    if channel not in ('dev','stable'):raise ValueError('Invalid channel')
    code,run=github_json('actions/runs/'+str(run_id))
    if code!=200:return evidence_result('unknown',f'GitHub workflow API HTTP {code}；网络、限流或权限未确认。')
    if run.get('head_sha')!=revision:return evidence_result('failed','工作流源码 SHA 与候选版本不同。')
    if run.get('status')!='completed':return evidence_result('unknown','发布工作流尚未完成。')
    if run.get('conclusion')!='success':return evidence_result('failed','发布工作流结果不是 success。')
    if run.get('repository',{}).get('full_name','').lower()!='artisancloud/powerx' or run.get('path','').split('@')[0]!='.github/workflows/publish-container.yml':
        return evidence_result('unknown','发布项目或工作流与当前支持的证据格式不匹配，请由 PowerX 维护者确认。')
    if channel=='dev' and run.get('head_branch')!='develop':return evidence_result('failed','开发通道的工作流不来自 develop。')
    code,data=github_json('actions/runs/'+str(run_id)+'/jobs?per_page=100')
    if code!=200:return evidence_result('unknown',f'GitHub jobs API HTTP {code}，未核验发布及安装步骤。')
    if not isinstance(data.get('jobs'),list) or data.get('total_count',101)>100:return evidence_result('unknown','任务列表不完整，未据此判定验收通过。')
    matching=[]
    for job in data['jobs']:
        if job.get('status')!='completed' or job.get('conclusion')!='success':continue
        passed={s.get('name') for s in job.get('steps',[]) if s.get('status')=='completed' and s.get('conclusion')=='success'}
        if STEPS<=passed:matching.append(job)
    if not matching:return evidence_result('unknown','没有找到当前支持的前后端发布及空库/登录/前端/API验收步骤，需确认发布契约。')
    if channel=='stable':
        if not isinstance(release_tag,str) or not RELEASE.fullmatch(release_tag):raise ValueError('Invalid release tag')
        code,release=github_json('releases/tags/'+quote(release_tag,safe=''))
        if code!=200:return evidence_result('unknown',f'正式 Release API HTTP {code}，发布未确认。')
        names={a.get('name') for a in release.get('assets',[]) if a.get('state')=='uploaded' and isinstance(a.get('size'),int) and a['size']>0}
        if (release.get('tag_name')!=release_tag or release.get('draft') is not False or release.get('prerelease') is not False
                or not {'powerx-docker.tar.gz','powerx-docker.tar.gz.sha256'}<=names):
            return evidence_result('failed','正式 Release 未满足正式发布与部署包条件。')
        code,reference=github_json('git/ref/tags/'+quote(release_tag,safe=''))
        if code!=200:return evidence_result('unknown','无法核对 Release tag 对应的源码。')
        obj=reference.get('object',{})
        if obj.get('type')=='tag':
            tag_sha=obj.get('sha','')
            if not SHA.fullmatch(tag_sha):raise ValueError('Invalid annotated tag')
            code,annotated=github_json('git/tags/'+tag_sha)
            if code!=200:return evidence_result('unknown','无法核对 annotated tag。')
            obj=annotated.get('object',{})
        if obj.get('type')!='commit' or obj.get('sha')!=revision:return evidence_result('failed','正式 Release tag 与候选源码 SHA 不一致。')
    return evidence_result('verified','已核验同一 SHA 的完整工作流及支持的发布/安装步骤。'+(' 正式 Release 与源码及部署包匹配。' if channel=='stable' else ''),
                           run_id=run_id,source_revision=revision,head_branch=run.get('head_branch'),
                           url='https://github.com/ArtisanCloud/PowerX/actions/runs/'+str(run_id),
                           scope='仅核验这些 CI 步骤与发布记录；不代表本服务器安装、全部业务或数据库回退已验收。')


def isolated_evidence(candidate):
    args=[sys.executable,str(Path(__file__).resolve()),'--evidence',str(candidate['evidence']['run_id']),
          candidate['source_revision'],candidate['channel'],candidate['evidence']['release_tag'] or '']
    try:
        call=subprocess.run(args,capture_output=True,timeout=25,check=False)
        if call.returncode!=0 or len(call.stdout)>8192:raise ValueError('Invalid evidence output')
        data=json.loads(call.stdout)
        if not isinstance(data,dict) or data.get('state') not in ('verified','unknown','failed'):raise ValueError('Invalid evidence result')
        return data
    except subprocess.TimeoutExpired:return evidence_result('unknown','证据核验超过 25 秒，已结束本次子进程。')
    except (OSError,ValueError):return evidence_result('unknown','证据检查未返回有效结果；未接受原始响应或凭据。')


def safe_image(info):
    identifier=info.get('Id')
    if not isinstance(identifier,str) or not DIGEST.fullmatch(identifier):raise ValueError('Invalid image ID')
    labels=info.get('Config',{}).get('Labels') or {}
    revision=labels.get('org.opencontainers.image.revision')
    version=labels.get('org.opencontainers.image.version')
    digests=[]
    for reference in (info.get('RepoDigests') or [])[:30]:
        try:
            _,digest=valid_reference(reference)
            if '@' in reference and DIGEST.fullmatch(digest):digests.append(reference)
        except (ValueError,TypeError):continue
    return {'image_id':identifier,'source_revision':revision if isinstance(revision,str) and SHA.fullmatch(revision) else None,
            'version_label':version if isinstance(version,str) and VERSION.fullmatch(version) else None,
            'repo_digests':digests,'os':info.get('Os') if info.get('Os') in ('linux','windows') else None,
            'architecture':info.get('Architecture') if info.get('Architecture') in ('amd64','arm64','arm','386') else None}


class VersionCheckBusy(Exception):pass


class ImageVersions:
    def __init__(self,engine,config,catalog,allowed,containers,backup_records):
        self.engine,self.config,self.path=engine,Path(config),Path(catalog)
        self.allowed,self.containers,self.backup_records=allowed,containers,backup_records
        self.lock=threading.Lock();self.checks={}

    def catalog(self):
        if self.path.is_symlink():raise ValueError('Invalid catalog symlink')
        if not self.path.exists():return []
        if not self.path.is_file() or self.path.stat().st_size>128*1024:raise ValueError('Invalid catalog file')
        raw=json.loads(self.path.read_text())
        if not isinstance(raw,dict) or set(raw)!={'schema','versions'} or type(raw['schema']) is not int or raw['schema']!=1 or not isinstance(raw['versions'],list) or len(raw['versions'])>40:raise ValueError('Invalid catalog')
        rows=[validate_candidate(r) for r in raw['versions']]
        if len({r['id'] for r in rows})!=len(rows):raise ValueError('Duplicate candidate identity')
        return rows

    def configured(self,instance):
        if instance not in self.allowed():raise KeyError(instance)
        values=read_env(self.config/'instances'/instance/'.env')
        environment=values.get('DEPLOYMENT_ENV')
        return {'instance':instance,'environment':environment if environment in ('dev','development','test','prod','production') else 'unknown',
                'configured':{role:reference_fields(values.get(key,'')) for role,key in ENV_KEYS.items()}}

    def listing(self):
        error=None
        try:catalog=self.catalog()
        except (OSError,ValueError,TypeError):catalog=[];error='候选目录格式异常，未接受任何候选；当前实例仍可单独检查。'
        return {'at':now(),'instances':[self.configured(p) for p in sorted(self.allowed())],'candidates':catalog,'catalog_error':error,
                'catalog_notice':'候选是 XDocker 维护者登记的记录；不代表 PowerX 已提供自动发布契约或完成在线核验。',
                'checks':[self.public_check(r) for r in catalog]}

    def fingerprint(self,candidate):return hashlib.sha256(json.dumps(candidate,sort_keys=True).encode()).hexdigest()

    def public_check(self,candidate):
        entry=self.checks.get(candidate['id'])
        if not entry or entry['fingerprint']!=self.fingerprint(candidate):return {'id':candidate['id'],'evidence':None,'registry':{}}
        result={'id':candidate['id'],'evidence':entry.get('evidence'),'registry':dict(entry.get('registry',{}))}
        for row in ([result['evidence']] if result['evidence'] else [])+list(result['registry'].values()):
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(row['at'])).total_seconds()
            row['stale']=not -300<=age<=900
        return result

    def candidate(self,identifier):
        if not KEY.fullmatch(identifier):raise KeyError(identifier)
        row=next((r for r in self.catalog() if r['id']==identifier),None)
        if row is None:raise KeyError(identifier)
        return row

    def check(self,identifier,kind,role=None):
        if not self.lock.acquire(blocking=False):raise VersionCheckBusy()
        try:
            candidate=self.candidate(identifier)
            if kind=='evidence':data=isolated_evidence(candidate)
            elif kind=='registry' and role in REPOS:data=isolated_probe('manifest',candidate['images'][role])
            else:raise KeyError(kind)
            data={**data,'at':now(),'stale':False}
            entry=self.checks.get(identifier)
            if not entry or entry['fingerprint']!=self.fingerprint(candidate):entry={'fingerprint':self.fingerprint(candidate),'registry':{}}
            if kind=='evidence':entry['evidence']=data
            else:entry['registry'][role]=data
            self.checks={k:v for k,v in self.checks.items() if k in {r['id'] for r in self.catalog()}}
            self.checks[identifier]=entry
            return {'id':identifier,'kind':kind,'role':role,'result':data}
        finally:self.lock.release()

    def engine_platform(self):
        info=self.engine.request('GET','/version',timeout=4)
        if info.get('Os') not in ('linux','windows') or info.get('Arch') not in ('amd64','arm64','arm','386'):raise ValueError('Unknown host platform')
        return {'os':info['Os'],'architecture':info['Arch']}

    def inspect_image(self,reference):
        image_parts(reference) if not DIGEST.fullmatch(reference) else None
        return safe_image(self.engine.request('GET','/images/'+quote(reference,safe='')+'/json',timeout=4))

    def current(self,instance):
        row=self.configured(instance);row.update(at=now(),runtime={},pair={'state':'unknown'},engine_error=None)
        try:
            row['platform']=self.engine_platform()
            containers=[c for c in self.containers() if c.get('Labels',{}).get('com.docker.compose.project')==instance
                        and c.get('Labels',{}).get('com.docker.compose.service') in REPOS]
        except Exception:
            row['engine_error']='Docker 状态不可用，不能据配置引用判断实际部署。';return row
        for role in REPOS:
            matches=[c for c in containers if c['Labels']['com.docker.compose.service']==role]
            if len(matches)!=1:row['runtime'][role]={'state':'unknown','detail':'未找到唯一登记容器。'};continue
            try:
                identifier=matches[0]['Id']
                if not re.fullmatch(r'[a-f0-9]{64}',identifier):raise ValueError('Invalid container ID')
                details=self.engine.request('GET','/containers/'+identifier+'/json',timeout=4)
                labels=details.get('Config',{}).get('Labels') or {}
                if labels.get('com.docker.compose.project')!=instance or labels.get('com.docker.compose.service')!=role:raise ValueError('Container identity changed')
                image=details.get('Image')
                if not isinstance(image,str) or not DIGEST.fullmatch(image):raise ValueError('Unknown actual image')
                actual=self.inspect_image(image)
                if actual['image_id']!=image:raise ValueError('Image identity changed')
                state=details.get('State',{}).get('Status')
                health=details.get('State',{}).get('Health',{}).get('Status')
                actual['state']=state if state in ('created','restarting','running','removing','paused','exited','dead') else 'unknown'
                actual['health']=health if health in ('starting','healthy','unhealthy') else 'unknown'
                actual['container_reference']=reference_fields(details.get('Config',{}).get('Image',''))['reference']
                configured=row['configured'][role]
                try:
                    target=self.inspect_image(configured['reference']) if configured['reference'] else None
                    actual['configuration_match']='matches' if target and target['image_id']==image else 'different'
                except Exception:actual['configuration_match']='unknown'
                row['runtime'][role]=actual
            except Exception:row['runtime'][role]={'state':'unknown','detail':'无法核对容器身份与实际镜像。'}
        revisions=[row['runtime'].get(r,{}).get('source_revision') for r in REPOS]
        row['pair']={'state':'matched' if all(revisions) and len(set(revisions))==1 else 'different' if all(revisions) else 'unknown',
                     'source_revision':revisions[0] if all(revisions) and len(set(revisions))==1 else None}
        return row

    def local(self,instance,identifier):
        self.configured(instance);candidate=self.candidate(identifier)
        result={'at':now(),'id':identifier,'instance':instance,'images':{}}
        try:platform=self.engine_platform();result['platform']=platform
        except Exception:
            result['engine_error']='无法读取 Docker 平台，未核验本机候选镜像。';return result
        for role,reference in candidate['images'].items():
            try:
                image=self.inspect_image(reference)
                digest_match=reference in image['repo_digests']
                source_match=image['source_revision']==candidate['source_revision']
                platform_match=image['os']==platform['os'] and image['architecture']==platform['architecture']
                result['images'][role]={**image,'state':'verified' if digest_match and source_match and platform_match else 'mismatch',
                                        'digest_match':digest_match,'source_match':source_match,'platform_match':platform_match}
            except Exception:result['images'][role]={'state':'unknown','detail':'本机未能按指定 digest 读取镜像；网页不会 pull。'}
        return result

    def preview(self,instance,identifier):
        candidate=self.candidate(identifier);current=self.current(instance);local=self.local(instance,identifier)
        check=self.public_check(candidate);blockers=[];warnings=[]
        if current['pair']['state']!='matched':blockers.append('当前运行前后端源码 SHA 未核对成对。')
        if any(current['runtime'].get(r,{}).get('configuration_match')!='matches' for r in REPOS):blockers.append('当前配置与运行镜像不一致或尚未核对。')
        if current['environment']=='unknown':blockers.append('部署环境未确认。')
        if current['environment'] in ('prod','production') and candidate['channel']!='stable':blockers.append('正式实例不能预览为开发通道升级。')
        if any(local.get('images',{}).get(r,{}).get('state')!='verified' for r in REPOS):blockers.append('候选的本机 digest、源码 SHA 或平台未核验通过。')
        evidence=check['evidence']
        if not evidence or evidence.get('stale') or evidence.get('state')!='verified':blockers.append('发布与安装验收证据未核验通过或已过期。')
        migration=candidate['migration'];revision=current['pair'].get('source_revision')
        if migration['status']=='unknown' or revision not in migration['from_revisions']:blockers.append('没有针对当前源码版本的明确迁移说明。')
        if migration['rollback']=='unknown':blockers.append('数据库回退策略未明确。')
        if migration['status']=='required':warnings.append('升级包含数据迁移；应先由 PowerX 维护者明确执行与验收方案。')
        if migration['rollback']=='restore-required':warnings.append('回退需要对应数据恢复，不能只切换旧镜像。')
        if any(current['configured'][r]['pin']=='mutable' for r in REPOS):warnings.append('当前配置含可变 tag；预览使用固定 digest，本页不会跟随 tag 更新运行实例。')
        if any(current['runtime'].get(r,{}).get('health')!='healthy' for r in REPOS):warnings.append('当前容器健康未确认或异常，需按实际服务状态另行验收。')
        records=[r for r in self.backup_records([instance]) if r.get('state')=='ready']
        backup=records[0] if records else None
        backup_summary=None
        if backup and isinstance(backup.get('id'),str) and re.fullmatch(r'[a-f0-9]{32}',backup['id']):
            try:
                created=datetime.fromisoformat(backup['created'])
                if created.tzinfo is None:raise ValueError('Unknown backup timestamp')
                backup_summary={'id':backup['id'],'created':created.isoformat(),'state':'ready','verified':backup.get('verified') is True}
            except (KeyError,ValueError,TypeError):pass
        warnings.append('升级前必须另行创建并校验完整实例备份，确认恢复方案；历史 ready/verified 字段不代表本次已复核备份字节。')
        matched=all(local.get('images',{}).get(r,{}).get('state')=='verified'
                    and bool(current['runtime'].get(r,{}).get('image_id'))
                    and current['runtime'][r]['image_id']==local['images'][r].get('image_id') for r in REPOS)
        deployed='matching-running' if matched and all(current['runtime'].get(r,{}).get('state')=='running' for r in REPOS) else 'matching-stopped' if matched else 'not-confirmed'
        return {'at':now(),'instance':instance,'candidate':candidate,'current':current,'local':local,'evidence':check,
                'technical_preview':'blocked' if blockers else 'reviewable','blockers':blockers,'warnings':warnings,
                'backup':backup_summary,'deployment':deployed,
                'config_preview':'\n'.join(ENV_KEYS[r]+'='+candidate['images'][r] for r in REPOS),
                'applied':False,'backup_required':True,'scope':'只读预览；不保存配置，不拉取镜像，不迁移数据库或重启服务。迁移/回退说明来自维护者登记，仍需人工复核与单独授权。'}


if __name__=='__main__':
    if len(sys.argv)!=6 or sys.argv[1]!='--evidence':sys.exit(2)
    try:data=verify_evidence(int(sys.argv[2]),sys.argv[3],sys.argv[4],sys.argv[5] or None)
    except Exception:data=evidence_result('unknown','证据响应、网络或格式异常；没有回显原始响应或凭据。')
    print(json.dumps(data,ensure_ascii=False))
