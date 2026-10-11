"""Private instance backup plans, conservative due slots and an opt-in coordinator."""
from contextlib import contextmanager
from datetime import datetime, time as daytime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

KEY=re.compile(r'^[a-z0-9][a-z0-9-]*$')
ZONES={'UTC':timezone.utc,'Asia/Shanghai':timezone(timedelta(hours=8),name='Asia/Shanghai')}
CONFIG_FIELDS={'enabled','frequency','weekday','time','timezone','grace_minutes','keep','auto_prune'}
RUN_STATES={'running','success','failed','interrupted'}


def stamp(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def defaults():
    return {'enabled':False,'frequency':'daily','weekday':6,'time':'03:00','timezone':'Asia/Shanghai',
            'grace_minutes':5,'keep':7,'auto_prune':False}


def validate(value):
    if not isinstance(value,dict) or set(value)!=CONFIG_FIELDS:raise ValueError('计划字段不完整或包含未知字段。')
    if type(value['enabled']) is not bool or type(value['auto_prune']) is not bool:raise ValueError('启用及清理字段必须为布尔值。')
    if value['frequency'] not in ('daily','weekly') or value['timezone'] not in ZONES:raise ValueError('请选择每天/每周及 UTC 或上海时间。')
    if not isinstance(value['time'],str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',value['time']):raise ValueError('时间必须为 HH:MM。')
    for key,low,high in (('weekday',0,6),('grace_minutes',1,30),('keep',1,100)):
        if type(value[key]) is not int or not low<=value[key]<=high:raise ValueError('星期、允许迟到分钟或保留份数超出范围。')
    return dict(value)


def next_slot(plan,after):
    """Strictly future, so enabling/restarting never immediately backfills an old slot."""
    local=after.astimezone(ZONES[plan['timezone']]);hour,minute=map(int,plan['time'].split(':'))
    for offset in range(8):
        date=local.date()+timedelta(days=offset)
        if plan['frequency']=='weekly' and date.weekday()!=plan['weekday']:continue
        candidate=datetime.combine(date,daytime(hour,minute),tzinfo=local.tzinfo)
        if candidate>local:return candidate.astimezone(timezone.utc)
    raise ValueError('无法计算下一计划时间。')


class PlanConflict(Exception):pass
class OperationBusy(Exception):pass
class PlanCancelled(Exception):pass


class BackupPlans:
    def __init__(self,state,allowed):
        self.root=Path(state);self.allowed=allowed;self.lock=threading.RLock()

    @contextmanager
    def store(self):
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=self.root/'backup-plans.lock'
        if path.is_symlink():raise ValueError('拒绝符号链接计划锁文件。')
        with self.lock,path.open('a') as lock:
            os.fchmod(lock.fileno(),0o600);fcntl.flock(lock,fcntl.LOCK_EX)
            file=self.root/'backup-plans.json'
            if file.is_symlink():raise ValueError('拒绝符号链接计划文件。')
            if file.exists():
                if file.stat().st_size>1024*1024:raise ValueError('计划文件超出大小限制。')
                data=json.loads(file.read_text())
                if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('plans'),dict):raise ValueError('计划文件格式不正确；不会覆盖原文件。')
            else:data={'version':1,'plans':{}}
            yield data

    def save_file(self,data):
        fd,name=tempfile.mkstemp(prefix='.backup-plans.',dir=self.root)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(data,stream,ensure_ascii=False,indent=2)
                stream.flush();os.fsync(stream.fileno())
            Path(name).replace(self.root/'backup-plans.json')
            directory=os.open(self.root,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:Path(name).unlink(missing_ok=True)

    def read_plan(self,instance,raw):
        if not isinstance(raw,dict):raise ValueError('计划记录格式不正确。')
        plan=validate({key:raw.get(key) for key in CONFIG_FIELDS})
        revision=raw.get('revision')
        if not isinstance(revision,str) or not re.fullmatch(r'[a-f0-9]{32}',revision):raise ValueError('计划版本不正确。')
        row={**plan,'instance':instance,'revision':revision,'updated_at':raw.get('updated_at'),'next_run_at':raw.get('next_run_at')}
        if row['next_run_at'] is not None:
            value=datetime.fromisoformat(row['next_run_at'])
            if value.tzinfo is None:raise ValueError('计划时间缺少时区。')
        if plan['enabled'] and row['next_run_at'] is None:raise ValueError('启用计划缺少下一次时间。')
        for name in ('last_run','last_skip'):
            value=raw.get(name)
            if value is not None:
                if not isinstance(value,dict):raise ValueError('计划结果格式不正确。')
                if name=='last_run':
                    if value.get('state') not in RUN_STATES or not isinstance(value.get('job_id'),str) or not re.fullmatch(r'[a-f0-9]{32}',value['job_id']):raise ValueError('任务状态或身份不正确。')
                    if value.get('backup_state') not in ('pending','ready','failed') or value.get('cleanup_state') not in ('pending','success','failed','disabled','skipped'):raise ValueError('任务阶段不正确。')
                    if value.get('reason') not in (None,'admin-restarted'):raise ValueError('任务原因不正确。')
                elif value.get('state')!='skipped' or value.get('reason')!='window-expired':raise ValueError('跳过状态不正确。')
                # Private JSON does not become an arbitrary response payload.
                row[name]={key:value[key] for key in ('state','slot','job_id','started_at','completed_at','reason','backup_state','cleanup_state','at') if key in value}
        return row

    def listing(self):
        allowed=sorted(self.allowed())
        with self.store() as data:
            rows=[]
            for instance in allowed:
                if not KEY.fullmatch(instance):continue
                raw=data['plans'].get(instance)
                if raw is None:rows.append({'instance':instance,**defaults(),'revision':'','next_run_at':None,'updated_at':None})
                else:rows.append(self.read_plan(instance,raw))
            return rows

    def update(self,instance,config,revision,at=None):
        if instance not in self.allowed() or not KEY.fullmatch(instance):raise KeyError(instance)
        config=validate(config);at=at or datetime.now(timezone.utc)
        if not isinstance(revision,str):raise ValueError('缺少计划版本，请重新读取。')
        with self.store() as data:
            old=data['plans'].get(instance)
            current=self.read_plan(instance,old) if old else None
            if revision!=(current['revision'] if current else ''):raise PlanConflict('计划已被其他页面修改，请重新读取后再确认。')
            row={'instance':instance,**config,'revision':secrets.token_hex(16),'updated_at':stamp(at),
                 'next_run_at':stamp(next_slot(config,at)) if config['enabled'] else None}
            if current:
                for name in ('last_run','last_skip'):
                    if name in current:row[name]=current[name]
            data['plans'][instance]=row;self.save_file(data)
            return row

    def disable(self,instance,revision,at=None):
        row=next((p for p in self.listing() if p['instance']==instance),None)
        if row is None:raise KeyError(instance)
        config={k:row[k] for k in CONFIG_FIELDS};config['enabled']=False
        return self.update(instance,config,revision,at)

    def claim(self,instance,revision,slot,identifier,at):
        """Durably consume the slot before the worker can stop an application."""
        with self.store() as data:
            if instance not in self.allowed():return False
            row=self.read_plan(instance,data['plans'].get(instance))
            if not row['enabled'] or row['revision']!=revision or row['next_run_at']!=slot:return False
            due=datetime.fromisoformat(slot)
            if at<due or at>due+timedelta(minutes=row['grace_minutes']):return False
            if row.get('last_run',{}).get('state')=='running':return False
            row['next_run_at']=stamp(next_slot(row,at))
            row['last_run']={'state':'running','slot':slot,'job_id':identifier,'started_at':stamp(at),'backup_state':'pending','cleanup_state':'pending' if row['auto_prune'] else 'disabled'}
            data['plans'][instance]=row;self.save_file(data);return True

    def stage(self,instance,identifier,**fields):
        if not set(fields)<={'backup_state','cleanup_state'}:raise ValueError('未知备份阶段。')
        with self.store() as data:
            row=data['plans'].get(instance)
            if row and row.get('last_run',{}).get('job_id')==identifier:
                row['last_run'].update(fields);self.save_file(data)

    def finish(self,instance,identifier,status,at=None):
        if status not in ('success','failed'):raise ValueError('未知计划结果。')
        with self.store() as data:
            row=data['plans'].get(instance)
            if row and row.get('last_run',{}).get('job_id')==identifier:
                if status=='failed':
                    if row['last_run'].get('backup_state')=='pending':row['last_run']['backup_state']='failed'
                    if row['last_run'].get('cleanup_state')=='pending':row['last_run']['cleanup_state']='skipped'
                row['last_run'].update(state=status,completed_at=stamp(at));self.save_file(data)

    def skip(self,instance,revision,slot,at,reason='window-expired'):
        with self.store() as data:
            row=self.read_plan(instance,data['plans'].get(instance))
            if not row['enabled'] or row['revision']!=revision or row['next_run_at']!=slot:return False
            row['next_run_at']=stamp(next_slot(row,at))
            row['last_skip']={'state':'skipped','slot':slot,'at':stamp(at),'reason':reason}
            data['plans'][instance]=row;self.save_file(data);return True

    def recover(self):
        # Existing application/helper recovery happens in app.py before this metadata recovery.
        with self.store() as data:
            changed=False
            for instance,row in data['plans'].items():
                if instance not in self.allowed():continue
                self.read_plan(instance,row)
                if row.get('last_run',{}).get('state')=='running':
                    row['last_run'].update(state='interrupted',completed_at=stamp(),reason='admin-restarted');changed=True
            if changed:self.save_file(data)


class BackupScheduler:
    def __init__(self,plans,submit,enabled=False,audit=None,clock=None):
        self.plans,self.submit,self.enabled=plans,submit,enabled
        self.audit=audit or (lambda *_:None)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.stop_event=threading.Event();self.worker=None;self.lease=None;self.error=None

    def tick(self,at=None):
        if not self.enabled:return
        at=at or self.clock()
        for plan in self.plans.listing():
            if not plan['enabled']:continue
            due=datetime.fromisoformat(plan['next_run_at'])
            if at<due:continue
            if at>due+timedelta(minutes=plan['grace_minutes']):
                if self.plans.skip(plan['instance'],plan['revision'],plan['next_run_at'],at):
                    self.audit('backup-scheduled-skipped',plan['instance'],'window-expired')
                continue
            try:self.submit(plan,at)
            except OperationBusy:continue  # Retry only admission while inside the start window.
            except PlanCancelled:continue

    def start(self):
        if not self.enabled or self.worker is not None:return
        path=self.plans.root/'backup-scheduler.lock'
        if path.is_symlink():raise ValueError('拒绝符号链接调度锁。')
        self.lease=path.open('a');os.fchmod(self.lease.fileno(),0o600)
        try:fcntl.flock(self.lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close();self.lease=None;self.error='已有调度进程，当前进程不启动重复调度。';return
        def loop():
            while not self.stop_event.is_set():
                try:self.tick();self.error=None
                except Exception:self.error='备份计划检查失败，请核对计划文件和管理服务状态。'
                self.stop_event.wait(30)
        self.worker=threading.Thread(target=loop,daemon=True,name='xdocker-backup-scheduler');self.worker.start()

    def stop(self):
        self.stop_event.set()
        if self.worker:self.worker.join(timeout=2)
        if self.lease and (not self.worker or not self.worker.is_alive()):self.lease.close();self.lease=None
