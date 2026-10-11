"""Bounded, read-only deployment checks. Never pulls images or reads Docker credentials."""
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from urllib.parse import quote, urlencode
import urllib.error
import urllib.request

NETWORKS = {
    'network-ghcr': ('GHCR 连通性', 'https://ghcr.io/v2/'),
    'network-dockerhub': ('Docker Hub 连通性', 'https://registry-1.docker.io/v2/'),
    'network-acme': ('正式 ACME 连通性', 'https://acme-v02.api.letsencrypt.org/directory'),
    'network-acme-staging': ('测试 ACME 连通性', 'https://acme-staging-v02.api.letsencrypt.org/directory'),
}
HOST_CHECKS = {'host-compose': '宿主机 Compose 与配置', 'host-storage': '宿主机磁盘空间',
               'host-permissions': '部署目录访问权限', 'host-ports': '宿主机端口监听'}
STATUSES = {'pass', 'warn', 'fail', 'unknown'}
DOMAIN = re.compile(r'^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
IMAGE = re.compile(r'^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[a-f0-9]{64})?$')
MAX_ITEMS = 100


def now():
    return datetime.now(timezone.utc).isoformat()


def read_env(path):
    result = {}
    if not Path(path).is_file():return result
    for line in Path(path).read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$', line)
        if match:
            try:
                parts = shlex.split(match[2], comments=True)
                if len(parts) <= 1:result[match[1]] = parts[0] if parts else ''
            except ValueError:pass
    return result


def result(status, detail, advice='', **fields):
    return {'status':status, 'detail':detail, 'advice':advice, **fields}


def image_parts(image):
    if not isinstance(image, str) or len(image)>300 or not IMAGE.fullmatch(image):
        raise ValueError('Invalid image reference')
    name, separator, digest = image.partition('@')
    if separator:reference = digest
    elif ':' in name.rsplit('/',1)[-1]:name, reference = name.rsplit(':',1)
    else:reference='latest'
    first=name.split('/')[0]
    if '/' in name and ('.' in first or ':' in first or first=='localhost'):
        registry, name = name.split('/',1)
    else:registry='docker.io'
    if registry in ('docker.io','index.docker.io','registry-1.docker.io'):
        registry='docker.io'
        if '/' not in name:name='library/'+name
    return registry, name, reference


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):return None


def http_read(url, headers=None):
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with client.open(urllib.request.Request(url,headers=headers or {}),timeout=4) as response:
            body=response.read(512*1024+1)
            if len(body)>512*1024:raise ValueError('Response too large')
            return response.status,response.headers,body
    except urllib.error.HTTPError as error:
        code, headers=error.code,error.headers
        error.close()
        return code,headers,b''


def probe_network(key):
    _,url=NETWORKS[key]
    code,headers,body=http_read(url)
    if key in ('network-ghcr','network-dockerhub'):
        if code in (200,401) and headers.get('Docker-Distribution-Api-Version')=='registry/2.0':
            return result('pass',f'GET /v2/ → HTTP {code}，Registry V2 可达。',
                          '此项只验证连通性，不验证私有镜像权限、镜像层下载或 Docker daemon 出网。')
    elif code==200:
        data=json.loads(body)
        if isinstance(data,dict) and all(isinstance(data.get(k),str) and data[k].startswith('https://') for k in ('newAccount','newOrder','newNonce')):
            return result('pass','ACME directory → HTTP 200，返回预期目录。','签发还需验证域名与公网 80 端口；本检查不会签发证书。')
    return result('fail',f'目标返回 HTTP {code} 或非预期响应。','核对服务器出网与目标服务；重定向不会自动跟随。')


def probe_manifest(image):
    registry,name,reference=image_parts(image)
    if registry not in ('ghcr.io','docker.io'):
        return result('unknown','此 registry 未实现匿名清单检查。','在服务器终端使用既有认证核对镜像；网页不会读取 Docker 登录凭据。')
    host='ghcr.io' if registry=='ghcr.io' else 'registry-1.docker.io'
    url='https://'+host+'/v2/'+quote(name,safe='/')+'/manifests/'+quote(reference,safe=':')
    headers={'Accept':', '.join(('application/vnd.oci.image.index.v1+json','application/vnd.oci.image.manifest.v1+json',
                                'application/vnd.docker.distribution.manifest.list.v2+json','application/vnd.docker.distribution.manifest.v2+json'))}
    code,_,body=http_read(url,headers)
    if code==401:
        # Fixed token endpoints only; never follow a challenge to a caller-selected host.
        token_url=('https://ghcr.io/token' if registry=='ghcr.io' else 'https://auth.docker.io/token')+'?'+urlencode({
            'service':host if registry=='ghcr.io' else 'registry.docker.io','scope':'repository:'+name+':pull'})
        token_code,_,token_body=http_read(token_url)
        if token_code in (401,403,404):
            return result('warn','无法匿名确认镜像，可能是私有包或没有访问权限。','用部署用户实际使用的 Docker 登录验证；不能据此断言镜像不存在。')
        if token_code!=200:return result('fail',f'匿名 token 接口 HTTP {token_code}。','核对 registry 及认证端点的网络。')
        data=json.loads(token_body)
        token=data.get('token',data.get('access_token')) if isinstance(data,dict) else None
        if not isinstance(token,str) or not token or len(token)>16384:raise ValueError('Invalid token response')
        headers['Authorization']='Bearer '+token
        code,_,body=http_read(url,headers)
    if code in (401,403):
        return result('warn','镜像需要访问权限，匿名检查未通过。','此项未验证服务器已保存的 GHCR 登录；请在服务器终端核对。')
    if code==404:return result('fail','Registry 返回 HTTP 404，未找到该清单。','核对已发布的镜像名称、tag/digest 和访问权限。')
    if code==200:
        manifest=json.loads(body)
        if isinstance(manifest,dict) and manifest.get('schemaVersion')==2 and (isinstance(manifest.get('manifests'),list) or isinstance(manifest.get('layers'),list)):
            return result('pass','匿名读取镜像清单成功。','只验证元数据可读；不下载镜像层，不证明完整拉取、平台匹配或启动成功。')
    return result('fail',f'镜像清单 HTTP {code} 或格式异常。','检查版本与 registry 连通性；429 时稍后重试。')


def probe_dns(domain):
    if not DOMAIN.fullmatch(domain):raise ValueError('Invalid configured domain')
    addresses=sorted({entry[4][0] for entry in socket.getaddrinfo(domain,80,type=socket.SOCK_STREAM)})
    addresses=[str(ipaddress.ip_address(a)) for a in addresses]
    return result('pass','系统 DNS 解析成功。',addresses=addresses)


def isolated_probe(kind, value):
    try:
        call=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--probe',kind,value],
                            capture_output=True,timeout=10,check=False)
        if call.returncode!=0 or len(call.stdout)>8192:raise ValueError('Invalid probe output')
        data=json.loads(call.stdout)
        if not isinstance(data,dict) or data.get('status') not in STATUSES:raise ValueError('Invalid probe result')
        return data
    except subprocess.TimeoutExpired:
        return result('fail','检查超过 10 秒，已停止本次检查子进程。','检查 DNS 与服务器出网，稍后单项重试。')
    except (OSError,ValueError):
        return result('fail','检查进程未返回有效结果。','查看管理服务状态；不会执行部署操作。')


def storage_result(path):
    usage=shutil.disk_usage(path)
    fraction=usage.free/usage.total if usage.total else 0
    low=usage.free<1024**3 or fraction<.1
    return result('warn' if low else 'pass',f'可用 {usage.free/1024**3:.2f} GiB / 总计 {usage.total/1024**3:.2f} GiB。',
                  '提示阈值：不足 1 GiB 或 10%；需另外估算镜像、数据库及备份容量。',free_bytes=usage.free,total_bytes=usage.total)


class BusyCheck(Exception):pass


class Preflight:
    def __init__(self, engine, config, state, reports, host_root, sites, projects):
        self.engine,self.config,self.state,self.reports=engine,Path(config),Path(state),Path(reports)
        self.host_root,self.sites,self.projects=host_root,sites,projects
        self.lock=threading.Lock()
        self.results={}

    def host_report(self):
        file=self.reports/'deployment-host.json'
        try:
            if file.is_symlink() or file.stat().st_size>128*1024:raise ValueError('Invalid host report')
            data=json.loads(file.read_text())
            if not isinstance(data,dict) or data.get('schema')!=1 or data.get('root')!=self.host_root:raise ValueError('Wrong host report identity')
            stamp=datetime.fromisoformat(data['at'])
            age=(datetime.now(timezone.utc)-stamp).total_seconds()
            if age< -300 or age>900:raise ValueError('Host report expired')
            revision=os.getenv('ADMIN_DEPLOY_REVISION','development')
            if revision not in ('development','unknown') and data.get('revision')!=revision:raise ValueError('Host report revision differs')
            return data
        except (OSError,ValueError,TypeError,KeyError):return None

    def catalog(self):
        items=[{'key':'docker-engine','title':'Docker Engine','group':'环境'},
               {'key':'admin-storage','title':'Admin 数据所在文件系统','group':'环境'},
               {'key':'config-permissions','title':'配置文件读取与权限','group':'环境'},
               {'key':'docker-ports','title':'Docker 发布的入口端口','group':'环境'}]
        items += [{'key':k,'title':v,'group':'宿主机报告'} for k,v in HOST_CHECKS.items()]
        items += [{'key':k,'title':v[0],'group':'网络','target':v[1]} for k,v in NETWORKS.items()]
        env=read_env(self.config/'root.env')
        images={env.get('NGINX_IMAGE','nginx:alpine'),env.get('CERTBOT_IMAGE','certbot/certbot:latest')}
        for site in self.sites():
            if not site['enabled']:continue
            if site.get('kind','static')=='static':images.add(site.get('image',''))
            items.append({'key':'dns-'+site['key'],'title':site['domain'],'group':'DNS','domain':site['domain']})
        for project in self.projects():
            values=read_env(self.config/'instances'/project/'.env')
            images.update(values.get(k,'') for k in ('POWERX_BACKEND_IMAGE','POWERX_WEB_IMAGE'))
            images.update((values.get('POSTGRES_IMAGE','pgvector/pgvector:pg16'),values.get('REDIS_IMAGE','redis:7-alpine')))
        report=self.host_report()
        if report and isinstance(report.get('images'),list):images.update(i for i in report['images'] if isinstance(i,str))
        invalid=False
        for image in sorted(images):
            try:image_parts(image)
            except ValueError:invalid=True;continue
            items.append({'key':'image-'+hashlib.sha256(image.encode()).hexdigest()[:16],'title':image,'group':'镜像','image':image})
        if invalid:items.append({'key':'invalid-images','title':'配置的镜像引用格式','group':'镜像'})
        if len(items)>MAX_ITEMS:raise ValueError('Too many deployment checks')
        return items

    def listing(self):
        items=self.catalog();stamp=datetime.now(timezone.utc)
        for item in items:
            saved=self.results.get(item['key'])
            if saved:
                changed=saved['signature']!=self.signature(item)
                item['result']=dict(saved['value'])
                source_at=item['result'].get('source_at',item['result']['at'])
                item['result']['stale']=(stamp-datetime.fromisoformat(source_at)).total_seconds()>900 or changed
        return {'at':now(),'items':items,'scope':'当前已启用站点、登记实例和宿主机报告中的配置；外网检查从 Admin 容器发起。',
                'host_command':'sh scripts/preflight.sh --save'}

    def signature(self,item):
        inputs=dict(item)
        env=read_env(self.config/'root.env')
        if item['key'].startswith('dns-'):inputs['expected_ips']=env.get('PREFLIGHT_EXPECTED_IPS','')
        if item['key']=='docker-ports':inputs['ports']={k:env.get(k,'') for k in ('HTTP_PORT','HTTP_BIND','HTTPS_PORT','HTTPS_BIND')}
        if item['key'] in HOST_CHECKS:inputs['report_at']=(self.host_report() or {}).get('at')
        return hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()

    def run(self,key):
        if not self.lock.acquire(blocking=False):raise BusyCheck('Another deployment check is running')
        try:
            item=next((i for i in self.catalog() if i['key']==key),None)
            if item is None:raise KeyError(key)
            signature=self.signature(item)
            started=now()
            try:data=self.check(item)
            except Exception as error:
                # No raw exceptions, environment assignments, registry tokens or Docker credential files.
                data=result('fail','检查未完成（'+type(error).__name__+'）。','核对服务和配置，稍后重新检查。')
            saved={**data,'at':now(),'started_at':started,'stale':False}
            valid_keys={i['key'] for i in self.catalog()}
            self.results={k:v for k,v in self.results.items() if k in valid_keys}
            self.results[key]={'signature':signature,'value':saved}
            return {'key':key,'result':saved}
        finally:self.lock.release()

    def check(self,item):
        key=item['key']
        if key in NETWORKS:return isolated_probe('network',key)
        if key.startswith('dns-'):
            data=isolated_probe('dns',item['domain'])
            if data['status']!='pass':return data
            expected=[]
            for text in read_env(self.config/'root.env').get('PREFLIGHT_EXPECTED_IPS','').split():
                try:expected.append(str(ipaddress.ip_address(text)))
                except ValueError:return result('warn','PREFLIGHT_EXPECTED_IPS 存在无效地址。','在本地修改配置并按授权发布；网页不改 DNS。',addresses=data['addresses'])
            if not expected:return result('warn','解析成功，但未配置目标服务器 IP，尚未核对指向。','在根 .env 配置 PREFLIGHT_EXPECTED_IPS；包含计划使用的 IPv4/IPv6。',addresses=data['addresses'])
            unexpected=sorted(set(data['addresses'])-set(expected))
            if not data['addresses'] or unexpected:
                return result('fail','DNS 返回未登记的地址，可能仍指向其他服务器。','核对 A、AAAA 和 CNAME；系统解析结果不代替外部传播和 HTTP-01 验证。',addresses=data['addresses'],expected=expected)
            return result('pass','系统 DNS 返回地址与目标清单一致。','还需外部确认 DNS 传播与公网 80/443 可访问。',addresses=data['addresses'],expected=expected)
        if key=='docker-engine':
            version=self.engine.request('GET','/version',timeout=4)
            if not isinstance(version,dict) or not isinstance(version.get('Version'),str) or not version['Version']:
                return result('fail','Docker API 未返回有效版本。','核对 Docker socket 与 Engine 状态。')
            return result('pass','Docker '+str(version['Version'])+' · '+str(version.get('Os',''))+'/'+str(version.get('Arch','')),
                          'API 可访问不代表 Compose 配置、镜像或业务启动已验收。')
        if key=='admin-storage':
            data=storage_result(self.state);data['advice']+=' 此项只检查 Admin 数据挂载所在文件系统，不代表 DockerRootDir。';return data
        if key=='config-permissions':
            paths=[self.config/'root.env']
            paths += [self.config/'sites'/s['key']/'.env' for s in self.sites() if s['enabled']]
            paths += [self.config/'instances'/p/'.env' for p in self.projects()]
            unsafe=[];missing=[]
            for path in paths:
                if not path.is_file() or not os.access(path,os.R_OK):missing.append(str(path.relative_to(self.config)))
                elif path.is_symlink() or path.stat().st_mode & 0o077:unsafe.append(str(path.relative_to(self.config)))
            if missing:return result('fail','无法读取：'+', '.join(missing),'检查初始化与挂载；只验证 Admin 当前身份读取，不测试宿主机写入。')
            if unsafe:return result('warn','私有配置权限过宽或使用符号链接：'+', '.join(unsafe),'核对文件所有者和 600 权限；网页不会修改权限。')
            return result('pass','当前 Admin 身份可读取检查范围内的私有 .env，组/其他用户无权限。','宿主机写入权限及数据库容器 UID 请另行核验。')
        if key in HOST_CHECKS:
            report=self.host_report()
            if not report:return result('unknown','缺少有效宿主机报告（可能过期、路径或提交不匹配）。','在当前服务器仓库执行 sh scripts/preflight.sh --save，再单项检查；有效期 15 分钟。')
            rows=report.get('checks',{})
            row=rows.get(key) if isinstance(rows,dict) else None
            if not isinstance(row,dict) or row.get('status') not in STATUSES:return result('unknown','宿主机报告缺少该项。','重新生成当前版本报告。')
            # Only accept defined fields, never return an arbitrary uploaded report payload.
            return result(row['status'],str(row.get('detail',''))[:1000],str(row.get('advice',''))[:1000],source_at=report['at'],source='宿主机报告')
        if key=='docker-ports':return self.ports()
        if key=='invalid-images':return result('fail','存在空占位或不支持的镜像引用，未回显原值。','核对镜像配置，使用已发布的 name:tag 或 name@sha256:digest。')
        if key.startswith('image-'):
            image=item['image'];cached=False
            try:
                local=self.engine.request('GET','/images/'+quote(image,safe='')+'/json',timeout=4)
                cached=bool(local.get('Id'))
            except Exception:pass
            remote=isolated_probe('manifest',image)
            remote['local_cached']=cached
            remote['detail']=('本机已有镜像缓存；' if cached else '未确认本机镜像缓存；')+remote['detail']
            remote['advice']+=' 网页不会 pull；私有包须在实际部署用户/ sudo 上下文验证认证。'
            return remote
        raise KeyError(key)

    def ports(self):
        env=read_env(self.config/'root.env');wanted={}
        for kind,default in (('HTTP',80),('HTTPS',443)):
            try:port=int(env.get(kind+'_PORT',default))
            except ValueError:return result('fail','入口端口配置不是整数。','核对根 .env 的 HTTP_PORT/HTTPS_PORT。')
            if not 1<=port<=65535:return result('fail','入口端口超出范围。','端口须为 1–65535。')
            wanted[port]=env.get(kind+'_BIND','0.0.0.0')
        containers=self.engine.request('GET','/containers/json?all=false',timeout=4)
        conflicts=[];owned=[]
        for c in containers:
            labels=c.get('Labels',{})
            for binding in c.get('Ports',[]):
                port=binding.get('PublicPort');ip=binding.get('IP','0.0.0.0')
                if port not in wanted or binding.get('Type','tcp')!='tcp':continue
                if wanted[port] not in ('0.0.0.0','::') and ip not in ('0.0.0.0','::',wanted[port]):continue
                if labels.get('com.docker.compose.project')=='xdocker-web-hosting' and labels.get('com.docker.compose.service')=='nginx':owned.append(port)
                else:conflicts.append(port)
        if conflicts:return result('fail','其他 Docker 容器发布了目标端口：'+', '.join(map(str,sorted(set(conflicts)))),'人工核对占用方；不会停止任何容器。')
        return result('pass','未发现其他 Docker 容器占用目标端口；XDocker Nginx 发布：'+(', '.join(map(str,sorted(set(owned)))) or '无'),
                      '只检查 Docker 发布信息；非 Docker 进程与公网防火墙需查看宿主机报告及外部验证。')


def probe_main(kind,value):
    try:
        if kind=='network':data=probe_network(value)
        elif kind=='dns':data=probe_dns(value)
        elif kind=='manifest':data=probe_manifest(value)
        else:raise ValueError('Unsupported probe')
    except (TimeoutError,socket.timeout):data=result('fail','网络请求超时。','核对 DNS、出网、防火墙或代理；稍后重试。')
    except ssl.SSLError:data=result('fail','TLS 证书验证失败。','核对系统时间与可信 CA；不关闭证书验证。')
    except socket.gaierror:data=result('fail','DNS 解析失败。','检查系统 DNS 和域名记录。')
    except urllib.error.URLError as error:
        detail='TLS 证书验证失败。' if isinstance(error.reason,ssl.SSLError) else '网络请求超时。' if isinstance(error.reason,(TimeoutError,socket.timeout)) else '网络连接失败。'
        data=result('fail',detail,'核对服务器出网、DNS 与目标服务。')
    except Exception:data=result('fail','目标未返回预期结果。','核对配置与服务；原始响应和凭据不回显。')
    print(json.dumps(data,ensure_ascii=False))


if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--probe':probe_main(sys.argv[2],sys.argv[3])
    else:sys.exit(2)
