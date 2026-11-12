'use strict';
const codeSyncUI={data:null,error:'',loading:false,busy:false,view:'sources',selected:'',editor:null,drafts:{}};
const codeSyncViews={sources:'源码仓库',schedules:'上传计划',devices:'目标设备',targets:'分发策略',records:'执行记录'};
const syncMessages={XD_SYNC_EXECUTION_DISABLED:'执行尚未开启，请检查本机授权及项目开关。',XD_SYNC_REVISION_CONFLICT:'配置已被修改，请重新读取后保存。',XD_SYNC_SOURCE_NOT_DISCOVERED:'目录、仓库或当前分支与发现结果不一致，请刷新后选择。',XD_SYNC_PATH_NOT_GRANTED:'目录尚未在本机源码范围中授权。',XD_SYNC_CREDENTIAL_REQUIRED:'请选择已配置的 GitHub 凭据。',XD_SYNC_CONFIRMATION_REQUIRED:'启用或执行前请输入完整项目名称。',XD_SYNC_TARGET_DIRTY:'副本存在本地修改，本次没有覆盖。',XD_SYNC_TARGET_DIRTY_OR_RESTART_NOT_GRANTED:'副本有修改，或目标尚未授权服务重启。',XD_SYNC_TARGET_GRANT_MISMATCH:'设备端授权的目录、仓库或分支与本页不一致。',XD_SYNC_TARGET_GRANT_CHANGED:'设备端授权已变化，请核对并重新保存分发策略。',XD_SYNC_AGENT_VERSION_MISMATCH:'设备端 Agent 版本与本机不一致，请先更新设备上的 XDocker Agent。',XD_SYNC_HOST_KEY_REQUIRED:'请填写设备已核对的完整 SSH 公钥，例如 ssh-ed25519 AAAA…',XD_SYNC_IDENTITY_NOT_GRANTED:'本机尚未授权此 SSH 身份，请按连接指南登记密钥引用。',XD_SYNC_BACKUP_BRANCH_REQUIRED:'上传目的分支必须位于 xdocker-backup/ 下。',XD_SYNC_SOURCE_BUSY_OR_UNCONFIRMED:'该项目有进行中或待核对任务，请先处理执行记录。',XD_SYNC_SOURCE_CHANGED:'生成快照期间代码发生变化，本次未上传，请重新预览。',XD_SYNC_GIT_REJECTED:'Git 操作被拒绝，请检查仓库权限、分支状态和网络。',XD_SYNC_UPLOAD_UNCONFIRMED:'上传结果未确认，先核对 GitHub 提交，勿直接重放任务。',XD_SYNC_HISTORY_CAPACITY:'执行记录已满，请先归档已结束记录。',XD_SYNC_NO_CONFIRMED_UPLOAD:'尚无核对通过的上传提交，请先运行上传。',XD_SYNC_UNMANAGED_BACKUP_BRANCH:'远端备份分支已有其他来源历史，本次未写入。',XD_SYNC_TRANSPORT_REJECTED:'设备连接失败，请检查 VPN、SSH 身份、主机公钥和 Agent 安装。',XD_SYNC_AGENT_RESPONSE_INVALID:'Agent 未返回有效协议响应，请检查版本与设备授权。',XD_SYNC_FOLLOWUP_CLOSED_BY_OPERATOR:'已结束自动跟进，原操作结果仍未确认。'};
const syncText=v=>syncMessages[v]||v;
const syncButton=(label,data,disabled=false)=>`<button type="button" ${Object.entries(data).map(([k,v])=>`data-sync-${k}="${esc(v)}"`).join(' ')} ${disabled||codeSyncUI.busy||(codeSyncUI.data?.configuration_writable===false&&!('refresh' in data||'job' in data||'dismiss' in data))?'disabled':''}>${esc(label)}</button>`;
function resetCodeSync(){codeSyncUI.data=null;codeSyncUI.error='';codeSyncUI.drafts={};codeSyncUI.editor=null;$('sync-dialog')?.remove();}
async function loadCodeSync(){
 if(!loggedIn||codeSyncUI.loading)return;codeSyncUI.loading=true;
 try{const d=await api('code-sync');if(loggedIn){codeSyncUI.data=d;codeSyncUI.error='';}}
 catch(e){codeSyncUI.error=e.message;}
 finally{codeSyncUI.loading=false;if(loggedIn&&current==='code-sync')renderCodeSync();}
}
function syncSource(id){return codeSyncUI.data?.sources.find(s=>s.id===id);}
function renderCodeSync(){
 const route=location.hash.slice(1).split('/')[1];if(codeSyncViews[route])codeSyncUI.view=route;
 const d=codeSyncUI.data,v=codeSyncUI.view,s=codeSyncUI.selected,filtered=rows=>rows.filter(r=>!s||r.source===s||r.id===s),alerts=[];
 if(codeSyncUI.error)alerts.push(syncText(codeSyncUI.error));
 if(d?.configuration_writable===false)alerts.push('当前为只读镜像；请在服务器管理入口配置代码同步。');
 if(d&&!d.policy.source_roots.length)alerts.push('本机尚未登记源码目录范围，请按代码同步指南授权后发现项目。');
 if(d?.jobs.some(j=>['interrupted','upload-unconfirmed','partial'].includes(j.state)))alerts.push('有上传或设备结果待核对，请到执行记录处理；不会自动重放未知结果。');
 if(d?.archive_error)alerts.push('历史归档受阻：'+syncText(d.archive_error));
 if(d?.schedules.some(r=>r.last_error))alerts.push('有定时任务被阻断，请在上传计划查看原因。');
 let body='<div class="empty">正在读取代码同步配置…</div>';
 if(d){
  if(v==='sources')body=panel('源码仓库','读取项目工作区，在独立备份分支保存代码快照；项目暂存区和当前分支保持原样',table(['项目 / 目录','GitHub / 分支','上传开关','操作'],filtered(d.sources).map(r=>`<tr><td><strong>${esc(r.name)}</strong><small>${esc(r.directory)}</small></td><td>${esc(r.repository)}<small>来源：${esc(r.branch)}</small><small>备份：${esc(r.backup_branch)}</small></td><td>${badge(r.enabled?'已开启':'未开启',r.enabled?'good':'')}</td><td><div class="actions">${syncButton('编辑',{edit:'sources',id:r.id})}${syncButton('预览文件',{preview:r.id})}${syncButton('上传并分发',{run:r.id,kind:'upload'},!r.enabled||!d.execution_enabled)}</div></td></tr>`)),`<div class="actions">${syncButton('GitHub 凭据',{edit:'credentials',id:'new'})}${syncButton('添加源码',{edit:'sources',id:'new'},!d.inventory.length)}</div>`)+`<p class="sync-help">按 workspace 对齐发现 ${d.inventory.length} 个主仓库；自动备份工作树、嵌套文档副本不重复接入。默认排除 .env、密钥、数据库及运行目录，并遵循源仓库的 Git 忽略规则。<a href="/assets/code-sync-guide.html" target="_blank" rel="noreferrer">连接与授权指南 ↗</a></p>`;
  if(v==='schedules')body=panel('每日上传与副本核对','上传任务成功后才分发；也可单独安排副本核对。启用时须确认项目名称',table(['项目 / 类型','时间','下次运行','状态 / 原因','操作'],filtered(d.schedules).map(r=>`<tr><td>${esc(syncSource(r.source)?.name||r.source)}<small>${r.mode==='upload'?'上传并分发':'仅同步已确认提交'}</small></td><td>${String(r.hour).padStart(2,'0')}:${String(r.minute).padStart(2,'0')}<small>${esc(r.timezone)}</small></td><td>${date(r.next_at*1000)}</td><td>${badge(r.enabled?'已启用':'未启用',r.enabled?'good':'')}<small>${esc(syncText(r.last_error||''))}</small></td><td>${syncButton('编辑',{edit:'schedules',id:r.id})}</td></tr>`)),syncButton('新增计划',{edit:'schedules',id:'new'},!d.sources.length));
  if(v==='devices')body=panel('目标设备','通过 VPN 与固定 Agent 通信，固定 SSH 身份和主机公钥；连接测试不更新副本',table(['设备','SSH 地址','状态','操作'],d.devices.map(r=>`<tr><td><strong>${esc(r.name)}</strong><small>${esc(r.id)}</small></td><td>${esc(r.username)}@${esc(r.host)}:${r.port}<small>身份：${esc(r.identity)}</small></td><td>${badge(r.enabled?'允许连接':'未启用',r.enabled?'good':'')}</td><td><div class="actions">${syncButton('编辑',{edit:'devices',id:r.id})}${syncButton('核对 Agent',{device:r.id},!r.enabled)}</div></td></tr>`)),syncButton('添加设备',{edit:'devices',id:'new'}))+`<p class="sync-help">本机可选身份：${esc(d.policy.identities.join('、')||'尚未授权')}。目标设备需安装同一版本 Agent，并在其私有策略中批准副本目录、仓库、备份分支及可选服务；网页不接受远程 Shell 命令。</p>`;
  if(v==='targets')body=panel('项目副本与服务策略','只快进到本次 GitHub 已确认的提交；有本地修改、分支分叉或授权漂移时停止',table(['项目 / 设备','目标目录','同步 / 重启','操作'],filtered(d.targets).map(r=>`<tr><td><strong>${esc(syncSource(r.source)?.name||r.source)}</strong><small>${esc(d.devices.find(x=>x.id===r.device)?.name||r.device)}</small></td><td>${esc(r.directory)}<small>Agent 授权：${esc(r.remote_target)}</small></td><td>${badge(r.enabled?'已启用':'未启用',r.enabled?'good':'')}<small>${r.restart?'更新后重启已授权服务':'只同步副本'}</small></td><td><div class="actions">${syncButton('编辑',{edit:'targets',id:r.id})}${syncButton('检查副本',{target:r.id})}${syncButton('立即同步',{run:r.source,kind:'distribute'},!d.execution_enabled||!syncSource(r.source)?.enabled)}</div></td></tr>`)),syncButton('添加副本',{edit:'targets',id:'new'},!d.sources.length||!d.devices.length));
  if(v==='records')body=panel('执行记录','记录 GitHub 的准确 SHA 和各设备回执；命令返回成功不替代目标 HEAD 与服务状态核对',table(['时间 / 项目','任务 / 提交','状态','操作'],filtered(d.jobs).map(r=>`<tr><td>${date(r.at*1000)}<small>${esc(syncSource(r.source)?.name||r.source)}</small></td><td>${r.kind==='upload'?'上传并分发':'副本同步'}<small class="code-line">${esc(r.sha||'尚无提交')}</small></td><td>${badge(({queued:'排队中',running:'执行中',success:'已核对',partial:'部分副本待核对','upload-unconfirmed':'上传待核对',interrupted:'执行中断','reconcile-queued':'核对排队',failed:'失败','closed-unconfirmed':'未确认 · 已结束跟进'})[r.state]||r.state,r.state==='success'?'good':['partial','upload-unconfirmed','interrupted'].includes(r.state)?'warn':r.state==='failed'?'bad':'')}<small>${esc(syncText(r.error||''))}</small></td><td><div class="actions">${syncButton('详情',{job:r.id})}${['upload-unconfirmed','interrupted'].includes(r.state)&&r.sha?syncButton('核对上传结果',{reconcile:r.id},!d.execution_enabled):''}${['upload-unconfirmed','interrupted','partial'].includes(r.state)?syncButton('结束跟进',{closeJob:r.id}):''}</div></td></tr>`)),syncButton('归档已结束记录',{archive:'yes'}))+`<div class="sync-help"><label>历史归档（${d.archives.length}）<select id="sync-archive-select"><option value="">选择一批归档回执</option>${d.archives.map(name=>`<option value="${esc(name)}">${esc(name.slice(0,16))}…</option>`).join('')}</select></label></div>`;
 }
 const top=`<div class="intro"><div><h2>代码同步</h2><p class="muted">源项目 → GitHub 备份分支 → 目标设备副本。</p></div>${syncButton('刷新',{refresh:'yes'},codeSyncUI.loading)}</div>${d?`<div class="sync-context"><label>当前项目<select id="sync-project"><option value="">全部项目</option>${d.sources.map(r=>`<option value="${esc(r.id)}" ${s===r.id?'selected':''}>${esc(r.name)}</option>`).join('')}</select></label><span>${badge(d.execution_enabled?'执行授权已开启':'执行授权未开启',d.execution_enabled?'good':'warn')} ${badge(d.worker.connected?'调度器在线':'调度器待连接',d.worker.connected?'good':'warn')}</span></div>`:''}${alerts.map(t=>`<div class="sync-alert" role="alert">${esc(t)}</div>`).join('')}`;
 $('content').innerHTML='<div class="sync-workspace">'+top+verticalTabs('code-sync','代码同步分区',Object.entries(codeSyncViews).map(([key,label])=>({key,label})),v,body)+'</div>';
 if(codeSyncUI.lastView!==v){$('view-code-sync-'+v)?.scrollIntoView({block:'nearest',inline:'nearest'});codeSyncUI.lastView=v;if(innerWidth<=720)document.querySelector('.sync-workspace .vertical-tab-list')?.scrollIntoView({block:'start'});}
}
function syncDialog(){let dialog=$('sync-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='sync-dialog';dialog.className='sync-dialog';document.body.append(dialog);dialog.addEventListener('cancel',()=>{syncCapture();const token=$('sync-form')?.elements.token;if(token)token.value='';});}return dialog;}
function syncCapture(){const f=$('sync-form'),e=codeSyncUI.editor;if(!f||!e||e.kind==='credentials')return;const value={...Object.fromEntries(new FormData(f)),__revision:e.revision};for(const box of f.querySelectorAll('input[type=checkbox]'))value[box.name]=box.checked;codeSyncUI.drafts[e.kind+':'+e.id]=value;}
function syncEdit(kind,id='new'){
 const d=codeSyncUI.data,old=d[kind]?.find(x=>x.id===id),selected=codeSyncUI.selected||d.sources[0]?.id||'',defaults={sources:{id:'',name:'',directory:d.inventory[0]?.directory||'',repository:d.inventory[0]?.repository||'',branch:d.inventory[0]?.branch||'',project:d.inventory[0]?.project||'',backup_branch:'xdocker-backup/',includes:'.',excludes:'',credential:'',enabled:false},devices:{id:'',name:'',host:'',port:22,username:'',identity:d.policy.identities[0]||'',host_key:'',enabled:false},targets:{id:'',source:selected,device:d.devices[0]?.id||'',remote_target:selected,directory:'/data/workspace/',restart:false,enabled:false},schedules:{id:'',source:selected,mode:'upload',clock:'02:00',timezone:'Asia/Shanghai',enabled:false},credentials:{id:'',name:'',token:''}};
 const normalized=old?{...old,includes:old.includes?.join('\n'),excludes:old.excludes?.join('\n'),clock:String(old.hour).padStart(2,'0')+':'+String(old.minute).padStart(2,'0')}:defaults[kind];
 const value=codeSyncUI.drafts[kind+':'+id]||normalized;codeSyncUI.editor={kind,id,revision:value.__revision??old?.revision??0};
 const input=(key,label,attrs='')=>`<label>${label}<input name="${key}" value="${esc(value[key]??'')}" ${attrs}></label>`;
 const select=(key,label,items)=>`<label>${label}<select name="${key}">${items.map(([k,label])=>`<option value="${esc(k)}" ${String(value[key]??'')===String(k)?'selected':''}>${esc(label)}</option>`).join('')}</select></label>`;
 const area=(key,label)=>`<label class="sync-wide">${label}<textarea name="${key}" rows="3">${esc(value[key]??'')}</textarea></label>`;
 const check=(key,label)=>`<label class="sync-check"><input type="checkbox" name="${key}" ${value[key]?'checked':''}>${label}</label>`;
 let fields=input('id','标识','required pattern="[a-z0-9][a-z0-9-]{0,63}" '+(old?'readonly':''));
 if(kind==='sources')fields+=input('name','项目名称','required maxlength="80"')+select('directory','已发现的源码目录',d.inventory.map(r=>[r.directory,r.name+' · '+r.directory]))+input('repository','GitHub 仓库（owner/repository）','required')+input('branch','当前来源分支','required')+input('backup_branch','独立备份分支','required')+select('credential','GitHub 写入凭据',[['','尚未配置'],...d.credentials.map(r=>[r.id,r.name])])+area('includes','备份范围，每行一个相对目录或文件；. 表示项目代码')+area('excludes','额外排除规则，每行一个，可使用通配符')+check('enabled','允许手动或定时上传此项目')+input('confirmation','启用时输入完整项目名称','autocomplete="off"');
 if(kind==='credentials')fields+=input('name','凭据名称','required maxlength="80"')+input('token','GitHub 仓库写入令牌','type="password" autocomplete="new-password" required')+'<p class="sync-help">令牌保存在本机私有文件，仅返回凭据名称；不会回显、写入 Git URL 或项目代码。</p>';
 if(kind==='devices')fields+=input('name','设备名称','required maxlength="80"')+input('host','VPN / 内网 IP','required')+input('port','SSH 端口','type="number" min="1" max="65535" required')+input('username','SSH 用户','required')+select('identity','已授权的本机 SSH 身份',d.policy.identities.map(r=>[r,r]))+area('host_key','已核对的 SSH 主机公钥（类型 + Base64）')+check('enabled','允许连接此设备')+'<p class="sync-help">首次需按连接指南安装 Agent 和设备端副本授权；不接受任意远程命令。</p>';
 if(kind==='targets')fields+=select('source','来源项目',d.sources.map(r=>[r.id,r.name]))+select('device','目标设备',d.devices.map(r=>[r.id,r.name]))+input('remote_target','Agent 中已批准的副本标识','required')+input('directory','设备端副本目录（须与授权一致）','required')+check('restart','副本实际更新后重启已授权服务')+check('enabled','核对设备授权并启用此副本')+input('confirmation','启用时输入完整来源项目名称','autocomplete="off"');
 if(kind==='schedules')fields+=select('source','项目',d.sources.map(r=>[r.id,r.name]))+select('mode','每日执行内容',[['upload','上传并分发'],['distribute','仅同步已确认提交']])+input('clock','每天时间','type="time" required')+input('timezone','时区','required')+check('enabled','启用每日计划')+input('confirmation','启用时输入完整项目名称','autocomplete="off"');
 const dialog=syncDialog();dialog.innerHTML=`<form id="sync-form" method="post"><div class="detail-head"><h2>${old?'编辑':'新增'}${esc({sources:'源码仓库',devices:'目标设备',targets:'分发策略',schedules:'每日计划',credentials:'GitHub 凭据'}[kind])}</h2>${syncButton('关闭',{dismiss:'yes'})}</div><div class="sync-form-body"><div class="sync-fields">${fields}</div></div><p id="sync-form-error" class="error" role="alert"></p><div class="dialog-actions">${old?syncButton('重读配置（放弃草稿）',{reload:kind,id}):''}${syncButton('取消',{dismiss:'yes'})}<button type="submit" class="primary">保存配置</button></div></form>`;if(!dialog.open)dialog.showModal();
}
function syncConfirm(kind,id,mode){
 const source=kind==='run'?syncSource(id):syncSource(codeSyncUI.data.jobs.find(j=>j.id===id)?.source);codeSyncUI.editor={kind,id,mode,nonce:crypto.randomUUID()};
 const dialog=syncDialog();dialog.innerHTML=`<form id="sync-operation" method="post"><div class="detail-head"><h2>${kind==='run'?(mode==='upload'?'上传并分发':'同步已确认提交'):'结束跟进'}</h2>${syncButton('关闭',{dismiss:'yes'})}</div><p>${kind==='run'?'只操作此项目已批准的备份分支及目标副本；有冲突时停止。':'保留未确认回执，停止本条任务的自动跟进；不会撤销 GitHub 提交或重放设备操作。'}</p><label>输入完整项目名称 <strong>${esc(source?.name)}</strong><input name="confirmation" required autocomplete="off"></label><p id="sync-form-error" class="error"></p><div class="dialog-actions">${syncButton('取消',{dismiss:'yes'})}<button type="submit" class="primary">确认执行</button></div></form>`;if(!dialog.open)dialog.showModal();
}
document.addEventListener('submit',async event=>{
 if(!['sync-form','sync-operation'].includes(event.target.getAttribute('id')))return;event.preventDefault();syncCapture();const e=codeSyncUI.editor,f=event.target,v=Object.fromEntries(new FormData(f));codeSyncUI.busy=true;event.submitter.disabled=true;
 try{
  if(e.kind==='run')await api('code-sync/run',{source:e.id,kind:e.mode,confirmation:v.confirmation,idempotency_key:e.nonce});
  else if(e.kind==='close-job')await api('code-sync/jobs/'+e.id+'/close',{confirmation:v.confirmation});
  else{
   let body={...v};if(e.kind!=='credentials')body={...body,revision:e.revision+1,enabled:f.elements.enabled.checked};
   if(e.kind==='sources'){const row=codeSyncUI.data.inventory.find(r=>r.directory===v.directory);Object.assign(body,{project:row?.project||null,credential:v.credential||null,includes:v.includes.split('\n').map(x=>x.trim()).filter(Boolean),excludes:v.excludes.split('\n').map(x=>x.trim()).filter(Boolean)});}
   if(e.kind==='devices')body.port=Number(v.port);
   if(e.kind==='targets')body.restart=f.elements.restart.checked;
   if(e.kind==='schedules'){const [hour,minute]=v.clock.split(':').map(Number);delete body.clock;Object.assign(body,{hour,minute});}
   await api('code-sync/'+e.kind,body);delete codeSyncUI.drafts[e.kind+':'+e.id];
  }
  if(e.kind==='credentials'&&f.elements.token)f.elements.token.value='';$('sync-dialog').close();codeSyncUI.editor=null;await loadCodeSync();notice(e.kind==='run'?'任务已排队，上传及各设备结果将在执行记录中更新。':'配置已保存。');
 }catch(error){$('sync-form-error').textContent=syncText(error.message);}
 finally{codeSyncUI.busy=false;event.submitter.disabled=false;if(current==='code-sync')renderCodeSync();}
});
document.addEventListener('input',event=>{if(event.target.closest('#sync-form'))syncCapture();});
document.addEventListener('change',event=>{
 if(event.target.id==='sync-archive-select'&&event.target.value){api('code-sync/archives/'+event.target.value).then(value=>{$('detail-title').textContent='历史代码同步回执';$('detail-content').innerHTML='<pre>'+esc(JSON.stringify(value.result,null,2))+'</pre>';$('detail-dialog').showModal();}).catch(error=>notice(syncText(error.message),true));return;}
 if(event.target.id==='sync-project'){codeSyncUI.selected=event.target.value;renderCodeSync();return;}
 if(event.target.closest('#sync-form')){
  if(event.target.name==='directory'&&codeSyncUI.editor?.kind==='sources'){
   const row=codeSyncUI.data.inventory.find(r=>r.directory===event.target.value),f=$('sync-form');if(row){f.elements.repository.value=row.repository;f.elements.branch.value=row.branch;if(!f.elements.name.value)f.elements.name.value=row.name;}
  }
  syncCapture();
 }
});
document.addEventListener('click',async event=>{
 const b=event.target.closest('[data-sync-refresh],[data-sync-edit],[data-sync-reload],[data-sync-dismiss],[data-sync-preview],[data-sync-device],[data-sync-target],[data-sync-run],[data-sync-job],[data-sync-reconcile],[data-sync-close-job],[data-sync-archive]');if(!b||b.disabled||!loggedIn)return;event.preventDefault();const d=b.dataset;
 try{
  if(d.syncRefresh){await loadCodeSync();return;}if(d.syncEdit){syncEdit(d.syncEdit,d.syncId);return;}if(d.syncDismiss){syncCapture();if(codeSyncUI.editor?.kind==='credentials'&&$('sync-form')?.elements.token)$('sync-form').elements.token.value='';$('sync-dialog').close();return;}
  if(d.syncReload){delete codeSyncUI.drafts[d.syncReload+':'+d.syncId];codeSyncUI.editor=null;await loadCodeSync();syncEdit(d.syncReload,d.syncId);return;}
  if(d.syncRun){syncConfirm('run',d.syncRun,d.syncKind);return;}if(d.syncCloseJob){syncConfirm('close-job',d.syncCloseJob);return;}
  if(d.syncReconcile){await api('code-sync/jobs/'+d.syncReconcile+'/reconcile',{});await loadCodeSync();return;}
  if(d.syncArchive){await api('code-sync/archive-history',{});await loadCodeSync();notice('已归档结束记录；未确认与进行中的记录继续保留。');return;}
  let result,title;
  if(d.syncPreview){result=(await api('code-sync/sources/'+d.syncPreview+'/preview',{})).result;title='所选代码快照预览';}
  if(d.syncDevice){result=(await api('code-sync/devices/'+d.syncDevice+'/check',{})).result;title='设备 Agent 核对';}
  if(d.syncTarget){result=(await api('code-sync/targets/'+d.syncTarget+'/check',{})).result;title='副本当前状态';}
  if(d.syncJob){result=codeSyncUI.data.jobs.find(j=>j.id===d.syncJob);title='代码同步回执';}
  $('detail-title').textContent=title;$('detail-content').innerHTML='<pre>'+esc(JSON.stringify(result,null,2))+'</pre>';$('detail-dialog').showModal();
 }catch(error){notice(syncText(error.message),true);}
});
