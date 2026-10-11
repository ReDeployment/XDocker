"""Read-only host checks; --save writes only the fixed XDocker diagnostic report."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/admin'))
from preflight import HOST_CHECKS, image_parts, now, read_env, result, storage_result


def command(args,root):
    try:
        call=subprocess.run(args,cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,timeout=10,check=False)
        if len(call.stdout)>128*1024:return None,''
        return call.returncode,call.stdout.decode('utf-8',errors='replace').strip()
    except (OSError,subprocess.TimeoutExpired):return None,''


def collect(root):
    root=Path(root).resolve();env=read_env(root/'.env');checks={};images=set()
    code,version=command(['docker','compose','version','--short'],root)
    failures=[]
    targets=[['sh','scripts/hosting.sh','compose']]
    for file in sorted((root/'instances').glob('*/instance.json')):
        try:
            data=json.loads(file.read_text())
            if data.get('app')=='powerx' and data.get('enabled') is True and data.get('project')==file.parent.name and re.fullmatch(r'[a-z0-9][a-z0-9-]*',file.parent.name):
                targets.append(['sh','scripts/apps.sh','compose',file.parent.name])
        except (OSError,ValueError,AttributeError):failures.append('实例登记格式')
    if code!=0 or not re.fullmatch(r'v?\d+(?:\.\d+)+(?:[-+][A-Za-z0-9_.-]+)?',version):
        checks['host-compose']=result('fail','Docker Compose version 未返回有效版本。','先核对 Docker/Compose 安装与当前用户的 Docker socket 权限。')
    else:
        for target in targets:
            quiet,_=command(target+['config','--quiet'],root)
            code,output=command(target+['config','--images'],root)
            if quiet!=0 or code!=0:failures.append('共享入口' if len(target)==3 else target[3]);continue
            for image in output.splitlines():
                try:image_parts(image)
                except ValueError:failures.append('镜像引用格式');continue
                images.add(image)
        checks['host-compose']=result('fail' if failures else 'pass','Compose '+version+'；'+('配置未通过：'+', '.join(sorted(set(failures))) if failures else f'{len(targets)} 组部署配置静态检查通过。'),
                                      '仅 config --quiet/--images，不创建容器、不打印展开的配置或原始错误；失败请在服务器终端核对对应配置。')
    storage=[];warning=False
    for title,path in [('部署目录',root)]:
        row=storage_result(path);storage.append(title+'：'+row['detail']);warning |= row['status']!='pass'
    code,docker_root=command(['docker','info','--format','{{json .DockerRootDir}}'],root)
    try:
        path=json.loads(docker_root)
        if code!=0 or not isinstance(path,str) or not Path(path).is_absolute():raise ValueError('Unknown DockerRootDir')
        row=storage_result(path);storage.append('DockerRootDir：'+row['detail']);warning |= row['status']!='pass'
    except (OSError,ValueError):storage.append('DockerRootDir 文件系统未检查。');warning=True
    checks['host-storage']=result('warn' if warning else 'pass',' '.join(storage),
                                 '提示阈值：不足 1 GiB 或 10%；不自动清理镜像、卷或备份。')
    missing=[]
    for relative in ('.','data','data/events','data/instance-backups'):
        path=root/relative
        if not path.is_dir() or not os.access(path,os.R_OK|os.W_OK|os.X_OK):missing.append(relative)
    if not (root/'.env').is_file() or not os.access(root/'.env',os.R_OK):missing.append('.env（读取）')
    checks['host-permissions']=result('fail' if missing else 'warn' if os.geteuid()==0 else 'pass',
        f'检查身份 UID={os.geteuid()}；'+('未满足访问要求：'+', '.join(missing) if missing else '部署目录、报告/备份目录可访问。'),
        '按实际部署用户运行；root 的访问权限不证明 ubuntu 或数据库容器的访问权限。不会 chmod/chown 部署目录。')
    ports=[];port_warning=False
    for kind,default in (('HTTP',80),('HTTPS',443)):
        try:
            port=int(env.get(kind+'_PORT',default));bind=env.get(kind+'_BIND','0.0.0.0')
            if not 1<=port<=65535:raise ValueError('Invalid port')
            address=ipaddress.ip_address(bind)
            with socket.socket(socket.AF_INET6 if address.version==6 else socket.AF_INET) as listener:
                listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                try:
                    listener.bind((bind,port));ports.append(kind+' '+str(port)+'：当前可绑定。')
                except OSError:
                    ports.append(kind+' '+str(port)+'：存在监听或当前身份无法绑定。');port_warning=True
        except ValueError:ports.append(kind+'：地址/端口配置无效。');port_warning=True
    checks['host-ports']=result('warn' if port_warning else 'pass',' '.join(ports),
                               '临时 bind 后立即关闭，不 listen；有监听时结合网页 Docker 发布信息核对归属。未检查公网防火墙或入站可达性。')
    code,revision=command(['git','rev-parse','HEAD'],root)
    if code!=0 or not re.fullmatch(r'[a-f0-9]{40}',revision):revision='unknown'
    if len(images)>80:
        images=set();checks['host-compose']=result('fail','配置镜像数量超过检查上限。','请缩小此次检查范围；未保存不完整的镜像清单。')
    return {'schema':1,'at':now(),'root':str(root),'revision':revision,'checks':checks,'images':sorted(images)}


def save(root,report):
    root=Path(root).resolve();directory=root/'data/events';path=directory/'deployment-host.json'
    for candidate in (root/'data',directory,path):
        if candidate.is_symlink():raise ValueError('Refusing report symlink')
    directory.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.deployment-host.',dir=directory)
    try:
        with os.fdopen(fd,'w') as stream:
            if os.geteuid()==0 and (root/'.env').is_file():
                owner=(root/'.env').stat();os.fchown(stream.fileno(),owner.st_uid,owner.st_gid)
            json.dump(report,stream,ensure_ascii=False)
        Path(name).replace(path)
    finally:Path(name).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--save',action='store_true',help='save a private report for the admin page')
    args=parser.parse_args();report=collect(ROOT)
    if args.save:save(ROOT,report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError):
        print('宿主机检查失败：请核对部署目录和报告权限；未执行部署或修改现有配置。',file=sys.stderr)
        sys.exit(1)
