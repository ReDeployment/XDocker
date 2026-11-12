'use strict';
const codeSyncUI={data:null,error:'',loading:false,busy:false,view:'sources',selected:'',editor:null,drafts:{}};
const codeSyncViews={sources:'源码仓库',schedules:'推送计划',devices:'目标设备',targets:'分发策略',records:'执行记录'};
const syncMessages={XD_SYNC_GRAFTED_SOURCE_UNSUPPORTED:'本地仓库存在历史替换配置，无法完整检查，请由项目核对后再同步。',XD_SYNC_SCAN_INVALID_TREE:'提交目录无法完整检查，已停止同步。',XD_SYNC_OUTPUT_LIMIT:'读取内容或对象传输超出上限，已停止同步。',XD_SYNC_EXECUTION_DISABLED:'执行尚未开启，请检查本机授权及项目开关。',XD_SYNC_REVISION_CONFLICT:'配置已被修改，请重新读取后保存。',XD_SYNC_SOURCE_NOT_DISCOVERED:'目录、仓库或当前分支与发现结果不一致，请刷新后选择。',XD_SYNC_PATH_NOT_GRANTED:'目录尚未在本机源码范围中授权。',XD_SYNC_CREDENTIAL_REQUIRED:'请选择已配置的 GitHub 凭据。',XD_SYNC_CONFIRMATION_REQUIRED:'启用或执行前请输入完整项目名称。',XD_SYNC_TARGET_DIRTY:'副本存在本地修改，本次没有覆盖。',XD_SYNC_TARGET_DIRTY_OR_RESTART_NOT_GRANTED:'副本有修改，或目标尚未授权服务重启。',XD_SYNC_TARGET_GRANT_MISMATCH:'设备端授权的目录、仓库或分支与本页不一致。',XD_SYNC_TARGET_GRANT_CHANGED:'设备端授权已变化，请核对并重新保存分发策略。',XD_SYNC_AGENT_VERSION_MISMATCH:'设备端 Agent 版本与本机不一致，请先更新设备上的 XDocker Agent。',XD_SYNC_HOST_KEY_REQUIRED:'请填写设备已核对的完整 SSH 公钥，例如 ssh-ed25519 AAAA…',XD_SYNC_IDENTITY_NOT_GRANTED:'本机尚未授权此 SSH 身份，请按连接指南登记密钥引用。',XD_SYNC_SOURCE_BRANCH_REQUIRED:'来源需要处于明确的开发分支；请由项目选择分支后刷新。',XD_SYNC_BRANCH_INVALID:'分支名称无效。',XD_SYNC_SENSITIVE_CONTENT:'待推送提交包含敏感信息、临时文件或无法检查的内容，已停止推送；请查看回执中的风险位置。',XD_SYNC_REMOTE_DIVERGED:'远端分支不是本地提交的祖先，请由项目核对分支；本次没有强推。',XD_SYNC_SHALLOW_SOURCE_UNSUPPORTED:'来源是浅克隆，无法完整检查待推送历史；请先由项目准备完整仓库。',XD_SYNC_SCAN_COMMIT_LIMIT:'待检查提交超过 500 个，已停止；请先人工审查并发布初始历史。',XD_SYNC_SCAN_FILE_LIMIT:'待检查文件数量或单文件大小超出检查范围，已停止推送。',XD_SYNC_SCAN_BYTE_LIMIT:'待检查内容超过 64 MiB，已停止推送。',XD_SYNC_POLICY_REVIEW_REQUIRED:'同步策略已升级，请重新核对来源分支和设备授权。',XD_SYNC_SOURCE_BUSY_OR_UNCONFIRMED:'该项目有进行中或待核对任务，请先处理执行记录。',XD_SYNC_SOURCE_CHANGED:'检查期间来源提交发生变化，本次已停止，请刷新后重新预览。',XD_SYNC_GIT_REJECTED:'Git 操作被拒绝，请检查仓库权限、分支状态和网络。',XD_SYNC_UPLOAD_UNCONFIRMED:'上传结果未确认，先核对 GitHub 提交，勿直接重放任务。',XD_SYNC_HISTORY_CAPACITY:'执行记录已满，请先归档已结束记录。',XD_SYNC_NO_CONFIRMED_UPLOAD:'尚无核对通过的上传提交，请先运行上传。',XD_SYNC_UNMANAGED_BACKUP_BRANCH:'远端备份分支已有其他来源历史，本次未写入。',XD_SYNC_TRANSPORT_REJECTED:'设备连接失败，请检查 VPN、SSH 身份、主机公钥和 Agent 安装。',XD_SYNC_AGENT_RESPONSE_INVALID:'Agent 未返回有效协议响应，请检查版本与设备授权。',XD_SYNC_FOLLOWUP_CLOSED_BY_OPERATOR:'已结束自动跟进，原操作结果仍未确认。'};
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
 if(d?.sources.some(s=>{const row=d.inventory.find(r=>r.directory===s.directory);return row&&row.branch!==s.branch;}))alerts.push('部分项目已切换分支，请重新编辑源码配置并核对。');
 if(d?.archive_error)alerts.push('历史归档受阻：'+syncText(d.archive_error));
 const latestJobs=new Map();for(const job of d?.jobs||[])if(!latestJobs.has(job.source))latestJobs.set(job.source,job);
 const failures=[...latestJobs.values()].filter(job=>job.state==='failed');
 if(failures.length)alerts.push('最近有 '+failures.length+' 个项目同步失败：'+failures.slice(0,3).map(job=>(syncSource(job.source)?.name||job.source)+' · '+syncText(job.error||'XD_SYNC_OPERATION_FAILED')).join('；')+'。请在执行记录核对阻断原因。');
 if(d?.schedules.some(r=>r.last_error))alerts.push('有定时任务被阻断，请在推送计划查看原因。');
 let body='<div class="empty">正在读取代码同步配置…</div>';
 if(d){
  if(v==='sources')body=panel('源码仓库','推送选定分支中已有的提交，保留原描述与作者；未提交文件仅提示待处理',table(['项目 / 目录','GitHub / 分支','推送开关','操作'],filtered(d.sources).map(r=>`<tr><td><strong>${esc(r.name)}</strong><small>${esc(r.directory)}</small>${syncPending(r.directory)}</td><td>${esc(r.repository)}<small>同步分支：${esc(r.branch)}</small></td><td>${badge(r.enabled?'已开启':'未开启',r.enabled?'good':'')}</td><td><div class="actions">${syncButton('编辑',{edit:'sources',id:r.id})}${syncButton('推送预览',{preview:r.id})}${syncButton('推送并分发',{run:r.id,kind:'upload'},!r.enabled||!d.execution_enabled)}</div></td></tr>`)),`<div class="actions">${syncButton('GitHub 凭据',{edit:'credentials',id:'new'})}${syncButton('添加源码',{edit:'sources',id:'new'},!d.inventory.length)}</div>`)+`<p class="sync-help">按 workspace 对齐发现 ${d.inventory.length} 个主仓库；自动备份工作树、嵌套文档副本不重复接入。仅推送已有提交，推送前检查待发送历史中的敏感内容；扫描命中或超限时停止。<a href="/assets/code-sync-guide.html" target="_blank" rel="noreferrer">连接与授权指南 ↗</a></p>`;
  if(v==='schedules')body=panel('每日推送与副本核对','推送任务成功后才分发；也可单独安排副本核对。启用时须确认项目名称',table(['项目 / 类型','时间','下次运行','状态 / 原因','操作'],filtered(d.schedules).map(r=>`<tr><td>${esc(syncSource(r.source)?.name||r.source)}<small>${r.mode==='upload'?'推送并分发':'仅同步已确认提交'}</small></td><td>${String(r.hour).padStart(2,'0')}:${String(r.minute).padStart(2,'0')}<small>${esc(r.timezone)}</small></td><td>${date(r.next_at*1000)}</td><td>${badge(r.enabled?'已启用':'未启用',r.enabled?'good':'')}<small>${esc(syncText(r.last_error||''))}</small></td><td>${syncButton('编辑',{edit:'schedules',id:r.id})}</td></tr>`)),syncButton('新增计划',{edit:'schedules',id:'new'},!d.sources.length));
  if(v==='devices')body=panel('目标设备','通过 VPN 与固定 Agent 通信，固定 SSH 身份和主机公钥；连接测试不更新副本',table(['设备','SSH 地址','状态','操作'],d.devices.map(r=>`<tr><td><strong>${esc(r.name)}</strong><small>${esc(r.id)}</small></td><td>${esc(r.username)}@${esc(r.host)}:${r.port}<small>身份：${esc(r.identity)}</small></td><td>${badge(r.enabled?'允许连接':'未启用',r.enabled?'good':'')}</td><td><div class="actions">${syncButton('编辑',{edit:'devices',id:r.id})}${syncButton('核对 Agent',{device:r.id},!r.enabled)}</div></td></tr>`)),syncButton('添加设备',{edit:'devices',id:'new'}))+`<p class="sync-help">本机可选身份：${esc(d.policy.identities.join('、')||'尚未授权')}。目标设备需安装同一版本 Agent，并在其私有策略中批准副本目录、仓库、同步分支及可选服务；网页不接受远程 Shell 命令。</p>`;
  if(v==='targets')body=panel('项目副本与服务策略','只快进到本次 GitHub 已确认的提交；有本地修改、分支分叉或授权漂移时停止',table(['项目 / 设备','目标目录','同步 / 重启','操作'],filtered(d.targets).map(r=>`<tr><td><strong>${esc(syncSource(r.source)?.name||r.source)}</strong><small>${esc(d.devices.find(x=>x.id===r.device)?.name||r.device)}</small></td><td>${esc(r.directory)}<small>Agent 授权：${esc(r.remote_target)}</small></td><td>${badge(r.enabled?'已启用':'未启用',r.enabled?'good':'')}<small>${r.restart?'更新后重启已授权服务':'只同步副本'}</small></td><td><div class="actions">${syncButton('编辑',{edit:'targets',id:r.id})}${syncButton('检查副本',{target:r.id})}${syncButton('立即同步',{run:r.source,kind:'distribute'},!d.execution_enabled||!syncSource(r.source)?.enabled)}</div></td></tr>`)),syncButton('添加副本',{edit:'targets',id:'new'},!d.sources.length||!d.devices.length));
  if(v==='records')body=panel('执行记录','记录 GitHub 的准确 SHA 和各设备回执；命令返回成功不替代目标 HEAD 与服务状态核对',table(['时间 / 项目','任务 / 提交','状态','操作'],filtered(d.jobs).map(r=>`<tr><td>${date(r.at*1000)}<small>${esc(syncSource(r.source)?.name||r.source)}</small></td><td>${r.kind==='upload'?'推送并分发':'副本同步'}<small class="code-line">${esc(r.sha||'尚无提交')}</small></td><td>${badge(({queued:'排队中',running:'执行中',success:'已核对',partial:'部分副本待核对','upload-unconfirmed':'上传待核对',interrupted:'执行中断','reconcile-queued':'核对排队',failed:'失败','closed-unconfirmed':'未确认 · 已结束跟进'})[r.state]||r.state,r.state==='success'?'good':['partial','upload-unconfirmed','interrupted'].includes(r.state)?'warn':r.state==='failed'?'bad':'')}<small>${esc(syncText(r.error||''))}</small></td><td><div class="actions">${syncButton('详情',{job:r.id})}${['upload-unconfirmed','interrupted'].includes(r.state)&&r.sha?syncButton('核对上传结果',{reconcile:r.id},!d.execution_enabled):''}${['upload-unconfirmed','interrupted','partial'].includes(r.state)?syncButton('结束跟进',{closeJob:r.id}):''}</div></td></tr>`)),syncButton('归档已结束记录',{archive:'yes'}))+`<div class="sync-help"><label>历史归档（${d.archives.length}）<select id="sync-archive-select"><option value="">选择一批归档回执</option>${d.archives.map(name=>`<option value="${esc(name)}">${esc(name.slice(0,16))}…</option>`).join('')}</select></label></div>`;
 }
 const top=`${d?.mode_migration?'<p class="sync-help">同步策略已升级为仅推送已有提交；原快照计划和设备策略已关闭，请核对分支与 Agent 后再启用。</p>':''}<div class="intro"><div><h2>代码同步</h2><p class="muted">已有开发提交 → GitHub 同一分支 → 目标设备副本。</p></div>${syncButton('刷新',{refresh:'yes'},codeSyncUI.loading)}</div>${d?`<div class="sync-context"><label>当前项目<select id="sync-project"><option value="">全部项目</option>${d.sources.map(r=>`<option value="${esc(r.id)}" ${s===r.id?'selected':''}>${esc(r.name)}</option>`).join('')}</select></label><span>${badge(d.execution_enabled?'执行授权已开启':'执行授权未开启',d.execution_enabled?'good':'warn')} ${badge(d.worker.connected?'调度器在线':'调度器待连接',d.worker.connected?'good':'warn')}</span></div>`:''}${alerts.map(t=>`<div class="sync-alert" role="alert">${esc(t)}</div>`).join('')}`;
 $('content').innerHTML='<div class="sync-workspace">'+top+verticalTabs('code-sync','代码同步分区',Object.entries(codeSyncViews).map(([key,label])=>({key,label})),v,body)+'</div>';
 if(codeSyncUI.lastView!==v){$('view-code-sync-'+v)?.scrollIntoView({block:'nearest',inline:'nearest'});codeSyncUI.lastView=v;if(innerWidth<=720)document.querySelector('.sync-workspace .vertical-tab-list')?.scrollIntoView({block:'start'});}
}
function syncDialog(){let dialog=$('sync-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='sync-dialog';dialog.className='sync-dialog';document.body.append(dialog);dialog.addEventListener('cancel',()=>{syncCapture();const token=$('sync-form')?.elements.token;if(token)token.value='';});}return dialog;}
function syncCapture(){const f=$('sync-form'),e=codeSyncUI.editor;if(!f||!e||e.kind==='credentials')return;const value={...Object.fromEntries(new FormData(f)),__revision:e.revision};for(const box of f.querySelectorAll('input[type=checkbox]'))value[box.name]=box.checked;codeSyncUI.drafts[e.kind+':'+e.id]=value;}
function syncEdit(kind,id='new'){
 const d=codeSyncUI.data,old=d[kind]?.find(x=>x.id===id),selected=codeSyncUI.selected||d.sources[0]?.id||'',defaults={sources:{id:'',name:'',directory:d.inventory[0]?.directory||'',repository:d.inventory[0]?.repository||'',branch:d.inventory[0]?.branch||'',project:d.inventory[0]?.project||'',credential:'',enabled:false},devices:{id:'',name:'',host:'',port:22,username:'',identity:d.policy.identities[0]||'',host_key:'',enabled:false},targets:{id:'',source:selected,device:d.devices[0]?.id||'',remote_target:selected,directory:'/data/workspace/',restart:false,enabled:false},schedules:{id:'',source:selected,mode:'upload',clock:'02:00',timezone:'Asia/Shanghai',enabled:false},credentials:{id:'',name:'',token:''}};
 const normalized=old?{...old,branch:d.inventory.find(r=>r.directory===old.directory)?.branch||old.branch,clock:String(old.hour).padStart(2,'0')+':'+String(old.minute).padStart(2,'0')}:defaults[kind];
 const value=codeSyncUI.drafts[kind+':'+id]||normalized;codeSyncUI.editor={kind,id,revision:value.__revision??old?.revision??0};
 const input=(key,label,attrs='')=>`<label>${label}<input name="${key}" value="${esc(value[key]??'')}" ${attrs}></label>`;
 const select=(key,label,items)=>`<label>${label}<select name="${key}">${items.map(([k,label])=>`<option value="${esc(k)}" ${String(value[key]??'')===String(k)?'selected':''}>${esc(label)}</option>`).join('')}</select></label>`;
 const area=(key,label)=>`<label class="sync-wide">${label}<textarea name="${key}" rows="3">${esc(value[key]??'')}</textarea></label>`;
 const check=(key,label)=>`<label class="sync-check"><input type="checkbox" name="${key}" ${value[key]?'checked':''}>${label}</label>`;
 let fields=input('id','标识','required pattern="[a-z0-9][a-z0-9-]{0,63}" '+(old?'readonly':''));
 if(kind==='sources')fields+=input('name','项目名称','required maxlength="80"')+select('directory','已发现的源码目录',d.inventory.map(r=>[r.directory,r.name+' · '+r.directory]))+input('repository','GitHub 仓库（owner/repository）','required')+input('branch','检测到的来源分支（同步同名远端分支）','required readonly')+select('credential','GitHub 写入凭据',[['','尚未配置'],...d.credentials.map(r=>[r.id,r.name])])+'<p class="sync-help sync-wide">固定策略：仅推送已有提交，保留提交描述和作者。未暂存、已暂存和未跟踪的改动都不会自动提交。若项目切换分支，请刷新并重新核对配置。</p>'+check('enabled','允许手动或定时推送已有提交')+input('confirmation','启用时输入完整项目名称','autocomplete="off"');
 if(kind==='credentials')fields+=input('name','凭据名称','required maxlength="80"')+input('token','GitHub 仓库写入令牌','type="password" autocomplete="new-password" required')+'<p class="sync-help">令牌保存在本机私有文件，仅返回凭据名称；不会回显、写入 Git URL 或项目代码。</p>';
 if(kind==='devices')fields+=input('name','设备名称','required maxlength="80"')+input('host','VPN / 内网 IP','required')+input('port','SSH 端口','type="number" min="1" max="65535" required')+input('username','SSH 用户','required')+select('identity','已授权的本机 SSH 身份',d.policy.identities.map(r=>[r,r]))+area('host_key','已核对的 SSH 主机公钥（类型 + Base64）')+check('enabled','允许连接此设备')+'<p class="sync-help">首次需按连接指南安装 Agent 和设备端副本授权；不接受任意远程命令。</p>';
 if(kind==='targets')fields+=select('source','来源项目',d.sources.map(r=>[r.id,r.name]))+select('device','目标设备',d.devices.map(r=>[r.id,r.name]))+input('remote_target','Agent 中已批准的副本标识','required')+input('directory','设备端副本目录（须与授权一致）','required')+check('restart','副本实际更新后重启已授权服务')+check('enabled','核对设备授权并启用此副本')+input('confirmation','启用时输入完整来源项目名称','autocomplete="off"');
 if(kind==='schedules')fields+=select('source','项目',d.sources.map(r=>[r.id,r.name]))+select('mode','每日执行内容',[['upload','推送并分发'],['distribute','仅同步已确认提交']])+input('clock','每天时间','type="time" required')+input('timezone','时区','required')+check('enabled','启用每日计划')+input('confirmation','启用时输入完整项目名称','autocomplete="off"');
 const dialog=syncDialog();dialog.innerHTML=`<form id="sync-form" method="post"><div class="detail-head"><h2>${old?'编辑':'新增'}${esc({sources:'源码仓库',devices:'目标设备',targets:'分发策略',schedules:'每日计划',credentials:'GitHub 凭据'}[kind])}</h2>${syncButton('关闭',{dismiss:'yes'})}</div><div class="sync-form-body"><div class="sync-fields">${fields}</div></div><p id="sync-form-error" class="error" role="alert"></p><div class="dialog-actions">${old?syncButton('重读配置（放弃草稿）',{reload:kind,id}):''}${syncButton('取消',{dismiss:'yes'})}<button type="submit" class="primary">保存配置</button></div></form>`;if(!dialog.open)dialog.showModal();
}
function syncConfirm(kind,id,mode){
 const source=kind==='run'?syncSource(id):syncSource(codeSyncUI.data.jobs.find(j=>j.id===id)?.source);codeSyncUI.editor={kind,id,mode,nonce:crypto.randomUUID()};
 const dialog=syncDialog();dialog.innerHTML=`<form id="sync-operation" method="post"><div class="detail-head"><h2>${kind==='run'?(mode==='upload'?'推送并分发':'同步已确认提交'):'结束跟进'}</h2>${syncButton('关闭',{dismiss:'yes'})}</div><p>${kind==='run'?'将已有提交推送到同名远端分支，再同步已批准的副本；推送前检查全部待发送提交，命中风险时停止。':'保留未确认回执，停止本条任务的自动跟进；不会撤销 GitHub 提交或重放设备操作。'}</p><p class="muted">${esc(source?.repository)} · ${esc(source?.branch)}</p><label>输入完整项目名称 <strong>${esc(source?.name)}</strong><input name="confirmation" required autocomplete="off"></label><p id="sync-form-error" class="error"></p><div class="dialog-actions">${syncButton('取消',{dismiss:'yes'})}<button type="submit" class="primary">确认执行</button></div></form>`;if(!dialog.open)dialog.showModal();
}
document.addEventListener('submit',async event=>{
 if(!['sync-form','sync-operation'].includes(event.target.getAttribute('id')))return;event.preventDefault();syncCapture();const e=codeSyncUI.editor,f=event.target,v=Object.fromEntries(new FormData(f));codeSyncUI.busy=true;event.submitter.disabled=true;
 try{
  if(e.kind==='run')await api('code-sync/run',{source:e.id,kind:e.mode,confirmation:v.confirmation,idempotency_key:e.nonce});
  else if(e.kind==='close-job')await api('code-sync/jobs/'+e.id+'/close',{confirmation:v.confirmation});
  else{
   let body={...v};if(e.kind!=='credentials')body={...body,revision:e.revision+1,enabled:f.elements.enabled.checked};
   if(e.kind==='sources'){const row=codeSyncUI.data.inventory.find(r=>r.directory===v.directory);Object.assign(body,{project:row?.project||null,credential:v.credential||null});}
   if(e.kind==='devices')body.port=Number(v.port);
   if(e.kind==='targets')body.restart=f.elements.restart.checked;
   if(e.kind==='schedules'){const [hour,minute]=v.clock.split(':').map(Number);delete body.clock;Object.assign(body,{hour,minute});}
   await api('code-sync/'+e.kind,body);delete codeSyncUI.drafts[e.kind+':'+e.id];
  }
  if(e.kind==='credentials'&&f.elements.token)f.elements.token.value='';$('sync-dialog').close();codeSyncUI.editor=null;await loadCodeSync();notice(e.kind==='run'?'任务已排队，推送及各设备结果将在执行记录中更新。':'配置已保存。');
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
  if(d.syncPreview){result=(await api('code-sync/sources/'+d.syncPreview+'/preview',{})).result;$('detail-title').textContent='已有提交推送预览';$('detail-content').innerHTML=renderSyncPreview(result);$('detail-dialog').showModal();return;}
  if(d.syncDevice){result=(await api('code-sync/devices/'+d.syncDevice+'/check',{})).result;title='设备 Agent 核对';}
  if(d.syncTarget){result=(await api('code-sync/targets/'+d.syncTarget+'/check',{})).result;title='副本当前状态';}
  if(d.syncJob){result=codeSyncUI.data.jobs.find(j=>j.id===d.syncJob);title='代码同步回执';}
  $('detail-title').textContent=title;$('detail-content').innerHTML='<pre>'+esc(JSON.stringify(result,null,2))+'</pre>';$('detail-dialog').showModal();
 }catch(error){notice(syncText(error.message),true);}
});

function syncPending(directory){
 const row=codeSyncUI.data?.inventory.find(r=>r.directory===directory),p=row?.pending;
 if(!row)return '<small>来源状态待刷新</small>';
 if(!row.pushable)return '<small class="error">当前为固定提交，请项目选择分支后刷新</small>';
 if(!p)return '';
 return `<small>待处理：未暂存 ${p.modified.count} · 已暂存 ${p.staged.count} · 新文件 ${p.untracked.count}（均不上传）</small>`;
}
function renderSyncPreview(r){
 const p=r.pending,rules={'private-key':'私钥','github-token':'GitHub 令牌','aws-access-key':'云访问密钥','slack-token':'消息平台令牌','api-token':'API 令牌','credential-url':'含凭据的连接地址','literal-credential':'疑似明文凭据','temporary-or-private-file':'临时或私有文件','environment-file':'运行环境文件','credential-file':'凭据文件','unscannable-binary':'无法扫描的二进制文件','external-lfs-object':'需要独立审查的 LFS 文件','symlink-or-submodule':'符号链接或子模块'};
 return `<p>${badge('仅推送已有提交','blue')} ${badge(r.blocked?'风险检查已阻断':r.scope==='outgoing-history'?'待推送范围检查未命中':'本地检查未命中',r.blocked?'bad':r.scope==='outgoing-history'?'good':'warn')}</p><p>分支 ${esc(r.branch)} · 提交 ${esc(r.head)}</p><p class="muted">${r.scope==='outgoing-history'?'已核对远端与全部待推送提交。':'尚未核对远端，仅检查当前提交；配置 GitHub 凭据后才能预览完整待推送范围。'} 检查通过仍需遵守项目提交审查。</p><p>待处理改动：未暂存 ${p.modified.count}、已暂存 ${p.staged.count}、未跟踪 ${p.untracked.count}。这些工作区改动不会进入推送。</p>`+table(['已有提交','原提交描述'],r.commits.map(c=>`<tr><td>${esc(c.sha.slice(0,12))}</td><td>${esc(c.title)}</td></tr>`))+(r.blocked?table(['风险文件 / 提交','规则','行号'],r.findings.map(f=>`<tr><td>${esc(f.path)}<small>${esc(f.commit.slice(0,12))}</small></td><td>${esc(rules[f.rule]||f.rule)}</td><td>${f.line??'—'}</td></tr>`))+'<p class="error">请在项目中处理未推送历史后重新检查。仅删除后续版本中的文件，仍可能保留先前提交的敏感内容。</p>':'');
}
