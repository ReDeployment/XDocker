'use strict';
const resourcesUI={data:null,error:'',loading:false,view:'overview',gpuSelected:'',query:'',editor:null,editRevision:0,draft:null,selected:'host',history:null,historyError:'',historyLoading:false};
const resourceViews={overview:'服务器概览',gpu:'GPU 资源',storage:'硬盘与存储',apps:'已登记应用',discover:'发现应用',history:'历史趋势'};
const resourceMessages={XD_MONITOR_HOST_CHANGED:'服务器身份发生变化，请重新核对应用绑定和采集目录。',XD_MONITOR_TARGET_INVALID:'请选择 1–16 个不同组件。',XD_MONITOR_ID_INVALID:'应用标识须为小写字母、数字或短横线，且不能使用 host。',XD_MONITOR_CONFIRMATION_REQUIRED:'请输入完整应用名称确认移除。',XD_MONITOR_CAPACITY:'已达到 100 个应用的登记上限。',XD_METRICS_NOT_CONNECTED:'尚未接入这台服务器的采集器。',XD_METRICS_REPORT_UNAVAILABLE:'采集报告无法读取，请检查采集器和目录权限。',XD_METRICS_DOCKER_UNAVAILABLE:'容器指标读取失败，当前仅有可读取的宿主机进程。',XD_METRICS_HISTORY_UNAVAILABLE:'历史采样无法读取或保存。',XD_MONITOR_REVISION_CONFLICT:'登记信息已被更新，请重新读取后编辑。',XD_MONITOR_TARGET_ALREADY_BOUND:'所选组件已属于其他应用，请先调整原登记。',XD_METRICS_STALE:'采集数据已过期，请恢复采集器后再登记。',XD_MONITOR_TARGET_NOT_DISCOVERED:'所选组件已不在当前发现结果中，请重新选择。'};
const resourceText=v=>resourceMessages[v]||v;
const resourceBytes=v=>v===null||v===undefined?'—':v>=1073741824?(v/1073741824).toFixed(2)+' GB':(v/1048576).toFixed(1)+' MB';
const resourceCPU=v=>v===null||v===undefined?'待采样':Number(v).toFixed(1)+'%';
const resourceButton=(label,data,blocked=false)=>`<button type="button" ${Object.entries(data).map(([k,v])=>`data-resource-${k}="${esc(v)}"`).join(' ')} ${blocked?'disabled':''}>${esc(label)}</button>`;
function resetResources(){resourcesUI.data=null;resourcesUI.error='';resourcesUI.history=null;resourcesUI.draft=null;resourcesUI.editor=null;resourcesUI.gpuSelected='';document.getElementById('resource-dialog')?.close();document.getElementById('storage-dialog')?.close();}
const gpuValue=(v,suffix='',digits=1)=>v===null||v===undefined?'不可用':Number(v).toFixed(digits)+suffix;
const gpuBytes=v=>v===null||v===undefined?'不可用':v>=1073741824?(v/1073741824).toFixed(2)+' GiB':(v/1048576).toFixed(1)+' MiB';
const gpuStates={'not-collected':'尚未采集 GPU 指标，请核对采集器版本。','not-detected':'当前未发现 DRM 显卡设备，请核对驱动及采集器可见范围。',unavailable:'GPU 指标无法读取，请核对采集器权限和驱动。'};
function gpuNotice(gpu){
 if(!gpu||gpuStates[gpu.state])return gpuStates[gpu?.state||'not-collected'];
 const notes=[];
 if(gpu.devices.some(d=>d.state==='unsupported'))notes.push('部分 GPU 驱动暂不支持指标采集。');
 if(gpu.devices.some(d=>['partial','unavailable'].includes(d.state)))notes.push('部分 GPU 指标无法读取，缺失项显示“不可用”。');
 if(gpu.limit_reached)notes.push('GPU 发现范围达到上限，本轮最多展示 8 台设备。');
 return notes.join(' ');
}
function resourceGPU(gpu){
 const devices=gpu?.devices||[];
 if(!devices.length)return `<div class="empty">${esc(gpuNotice(gpu)||'暂无 GPU 数据。')}</div>`;
 const d=devices.find(d=>d.id===resourcesUI.gpuSelected)||devices[0];resourcesUI.gpuSelected=d.id;
 const selection=devices.length>1?`<label class="resource-gpu-select">查看显卡<select id="resource-gpu-select">${devices.map(d=>`<option value="${esc(d.id)}" ${d.id===resourcesUI.gpuSelected?'selected':''}>${esc(d.name)} · ${esc(d.bus_id||d.driver)}</option>`).join('')}</select></label>`:'';
 return panel('GPU 资源','整台设备的当前采样；应用分组和历史趋势仍展示 CPU 与内存。',`<div class="panel-body">${selection}<h3 class="resource-gpu-name">${esc(d.name)}</h3><div class="kpis resource-kpis">${[
 ['GPU 利用率',gpuValue(d.utilization_percent,'%'),'当前设备忙碌比例'],
 ['温度',gpuValue(d.temperature_celsius,' °C'),'驱动 hwmon 传感器'],
 [d.integrated?'预留显存已用':'显存已用',gpuBytes(d.vram_used_bytes),'总量 '+gpuBytes(d.vram_total_bytes)],
 ['共享内存（GTT）已用',gpuBytes(d.shared_used_bytes),'驱动上限 '+gpuBytes(d.shared_total_bytes)]
 ].map(([label,value,help])=>`<article class="kpi"><div class="kpi-label">${esc(label)}</div><strong>${esc(value)}</strong><small>${esc(help)}</small></article>`).join('')}</div><div class="stack-row"><span>驱动</span><strong>${esc(d.driver)}</strong></div><div class="stack-row"><span>PCI 地址 / 设备 ID</span><span>${esc(d.bus_id||'不可用')} / ${esc(d.pci_id||'不可用')}</span></div><p class="muted">${d.integrated?'这张核显的预留显存与共享内存分开统计。':''}GTT 使用系统内存；驱动上限不是额外物理内存，也不代表已占用。当前不提供进程或容器的 GPU 归属。</p></div>`);
}
async function loadResources(){
 if(!loggedIn||resourcesUI.loading)return;resourcesUI.loading=true;
 try{const data=await api('resources');if(loggedIn){resourcesUI.data=data;resourcesUI.error='';}}
 catch(error){resourcesUI.error=error.message;}
 finally{resourcesUI.loading=false;if(current==='resources'&&loggedIn)renderResourcesPage();}
}
async function loadResourceHistory(){
 if(!loggedIn||resourcesUI.historyLoading)return;resourcesUI.historyLoading=true;const selected=resourcesUI.selected;
 try{const data=await api('resources/history/'+encodeURIComponent(selected));if(loggedIn&&selected===resourcesUI.selected){resourcesUI.history=data;resourcesUI.historyError='';}}
 catch(error){if(selected===resourcesUI.selected)resourcesUI.historyError=error.message;}
 finally{resourcesUI.historyLoading=false;if(current==='resources'&&loggedIn)renderResourcesPage();}
}
function resourceChart(history){
 if(!history?.points.length)return '<div class="empty">尚无历史采样。采集器运行后逐步保留最近 24 小时的数据。</div>';
 const points=history.points.filter(p=>p.memory_bytes!==null),max=Math.max(1,...points.map(p=>p.memory_bytes));
 const end=Date.now()/1000,start=end-86400;
 const segments=[];let segment=[],previous=null;
 for(const p of points){if(previous!==null&&p.at-previous>300){segments.push(segment);segment=[];}segment.push(`${Math.max(10,Math.min(590,10+(p.at-start)/86400*580)).toFixed(1)},${(110-p.memory_bytes/max*90).toFixed(1)}`);previous=p.at;}segments.push(segment);
 return `<div class="resource-chart"><p>内存峰值 ${resourceBytes(max)} · 已采集 ${history.points.length} 个五分钟区间</p><svg viewBox="0 0 600 125" role="img" aria-label="最近 24 小时内存趋势，空白代表缺少采样"><path d="M0 110H600" stroke="#dce5ee"/>${segments.map(s=>s.length>1?`<polyline points="${s.join(' ')}" fill="none" stroke="#2563eb" stroke-width="2"/>`:`<circle cx="${s[0].split(',')[0]}" cy="${s[0].split(',')[1]}" r="3" fill="#2563eb"/>`).join('')}</svg><div class="resource-chart-labels"><span>24 小时前</span><span>现在</span></div></div>`+table(['最近区间','CPU 均值','内存峰值','分钟样本'],history.points.slice(-12).reverse().map(p=>`<tr><td>${date(p.at*1000)}</td><td>${resourceCPU(p.cpu_percent)}</td><td>${resourceBytes(p.memory_bytes)}</td><td>${p.samples}</td></tr>`));
}
function renderResourcesPage(){
 const route=location.hash.slice(1).split('/')[1];if(resourceViews[route])resourcesUI.view=route;
 const d=resourcesUI.data,r=d?.report,view=resourcesUI.view,fresh=!!r&&r.fresh&&Date.now()/1000-r.at<=90,writable=fresh&&d?.configuration_writable!==false;
 const errors=[resourcesUI.error,d?.error,...(r?.errors||[])].filter(Boolean);
 if(d?.configuration_writable===false)errors.push('当前为只读视图；登记应用请打开服务器的完整控制台。');
 if(r&&!fresh)errors.push('采集数据已过期，以下是上次观测；当前资源占用未确认。');
 if(r?.coverage.process_limit_reached||r?.coverage.group_limit_reached||r?.coverage.docker_limit_reached)errors.push('发现数量达到本轮采集上限，应用清单或汇总可能不完整。');
 if(r?.coverage.docker_incomplete)errors.push('部分运行容器未取得指标，应用汇总保持未确认。');
 if(r?.host.memory_available/r?.host.memory_total<.1)errors.push('服务器可用内存低于 10%。');
 if(r?.host.disk_free/r?.host.disk_total<.1)errors.push('服务器根分区可用空间低于 10%。');
 if(r?.host.cpu_percent>=85)errors.push('本次采样的整机 CPU 使用率达到 85%。');
 if(r&&gpuNotice(r.gpu))errors.push(gpuNotice(r.gpu));
 if(r&&storageNotice(r.storage))errors.push(storageNotice(r.storage));
 const gpus=r?.gpu?.devices||[],gpuBusy=gpus.map(d=>d.utilization_percent).filter(v=>v!==null&&v!==undefined);
 let body='<div class="empty">正在读取服务器资源…</div>';
 if(d){
  if(view==='overview')body=r?`<div class="kpis resource-kpis">${[['CPU',resourceCPU(r.host.cpu_percent),r.host.cpu_logical+' 个逻辑 CPU'],['内存可用',resourceBytes(r.host.memory_available),'总内存 '+resourceBytes(r.host.memory_total)],['GPU',gpuBusy.length?gpuValue(Math.max(...gpuBusy),'%'):'不可用',gpus.length===1?gpus[0].name:gpus.length+' 台设备 · 最高利用率'],['根分区可用',resourceBytes(r.host.disk_free),'总空间 '+resourceBytes(r.host.disk_total)]].map(([label,value,help])=>`<article class="kpi"><div class="kpi-label">${esc(label)}</div><strong>${esc(value)}</strong><small>${esc(help)}</small>${label==='GPU'?resourceButton('查看 GPU',{'gpu-view':'yes'}):label==='根分区可用'?resourceButton('查看硬盘与存储',{'storage-view':'yes'}):''}</article>`).join('')}</div>`+panel('服务器状态',esc(r.host.hostname),`<div class="panel-body"><div class="stack-row"><span>最近采样</span><strong>${date(r.at*1000)}</strong></div><div class="stack-row"><span>已登记应用 / 发现组件</span><span>${d.apps.length} / ${r.targets.length}</span></div><div class="stack-row"><span>负载（1 / 5 / 15 分钟）</span><span>${r.host.load.map(v=>v.toFixed(2)).join(' / ')}</span></div><div class="stack-row"><span>交换空间已用</span><span>${resourceBytes(r.host.swap_used)}</span></div><p class="muted">整机 CPU 以全部逻辑 CPU 为 100%；应用 CPU 以单核为 100%，可以超过 100%。</p><p class="muted">已扫描 ${r.coverage.processes_scanned||0} 个进程；${r.coverage.processes_unreadable||0} 个内核、已退出或无权读取的进程未归入应用。容器进程不重复计入宿主机应用。</p></div>`):'<div class="empty">尚未接入宿主机采集器。安装步骤见仓库的应用资源监控指南。</div>';
  if(view==='gpu')body=resourceGPU(r?.gpu);
  if(view==='storage')body=resourceStorage(r?.storage);
  if(view==='apps')body=panel('已登记应用','将同一项目的多个组件归为一个应用',table(['应用','观测状态','CPU / 内存','组件','操作'],d.apps.map(a=>`<tr><td><strong>${esc(a.name)}</strong><small>${esc(a.id)}</small></td><td>${badge(a.status==='observed'&&fresh?'已观测':'未确认',a.status==='observed'&&fresh?'good':'warn')}<small>${esc(a.reason)}</small></td><td>${resourceCPU(a.cpu_percent)}<small>${resourceBytes(a.memory_bytes)}</small></td><td>${a.targets.length} 个${a.missing_targets.length?`<small>${a.missing_targets.length} 个暂未发现</small>`:''}</td><td><div class="actions">${resourceButton('详情',{'detail':a.id})}${resourceButton('编辑',{'edit':a.id},!writable)}</div></td></tr>`)),resourceButton('登记应用',{'edit':'new'},!writable||!r?.targets.length));
  if(view==='discover'){
   const bound=new Set(d.apps.flatMap(a=>a.targets)),q=resourcesUI.query.toLowerCase(),rows=(r?.targets||[]).filter(t=>(t.name+' '+t.description).toLowerCase().includes(q));
   body=panel('发现应用','选择要持续观察的组件；发现结果不会自动获得启停权限',`<div class="panel-body"><label>搜索应用或目录<input id="resource-search" type="search" value="${esc(resourcesUI.query)}" placeholder="服务名、可执行文件或工作目录"></label></div>`+table(['组件','类型 / 状态','CPU / 内存','归属','操作'],rows.map(t=>`<tr><td><strong>${esc(t.name)}</strong><small class="resource-path">${esc(t.description)}</small></td><td>${esc({docker:'Docker 容器',systemd:'systemd 服务',process:'可执行文件与工作目录'}[t.kind])}<small>${esc(t.state)}</small></td><td>${resourceCPU(t.cpu_percent)}<small>${resourceBytes(t.memory_bytes)}</small></td><td>${bound.has(t.id)?'已登记':'待选择'}</td><td>${resourceButton('查看进程',{'target':t.id})} ${resourceButton('登记',{'bind':t.id},!writable||bound.has(t.id))}</td></tr>`)));
  }
  if(view==='history')body=panel('历史趋势','每分钟保留一份样本；每五分钟显示 CPU 均值、内存峰值。缺失区间保持空白。',`<div class="panel-body"><div class="actions"><label>查看对象<select id="resource-history-select"><option value="host">整台服务器</option>${d.apps.map(a=>`<option value="${esc(a.id)}" ${a.id===resourcesUI.selected?'selected':''}>${esc(a.name)}</option>`).join('')}</select></label>${resourceButton('读取趋势',{'history':'yes'},resourcesUI.historyLoading)}</div>${resourcesUI.historyError?`<p class="error" role="alert">${esc(resourceText(resourcesUI.historyError))}</p>`:''}${resourceChart(resourcesUI.history)}</div>`);
 }
 const top=`<div class="intro"><div><h2>应用资源</h2><p class="muted">查看本服务器的资源与应用占用。</p></div>${resourceButton(resourcesUI.loading?'正在读取…':'刷新指标',{'refresh':'yes'},resourcesUI.loading)}</div><div class="resource-status" role="status">${fresh?'采集连接正常':'采集尚未确认'} · 监控登记只影响展示，不会启停服务</div>${[...new Set(errors)].map(e=>`<div class="resource-alert" role="alert">${esc(resourceText(e))}</div>`).join('')}`;
 const tabs=Object.entries(resourceViews).map(([key,label])=>({key,label}));
 $('content').innerHTML='<div class="resource-workspace">'+top+verticalTabs('resources','应用资源分区',tabs,view,body)+'</div>';
 renderStorageDialog();
 if(resourcesUI.lastView!==view){$(`view-resources-${view}`)?.scrollIntoView({block:'nearest',inline:'nearest'});resourcesUI.lastView=view;}
 const nav=document.querySelector('.resource-workspace [role=tablist]'),active=$(`view-resources-${view}`);
 if(nav&&active&&matchMedia('(max-width:720px)').matches){const n=nav.getBoundingClientRect(),a=active.getBoundingClientRect();nav.scrollLeft+=a.left-n.left-(nav.clientWidth-a.width)/2;nav.setAttribute('aria-orientation','horizontal');}
}
function resourceDraft(){const f=$('resource-form');if(f)resourcesUI.draft={id:f.elements.id.value,name:f.elements.name.value,targets:[...f.querySelectorAll('[name=targets]:checked')].map(e=>e.value)};}
function resourceEdit(identifier='new',target=null){
 const d=resourcesUI.data,row=d.apps.find(a=>a.id===identifier),draftKey=row?.id||'new';
 if(resourcesUI.editor!==draftKey){resourcesUI.draft=null;resourcesUI.editRevision=row?.revision||0;}
 resourcesUI.editor=draftKey;const draft=resourcesUI.draft||row||{id:'',name:'',targets:target?[target]:[]};
 let dialog=$('resource-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='resource-dialog';dialog.className='resource-dialog';document.body.append(dialog);dialog.addEventListener('cancel',resourceDraft);}
 const other=new Set(d.apps.filter(a=>a.id!==row?.id).flatMap(a=>a.targets));
 const candidates=[...(d.report?.targets||[])];for(const id of draft.targets)if(!candidates.some(t=>t.id===id))candidates.push({id,name:'暂未发现的已选组件',description:id});
 dialog.innerHTML=`<form id="resource-form"><div class="detail-head"><h2>${row?'编辑':'登记'}应用</h2>${resourceButton('关闭',{'close':'yes'})}</div><label>应用标识<input name="id" value="${esc(draft.id)}" required pattern="[a-z0-9][a-z0-9-]{0,63}" ${row?'readonly':''} placeholder="例如 powerx-dev"></label><label>应用名称<input name="name" maxlength="80" required value="${esc(draft.name)}" placeholder="例如 PowerX 开发环境"></label><fieldset class="resource-targets"><legend>应用组件（最多 16 个）</legend>${candidates.filter(t=>!other.has(t.id)).map(t=>`<label><input type="checkbox" name="targets" value="${esc(t.id)}" ${draft.targets.includes(t.id)?'checked':''}><span>${esc(t.name)}<small>${esc(t.description)}</small></span></label>`).join('')}</fieldset><p class="muted">宿主机进程按服务或可执行文件与工作目录分组。共享数据库请单独登记；RSS 合计可能重复计算共享内存页。</p><p id="resource-form-error" class="error" role="alert"></p><div class="dialog-actions">${row?resourceButton('重读登记（放弃草稿）',{'reload':row.id})+resourceButton('移除登记',{'remove':row.id}):''}${resourceButton('取消',{'close':'yes'})}<button class="primary" type="submit">保存登记</button></div></form>`;
 if(!dialog.open)dialog.showModal();
}
document.addEventListener('submit',async event=>{
 if(event.target.getAttribute('id')!=='resource-form')return;event.preventDefault();resourceDraft();event.submitter.disabled=true;
 try{await api('resources/apps',{...resourcesUI.draft,revision:resourcesUI.editRevision+1});$('resource-dialog').close();resourcesUI.draft=null;resourcesUI.editor=null;await loadResources();notice('应用监控登记已保存。');}
 catch(error){$('resource-form-error').textContent=resourceText(error.message);}
 finally{event.submitter.disabled=false;}
});
document.addEventListener('input',event=>{if(event.target.closest('#resource-form'))resourceDraft();});
document.addEventListener('change',event=>{
 if(event.target.id==='resource-search'){resourcesUI.query=event.target.value;renderResourcesPage();}
 if(event.target.id==='resource-gpu-select'){resourcesUI.gpuSelected=event.target.value;renderResourcesPage();}
 if(event.target.id==='resource-history-select'){resourcesUI.selected=event.target.value;resourcesUI.history=null;loadResourceHistory();}
});
document.addEventListener('click',async event=>{
 const b=event.target.closest('[data-resource-gpu-view],[data-resource-reload],[data-resource-refresh],[data-resource-edit],[data-resource-bind],[data-resource-close],[data-resource-detail],[data-resource-target],[data-resource-remove],[data-resource-history]');if(!b||b.disabled||!loggedIn)return;event.preventDefault();const v=b.dataset;
 try{
  if(v.resourceGpuView){location.hash='resources/gpu';return;}
  if(v.resourceReload){resourcesUI.editor=null;resourcesUI.draft=null;resourceEdit(v.resourceReload);return;}
  if(v.resourceRefresh){await loadResources();return;}
  if(v.resourceEdit||v.resourceBind){resourceEdit(v.resourceEdit||'new',v.resourceBind);return;}
  if(v.resourceClose){resourceDraft();$('resource-dialog').close();return;}
  if(v.resourceHistory){await loadResourceHistory();return;}
  if(v.resourceRemove){const row=resourcesUI.data.apps.find(a=>a.id===v.resourceRemove);$('detail-title').textContent='移除监控登记';$('detail-content').innerHTML=`<p>只移除「${esc(row.name)}」的监控分组，应用继续运行。</p><label>输入应用名称<input id="resource-remove-confirm"></label>${resourceButton('确认移除',{'remove-confirm':row.id})}`;$('resource-dialog').close();$('detail-dialog').showModal();return;}
  const r=resourcesUI.data.report,app=resourcesUI.data.apps.find(a=>a.id===v.resourceDetail),ids=app?.targets||[v.resourceTarget],targets=(r?.targets||[]).filter(t=>ids.includes(t.id)),processes=(r?.processes||[]).filter(p=>ids.includes(p.target));
  $('detail-title').textContent=app?.name||targets[0]?.name||'应用详情';
  $('detail-content').innerHTML=table(['组件','CPU','内存 / 口径'],targets.map(t=>`<tr><td>${esc(t.name)}</td><td>${resourceCPU(t.cpu_percent)}</td><td>${resourceBytes(t.memory_bytes)}<small>${t.memory_kind==='rss'?'RSS（共享页可能重复）':'容器工作集'}</small></td></tr>`))+panel('宿主机进程','容器内部进程不在此列表；这里显示最近采样。',table(['PID / 父 PID','进程','CPU / 内存'],processes.map(p=>`<tr><td>${p.pid} / ${p.ppid}</td><td>${esc(p.name)}</td><td>${resourceCPU(p.cpu_percent)} / ${resourceBytes(p.memory_bytes)}</td></tr>`)));
  $('detail-dialog').showModal();
 }catch(error){notice(resourceText(error.message),true);}
});
document.addEventListener('click',async event=>{
 const b=event.target.closest('[data-resource-remove-confirm]');if(!b||b.disabled||!loggedIn)return;const row=resourcesUI.data.apps.find(a=>a.id===b.dataset.resourceRemoveConfirm);b.disabled=true;
 try{await api('resources/apps/'+row.id+'/remove',{revision:row.revision,confirmation:$('resource-remove-confirm').value});$('detail-dialog').close();resourcesUI.editor=null;resourcesUI.draft=null;if(resourcesUI.selected===row.id){resourcesUI.selected='host';resourcesUI.history=null;}await loadResources();notice('已移除监控登记，应用继续运行。');}
 catch(error){notice(resourceText(error.message),true);}finally{b.disabled=false;}
});

// Storage stays a device list; capacity and I/O details are opened on demand.
const storageUI={detail:'',view:'capacity'};
const storageBytes=v=>v===null||v===undefined?'不可用':v>=2**40?(v/2**40).toFixed(2)+' TiB':v>=2**30?(v/2**30).toFixed(2)+' GiB':(v/2**20).toFixed(1)+' MiB';
const storageRate=(v,state='sampling')=>v===null||v===undefined?(state==='unavailable'?'不可用':'待采样'):v>=2**20?(v/2**20).toFixed(2)+' MiB/s':(v/1024).toFixed(1)+' KiB/s';
const storageKind=d=>({partition:'分区',lvm:'LVM 卷',virtual:'虚拟设备'}[d.kind]||(d.rotational===true?'机械硬盘':d.rotational===false?'固态硬盘':'硬盘'));
const storageHealth=d=>d.health==='passed'?'SMART 通过':d.health==='failed'?'SMART 告警':'健康数据不可用';
function storageNotice(s){
 if(!s||s.state==='not-collected')return '尚未采集硬盘指标，请核对采集器版本。';
 if(s.state==='unavailable')return '硬盘指标无法读取，CPU、内存和 GPU 指标仍可独立查看。';
 if(s.state==='not-detected')return '当前未发现可读取的块设备，请核对采集器可见范围。';
 const notes=[];
 if(s.limit_reached)notes.push('硬盘或挂载发现达到采集上限，当前列表可能不完整。');
 if(s.unreadable_devices||s.mounts_unavailable||s.filesystems.some(f=>f.state!=='ready'))notes.push('部分磁盘或挂载容量无法读取，缺失项显示“不可用”。');
 if(s.devices.some(d=>d.health==='failed'))notes.push('有硬盘报告 SMART 告警，请查看硬盘详情。');
 const low=s.filesystems.filter(f=>f.state==='ready'&&f.available_bytes/f.total_bytes<.1).map(f=>f.mounts[0].path);
 if(low.length)notes.push('可用空间低于 10%：'+low.join('、'));
 return notes.join(' ');
}
function storageFamily(s,id){
 const selected=new Set([id]);
 for(let i=0;i<s.devices.length;i++){const old=selected.size;for(const d of s.devices)if(d.parents.some(p=>selected.has(p)))selected.add(d.id);if(old===selected.size)break;}
 return selected;
}
function resourceStorage(s){
 if(!s?.devices.length)return `<div class="empty">${esc(storageNotice(s))}</div>`;
 const disks=s.devices.filter(d=>d.kind==='disk');
 return panel('硬盘与存储',`${disks.length} 块物理硬盘 · ${s.filesystems.length} 个已挂载文件系统；容量按层级展示，不重复相加（GiB / TiB）。`,
 table(['硬盘 / 接口','物理容量','挂载位置','读 / 写速率','健康','操作'],disks.map(d=>{
  const family=storageFamily(s,d.id),mounts=s.filesystems.filter(f=>family.has(f.device)).flatMap(f=>f.mounts.map(m=>m.path));
  return `<tr><td><strong>${esc(d.model)}</strong><small>${esc(d.name)} · ${esc(d.transport.toUpperCase())} · ${esc(storageKind(d))}</small></td><td>${storageBytes(d.size_bytes)}</td><td>${mounts.length?mounts.map(m=>`<small class="resource-path">${esc(m)}</small>`).join(''):'未发现挂载'}</td><td>${storageRate(d.io.read_bytes_per_second,d.io.state)}<small>${storageRate(d.io.write_bytes_per_second,d.io.state)}</small></td><td>${badge(storageHealth(d),d.health==='passed'?'good':d.health==='failed'?'warn':'')}<small>温度 ${gpuValue(d.temperature_celsius,' °C')}</small></td><td>${resourceButton('查看详情',{'storage-detail':d.id})}</td></tr>`;
 })))+`<p class="muted">温度和 SMART 按当前读取权限展示；未取得数据不代表硬盘健康。分区、LVM 卷及虚拟设备可在下方按需查看。</p><details class="storage-logical"><summary>分区与逻辑设备（${s.devices.length-disks.length} 个）</summary>${table(['设备','类型 / 上级','容量','操作'],s.devices.filter(d=>d.kind!=='disk').map(d=>`<tr><td>${esc(d.model)}<small>${esc(d.name)}</small></td><td>${esc(storageKind(d))}<small>${esc(d.parents.map(id=>s.devices.find(p=>p.id===id)?.name||'未知').join('、')||'未发现上级')}</small></td><td>${storageBytes(d.size_bytes)}</td><td>${resourceButton('查看详情',{'storage-detail':d.id})}</td></tr>`))}</details>`;
}
function renderStorageDialog(){
 const dialog=$('storage-dialog');if(!dialog?.open)return;
 const changed=dialog.dataset.view!==storageUI.view,focusId=dialog.contains(document.activeElement)?document.activeElement.id:'';
 const r=resourcesUI.data?.report,s=r?.storage,d=s?.devices.find(d=>d.id===storageUI.detail);
 if(!d){dialog.innerHTML='<h2>硬盘暂未发现</h2><p>设备可能已移除，请刷新服务器指标。</p>'+resourceButton('关闭',{'storage-close':'yes'});return;}
 const fresh=r.fresh&&Date.now()/1000-r.at<=90,notes=[!fresh?'采集数据已过期，当前资源占用未确认。':'',storageNotice(s)].filter(Boolean),family=storageFamily(s,d.id);
 const fs=s.filesystems.filter(f=>family.has(f.device));
 let body;
 if(storageUI.view==='capacity')body=`<div class="kpis resource-kpis"><article class="kpi"><div class="kpi-label">${d.kind==='disk'?'物理硬盘容量':'设备容量'}</div><strong>${storageBytes(d.size_bytes)}</strong><small>${esc(d.name)} · ${esc(storageKind(d))}</small></article><article class="kpi"><div class="kpi-label">关联文件系统</div><strong>${fs.length}</strong><small>同一文件系统的绑定挂载不重复计数</small></article></div>`+table(['挂载 / 文件系统','容量 / 可用','已用 / 使用率'],fs.map(f=>`<tr><td>${f.mounts.map(m=>`<small class="resource-path">${esc(m.path)}${m.read_only?'（只读）':''}</small>`).join('')}<small>${esc(s.devices.find(d=>d.id===f.device)?.name||'')} · ${esc(f.fstype)}</small></td><td>${storageBytes(f.total_bytes)}<small>可用 ${storageBytes(f.available_bytes)}</small></td><td>${storageBytes(f.used_bytes)}<small>${gpuValue(f.used_percent,'%')}</small>${f.state!=='ready'?badge('容量不可用','warn'):''}</td></tr>`))+`<p class="muted">文件系统容量与物理盘容量不同；“可用”扣除了文件系统保留空间。使用率按已用 /（已用 + 可用）计算，与 df 一致。未挂载分区不推测可用容量。</p>`;
 else body=`<div class="kpis resource-kpis">${[['读取速率',storageRate(d.io.read_bytes_per_second,d.io.state)],['写入速率',storageRate(d.io.write_bytes_per_second,d.io.state)],['读 / 写 IOPS',d.io.read_iops===null?(d.io.state==='unavailable'?'不可用':'待采样'):gpuValue(d.io.read_iops)+' / '+gpuValue(d.io.write_iops)],['I/O 忙碌时间比例',gpuValue(d.io.busy_percent,'%')]].map(([label,value])=>`<article class="kpi"><div class="kpi-label">${esc(label)}</div><strong>${esc(value)}</strong></article>`).join('')}</div><div class="stack-row"><span>温度</span><strong>${gpuValue(d.temperature_celsius,' °C')}</strong></div><div class="stack-row"><span>健康状态</span><strong>${esc(storageHealth(d))}</strong></div>${d.health_at?`<div class="stack-row"><span>健康检查采样</span><span>${date(d.health_at*1000)}</span></div>`:''}<p class="muted">${d.io.state==='ready'?`最近 ${d.io.interval_seconds} 秒的平均读写速率。`:d.io.state==='unavailable'?'无法读取这台设备的 I/O 指标。':'首次采样、设备重新接入或计数器重置后，需等待下一轮采样。'}忙碌时间比例不等同于硬盘带宽或性能上限。SMART 只读取已有状态，不启动自检。</p>`;
 dialog.innerHTML=`<div class="detail-head"><div><h2>${esc(d.model)}</h2><p class="muted">${esc(d.name)} · ${esc(d.transport.toUpperCase())} · ${date(r.at*1000)}</p></div>${resourceButton('关闭',{'storage-close':'yes'})}</div>${notes.map(n=>`<div class="resource-alert" role="alert">${esc(n)}</div>`).join('')}<div class="vertical-tabview storage-tabs"><div class="vertical-tab-list" role="tablist" aria-label="硬盘详情分区" aria-orientation="${matchMedia('(max-width:720px)').matches?'horizontal':'vertical'}">${[['capacity','容量与挂载'],['io','读写与健康']].map(([key,label])=>`<button id="storage-tab-${key}" type="button" role="tab" tabindex="${key===storageUI.view?0:-1}" aria-selected="${key===storageUI.view}" aria-controls="storage-detail-panel" class="${key===storageUI.view?'active':''}" data-resource-storage-tab="${key}">${label}</button>`).join('')}</div><section id="storage-detail-panel" role="tabpanel" aria-labelledby="storage-tab-${storageUI.view}">${body}</section></div>`;
 dialog.dataset.view=storageUI.view;if(changed){dialog.scrollTop=0;dialog.scrollLeft=0;}
 if(focusId)$(focusId)?.focus({preventScroll:true});
}
document.addEventListener('keydown',event=>{
 const b=event.target.closest('[data-resource-storage-tab]');if(!b||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key))return;event.preventDefault();
 storageUI.view=event.key==='Home'?'capacity':event.key==='End'?'io':storageUI.view==='capacity'?'io':'capacity';renderStorageDialog();$(`storage-tab-${storageUI.view}`)?.focus({preventScroll:true});
});
document.addEventListener('click',event=>{
 const b=event.target.closest('[data-resource-storage-view],[data-resource-storage-detail],[data-resource-storage-tab],[data-resource-storage-close]');if(!b||!loggedIn)return;event.preventDefault();
 if(b.dataset.resourceStorageView){location.hash='resources/storage';return;}
 let dialog=$('storage-dialog');
 if(b.dataset.resourceStorageClose){dialog?.close();return;}
 if(b.dataset.resourceStorageTab){storageUI.view=b.dataset.resourceStorageTab;renderStorageDialog();return;}
 const id=b.dataset.resourceStorageDetail;if(storageUI.detail!==id){storageUI.detail=id;storageUI.view='capacity';}
 if(!dialog){dialog=document.createElement('dialog');dialog.id='storage-dialog';dialog.className='storage-dialog';document.body.append(dialog);}
 if(!dialog.open)dialog.showModal();renderStorageDialog();
});
