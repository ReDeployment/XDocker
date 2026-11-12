'use strict';
const serviceHubUI={data:null,error:'',loading:false,view:'overview',drafts:{},editor:null,busy:false};
const hubViews={overview:'接入概览',projects:'项目登记',services:'常驻服务',records:'操作记录'};
const hubMessages={XD_SERVICE_BINDING_UNAVAILABLE:'接入地址或服务授权已变化，请重新选择。',XD_SERVICE_REVISION_CONFLICT:'项目已被修改，请重新读取登记。',XD_SERVICE_POLICY_REVISION_CONFLICT:'主机接入策略版本已变化。',XD_SERVICE_DOMAIN_CONFLICT:'域名已被其他入口使用。',XD_SERVICE_ACTION_NOT_GRANTED:'该服务尚未获准执行此操作。',XD_SERVICE_OPERATIONS_DISABLED:'运行操作尚未开启；登记和读取仍可使用。',XD_SERVICE_NOT_SUPERVISED:'该项目由自己的开发进程运行，尚未关联常驻服务。',XD_SERVICE_STATUS_UNAVAILABLE:'无法读取 systemd 状态，请检查主机权限与服务名称。',XD_SERVICE_DEFINITION_CHANGED:'服务定义或加载状态已变化，操作已阻断，请重新核对授权。',XD_SERVICE_OBSERVATION_CHANGED:'服务运行状态发生变化，请重新读取后确认。',XD_SERVICE_CONFIRMATION_REQUIRED:'请输入完整目标名称确认。',XD_SERVICE_COMMAND_TIMEOUT:'命令已超时，结果未确认；请先检查实际状态。',XD_SERVICE_ACTION_REJECTED:'系统拒绝了本次服务操作，请检查权限与服务日志。',XD_SERVICE_RUNTIME_UNCONFIRMED:'命令返回后实际服务状态仍未确认。',XD_GATEWAY_APPLY_DISABLED:'主机尚未启用 XDocker 网关。',XD_GATEWAY_PLAN_CHANGED:'项目或入口配置发生变化，请重新预览。',XD_GATEWAY_CONFIG_CHECK_FAILED:'Nginx 配置检查失败，原配置保持保留。',XD_GATEWAY_PORT_IN_USE:'网关端口已被占用，请检查主机接入策略。',XD_GATEWAY_BIND_PERMISSION_REQUIRED:'网关尚无绑定标准端口的权限，请按接入指南配置 XDocker 的标准端口授权。',XD_GATEWAY_OWNER_UNCONFIRMED:'无法确认网关进程归属，本次未接管已有 Nginx。',XD_GATEWAY_RUNTIME_UNCONFIRMED:'网关实际加载版本未确认，请检查回退记录与实际入口。',XD_SERVICE_PROXY_LOOP:'目标地址指向网关本身，请更换接入端口。',XD_SERVICE_MONITOR_UNAVAILABLE:'资源监控分组尚未登记或已被移除。'};
const hubText=value=>hubMessages[value]||value;
const hubButton=(text,data={},blocked=false)=>`<button type="button" ${Object.entries(data).map(([k,v])=>`data-hub-${k}="${esc(v)}"`).join(' ')} ${blocked||serviceHubUI.busy?'disabled':''}>${esc(text)}</button>`;
function resetServiceHub(){serviceHubUI.data=null;serviceHubUI.error='';serviceHubUI.drafts={};serviceHubUI.editor=null;sitesUI.source='all';sitesUI.lastView='';$('hub-dialog')?.close();}
async function loadServiceHub(){
 if(!loggedIn||serviceHubUI.loading)return;serviceHubUI.loading=true;
 try{const data=await api('service-hub');if(loggedIn){serviceHubUI.data=data;serviceHubUI.error='';}}
 catch(error){serviceHubUI.error=error.message;}
 finally{serviceHubUI.loading=false;if(loggedIn){if(current==='service-hub')renderServiceHubPage();if(current==='sites')renderSitesPage();if(current==='overview')render();}}
}
const hubEndpoint=(alias,d=serviceHubUI.data)=>{const e=d?.endpoints[alias];return e?`${e.scheme}://${e.host.includes(':')?'['+e.host+']':e.host}:${e.port}`:'接入地址待核对';};
function hubEntry(project,data,applied){const port=project.tls?data.gateway?.https_port:data.gateway?.http_port,suffix=port===(project.tls?443:80)?'':':'+port;return applied&&project.enabled&&port?`<a href="${project.tls?'https':'http'}://${esc(project.domain)}${suffix}" target="_blank" rel="noreferrer">${esc(project.domain)} ↗</a>`:esc(project.domain);}
function renderServiceHubPage(){
 if(location.hash==='#service-hub/gateway'){current='sites';moduleViews.sites='gateway';location.replace('#sites/gateway');render();if(loggedIn)loadServiceHub();return;}
 const route=location.hash.slice(1).split('/')[1];if(hubViews[route])serviceHubUI.view=route;
 const d=serviceHubUI.data,view=serviceHubUI.view,errors=[serviceHubUI.error,d?.gateway?.error].filter(Boolean);
 if(d?.projects.some(p=>!p.endpoint_available||!p.service_available||p.monitor_available===false))errors.push('部分项目的接入绑定已失效，请重新核对登记。');
 if(d?.operations.some(o=>o.state==='unconfirmed'))errors.push('有运行操作结果未确认，请在操作记录中核对实际状态，勿自动重复执行。');
 if(d?.gateway?.last_error)errors.push(d.gateway.last_error.rollback_verified?'上次网关应用失败，已核对恢复结果；请重新预览后处理。':'上次网关应用或回退结果未确认，请先核对实际入口与网关进程。');
 let body='<div class="empty">正在读取独立服务接入…</div>';
 if(d){
  const enabled=d.projects.filter(p=>p.enabled),fresh=d.gateway?.runtime_verified&&d.gateway?.fingerprint===d.gateway?.applied_fingerprint;
  if(view==='overview')body=`<div class="kpis hub-kpis">${[['已登记项目',d.projects.length],['纳入网页入口',enabled.length],['关联常驻服务',d.projects.filter(p=>p.service).length]].map(([k,v])=>`<article class="kpi"><div class="kpi-label">${esc(k)}</div><strong>${v}</strong></article>`).join('')}</div>`+panel('接入已有应用','项目继续使用自己的开发与部署方式',`<div class="panel-body"><p>项目先独立运行，再登记已批准的访问地址、域名和可选常驻服务。保存登记不启动应用。</p><div class="stack-row"><span>网关配置</span>${badge(fresh?'已核对当前配置':'待应用或待核对',fresh?'good':'warn')}</div><div class="stack-row"><span>接入地址</span><span>${Object.keys(d.endpoints).length} 个已批准地址</span></div><div class="actions">${hubButton('登记项目',{'edit':'new'},!d.configuration_writable||!Object.keys(d.endpoints).length)}<a href="#sites">管理网站入口 →</a></div></div>`);
  if(view==='projects')body=panel('项目登记','登记入口、监控和交付方式；项目启动命令由项目自身维护',table(['项目','域名 / 目标','交付方式','健康 / 监控','操作'],d.projects.map(p=>`<tr><td><strong>${esc(p.name)}</strong><small>${esc(p.id)}</small>${p.directory?`<small>${esc(p.directory)}</small>`:''}</td><td>${hubEntry(p,d,fresh)}<small>${esc(hubEndpoint(p.endpoint))}</small></td><td>${esc({external:'项目自行部署',source:'源码构建',binary:'二进制',image:'容器镜像'}[p.delivery])}</td><td>${p.health?badge(p.health.state==='healthy'?'连接正常':'连接异常',p.health.state==='healthy'?'good':'warn'):badge('尚未检查')}<small>${p.monitor?'资源分组：'+esc(p.monitor):'尚未关联资源分组'}</small></td><td><div class="actions">${hubButton('编辑',{'edit':p.id},!d.configuration_writable)}${hubButton('检查连接',{'health':p.id},!d.configuration_writable)}${p.monitor?`<a href="#resources/apps">查看资源</a>`:''}</div></td></tr>`)),hubButton('登记项目',{'edit':'new'},!d.configuration_writable||!Object.keys(d.endpoints).length));
  if(view==='services')body=panel('常驻服务','读取已有 systemd 服务；只有明确授权的操作才可执行',table(['项目','常驻服务','读取','运行操作'],d.projects.map(p=>`<tr><td><strong>${esc(p.name)}</strong></td><td>${p.service?esc(d.services[p.service]?.unit||'授权待核对'):'独立开发进程'}<small>${p.service?esc(d.services[p.service]?.scope||''):''}</small></td><td><div class="actions">${hubButton('状态',{'status':p.id},!p.service)}${hubButton('最近日志',{'logs':p.id},!p.service||!d.services[p.service]?.actions.includes('logs'))}</div></td><td>${hubButton('服务操作',{'action':p.id},!p.service||!d.operations_enabled||!d.services[p.service]?.actions.some(a=>['start','stop','restart'].includes(a)))}</td></tr>`)));
  if(view==='records')body=panel('运行操作记录','结果持久保存；超时或重载不自动重放操作',table(['时间','项目 / 操作','结果','说明'],[...d.operations].reverse().map(o=>`<tr><td>${date(o.at*1000)}</td><td>${esc(o.project)}<small>${esc(o.action)}</small></td><td>${badge(o.state==='observed'?'状态已核对':'结果未确认',o.state==='observed'?'good':'warn')}</td><td>${esc(hubText(o.error||'systemd 实际状态符合预期；业务健康仍需单独检查。'))}</td></tr>`)));
  if(!Object.keys(d.endpoints).length&&view!=='records')body='<div class="hub-alert">尚无已批准的接入地址。请先按独立服务接入指南准备这台主机的地址和服务清单。</div>'+body;
 }
 const top=`<div class="intro"><div><h2>独立服务</h2><p class="muted">项目自行启动，统一登记入口、监控与常驻服务。</p></div>${hubButton(serviceHubUI.loading?'正在读取…':'刷新',{'refresh':'yes'},serviceHubUI.loading)}</div><div class="hub-status" role="status">${d?.operations_enabled?'已开放主机明确授权的操作':'仅登记和读取 · 运行操作未开启'}</div>${errors.map(e=>`<div class="hub-alert" role="alert">${esc(hubText(e))}</div>`).join('')}`;
 $('content').innerHTML='<div class="hub-workspace">'+top+verticalTabs('service-hub','独立服务分区',Object.entries(hubViews).map(([key,label])=>({key,label})),view,body)+'</div>';
 if(serviceHubUI.lastView!==view){$(`view-service-hub-${view}`)?.scrollIntoView({block:'nearest',inline:'nearest'});serviceHubUI.lastView=view;}
}
function hubCapture(){const f=$('hub-form'),e=serviceHubUI.editor;if(f&&e?.kind==='project')serviceHubUI.drafts[e.draftKey]={...Object.fromEntries(new FormData(f)),enabled:f.elements.enabled.checked,__revision:e.revision};}
function hubDialog(){let dialog=$('hub-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='hub-dialog';dialog.className='hub-dialog';document.body.append(dialog);dialog.addEventListener('cancel',hubCapture);}return dialog;}
function hubEdit(identifier='new',entry=false){
 const d=serviceHubUI.data,row=d.projects.find(p=>p.id===identifier),draftKey=(entry?'entry:':'')+identifier,draft=serviceHubUI.drafts[draftKey]||row||{id:'',name:'',endpoint:Object.keys(d.endpoints)[0]||'',domain:'',service:'',monitor:'',delivery:'external',tls:'',enabled:false};
 serviceHubUI.editor={kind:'project',id:identifier,entry,draftKey,revision:draft.__revision??row?.revision??0};
 const input=(name,label,extra='')=>`<label>${label}<input name="${name}" value="${esc(draft[name]??'')}" ${extra}></label>`;
 const select=(name,label,items)=>`<label>${label}<select name="${name}">${items.map(([key,label])=>`<option value="${esc(key)}" ${String(draft[name]||'')===String(key)?'selected':''}>${esc(label)}</option>`).join('')}</select></label>`;
 const hidden=names=>names.map(name=>`<input type="hidden" name="${name}" value="${esc(draft[name]??'')}">`).join('');
 const access=select('endpoint','已批准的访问地址',Object.keys(d.endpoints).map(k=>[k,k+' · '+hubEndpoint(k)]))+input('domain','访问域名','required placeholder="例如 app.home.test"')+select('tls','入口协议 / 证书',[['','HTTP'],...Object.keys(d.certificates).map(k=>[k,'HTTPS · '+k])]);
 const enabled=`<label class="hub-check"><input type="checkbox" name="enabled" ${draft.enabled?'checked':''}>纳入下一次网关配置</label>`;
 const project=input('id','项目标识','required pattern="[a-z0-9][a-z0-9-]{0,63}" '+(row?'readonly':''))+input('name','项目名称','required maxlength="80"')+select('service','常驻服务',[['','独立启动，暂不关联'],...Object.entries(d.services).map(([k,v])=>[k,v.unit+' · '+v.scope])])+input('directory','项目目录（可选）')+input('monitor','资源分组标识（可选）')+select('delivery','项目交付方式',[['external','项目自行部署'],['source','源码构建'],['binary','二进制'],['image','容器镜像']]);
 const fields=entry?hidden(['id','name','service','directory','monitor','delivery'])+`<p class="muted">${esc(row.name)} · ${esc(row.id)}</p>`+access:project+(row?hidden(['endpoint','domain','tls'])+`<div><label>网站入口</label><a href="#sites" data-hub-entry-link>到网站入口管理 ${esc(row.domain)} →</a></div>`:access);
 const dialog=hubDialog();dialog.innerHTML=`<form id="hub-form" method="post"><div class="detail-head"><h2>${entry?'编辑网站入口':row?'编辑独立项目':'登记独立项目'}</h2>${hubButton('关闭',{'close':'yes'})}</div><div class="hub-form-body"><div class="hub-fields">${fields}</div>${!entry&&row?`<div hidden>${enabled}</div>`:enabled}<p class="muted">${entry?'保存后请在网关配置中预览并应用。':'登记不会启动项目；域名和转发目标在网站入口统一管理。'}</p></div><p id="hub-form-error" class="error" role="alert"></p><div class="dialog-actions">${row?hubButton('重读登记（放弃草稿）',{'discard':row.id,...(entry?{entryMode:'yes'}:{})})+(!entry?hubButton('移除登记',{'remove':row.id}):''):''}${hubButton('取消',{'close':'yes'})}<button type="submit" class="primary">${entry?'保存入口':'保存登记'}</button></div></form>`;
 if(!dialog.open)dialog.showModal();
}

async function hubConfirm(kind,id){
 const dialog=hubDialog();let extra='',target,editor;
 if(kind==='gateway'){const plan=await api('service-hub/gateway/preview');target='xdocker-gateway';editor={kind,id,plan};extra=`<p>将检查并应用 ${plan.routes} 条网页入口，监听 ${esc(plan.address)}。</p><p class="muted">应用失败会尝试恢复原配置；实际入口版本会再次核对。</p>`;}
 else if(kind==='action'){const p=serviceHubUI.data.projects.find(p=>p.id===id),s=(await api('service-hub/projects/'+id+'/status')).result;target=p.name;editor={kind,id,status:s,idempotency_key:crypto.randomUUID()};extra=`<p>${esc(s.unit)} · 当前 ${esc(s.state)}，PID ${s.pid??'—'}</p><label>操作<select name="action">${s.actions.filter(a=>['start','stop','restart'].includes(a)).map(a=>`<option value="${a}">${esc({start:'启动',stop:'停止',restart:'重启'}[a])}</option>`).join('')}</select></label>`;}
 else {const p=serviceHubUI.data.projects.find(p=>p.id===id);target=p.name;editor={kind,id,revision:p.revision};extra='<p>移除登记后需要重新应用网关配置。应用本身继续运行。</p>';}
 serviceHubUI.editor=editor;dialog.innerHTML=`<form id="hub-operation-form"><div class="detail-head"><h2>确认${kind==='gateway'?'应用网关':kind==='action'?'服务操作':'移除登记'}</h2>${hubButton('关闭',{'close':'yes'})}</div>${extra}<label>输入完整名称 <strong>${esc(target)}</strong><input name="confirmation" required autocomplete="off"></label><p id="hub-form-error" class="error" role="alert"></p><div class="dialog-actions">${hubButton('取消',{'close':'yes'})}<button type="submit" class="primary">确认执行</button></div></form>`;if(!dialog.open)dialog.showModal();
}
document.addEventListener('submit',async event=>{
 const id=event.target.getAttribute('id');if(!['hub-form','hub-operation-form'].includes(id))return;event.preventDefault();const form=event.target,e=serviceHubUI.editor,v=Object.fromEntries(new FormData(form));hubCapture();serviceHubUI.busy=true;event.submitter.disabled=true;
 try{
  if(e.kind==='project')await api('service-hub/projects',{...v,revision:e.revision+1,directory:v.directory||null,service:v.service||null,monitor:v.monitor||null,tls:v.tls||null,enabled:form.elements.enabled.checked});
  else if(e.kind==='gateway')await api('service-hub/gateway/apply',{fingerprint:e.plan.fingerprint,confirmation:v.confirmation});
  else if(e.kind==='remove')await api('service-hub/projects/'+e.id+'/remove',{revision:e.revision,confirmation:v.confirmation});
  else await api('service-hub/projects/'+e.id+'/action',{action:v.action,revision:e.status.revision,policy_sha256:e.status.policy_sha256,observation:e.status.observation,confirmation:v.confirmation,idempotency_key:e.idempotency_key});
  if(e.kind==='project')delete serviceHubUI.drafts[e.draftKey];serviceHubUI.editor=null;$('hub-dialog').close();await loadServiceHub();notice(e.kind==='project'?'项目接入已保存，应用启动方式保持独立。':'操作结果已返回，请继续核对入口和业务健康。');
 }catch(error){$('hub-form-error').textContent=hubText(error.message);}
 finally{serviceHubUI.busy=false;event.submitter.disabled=false;if(current==='service-hub')renderServiceHubPage();if(current==='sites')renderSitesPage();if(current==='overview')render();}
});
document.addEventListener('input',event=>{if(event.target.closest('#hub-form'))hubCapture();});
document.addEventListener('change',event=>{if(event.target.closest('#hub-form'))hubCapture();});
document.addEventListener('click',async event=>{
 const b=event.target.closest('[data-hub-entry],[data-hub-discard],[data-hub-view],[data-hub-refresh],[data-hub-edit],[data-hub-close],[data-hub-health],[data-hub-status],[data-hub-logs],[data-hub-action],[data-hub-preview],[data-hub-gateway],[data-hub-remove]');if(!b||b.disabled||!loggedIn)return;event.preventDefault();const d=b.dataset;
 try{
  if(d.hubDiscard){delete serviceHubUI.drafts[(d.hubEntryMode?'entry:':'')+d.hubDiscard];serviceHubUI.editor=null;await loadServiceHub();hubEdit(d.hubDiscard,!!d.hubEntryMode);return;}
  if(d.hubEntry){hubEdit(d.hubEntry,true);return;}
  if(d.hubView){location.hash='service-hub/'+d.hubView;serviceHubUI.view=d.hubView;renderServiceHubPage();return;}
  if(d.hubRefresh){if(current==='sites')await refresh();else await loadServiceHub();return;}
  if(d.hubEdit){hubEdit(d.hubEdit);return;}
  if(d.hubClose){hubCapture();$('hub-dialog').close();return;}
  if(d.hubAction||d.hubGateway||d.hubRemove){await hubConfirm(d.hubAction?'action':d.hubGateway?'gateway':'remove',d.hubAction||d.hubRemove||'gateway');return;}
  if(d.hubHealth){await api('service-hub/projects/'+d.hubHealth+'/health',{});await loadServiceHub();return;}
  if(d.hubStatus){const s=(await api('service-hub/projects/'+d.hubStatus+'/status')).result;$('detail-title').textContent=s.unit;$('detail-content').innerHTML=`<p>状态：${esc(s.state)} / ${esc(s.substate)} · PID ${s.pid??'—'}</p><p>服务定义：${s.definition_matches?'与授权匹配':'尚未匹配写操作授权'}</p><p>定义重新加载：${s.reload_required?'需要核对':'无待加载变更'}</p>`;}
  if(d.hubLogs){const s=await api('service-hub/projects/'+d.hubLogs+'/logs');$('detail-title').textContent='常驻服务最近日志';$('detail-content').innerHTML=`<p>${s.truncated?'输出已截断':'最近最多 100 行'} · 常见凭据已脱敏</p><pre>${esc(s.text)}</pre>`;}
  if(d.hubPreview){const s=await api('service-hub/gateway/preview');$('detail-title').textContent='Nginx 配置预览';$('detail-content').innerHTML=`<p>${s.routes} 条入口 · ${esc(s.address)}</p><pre>${esc(s.configuration)}</pre>`;}
  $('detail-dialog').showModal();
 }catch(error){notice(hubText(error.message),true);}
});

const sitesUI={source:'all',lastView:''};
function renderSitesPage(){
 const d=serviceHubUI.data,view=moduleViews.sites||'overview',fresh=!!(d?.gateway?.runtime_verified&&d.gateway.fingerprint===d.gateway.applied_fingerprint),legacy=snapshot?.sites||[],projects=d?.projects||[],alerts=[];
 if(serviceHubUI.error)alerts.push('独立项目入口读取失败：'+hubText(serviceHubUI.error));
 if(!snapshot)alerts.push('部署网站尚未读取，请检查管理连接。');
 if(d?.gateway?.error)alerts.push(hubText(d.gateway.error));
 if(d?.gateway?.last_error)alerts.push(d.gateway.last_error.rollback_verified?'上次网关应用失败，已恢复原配置。':'上次网关应用或回退结果尚未确认，请先核对实际入口。');
 for(const p of projects){
  if(!p.endpoint_available)alerts.push(p.domain+' 的目标地址已失效，请重新编辑入口。');
  const h=p.health||p.last_health;if(h&&h.state!=='healthy')alerts.push(p.domain+'：'+(h.status?'应用返回 HTTP '+h.status+'，请检查域名放行和访问权限。':'应用暂时无法连接，请核对运行状态。'));
 }
 const nativeRows=projects.map(p=>{const h=p.health||p.last_health;return `<tr data-site-source="project" data-site-id="${esc(p.id)}"><td><strong>${hubEntry(p,d,fresh)}</strong><small>${esc(p.name)}</small></td><td>${esc(hubEndpoint(p.endpoint))}</td><td>${badge(p.enabled?(fresh?'配置已加载':'待应用网关'):'未纳入网关',p.enabled&&fresh?'good':'warn')}<small>${h?(h.state==='healthy'?'应用连接正常':h.status?'应用返回 HTTP '+h.status:'应用无法连接'):'应用尚未检查'}</small>${h?`<small>${p.health?'检查于':'上次检查于'} ${date(h.at*1000)}</small>`:''}</td><td>${badge('独立项目','blue')}</td><td><div class="actions">${hubButton('编辑入口',{entry:p.id},!d.configuration_writable)}${hubButton('检查连接',{health:p.id},!d.configuration_writable)}</div></td></tr>`;});
 const legacyRows=legacy.map(s=>`<tr data-site-source="deployment" data-site-id="${esc(s.key)}"><td><strong><a href="https://${esc(s.domain)}" target="_blank" rel="noreferrer">${esc(s.domain)} ↗</a></strong><small>${esc(s.key)}</small></td><td>${esc(s.service)}<small>${esc(s.kind==='frp_http'?'FRP 穿透':s.kind==='app'?'动态应用':'静态站点')}</small></td><td>${badge(s.enabled?'已启用':'未启用',s.enabled?'good':'')}<small>${esc(s.probe?.output||'尚未执行公网检查')}</small>${s.probe?`<small>${date(s.probe.at)}</small>`:''}</td><td>${badge('部署网站')}<small>配置由部署仓库维护</small></td><td>${btn('检查连接',{probe:s.key},!s.enabled||d?.native===true)}</td></tr>`);
 let body;
 if(view==='gateway')body=d?panel('网关配置','将独立项目的域名转发到已批准地址',`<div class="panel-body"><div class="stack-row"><span>监听地址</span><strong>${esc(d.gateway?.address||'尚未核对')}</strong></div><div class="stack-row"><span>配置状态</span>${badge(fresh?'当前配置已加载':'待应用或待核对',fresh?'good':'warn')}</div><div class="actions">${hubButton('预览完整配置',{preview:'yes'})}${hubButton('检查并应用网关',{gateway:'yes'},!d.operations_enabled||!d.gateway_enabled)}</div><p class="muted">先保存网站入口，再预览并应用。域名需解析到这台主机；应用仍使用自己的启动方式。部署网站继续通过其部署配置发布。</p></div>`):'<div class="empty">正在读取网关配置…</div>';
 else{
  const rows=sitesUI.source==='project'?nativeRows:sitesUI.source==='deployment'?legacyRows:[...nativeRows,...legacyRows];
  body=panel('网站列表',`${projects.length} 个独立项目入口 · ${legacy.length} 个部署网站`,table(['域名 / 项目','转发目标','配置 / 应用检查','管理方式','操作'],rows),d?hubButton('登记项目',{edit:'new'},!d.configuration_writable||!Object.keys(d.endpoints).length):'');
 }
 const filter=`<label class="sites-filter">当前范围<select id="sites-source"><option value="all" ${sitesUI.source==='all'?'selected':''}>全部入口</option><option value="project" ${sitesUI.source==='project'?'selected':''}>独立项目</option><option value="deployment" ${sitesUI.source==='deployment'?'selected':''}>部署网站</option></select></label>`;
 const top=`<div class="intro"><div><h2>网站入口</h2><p class="muted">统一查看域名、转发目标与应用连接状态。</p></div>${hubButton('刷新',{refresh:'yes'},serviceHubUI.loading)}</div>${alerts.map(message=>`<div class="hub-alert" role="alert">${esc(message)}</div>`).join('')}${filter}`;
 $('content').innerHTML='<div class="hub-workspace sites-workspace">'+top+verticalTabs('sites','网站入口分区',[{key:'overview',label:'网站列表'},{key:'gateway',label:'网关配置'}],view,body)+'</div>';
 if(sitesUI.lastView!==view){$('view-sites-'+view)?.scrollIntoView({block:'nearest',inline:'nearest'});sitesUI.lastView=view;if(innerWidth<=720)document.querySelector('.sites-workspace .vertical-tab-list')?.scrollIntoView({block:'start'});}
}
document.addEventListener('change',event=>{if(event.target.id==='sites-source'){sitesUI.source=event.target.value;renderSitesPage();}});
document.addEventListener('click',event=>{if(event.target.closest('[data-hub-entry-link]')){hubCapture();$('hub-dialog')?.close();}});
