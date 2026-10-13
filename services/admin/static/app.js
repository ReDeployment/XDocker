'use strict';
const $ = id => document.getElementById(id);
const titles = {overview:'运行概览', instances:'PowerX 实例', services:'容器服务', sites:'网站入口', certificates:'HTTPS 证书', frp:'FRP 穿透', backups:'实例备份', audit:'操作记录', preflight:'部署前检查', 'backup-plans':'备份计划', 'image-versions':'镜像版本', github:'研发事件'};
let csrf='', current='overview', snapshot=null, retention={}, jobs=[], audit=[], backupData={instances:[],backups:[],application_centers:[]}, pending=null, trackedJob=null, loggedIn=false, refreshing=false;
let instanceList=[], selectedInstance='', instanceDetail=null, instanceError='';
const credentials=new Map();
const connection={state:'checking',message:'正在检查管理连接…',ready:false,lastRead:null,failedAt:null};
let checkingConnection=false, connectionEpoch=0;
const preflight={items:[],error:'',loading:false,running:null,cancelled:false,progress:''};
const backupPlans={rows:[],loaded:false,loading:false,error:'',enabled:false,running:false,schedulerError:null,reachable:false};
let editingPlan=null;
const imageVersions={instances:[],candidates:[],checks:[],selected:'',channel:'all',current:null,local:{},preview:null,loading:false,action:null,error:'',catalogError:null,catalogNotice:''};
const githubData={projects:[],notifications:[],unread:0,nextBefore:null,loading:false,reachable:false,error:'',autoEnabled:false,running:false,interval:300,editing:null,webhooks:null};
let githubEpoch=0,githubSummaryLoading=false;
const instanceModules={overview:'概览',postgres:'PostgreSQL',redis:'Redis',storage:'文件存储',services:'容器服务',backups:'备份'};
let instanceModule='overview';
const initialRoute=location.hash.slice(1).split('/');
if(titles[initialRoute[0]])current=initialRoute[0];
if(current==='instances'&&/^[a-z0-9][a-z0-9-]*$/.test(initialRoute[1]||''))selectedInstance=initialRoute[1];
if(instanceModules[initialRoute[2]])instanceModule=initialRoute[2];
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '尚无记录';
const badge = (text, type='') => `<span class="badge ${type}">${esc(text)}</span>`;
const stateBadge = value => badge({running:'运行中',exited:'已停止',created:'待启动',restarting:'重启中',success:'成功',ready:'已完成',interrupted:'未确认',resume_failed:'恢复运行失败',failed:'失败',running_job:'执行中',interrupted:'未确认',cancelled:'已取消',skipped:'已跳过',reachable:'可访问'}[value]||value, ['running','success','ready','reachable'].includes(value)?'good':['failed','dead','interrupted'].includes(value)?'bad':value==='exited'?'warn':'blue');
const certBadge = c => !c.enabled ? badge('未启用') : badge({LOCAL_VALID:'有效',EXPIRING:'即将到期',CRITICAL:'紧急续期',EXPIRED:'已过期',ERROR:'检查异常',NOT_YET_VALID:'尚未生效',UNKNOWN:'待检查'}[c.status]||c.status,c.status==='LOCAL_VALID'?'good':['EXPIRED','CRITICAL','ERROR'].includes(c.status)?'bad':'warn');
const busy = () => jobs.some(j=>j.state==='running');
const disabled = (condition=false) => condition || busy() || !connection.ready ? 'disabled' : '';
const btn = (text, data, condition=false, cls='') => `<button class="${cls}" ${Object.entries(data).map(([k,v])=>`data-${k}="${esc(v)}"`).join(' ')} ${disabled(condition)}>${text}</button>`;
const table = (headers, rows) => `<div class="table-scroll"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${headers.length}" class="empty">暂无数据</td></tr>`}</tbody></table></div>`;
const panel = (title, subtitle, body, action='') => `<section class="panel"><div class="panel-head"><div><h3>${title}</h3>${subtitle?`<p>${subtitle}</p>`:''}</div>${action}</div>${body}</section>`;
function notice(text, bad=false){$('notice').textContent=text;$('notice').className='notice'+(bad?' bad':'');}
class ApiError extends Error{constructor(message,kind,status=0){super(message);this.kind=kind;this.status=status;}}
function connectionState(state,message,ready=false){
  if(['offline','unreachable','unexpected','verification'].includes(state))backupPlans.reachable=false;
  connection.state=state;connection.message=message;connection.ready=ready;
  if(!ready&&state!=='checking'&&!connection.failedAt){connection.failedAt=Date.now();connectionEpoch++;clearCredentials();}
  if(ready)connection.failedAt=null;
  for(const prefix of ['login','shell']){
    $(prefix+'-connection').textContent=message;
    $(prefix+'-connection').dataset.state=state;
    $(prefix+'-connection-time').textContent=(connection.lastRead?'最近成功读取：'+date(connection.lastRead):'尚未成功读取运行状态')+(connection.failedAt?' · 异常始于 '+date(connection.failedAt):'');
  }
  $('last-update').textContent=connection.lastRead?(ready?'更新于 ':'上次数据（连接未就绪） ')+date(connection.lastRead):'';
}
async function requestJSON(url,options={}){
  const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),options.method==='POST'?30000:url.startsWith('/api/image-versions/')?60000:url.startsWith('/api/preflight/')?20000:15000);
  try{
    if(navigator.onLine===false)throw new ApiError('浏览器处于离线状态，请恢复网络后重新检查。','offline');
    let response;
    try{response=await fetch(url,{...options,signal:controller.signal,cache:'no-store'});}catch(error){
      if(navigator.onLine===false)throw new ApiError('浏览器处于离线状态，请恢复网络后重新检查。','offline');
      throw new ApiError(error.name==='AbortError'?'管理请求超时，请检查 SSH 隧道和服务器服务。':'管理接口不可达，请检查 SSH 隧道和服务器服务。','unreachable');
    }
    let data;try{data=await response.json();}catch(error){
      data=null;
      if(response.ok){
        if(navigator.onLine===false)throw new ApiError('浏览器处于离线状态，请恢复网络后重新检查。','offline');
        if(controller.signal.aborted||error.name==='AbortError')throw new ApiError('管理响应读取超时，请检查 SSH 隧道和服务器服务。','unreachable');
        if(error.name!=='SyntaxError')throw new ApiError('管理响应读取中断，请检查 SSH 隧道和服务器服务。','unreachable');
      }
    }
    if(!response.ok){
      const kind=response.status===401?'authentication':response.status===403?'verification':response.status>=500?'server':'request';
      const message=response.status>=500?'管理服务返回 HTTP '+response.status+'，请检查服务器管理服务。':data?.error||'管理请求返回 HTTP '+response.status;
      throw new ApiError(message,kind,response.status);
    }
    if(!data||typeof data!=='object'||Array.isArray(data))throw new ApiError('目标未返回预期的管理接口响应，请检查转发地址。','unexpected');
    return data;
  }finally{clearTimeout(timer);}
}
async function api(path, payload){
  const planControl=path.startsWith('backup-plans/')&&planWritable();
  const githubControl=path==='github/action'&&githubWritable();
  if(payload!==undefined&&path!=='login'&&path!=='logout'&&!connection.ready&&!planControl&&!githubControl)throw new ApiError('管理连接未就绪，请先重新检查连接。','blocked');
  const options=payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(payload)};
  try{return await requestJSON('/api/'+path,options);}catch(error){
    if(error.status===401){if(path!=='login')showLogin();connectionState('authentication',path==='login'?'访问令牌未被接受，请核对令牌或登录次数限制。':'管理服务可达，请登录或重新登录。',true);}
    else if(error.kind==='verification')connectionState('verification',error.message+' 请重新检查连接以更新会话；不会自动重试操作。');
    else if(['offline','unreachable','server','unexpected'].includes(error.kind)){connectionState(error.kind,error.message);if(payload!==undefined&&path!=='login'&&path!=='logout')error.message+=' 请求结果未确认，请恢复连接后先查操作记录和实际状态，再决定是否重新执行。';}
    throw error;
  }
}
async function checkConnection(){
  if(checkingConnection||refreshing)return;checkingConnection=true;
  try{
    const session=await api('session');csrf=session.csrf;
    connectionState('connected','管理连接正常，正在读取运行状态。',true);
    if(!loggedIn)await showShell();else await refresh();
  }catch(error){if(!loggedIn)$('login-error').textContent=error.status===401?'':error.message;else notice(error.message,true);}
  finally{checkingConnection=false;}
}
function showLogin(){githubEpoch++;githubData.reachable=false;githubData.projects=[];githubData.notifications=[];githubData.editing=null;githubData.webhooks=null;preflight.cancelled=true;backupPlans.reachable=false;clearCredentials();instanceDetail=null;loggedIn=false;csrf='';$('shell').classList.add('hidden');$('login').classList.remove('hidden');$('confirm-dialog').close();$('detail-dialog').close();$('plan-dialog').close();editingPlan=null;}
async function showShell(){loggedIn=true;$('login').classList.add('hidden');$('shell').classList.remove('hidden');await refresh();if(current==='preflight'&&loggedIn)await loadPreflight();if(current==='backup-plans'&&loggedIn)await loadBackupPlans();if(current==='image-versions'&&loggedIn)await loadImageVersions();if(loggedIn)await loadGithubSummary();if(current==='github'&&loggedIn)await loadGithub();if(current==='instances'&&selectedInstance)await loadInstance(selectedInstance,instanceModule);}
async function refresh(){
  if(!loggedIn||refreshing)return;refreshing=true;$('refresh').disabled=true;
  try{const wasBusy=busy();const responses=await Promise.allSettled([api('snapshot'),api('jobs'),api('audit'),api('backups'),api('instances')]);const failed=responses.find(r=>r.status==='rejected');if(failed)throw failed.reason;const results=responses.map(r=>r.value);if(!loggedIn)return;snapshot=results[0];jobs=results[1].jobs;audit=results[2].events;backupData=results[3];instanceList=results[4].instances;connection.lastRead=Date.now();connectionState('connected','管理连接正常',true);$('revision').textContent='Git '+snapshot.revision.slice(0,12)+' · Docker '+snapshot.engine.Version;render();const tracked=jobs.find(j=>j.id===trackedJob);if(tracked && tracked.state!=='running'){notice(tracked.state==='success'?'操作完成，请在操作记录中查看实际结果。':'操作未成功，请在操作记录中查看原因。',tracked.state!=='success');trackedJob=null;}else if(busy())notice('有操作正在执行。结果会自动更新，可在操作记录中查看。');else if(wasBusy)notice(jobs[0]?.state==='success'?'操作完成，请在操作记录中查看实际结果。':'操作未成功，请在操作记录中查看原因。',jobs[0]?.state!=='success');else if($('notice').dataset.connectionError==='yes'){notice('连接已恢复；仅恢复状态读取，请在操作记录中核对结果。');$('notice').dataset.connectionError='';}}
  catch(error){notice(error.message,true);$('notice').dataset.connectionError='yes';if(loggedIn)render();}finally{refreshing=false;$('refresh').disabled=false;}
}
function serviceRows(services){return services.map(s=>`<tr><td><strong>${esc(s.service)}</strong><small>${esc(s.name)}</small></td><td>${stateBadge(s.state)}<small>${esc(s.status)}</small></td><td><small class="code-line">${esc(s.image)}</small></td><td>${s.ports.map(p=>`${esc(p.ip)}:${p.host} → ${p.container}`).join('<br>')||'内部网络'}</td><td><div class="actions">${btn('日志 / 资源',{detail:s.id},false)}${btn(s.state==='running'?'停止':'启动',{service:s.id,action:s.state==='running'?'stop':'start'},s.protected,s.state==='running'?'danger':'')}${btn('重启',{service:s.id,action:'restart'},s.protected||s.state!=='running')}</div></td></tr>`);}
function siteRows(sites){return sites.map(s=>`<tr><td><strong><a href="https://${esc(s.domain)}" target="_blank" rel="noreferrer">${esc(s.domain)} ↗</a></strong><small>${esc(s.key)}</small></td><td>${badge(s.kind==='github_webhook'?'Webhook 接收':s.kind==='frp_http'?'FRP 穿透':s.kind==='app'?'动态应用':'静态站点','blue')}<small>${esc(s.service)}</small>${s.client_template?`<small>客户端模板：${esc(s.client_template.address)}:${esc(s.client_template.port)}</small>`:''}</td><td>${s.enabled?badge('已启用','good'):badge('未启用')}</td><td>${s.probe?stateBadge(s.probe.status):badge('待探测')}<small>${esc(s.probe?.output||'尚未执行公网检查')}</small><small>${s.probe?date(s.probe.at):''}</small></td><td>${btn('检查连接',{probe:s.key},!s.enabled)}</td></tr>`);}
function render(){
  document.querySelectorAll('nav [data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===current));$('page-title').textContent=titles[current];
  if(current==='github'){$('content').innerHTML=renderGithub();return;}
  if(current==='preflight'){$('content').innerHTML=renderPreflight();return;}
  if(current==='backup-plans'){$('content').innerHTML=renderBackupPlans();return;}
  if(current==='image-versions'){$('content').innerHTML=renderImageVersions();return;}
  if(!snapshot){$('content').innerHTML='<div class="empty">尚未读取运行状态。可打开 <button data-tab="preflight">部署前检查</button> 核对原因。</div>';return;}
  const services=snapshot.services,sites=snapshot.sites,certs=snapshot.certificates;
  const enabled=sites.filter(s=>s.enabled), activeCerts=certs.filter(c=>c.enabled);
  let html='';
  if(current==='overview'){
    const kpis=[['运行容器',services.filter(s=>s.state==='running').length,`${services.length} 个已部署服务`],['网站入口',enabled.length,`${enabled.filter(s=>s.kind==='frp_http').length} 条 FRP 链路`],['有效证书',activeCerts.filter(c=>c.status==='LOCAL_VALID').length,`${activeCerts.length} 组已启用证书`],['最近操作',jobs.filter(j=>j.state==='running').length,busy()?'正在执行，请稍候':'当前无运行任务']];
    html=`<div class="intro"><div><h2>你好，管理员</h2><p class="muted">查看实际运行状态，掌握服务与入口的健康情况。</p></div>${btn('检查所有站点 ↗',{probe:'all'},false,'primary')}</div><div class="kpis">${kpis.map(([a,b,c])=>`<article class="kpi"><div class="kpi-label">${a}</div><strong>${b}</strong><small>${c}</small></article>`).join('')}</div><div class="columns">`;
    html+=panel('服务运行状态','来自本服务器 Docker Engine',`<div class="panel-body">${services.map(s=>`<div class="stack-row"><div><strong>${esc(s.service)}</strong><small>${esc(s.status)}</small></div>${stateBadge(s.state)}</div>`).join('')}</div>`,btn('全部服务',{tab:'services'}));
    const renew=services.find(s=>s.service==='certbot-renew'); const lastRenew=snapshot.reports.filter(r=>r.action==='renew').sort((a,b)=>b.checked_at.localeCompare(a.checked_at))[0];
    html+='<div>'+panel('证书自动续期','自动调度与证书有效期分别检查',`<div class="panel-body"><div class="stack-row"><span>续期容器</span>${renew?stateBadge(renew.state):badge('未部署','warn')}</div><div class="stack-row"><span>最近续期检查</span><small>${date(lastRenew?.checked_at)}</small></div><div class="stack-row"><span>最近到期</span><strong>${activeCerts.filter(c=>c.days_remaining!==undefined).length?Math.min(...activeCerts.filter(c=>c.days_remaining!==undefined).map(c=>c.days_remaining))+' 天':'待检查'}</strong></div><div class="stack-row"><span>自动检查周期</span><span>约 12 小时</span></div></div>`,btn('管理证书',{tab:'certificates'}));
    html+=panel('服务器环境','当前管理范围：XDocker',`<div class="panel-body"><div class="stack-row"><span>Docker</span><strong>${esc(snapshot.engine.Version)}</strong></div><div class="stack-row"><span>系统 / 架构</span><span>${esc(snapshot.engine.Os)} / ${esc(snapshot.engine.Arch)}</span></div><div class="stack-row"><span>部署提交</span><span class="code-line">${esc(snapshot.revision.slice(0,12))}</span></div></div>`)+ '</div></div>';
    html+=panel('最近操作','请求、结果与时间均保留记录',jobTable(jobs.slice(0,4)),btn('查看全部',{tab:'audit'}));
  }else if(current==='instances'){
    html=renderInstances(services);
  }else if(current==='services'){
    html=`<div class="intro"><div><h2>容器服务</h2><p class="muted">查看日志与资源，按服务执行启停。停止入口服务会影响站点访问。</p></div></div>`+panel('已部署服务',`${services.length} 个服务 · 管理服务自身通过 SSH 维护`,table(['服务','运行状态','镜像版本','宿主机端口','操作'],serviceRows(services)));
  }else if(current==='sites'){
    html=`<div class="intro"><div><h2>域名与网站</h2><p class="muted">连接检查验证 HTTPS；FRP 网站同时检查 /healthz。</p></div>${btn('检查所有站点',{probe:'all'},false,'primary')}</div>`+panel('网站入口','域名、镜像与启用配置来自部署仓库',table(['域名 / 站点','类型 / 服务','配置状态','最近连接检查','操作'],siteRows(sites)))+`<p class="muted">新增网站、修改域名和升级镜像，请在本地修改配置，验证并推送 Git，再由服务器同步。</p>`;
  }else if(current==='certificates'){
    html=`<div class="intro"><div><h2>HTTPS 证书</h2><p class="muted">按证书组管理，共享 SAN 的域名一起续期。续期使用现有 Certbot 配置。</p></div></div>`+panel('证书清单','状态来自真实检查报告；检查时间显示数据是否新鲜',table(['证书 / 域名','状态','到期 / 剩余','最近证书检查','操作'],certs.map(c=>`<tr><td><strong>${esc(c.name)}</strong><small>${c.domains.map(esc).join('<br>')}</small></td><td>${certBadge(c)}</td><td>${c.expiry?date(c.expiry):'尚未签发 / 待检查'}<small>${c.days_remaining!==undefined?c.days_remaining+' 天':''}</small></td><td>${date(c.checked_at)}</td><td><div class="actions">${btn('检查',{certificate:c.name,action:'check'},!c.enabled)}${btn('续期测试',{certificate:c.name,action:'renew-test'},!c.enabled)}${btn('按需续期',{certificate:c.name,action:'renew'},!c.enabled)}</div></td></tr>`)))+`<p class="muted">续期测试使用 Let’s Encrypt 测试环境；按需续期遵守到期判断，不强制重复签发。尚未签发的证书请先按 Git 部署流程建立入口。</p>`;
  }else if(current==='frp'){
    const frp=services.find(s=>s.service==='frps');
    html=`<div class="intro"><div><h2>开发服务穿透</h2><p class="muted">公网域名通过 Nginx 和 FRPS 回到本地客户端。连接状态以端到端检查结果为准。</p></div>${frp?btn('FRPS 日志',{detail:frp.id}):''}</div>`+panel('连接路径',frp?'FRPS '+esc(frp.status):'FRPS 尚未部署',`<div class="topology"><div class="node">浏览器<small>HTTPS 域名</small></div><span class="arrow">→</span><div class="node">Nginx<small>证书 / 入口</small></div><span class="arrow">→</span><div class="node">FRPS<small>容器内部 HTTP</small></div><span class="arrow">⇄</span><div class="node">本地 FRPC<small>TLS 穿透连接</small></div><span class="arrow">→</span><div class="node">本地应用<small>/healthz 检查</small></div></div>`)+panel('穿透域名','客户端令牌和私钥不展示',table(['域名 / 站点','类型 / 服务','配置状态','端到端检查','操作'],siteRows(sites.filter(s=>s.kind==='frp_http'))));
  }else if(current==='backups'){
    html='<div class="intro"><div><h2>实例备份与恢复验证</h2><p class="muted">维护期间暂停所选实例前后端，导出 PostgreSQL、Redis 快照并打包配置与运行文件。备份包含私有密钥。</p></div></div>';
    html+=panel('登记的 PowerX 实例','实例备份可独立于 PowerX 应用是否完成 Setup 运行',table(['实例','创建备份','保留策略','应用内备份'],backupData.instances.map(instance=>`<tr><td><strong>${esc(instance)}</strong></td><td>${btn('创建实例备份',{backup:'create',instance},false,'primary')}</td><td><label>保留最新 <input id="keep-${esc(instance)}" type="number" min="1" max="100" value="${esc(retention[instance]??7)}" data-retention="${esc(instance)}" aria-label="保留成功备份份数"></label>${btn('清理旧备份',{backup:'prune',instance})}</td><td>${backupData.application_centers.filter(c=>c.instance===instance).map(c=>`<a href="${esc(c.url)}" target="_blank" rel="noreferrer">打开 PowerX 备份中心 ↗</a><small>Setup 完成后使用自己的 PowerX 账号管理数据库备份计划</small>`).join('')}</td></tr>`)));
    html+=panel('定时全实例备份','计划与调度开关默认关闭，启用前需确认实例维护影响',`<div class="panel-body"><button data-tab="backup-plans">管理备份计划 →</button><p class="muted">与 PowerX 应用内数据库计划分别管理；不会重复提交手动备份。</p></div>`);
    html+=panel('备份记录','下载前校验所有文件与打包 SHA256；隔离恢复验证不会覆盖运行实例',table(['备份','状态','大小 / 校验','恢复验证','操作'],backupRows(backupData.backups)));
    html+='<p class="muted">手动备份与定时计划分别管理，定时计划须显式启用；不会自动覆盖数据库。完整恢复应在新实例中进行，并单独验收业务数据和文件。</p>';
  }else if(current==='audit'){
    html=panel('操作任务','操作执行期间自动刷新；点击查看结果',jobTable(jobs))+panel('审计记录','记录管理登录、操作请求与最终结果',table(['时间','操作','目标','结果'],audit.map(e=>`<tr><td>${date(e.at)}</td><td>${esc(e.action)}</td><td>${esc(e.target)}</td><td>${stateBadge(e.result)}</td></tr>`)));
  }
  $('content').innerHTML=html;
  if(current==='instances'){for(const [service,value] of credentials){const field=$('credential-'+service);if(field&&value.instance===selectedInstance&&value.expires>Date.now()){field.value=value.password;field.type='text';}}}
}
function jobTable(list){return table(['操作','目标','状态','开始时间','结果'],list.map(j=>`<tr class="job-row" data-job="${esc(j.id)}"><td>${esc({probe:'连接检查',check:'证书检查','renew-test':'续期测试',renew:'按需续期',start:'启动',stop:'停止',restart:'重启','backup-scheduled':'计划实例备份'}[j.action]||j.action)}</td><td>${esc(j.target)}</td><td>${stateBadge(j.state)}</td><td>${date(j.created)}</td><td>查看详情 ↗</td></tr>`));}
function askOperation(path, body, target, title, impact){pending={path,body,target};$('confirm-title').textContent=title;$('confirm-impact').textContent=impact;$('confirm-target').textContent=target;$('confirmation').value='';$('confirm-error').textContent='';$('confirm-dialog').showModal();$('confirmation').focus();}
async function submitOperation(path, body){const result=await api(path,body);trackedJob=result.id;notice('操作已提交，正在执行；实际结果将在操作记录中显示。');await refresh();clearCredentials();current='audit';render();return result;}
async function detailService(id){const s=snapshot.services.find(s=>s.id===id);$('detail-title').textContent=s?.service||'服务详情';$('detail-content').innerHTML='<p class="muted">正在读取日志与资源…</p>';$('detail-dialog').showModal();const results=await Promise.allSettled([api('services/'+id+'/logs'),api('services/'+id+'/stats')]);const logs=results[0],stats=results[1];let html='';if(stats.status==='fulfilled'){const v=stats.value;html=`<div class="resource"><div><strong>${v.cpu_percent??'—'}%</strong><small>CPU 使用率（可超过单核 100%）</small></div><div><strong>${(v.memory_bytes/1048576).toFixed(1)} MB</strong><small>内存工作集</small></div><div><strong>${v.pids??'—'}</strong><small>进程数</small></div></div>`;}else html=`<p class="muted">${esc(stats.reason.message)}</p>`;html+=`<p class="muted">最近 200 行日志 · 已对常见凭据脱敏</p><pre>${esc(logs.status==='fulfilled'?logs.value.text:logs.reason.message)}</pre>`;$('detail-content').innerHTML=html;}
document.addEventListener('click',async event=>{const button=event.target.closest('[data-tab],[data-probe],[data-detail],[data-service],[data-certificate],[data-job],[data-backup],[data-open-instance],[data-instance-module],[data-credential],[data-copy-credential],[data-hide-credential],[data-preflight],[data-preflight-catalog],[data-preflight-cancel],[data-plan-edit],[data-plan-disable],[data-plan-refresh],[data-version-refresh],[data-version-current],[data-version-check],[data-version-local],[data-version-preview],[data-github-sync],[data-github-read],[data-github-configure],[data-github-save],[data-github-refresh],[data-github-more],[data-github-detail],[data-github-import]');if(!button||button.disabled||!loggedIn)return;const d=button.dataset;if(!snapshot&&!d.tab&&!d.preflight&&!d.preflightCatalog&&!d.preflightCancel&&!d.planRefresh&&!d.planEdit&&!d.planDisable&&!d.versionRefresh&&!d.versionCurrent&&!d.versionCheck&&!d.versionLocal&&!d.versionPreview&&!d.githubSync&&!d.githubRead&&!d.githubConfigure&&!d.githubSave&&!d.githubRefresh&&!d.githubMore&&!d.githubDetail&&!d.githubImport)return;try{
  if(d.tab){clearCredentials();current=d.tab;location.hash=d.tab==='instances'&&selectedInstance?'instances/'+selectedInstance+'/'+instanceModule:d.tab;render();if(current==='preflight')await loadPreflight();if(current==='backup-plans')await loadBackupPlans();if(current==='image-versions')await loadImageVersions();if(current==='github')await loadGithub();}
  else if(d.githubRefresh){await loadGithub();}
  else if(d.githubImport){const result=await api('github/action',{action:'consume-webhooks'});notice('已导入 '+result.imported+' 条事件；未请求 GitHub 或启动构建。');await loadGithub();await loadGithubSummary();}
  else if(d.githubMore){await loadGithub(true);}
  else if(d.githubSync){await api('github/action',{action:'sync',project:d.githubSync});notice('只读采集已请求；不会启动镜像构建或发布。');await loadGithub();}
  else if(d.githubRead){const ids=d.githubRead==='visible'?githubData.notifications.filter(n=>!n.read).map(n=>n.id).slice(0,50):[Number(d.githubRead)];if(ids.length){await api('github/action',{action:'read',ids});await loadGithub();await loadGithubSummary();}}
  else if(d.githubConfigure){openGithubConfig(d.githubConfigure);}
  else if(d.githubDetail){openGithubDetail(d.githubDetail);}
  else if(d.githubSave){await saveGithubConfig();}
  else if(d.versionRefresh){await loadImageVersions();}
  else if(d.versionCurrent){await versionAction('current');}
  else if(d.versionCheck){await versionAction('check',d.versionCheck);}
  else if(d.versionLocal){await versionAction('local',d.versionLocal);}
  else if(d.versionPreview){await versionAction('preview',d.versionPreview);}
  else if(d.planRefresh){await loadBackupPlans();}
  else if(d.planEdit){openBackupPlan(d.planEdit);}
  else if(d.planDisable){const row=backupPlans.rows.find(p=>p.instance===d.planDisable);if(row){askOperation('backup-plans/'+row.instance,{action:'disable',revision:row.revision},row.instance,'禁用 '+row.instance+' 备份计划','仅停止后续调度；正在执行的备份和清理会按开始时的计划完成并恢复服务。');pending.kind='plan-disable';}}
  else if(d.preflight){await runPreflight(d.preflight);}
  else if(d.preflightCatalog){await loadPreflight();}
  else if(d.preflightCancel){preflight.cancelled=true;preflight.progress='停止后续检查；正在执行的只读项目完成后退出。';render();}
  else if(d.openInstance){await loadInstance(d.openInstance,d.instanceView||'overview');}
  else if(d.instanceModule){selectInstanceModule(d.instanceModule);}
  else if(d.credential){const service=d.credential;askOperation('instances/'+selectedInstance+'/credentials',{service},selectedInstance,'显示 '+selectedInstance+' 的 '+(service==='postgres'?'数据库':'Redis')+' 密码','密码只显示 30 秒，可复制到此实例的 Setup。此操作只读取配置，不修改数据库或安装状态。');pending.kind='credential';pending.connectionEpoch=connectionEpoch;}
  else if(d.copyCredential){const value=credentials.get(d.copyCredential);if(!value||value.instance!==selectedInstance||instanceModule!==d.copyCredential||current!=='instances'||!loggedIn||value.expires<=Date.now())throw Error('密码已隐藏，请重新确认显示。');try{await navigator.clipboard.writeText(value.password);notice('密码已复制，请粘贴到对应实例的 Setup 字段。');}catch{const field=$('credential-'+d.copyCredential);field.focus();field.select();notice('浏览器未允许自动复制，已选中密码，请按 Cmd+C 或 Ctrl+C。');}}
  else if(d.hideCredential){hideCredential(d.hideCredential);}
  else if(d.backup){const body={instance:d.instance,action:d.backup,id:d.id};if(d.backup==='prune')body.keep=Number($('keep-'+d.instance).value);const target=['create','prune'].includes(d.backup)?d.instance:d.id;const title={create:'创建实例备份',verify:'校验备份',drill:'隔离恢复验证',prune:'清理旧备份'}[d.backup];const impact={create:'将暂停该实例的前后端，完成数据库、Redis、配置及文件备份后恢复原运行状态。其他实例不暂停。',verify:'校验备份组成文件与下载包的 SHA256；不会写入运行中数据库。',drill:'在无网络、无公网端口的临时 PostgreSQL 中恢复并验证数据库；不会替换运行中实例。',prune:'仅删除超过所选保留份数的旧成功备份。至少保留一份；失败或中断记录保留。'}[d.backup];askOperation('backups/action',body,target,title,impact);}
  else if(d.probe){await submitOperation('sites/probe',{site:d.probe});}
  else if(d.detail){await detailService(d.detail);}
  else if(d.service){const s=snapshot.services.find(s=>s.id===d.service);if(!s)return;const action={start:'启动',stop:'停止',restart:'重启'}[d.action];askOperation('services/'+s.id+'/action',{action:d.action},s.service,action+' '+s.service,s.service==='nginx'?'此操作将影响所有网站入口。请确认影响范围。':'此操作可能短暂中断该服务及依赖它的访问。配置与持久化数据保留。');}
  else if(d.certificate){const action={check:'检查证书','renew-test':'测试续期',renew:'按需续期'}[d.action];askOperation('certificates/action',{name:d.certificate,action:d.action},d.certificate,action,d.action==='renew'?'使用当前续期配置检查到期情况；需要时签发并通知 Nginx 自动重载。共享 SAN 的域名一起处理。':d.action==='renew-test'?'访问测试 ACME 服务并验证所有 SAN 域名。测试不会替换正式证书。':'核验当前证书、私钥匹配、SAN 与有效期，不签发证书。');}
  else if(d.job){const j=jobs.find(j=>j.id===d.job)||(await api('jobs/'+d.job)).job;$('detail-title').textContent=j.action+' · '+j.target;$('detail-content').innerHTML=`<p>${stateBadge(j.state)} ${date(j.completed||j.created)}</p><pre>${esc(j.output||'任务正在执行…')}</pre>`;$('detail-dialog').showModal();}
}catch(error){notice(error.message,true);}});
$('confirm-form').addEventListener('submit',async event=>{event.preventDefault();if(!pending||$('confirmation').value!==pending.target){$('confirm-error').textContent='名称不匹配，请输入完整名称。';return;}$('confirm-submit').disabled=true;try{const operation=pending;if(operation.kind==='credential'){const result=await api(operation.path,{...operation.body,confirmation:operation.target});if(loggedIn&&connection.ready&&operation.connectionEpoch===connectionEpoch&&!document.hidden&&current==='instances'&&selectedInstance===result.instance&&instanceModule===result.service&&pending===operation&&$('confirm-dialog').open){clearCredentials();credentials.set(result.service,{instance:result.instance,password:result.password,expires:Date.now()+30000});const service=result.service;credentials.get(service).timer=setTimeout(()=>hideCredential(service),30000);notice('密码已显示，30 秒后自动隐藏。');render();}}else if(operation.kind==='plan-disable'){await api(operation.path,{...operation.body,confirmation:operation.target});await loadBackupPlans();notice('计划已禁用，正在执行的任务按原计划完成。');}else{await submitOperation(pending.path,{...pending.body,confirmation:pending.target});}$('confirm-dialog').close();pending=null;}catch(error){$('confirm-error').textContent=error.message;}finally{$('confirm-submit').disabled=false;}});
$('cancel-confirm').onclick=()=>$('confirm-dialog').close();$('close-detail').onclick=()=>$('detail-dialog').close();$('refresh').onclick=checkConnection;
$('login-recheck').onclick=checkConnection;$('shell-recheck').onclick=checkConnection;
document.addEventListener('input',event=>{if(event.target.dataset.retention)retention[event.target.dataset.retention]=event.target.value;});
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('login-error').textContent='';try{const result=await api('login',{token:$('token').value.trim()});csrf=result.csrf;$('token').value='';connectionState('connected','管理连接正常，正在读取运行状态。',true);await showShell();}catch(error){$('login-error').textContent=error.message;}finally{button.disabled=false;}});
$('logout').onclick=async()=>{try{await api('logout',{});showLogin();}catch(error){notice(error.message,true);}};
setInterval(()=>{if(!document.hidden){if(loggedIn){refresh();loadGithubSummary();if(current==='github')loadGithub();if(current==='backup-plans')loadBackupPlans();}else checkConnection();}},5000);
window.addEventListener('online',checkConnection);
window.addEventListener('offline',()=>{connectionState('offline','浏览器处于离线状态，请恢复网络后重新检查。');if(loggedIn)render();});
showLogin();connectionState('checking','正在检查管理连接…');checkConnection();

function clearCredentials(){for(const value of credentials.values())clearTimeout(value.timer);credentials.clear();document.querySelectorAll('.credential-input').forEach(input=>{input.value='';input.type='password';});}
function hideCredential(service){const value=credentials.get(service);if(value)clearTimeout(value.timer);credentials.delete(service);const field=$('credential-'+service);if(field){field.value='';field.type='password';}if(current==='instances'&&loggedIn)render();}
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearCredentials();else if(current==='instances'&&loggedIn)render();});
window.addEventListener('hashchange',()=>{const parts=location.hash.slice(1).split('/');if(!titles[parts[0]])return;const module=instanceModules[parts[2]]?parts[2]:'overview';if(parts[0]===current&&parts[1]===selectedInstance&&module===instanceModule)return;clearCredentials();current=parts[0];if(current==='preflight'){render();if(loggedIn)loadPreflight();return;}if(current==='backup-plans'){render();if(loggedIn)loadBackupPlans();return;}if(current==='image-versions'){render();if(loggedIn)loadImageVersions();return;}if(current==='github'){render();if(loggedIn)loadGithub();return;}if(current==='instances'&&parts[1]){if(parts[1]!==selectedInstance)loadInstance(parts[1],module);else{instanceModule=module;if(snapshot)render();}}else{if(current==='instances'){selectedInstance='';instanceDetail=null;}if(snapshot)render();}});
async function loadInstance(name,module='overview'){clearCredentials();selectedInstance=name;instanceModule=instanceModules[module]?module:'overview';instanceDetail=null;instanceError='';current='instances';location.hash='instances/'+name+'/'+instanceModule;render();try{const detail=await api('instances/'+name);if(selectedInstance===name)instanceDetail=detail;}catch(error){if(selectedInstance===name)instanceError=error.message;}render();}
function selectInstanceModule(module){if(!instanceModules[module]||!selectedInstance)return;clearCredentials();instanceModule=module;location.hash='instances/'+selectedInstance+'/'+module;render();}
document.addEventListener('change',event=>{if(event.target.id==='instance-selector')loadInstance(event.target.value);});
document.addEventListener('keydown',event=>{const tab=event.target.closest('[data-instance-module]');if(!tab)return;const keys=Object.keys(instanceModules);let index=keys.indexOf(tab.dataset.instanceModule);if(event.key==='ArrowRight')index=(index+1)%keys.length;else if(event.key==='ArrowLeft')index=(index+keys.length-1)%keys.length;else if(event.key==='Home')index=0;else if(event.key==='End')index=keys.length-1;else return;event.preventDefault();selectInstanceModule(keys[index]);$('instance-tab-'+keys[index]).focus();});
function connectionRows(items,headers=['字段','值']){return table(headers,items.map(([label,value])=>`<tr><td>${esc(label)}</td><td><span class="code-line">${esc(value)}</span></td></tr>`));}
function credentialRow(service,available){const shown=credentials.has(service)&&credentials.get(service).instance===selectedInstance&&credentials.get(service).expires>Date.now();return `<div class="credential-row"><label>${service==='postgres'?'数据库密码':'Redis 密码'}<input class="credential-input" id="credential-${service}" type="password" readonly autocomplete="off" placeholder="确认显示后可复制"></label><div class="actions">${btn('显示密码',{credential:service},!available)}${btn('复制密码',{'copy-credential':service},!shown)}${btn('隐藏',{'hide-credential':service},!shown)}</div><small>密码来自此实例当前私有配置；显示 30 秒，离开页面后隐藏。</small></div>`;}
function backupRows(records){return records.map(b=>`<tr><td><strong>${esc(b.instance)}</strong><small>${esc(b.id)}</small><small>${date(b.created)}</small></td><td>${stateBadge(b.state)}</td><td>${b.bundle_size?(b.bundle_size/1048576).toFixed(2)+' MB':'—'}<small class="code-line">${esc(b.bundle_sha256||'等待完成')}</small></td><td>${b.last_restore_drill?stateBadge(b.last_restore_drill.state):badge('未演练')}<small>${b.last_restore_drill?date(b.last_restore_drill.at):''}</small></td><td><div class="actions">${btn('校验',{backup:'verify',instance:b.instance,id:b.id},b.state!=='ready')}${btn('隔离恢复验证',{backup:'drill',instance:b.instance,id:b.id},b.state!=='ready')}${b.state==='ready'?`<a href="/api/backups/${esc(b.instance)}/${esc(b.id)}/download">下载私有备份包 ↓</a>`:''}</div></td></tr>`);}
async function loadPreflight(){
  if(preflight.loading||preflight.running||!loggedIn)return;
  preflight.loading=true;preflight.error='';if(current==='preflight')render();
  try{const data=await api('preflight');if(loggedIn)preflight.items=data.items;}
  catch(error){preflight.error=error.message;}
  finally{preflight.loading=false;if(current==='preflight'&&loggedIn)render();}
}
async function runPreflight(key){
  if(preflight.loading||preflight.running||!loggedIn)return;
  const keys=key==='all'?preflight.items.map(i=>i.key):preflight.items.filter(i=>i.key===key).map(i=>i.key);
  if(!keys.length)return;
  preflight.running=key;preflight.cancelled=false;preflight.error='';
  try{
    for(let index=0;index<keys.length;index++){
      if(preflight.cancelled||!loggedIn)break;
      preflight.progress=`检查 ${index+1} / ${keys.length}：${preflight.items.find(i=>i.key===keys[index])?.title||keys[index]}`;
      if(current==='preflight')render();
      const response=await api('preflight/'+encodeURIComponent(keys[index]));
      if(!loggedIn)break;
      const item=preflight.items.find(i=>i.key===response.key);if(item)item.result=response.result;
      if(current==='preflight')render();
    }
    preflight.progress=preflight.cancelled?'已停止后续检查，已完成结果保留。':'本次只读检查完成。';
  }catch(error){preflight.error=error.message;preflight.progress='本次检查中断，已完成结果保留；请单项重新检查。';}
  finally{preflight.running=null;if(current==='preflight'&&loggedIn)render();}
}
function renderPreflight(){
  const stale=r=>r?.stale||!Number.isFinite(Date.parse(r?.source_at||r?.at))||Date.now()-Date.parse(r?.source_at||r?.at)>900000;
  const pending=preflight.loading||!!preflight.running;
  const button=(label,attribute,value,blocked=pending)=>`<button type="button" data-${attribute}="${esc(value)}" ${blocked?'disabled':''}>${label}</button>`;
  let html=`<div class="intro"><div><h2>部署前检查</h2><p class="muted">按项读取实际结果；检查不会部署业务、拉取镜像或签发证书。</p></div><div class="actions">${button('重新读取清单','preflight-catalog','yes')}${button('检查全部','preflight','all',pending||!preflight.items.length)}${preflight.running?button('停止后续检查','preflight-cancel','yes',preflight.cancelled):''}</div></div>`;
  html+=panel('宿主机报告','在服务器的 XDocker 仓库终端执行，再点击重新读取清单',`<div class="panel-body"><code>sh scripts/preflight.sh --save</code><p class="muted">报告有效期 15 分钟。缺失、过期或提交不匹配时显示未确认；root 检查不代表 ubuntu 权限。</p></div>`);
  if(preflight.loading)html+='<p class="muted">正在读取清单…</p>';
  if(preflight.progress)html+=`<p class="preflight-progress" role="status">${esc(preflight.progress)}</p>`;
  if(preflight.error)html+=`<p class="error" role="alert">${esc(preflight.error)}</p>`;
  const counts={pass:0,warn:0,fail:0,unknown:0};
  for(const item of preflight.items){const r=item.result;counts[r&&!stale(r)?r.status:'unknown']++;}
  html+=`<div class="kpis">${Object.entries({pass:'通过',warn:'需核对',fail:'失败',unknown:'未检查 / 过期'}).map(([key,label])=>`<article class="kpi"><div class="kpi-label">${label}</div><strong>${counts[key]}</strong></article>`).join('')}</div>`;
  for(const group of [...new Set(preflight.items.map(i=>i.group))]){
    const rows=preflight.items.filter(i=>i.group===group).map(item=>{
      const r=item.result;const expired=r&&stale(r);const status=expired?'unknown':r?.status||'unknown';
      const label=expired?'结果需重查':{pass:'通过',warn:'需核对',fail:'失败',unknown:'未确认'}[status];
      const extra=r?.addresses?`<small>实际解析：${esc(r.addresses.join(', ')||'无')}</small>${r.expected?`<small>预期地址：${esc(r.expected.join(', '))}</small>`:''}`:'';
      return `<tr><td><strong class="preflight-target">${esc(item.title)}</strong>${item.target?`<small>${esc(item.target)}</small>`:''}</td><td>${badge(label,status==='pass'?'good':status==='fail'?'bad':'warn')}</td><td>${esc(r?.detail||'尚未执行此项检查。')}${extra}${r?.advice?`<small>${esc(r.advice)}</small>`:''}</td><td>${r?date(r.at):'未检查'}${r?.source_at?`<small>宿主机报告：${date(r.source_at)}</small>`:''}</td><td>${button('重新检查','preflight',item.key)}</td></tr>`;
    });
    html+=panel(group,'检查时间与作用范围分别记录',table(['项目 / 目标','结果','证据 / 处理建议','时间','检查'],rows));
  }
  return html+'<p class="muted">结果保留在当前 Admin 进程内，重启后需重新检查。Registry 401 仅证明接口可达；匿名镜像清单可读不代表完整下载。DNS 和本机端口检查不代替公网入站与 HTTP-01 验收。</p>';
}
async function loadBackupPlans(){
  if(backupPlans.loading||!loggedIn)return;backupPlans.loading=true;
  try{
    const data=await api('backup-plans');if(!loggedIn)return;
    backupPlans.rows=data.plans;backupPlans.enabled=data.scheduler_enabled;backupPlans.running=data.scheduler_running;backupPlans.schedulerError=data.scheduler_error;backupPlans.loaded=true;backupPlans.error='';backupPlans.reachable=true;
  }catch(error){backupPlans.error=error.message;backupPlans.reachable=false;}
  finally{backupPlans.loading=false;if(current==='backup-plans'&&loggedIn)render();}
}
function openBackupPlan(instance){
  const plan=backupPlans.rows.find(p=>p.instance===instance);if(!plan)return;
  editingPlan={...plan};$('plan-title').textContent=instance+' 备份计划';
  for(const [id,key] of [['frequency','frequency'],['weekday','weekday'],['time','time'],['timezone','timezone'],['grace','grace_minutes'],['keep','keep']])$('plan-'+id).value=String(plan[key]);
  $('plan-enabled').checked=plan.enabled;$('plan-prune').checked=plan.auto_prune;
  $('plan-maintenance-ack').checked=false;$('plan-prune-ack').checked=false;$('plan-confirmation').value='';$('plan-error').textContent='';
  $('plan-weekday').disabled=plan.frequency!=='weekly';$('plan-dialog').showModal();$('plan-time').focus();
}
$('plan-frequency').onchange=()=>{$('plan-weekday').disabled=$('plan-frequency').value!=='weekly';};
$('cancel-plan').onclick=()=>{$('plan-dialog').close();editingPlan=null;};
$('plan-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!editingPlan)return;
  const target=editingPlan.instance;
  if($('plan-confirmation').value!==target){$('plan-error').textContent='名称不匹配，请输入完整实例名。';return;}
  const config={enabled:$('plan-enabled').checked,frequency:$('plan-frequency').value,weekday:Number($('plan-weekday').value),time:$('plan-time').value,timezone:$('plan-timezone').value,grace_minutes:Number($('plan-grace').value),keep:Number($('plan-keep').value),auto_prune:$('plan-prune').checked};
  if(config.enabled&&!$('plan-maintenance-ack').checked){$('plan-error').textContent='启用前请确认暂停前后端的维护影响。';return;}
  if(config.auto_prune&&!$('plan-prune-ack').checked){$('plan-error').textContent='开启自动清理前请确认删除旧成功备份的影响。';return;}
  $('save-plan').disabled=true;$('plan-error').textContent='';
  try{
    await api('backup-plans/'+target,{action:'save',plan:config,revision:editingPlan.revision,confirmation:target,maintenance_ack:$('plan-maintenance-ack').checked,prune_ack:$('plan-prune-ack').checked});
    $('plan-dialog').close();editingPlan=null;await loadBackupPlans();
    notice(config.enabled?(backupPlans.enabled?'计划已保存；只在未来允许开始时间调度，当前不立即备份。':'计划已保存，但服务器调度开关关闭，当前不会自动备份。'):'计划已保存为禁用状态，不会自动备份。');
  }catch(error){$('plan-error').textContent=error.message;}
  finally{$('save-plan').disabled=false;}
});
function planWritable(){return loggedIn&&backupPlans.reachable&&navigator.onLine!==false&&!['offline','unreachable','unexpected','verification'].includes(connection.state);}
function renderBackupPlans(){
  const week=['周一','周二','周三','周四','周五','周六','周日'];
  const plannedDate=(value,zone)=>value?new Date(value).toLocaleString('zh-CN',{hour12:false,timeZone:zone}):'未安排';
  const button=(label,attribute,value,blocked=!planWritable())=>`<button type="button" data-${attribute}="${esc(value)}" ${blocked?'disabled':''}>${label}</button>`;
  let html=`<div class="intro"><div><h2>实例备份计划</h2><p class="muted">全实例维护备份；仅处理登记的实例，启用前需确认暂停前后端的影响。</p></div>${button('重新读取计划','plan-refresh','yes',backupPlans.loading)}</div>`;
  html+=panel('服务器调度开关','与每个实例的启用状态分别管理',`<div class="panel-body"><p>${badge(backupPlans.enabled?(backupPlans.running?'调度进程运行中':'已配置开关，进程待核对'):'调度关闭',backupPlans.enabled&&backupPlans.running?'good':'warn')}</p><p class="muted">默认关闭。当前全局开关关闭时，保存启用计划也不会执行；开启开关需按授权部署流程维护 services/admin/.env。</p>${backupPlans.schedulerError?`<p class="error">${esc(backupPlans.schedulerError)}</p>`:''}</div>`);
  if(backupPlans.error)html+=`<p class="error">${esc(backupPlans.error)} 上次列表可能已过期，请重新读取；不会自动重新提交配置。</p>`;
  const rows=backupPlans.rows.map(plan=>{
    const run=plan.last_run,skip=plan.last_skip;
    let outcome=run?stateBadge(run.state)+`<small>开始：${date(run.started_at)}<br>完成：${date(run.completed_at)}</small><small>备份：${esc({pending:'待确认',ready:'已校验',failed:'失败'}[run.backup_state]||run.backup_state||'待核对')} · 清理：${esc({pending:'待确认',success:'成功',failed:'失败',disabled:'未启用',skipped:'未执行'}[run.cleanup_state]||run.cleanup_state||'待核对')}</small>${run.job_id?`<button data-job="${esc(run.job_id)}">查看操作记录</button>`:''}`:'尚未执行';
    if(skip)outcome+=`<small>最近跳过：${date(skip.at)}；${skip.reason==='window-expired'?'超出允许开始时间，未补跑':'请核对操作记录'}</small>`;
    return `<tr><td><strong>${esc(plan.instance)}</strong>${badge(plan.enabled?'计划已启用':'计划禁用',plan.enabled?'blue':'')}<small>${plan.enabled&&!backupPlans.enabled?'服务器调度关闭，当前不执行':''}</small></td><td>${plan.frequency==='weekly'?'每周 '+week[plan.weekday]:'每天'} ${esc(plan.time)}<small>${plan.timezone==='Asia/Shanghai'?'上海时间（UTC+08:00）':'UTC'}；允许迟到 ${plan.grace_minutes} 分钟</small></td><td>${plan.enabled?plannedDate(plan.next_run_at,plan.timezone):'未安排'}<small>${plan.enabled&&!backupPlans.enabled?'仅为配置的计划时间；开关关闭，不保证运行':''}</small></td><td>${plan.auto_prune?'成功校验后保留 '+plan.keep+' 份':'自动清理关闭'}<small>失败/中断记录保留</small></td><td>${outcome}</td><td><div class="actions">${button('编辑计划','plan-edit',plan.instance)}${button('禁用后续计划','plan-disable',plan.instance,!planWritable()||!plan.enabled)}</div></td></tr>`;
  });
  html+=panel('登记的 PowerX 实例','新计划默认禁用、保留 7 份、自动清理关闭。首次启用安排未来时间，不立刻运行',table(['实例 / 状态','周期 / 时区','下一计划时间（计划时区）','保留清理','最近执行 / 跳过','操作'],rows));
  return html+'<p class="muted">允许迟到只限制开始时间，开始后的备份与服务恢复可能超出这个范围。全局一次执行一个运行操作，建议错开多个实例的时间。错过时间、失败及重启未确认的任务不会自动补跑；禁用不会中断正在执行的任务。</p><p class="muted">这是 XDocker 整套实例备份计划，与 PowerX 应用内数据库计划分别管理。校验通过不代表数据库/完整业务恢复已通过；恢复仍须单独验证。</p>';
}
async function loadImageVersions(){
  if(imageVersions.loading||imageVersions.action||!loggedIn)return;imageVersions.loading=true;imageVersions.error='';
  try{
    const data=await api('image-versions');if(!loggedIn)return;
    imageVersions.instances=data.instances;imageVersions.candidates=data.candidates;imageVersions.checks=data.checks;imageVersions.catalogError=data.catalog_error;imageVersions.catalogNotice=data.catalog_notice;
    if(!data.instances.some(i=>i.instance===imageVersions.selected))imageVersions.selected=data.instances[0]?.instance||'';
    imageVersions.current=null;imageVersions.preview=null;imageVersions.local={};
  }catch(error){imageVersions.error=error.message;}
  finally{imageVersions.loading=false;if(current==='image-versions'&&loggedIn)render();}
}
async function versionAction(kind,identifier=''){
  if(imageVersions.action||imageVersions.loading||!loggedIn)return;
  const instance=imageVersions.selected;imageVersions.action=kind;imageVersions.error='';if(current==='image-versions')render();
  try{
    if(kind==='check'){
      for(const path of ['registry/backend','registry/web-admin','evidence']){
        if(!loggedIn)break;
        const response=await api('image-versions/check/'+encodeURIComponent(identifier)+'/'+path);
        let row=imageVersions.checks.find(c=>c.id===identifier);
        if(!row){row={id:identifier,evidence:null,registry:{}};imageVersions.checks.push(row);}
        if(response.kind==='evidence')row.evidence=response.result;else row.registry[response.role]=response.result;
        if(current==='image-versions')render();
      }
      imageVersions.preview=null;
    }else if(instance){
      const path='image-versions/'+encodeURIComponent(instance)+(kind==='current'?'':'/'+kind+'/'+encodeURIComponent(identifier));
      const response=await api(path);if(!loggedIn||instance!==imageVersions.selected)return;
      if(kind==='current'){imageVersions.current=response;imageVersions.preview=null;}
      else if(kind==='local')imageVersions.local[identifier]=response;
      else if(kind==='preview'){imageVersions.preview=response;imageVersions.current=response.current;imageVersions.local[identifier]=response.local;}
    }
  }catch(error){imageVersions.error=error.message;}
  finally{imageVersions.action=null;if(current==='image-versions'&&loggedIn)render();}
}
document.addEventListener('change',event=>{
  if(event.target.id==='version-instance'){imageVersions.selected=event.target.value;imageVersions.current=null;imageVersions.preview=null;imageVersions.local={};if(current==='image-versions')render();}
  if(event.target.id==='version-channel'){imageVersions.channel=event.target.value;if(current==='image-versions')render();}
});
function renderImageVersions(){
  const busy=imageVersions.loading||!!imageVersions.action;
  const button=(text,key,value,blocked=busy)=>`<button type="button" data-${key}="${esc(value)}" ${blocked?'disabled':''}>${text}</button>`;
  const fresh=r=>r&&!r.stale&&Number.isFinite(Date.parse(r.at))&&Date.now()-Date.parse(r.at)>=-300000&&Date.now()-Date.parse(r.at)<=900000;
  const proof=r=>!r?badge('未核验','warn'):!fresh(r)?badge('需重新核验','warn'):badge({verified:'证据匹配',failed:'未通过',unknown:'未确认',pass:'清单元数据可读',warn:'权限待核对',fail:'清单检查失败'}[r.state||r.status]||'未确认',['verified','pass'].includes(r.state||r.status)?'good':['failed','fail'].includes(r.state||r.status)?'bad':'warn');
  let html=`<div class="intro"><div><h2>镜像版本与升级预览</h2><p class="muted">只读核对前后端、源码 SHA 与 digest；本页不更新配置、拉取镜像或执行升级。</p></div>${button('重新读取目录','version-refresh','yes')}</div>`;
  html+=`<div class="instance-toolbar"><label>实例 <select id="version-instance" ${busy?'disabled':''}>${imageVersions.instances.map(i=>`<option value="${esc(i.instance)}" ${i.instance===imageVersions.selected?'selected':''}>${esc(i.instance)} · ${esc(i.environment)}</option>`).join('')||'<option value="">没有登记实例</option>'}</select></label><label>候选通道 <select id="version-channel"><option value="all" ${imageVersions.channel==='all'?'selected':''}>全部</option><option value="dev" ${imageVersions.channel==='dev'?'selected':''}>开发</option><option value="stable" ${imageVersions.channel==='stable'?'selected':''}>正式 Release</option></select></label>${button('核对当前运行版本','version-current','yes',busy||!imageVersions.selected)}</div>`;
  if(imageVersions.error)html+=`<p class="error" role="alert">${esc(imageVersions.error)} 已读取结果保留，请重新核对。</p>`;
  if(imageVersions.catalogError)html+=`<p class="error">${esc(imageVersions.catalogError)}</p>`;
  if(imageVersions.action)html+='<p class="preflight-progress" role="status">正在执行只读核验，候选不因此自动部署…</p>';
  const actual=imageVersions.current;
  const configured=actual?.configured||imageVersions.instances.find(i=>i.instance===imageVersions.selected)?.configured||{};
  const roles=['backend','web-admin'];
  html+=panel('当前配置与实际容器','SHA tag 是引用提示；OCI revision、镜像 ID 和 Registry digest 分别核对',table(['角色','配置引用 / 固定方式','实际容器 / 配置一致性','源码 / 镜像 ID / Registry digest'],roles.map(role=>{
    const c=configured[role]||{},r=actual?.runtime?.[role];
    return `<tr><td><strong>${role}</strong></td><td><small class="code-line">${esc(c.reference||'配置未确认')}</small>${badge({digest:'固定 digest','sha-tag':'SHA tag（可重指）',mutable:'可变 tag',invalid:'引用无效'}[c.pin]||'未知',c.pin==='digest'?'good':'warn')}<small>Tag SHA 提示：${esc(c.tag_revision||'无')}</small></td><td>${r?stateBadge(r.state):badge('未读取','warn')}<small>健康：${esc(r?.health||'未确认')}</small><small>平台：${esc(r?.os||'未知')}/${esc(r?.architecture||'未知')}</small><small>配置解析与容器：${esc({matches:'镜像 ID 匹配',different:'不一致',unknown:'未确认'}[r?.configuration_match]||'未确认')}</small></td><td><small>OCI 源码 SHA：${esc(r?.source_revision||'未知')}</small><small>镜像版本标签：${esc(r?.version_label||'未知')}</small><small class="code-line">镜像 ID：${esc(r?.image_id||'未知')}</small><small class="code-line">Registry digest：${r?.repo_digests?.length?r.repo_digests.map(esc).join('<br>'):'未知；不能用镜像 ID 替代'}</small></td></tr>`;
  })));
  if(actual)html+=`<p class="muted">核对时间：${date(actual.at)}；前后端源码：${esc({matched:'成对匹配',different:'不一致',unknown:'未确认'}[actual.pair.state])}${actual.engine_error?'；'+esc(actual.engine_error):''}</p>`;
  const candidates=imageVersions.candidates.filter(c=>imageVersions.channel==='all'||c.channel===imageVersions.channel);
  html+=panel('候选版本目录',imageVersions.catalogNotice||'没有目录记录时不推断最新版本或自动追随 develop/latest',table(['登记版本 / 通道','固定前后端引用','发布及安装证据','本机 / 远端清单','操作'],candidates.map(c=>{
    const check=imageVersions.checks.find(r=>r.id===c.id),local=imageVersions.local[c.id];
    const paired=fresh(local)&&roles.every(r=>local?.images?.[r]?.state==='verified');
    return `<tr><td><strong>${esc(c.version)}</strong>${badge(c.channel==='stable'?'正式候选':'开发候选','blue')}<small>源码：${esc(c.source_revision)}</small><small>仅登记，不等于已发布/已部署</small></td><td>${roles.map(r=>`<small class="code-line">${r}：${esc(c.images[r])}</small>`).join('')}</td><td>${proof(check?.evidence)}<small>${esc(check?.evidence?.detail||'尚未查询 CI 证据。')}</small><small>${check?.evidence?date(check.evidence.at):''}</small><a href="https://github.com/ArtisanCloud/PowerX/actions/runs/${c.evidence.run_id}" target="_blank" rel="noreferrer">查看登记的工作流 ↗</a>${c.evidence.release_tag?`<small><a href="https://github.com/ArtisanCloud/PowerX/releases/tag/${esc(c.evidence.release_tag)}" target="_blank" rel="noreferrer">查看登记 Release ↗</a></small>`:''}</td><td>${badge(local?(paired?'本机镜像已核验':'本机镜像未确认'):'本机未核验',paired?'good':'warn')}<small>平台、SHA、digest 均须核对${local?' · '+date(local.at):''}</small>${local?.engine_error?`<small>${esc(local.engine_error)}</small>`:''}${roles.map(r=>{const info=local?.images?.[r],remote=check?.registry?.[r];return `${info?`<small>${r} 本机：${esc(info.detail||('digest '+(info.digest_match?'匹配':'未确认')+' / SHA '+(info.source_match?'匹配':'未确认')+' / 平台 '+(info.platform_match?'匹配':'未确认')))}</small>`:''}<small>${r} 远端：${proof(remote)}</small>${remote?`<small>${esc(remote.detail)} · ${date(remote.at)}</small>`:''}`;}).join('')}<small>清单可读不代表镜像层下载完成</small></td><td><div class="actions">${button('核验远端与 CI','version-check',c.id)}${button('核验本机镜像','version-local',c.id,busy||!imageVersions.selected)}${button('升级预览','version-preview',c.id,busy||!imageVersions.selected)}</div></td></tr>`;
  })));
  if(!candidates.length)html+='<p class="muted">该通道尚无有效候选记录。由维护者在 XDocker 的版本目录登记已发布制品；页面不会生成正式版本。</p>';
  const preview=imageVersions.preview;
  if(preview){
    const migration=preview.candidate.migration;
    html+=panel('升级预览 · '+esc(preview.candidate.version),'核对内容与人工步骤；没有“应用升级”操作',`<div class="panel-body"><p>${badge(preview.technical_preview==='reviewable'?'技术项可供审查，仍需备份及授权':'存在未确认项，升级准备未通过',preview.technical_preview==='reviewable'?'blue':'warn')}</p>${preview.blockers.map(x=>`<p class="error">${esc(x)}</p>`).join('')}${preview.warnings.map(x=>`<p class="muted">${esc(x)}</p>`).join('')}<p>候选部署状态：${esc({'matching-running':'当前两容器与候选镜像 ID 匹配且运行中','matching-stopped':'镜像匹配，但容器未全部运行','not-confirmed':'未确认已部署此候选'}[preview.deployment])}；不代表业务验收。</p><p>迁移登记：${esc({none:'维护者声明无需迁移',required:'需要迁移',unknown:'未确认'}[migration.status])}；回退登记：${esc({compatible:'维护者声明兼容', 'restore-required':'需要对应数据恢复',unknown:'未确认'}[migration.rollback])}</p><p>${esc(migration.notes||'尚无说明')}</p><p>最近历史成功备份：${preview.backup?esc(preview.backup.id)+' · '+date(preview.backup.created):'无'}。升级前仍须另行创建、校验及确认恢复方案。</p><button data-tab="backups">打开实例备份 →</button><p class="fine">以下仅为未来授权修改实例 .env 时参考的两行；当前没有写入文件。</p><pre>${esc(preview.config_preview)}</pre><p>${esc(preview.scope)}</p><small>预览时间：${date(preview.at)}</small></div>`);
  }
  return html+'<p class="muted">候选目录、匿名清单可读、本机镜像存在、CI 验收和实际运行分别记录。网络/权限不足保持未确认；不会读取 Docker 登录凭据或向网页提供 PAT。</p>';
}
function renderInstances(services){
  let html='<div class="intro"><div><h2>PowerX 实例管理</h2><p class="muted">选择实例和服务模块，查看运行状态与 Setup 所需配置。</p></div></div>';
  const site=instanceList.find(i=>i.name===selectedInstance);
  if(!site)return html+panel('已登记实例','每个实例拥有独立的数据库、缓存与配置',table(['实例','网站入口','容器','操作'],instanceList.map(instance=>{const count=services.filter(s=>s.service.startsWith(instance.name+'/')).length;return `<tr><td><strong>${esc(instance.name)}</strong></td><td>${instance.url?`<a href="${esc(instance.url)}" target="_blank" rel="noreferrer">${esc(instance.url)} ↗</a>`:'未配置入口'}</td><td>${count} 个</td><td>${btn('管理实例',{'open-instance':instance.name},false,'primary')}</td></tr>`;})));
  html+=panel(esc(selectedInstance),'',`<div class="instance-toolbar"><label for="instance-selector">当前实例 <select id="instance-selector">${instanceList.map(i=>`<option value="${esc(i.name)}" ${i.name===selectedInstance?'selected':''}>${esc(i.name)}</option>`).join('')}</select></label><div class="actions">${site.url?`<a class="setup-link" href="${esc(site.url)}/setup" target="_blank" rel="noreferrer">打开 PowerX Setup ↗</a>`:''}${btn('重新读取配置',{'open-instance':selectedInstance,'instance-view':instanceModule})}<button data-tab="image-versions">镜像版本 →</button></div></div>`);
  html+=`<div class="instance-tabs" role="tablist" aria-label="PowerX 实例服务模块">${Object.entries(instanceModules).map(([key,label])=>`<button type="button" role="tab" id="instance-tab-${key}" aria-selected="${key===instanceModule}" aria-controls="instance-module-panel" tabindex="${key===instanceModule?0:-1}" data-instance-module="${key}" class="${key===instanceModule?'active':''}">${label}</button>`).join('')}</div>`;
  const instanceServices=services.filter(s=>s.service.startsWith(selectedInstance+'/')),v=instanceDetail;
  let body='';
  if(instanceModule==='overview'){
    const status=v?(v.install_status==='uninstalled'?'待完成 Setup':v.install_status==='installed'?'已安装':v.install_status):'配置未读取';
    const stats=[['部署环境',v?.environment||'—'],['安装状态',status],['运行容器',instanceServices.filter(s=>s.state==='running').length+' / '+instanceServices.length],['备份记录',backupData.backups.filter(b=>b.instance===selectedInstance).length]];
    body=`<div class="kpis instance-kpis">${stats.map(([label,value])=>`<article class="kpi"><div class="kpi-label">${label}</div><strong>${esc(value)}</strong></article>`).join('')}</div>`;
    body+=panel('实例概览','账号由你在 PowerX Setup 中设置',connectionRows([['网站入口',site.url||'未配置'],['前后端镜像',instanceServices.filter(s=>s.service.endsWith('/backend')||s.service.endsWith('/web-admin')).map(s=>s.image).join(' / ')]]));
    if(instanceError)body+=`<p class="error">${esc(instanceError)}</p>`;
  }else if(instanceModule==='services'){
    body=panel('容器服务','仅显示本实例的容器；运行操作需要确认完整服务名',table(['服务','运行状态','镜像版本','宿主机端口','操作'],serviceRows(instanceServices)));
  }else if(instanceModule==='backups'){
    const records=backupData.backups.filter(b=>b.instance===selectedInstance);
    body=panel('实例备份','创建备份会短暂暂停此实例前后端，完成后恢复运行',`<div class="panel-body"><div class="instance-toolbar">${btn('创建实例备份',{backup:'create',instance:selectedInstance},false,'primary')}<button data-tab="backup-plans">备份计划</button><label>保留最新 <input id="keep-${esc(selectedInstance)}" type="number" min="1" max="100" value="${esc(retention[selectedInstance]??7)}" data-retention="${esc(selectedInstance)}" aria-label="保留成功备份份数"></label>${btn('清理旧备份',{backup:'prune',instance:selectedInstance})}</div>${backupData.application_centers.filter(c=>c.instance===selectedInstance).map(c=>`<p><a href="${esc(c.url)}" target="_blank" rel="noreferrer">打开 PowerX 应用备份中心 ↗</a><small>完成 Setup 后使用自己的 PowerX 账号登录</small></p>`).join('')}</div>`);
    body+=panel('备份记录','隔离恢复验证不会覆盖运行实例；当前为手动备份与显式清理',table(['备份','状态','大小 / 校验','恢复验证','操作'],backupRows(records)));
  }else if(!v){body=panel('连接信息','',`<div class="panel-body">${instanceError?`<p class="error">${esc(instanceError)}</p>`:'正在读取配置…'}</div>`);
  }else if(instanceModule==='postgres'){
    const db=v.postgres;body=panel('PostgreSQL','测试连接由 PowerX 后端容器执行，Setup 使用下方 Docker 主机名',connectionRows([['数据库类型','postgresql'],['主机地址',db.host],['端口',db.port],['数据库名',db.database],['用户名',db.username],['SSL 模式',db.ssl_mode]],['Setup 字段','填写值'])+credentialRow('postgres',db.password_available));
  }else if(instanceModule==='redis'){
    const cache=v.redis;body=panel('Redis','Redis 使用自己的密码，请填入 Setup 的缓存配置',connectionRows([['缓存类型','redis'],['Redis 主机',cache.host],['Redis 端口',cache.port],['数据库索引',cache.db]],['Setup 字段','填写值'])+credentialRow('redis',cache.password_available));
  }else if(instanceModule==='storage'){
    body=panel('文件存储','将此实例的存储路径和公开地址填入 Setup',connectionRows([['本地存储路径',v.storage.path],['文件公开地址',v.storage.public_url]],['Setup 字段','填写值']));
  }
  return html+`<section role="tabpanel" id="instance-module-panel" aria-labelledby="instance-tab-${instanceModule}">${body}</section>`;
}
