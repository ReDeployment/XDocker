'use strict';
const $ = id => document.getElementById(id);
const titles = {overview:'运行概览', services:'容器服务', sites:'网站入口', certificates:'HTTPS 证书', frp:'FRP 穿透', audit:'操作记录'};
let csrf='', current='overview', snapshot=null, jobs=[], audit=[], pending=null, trackedJob=null, loggedIn=false, refreshing=false;
const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '尚无记录';
const badge = (text, type='') => `<span class="badge ${type}">${esc(text)}</span>`;
const stateBadge = value => badge({running:'运行中',exited:'已停止',created:'待启动',restarting:'重启中',success:'成功',failed:'失败',running_job:'执行中',interrupted:'未确认',reachable:'可访问'}[value]||value, ['running','success','reachable'].includes(value)?'good':['failed','dead','interrupted'].includes(value)?'bad':value==='exited'?'warn':'blue');
const certBadge = c => !c.enabled ? badge('未启用') : badge({LOCAL_VALID:'有效',EXPIRING:'即将到期',CRITICAL:'紧急续期',EXPIRED:'已过期',ERROR:'检查异常',NOT_YET_VALID:'尚未生效',UNKNOWN:'待检查'}[c.status]||c.status,c.status==='LOCAL_VALID'?'good':['EXPIRED','CRITICAL','ERROR'].includes(c.status)?'bad':'warn');
const busy = () => jobs.some(j=>j.state==='running');
const disabled = (condition=false) => condition || busy() ? 'disabled' : '';
const btn = (text, data, condition=false, cls='') => `<button class="${cls}" ${Object.entries(data).map(([k,v])=>`data-${k}="${esc(v)}"`).join(' ')} ${disabled(condition)}>${text}</button>`;
const table = (headers, rows) => `<div class="table-scroll"><table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')||`<tr><td colspan="${headers.length}" class="empty">暂无数据</td></tr>`}</tbody></table></div>`;
const panel = (title, subtitle, body, action='') => `<section class="panel"><div class="panel-head"><div><h3>${title}</h3>${subtitle?`<p>${subtitle}</p>`:''}</div>${action}</div>${body}</section>`;
function notice(text, bad=false){$('notice').textContent=text;$('notice').className='notice'+(bad?' bad':'');}
async function api(path, payload){
  const options=payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(payload)};
  const response=await fetch('/api/'+path,options); const data=await response.json();
  if(!response.ok){if(response.status===401 && path!=='login')showLogin();throw Error(data.error||`HTTP ${response.status}`);}return data;
}
function showLogin(){loggedIn=false;csrf='';$('shell').classList.add('hidden');$('login').classList.remove('hidden');$('confirm-dialog').close();$('detail-dialog').close();}
async function showShell(){loggedIn=true;$('login').classList.add('hidden');$('shell').classList.remove('hidden');await refresh();}
async function refresh(){
  if(!loggedIn||refreshing)return;refreshing=true;$('refresh').disabled=true;
  try{const wasBusy=busy();const results=await Promise.all([api('snapshot'),api('jobs'),api('audit')]);snapshot=results[0];jobs=results[1].jobs;audit=results[2].events;$('last-update').textContent='更新于 '+date(snapshot.at);$('revision').textContent='Git '+snapshot.revision.slice(0,12)+' · Docker '+snapshot.engine.Version;render();const tracked=jobs.find(j=>j.id===trackedJob);if(tracked && tracked.state!=='running'){notice(tracked.state==='success'?'操作完成，请在操作记录中查看实际结果。':'操作未成功，请在操作记录中查看原因。',tracked.state!=='success');trackedJob=null;}else if(busy())notice('有操作正在执行。结果会自动更新，可在操作记录中查看。');else if(wasBusy)notice(jobs[0]?.state==='success'?'操作完成，请在操作记录中查看实际结果。':'操作未成功，请在操作记录中查看原因。',jobs[0]?.state!=='success');}
  catch(error){notice(error.message,true);}finally{refreshing=false;$('refresh').disabled=false;}
}
function serviceRows(services){return services.map(s=>`<tr><td><strong>${esc(s.service)}</strong><small>${esc(s.name)}</small></td><td>${stateBadge(s.state)}<small>${esc(s.status)}</small></td><td><small class="code-line">${esc(s.image)}</small></td><td>${s.ports.map(p=>`${esc(p.ip)}:${p.host} → ${p.container}`).join('<br>')||'内部网络'}</td><td><div class="actions">${btn('日志 / 资源',{detail:s.id},false)}${btn(s.state==='running'?'停止':'启动',{service:s.id,action:s.state==='running'?'stop':'start'},s.protected,s.state==='running'?'danger':'')}${btn('重启',{service:s.id,action:'restart'},s.protected||s.state!=='running')}</div></td></tr>`);}
function siteRows(sites){return sites.map(s=>`<tr><td><strong><a href="https://${esc(s.domain)}" target="_blank" rel="noreferrer">${esc(s.domain)} ↗</a></strong><small>${esc(s.key)}</small></td><td>${badge(s.kind==='frp_http'?'FRP 穿透':'静态站点','blue')}<small>${esc(s.service)}</small>${s.client_template?`<small>客户端模板：${esc(s.client_template.address)}:${esc(s.client_template.port)}</small>`:''}</td><td>${s.enabled?badge('已启用','good'):badge('未启用')}</td><td>${s.probe?stateBadge(s.probe.status):badge('待探测')}<small>${esc(s.probe?.output||'尚未执行公网检查')}</small><small>${s.probe?date(s.probe.at):''}</small></td><td>${btn('检查连接',{probe:s.key},!s.enabled)}</td></tr>`);}
function render(){
  if(!snapshot)return;
  document.querySelectorAll('nav [data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===current));$('page-title').textContent=titles[current];
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
  }else if(current==='services'){
    html=`<div class="intro"><div><h2>容器服务</h2><p class="muted">查看日志与资源，按服务执行启停。停止入口服务会影响站点访问。</p></div></div>`+panel('已部署服务',`${services.length} 个服务 · 管理服务自身通过 SSH 维护`,table(['服务','运行状态','镜像版本','宿主机端口','操作'],serviceRows(services)));
  }else if(current==='sites'){
    html=`<div class="intro"><div><h2>域名与网站</h2><p class="muted">连接检查验证 HTTPS；FRP 网站同时检查 /healthz。</p></div>${btn('检查所有站点',{probe:'all'},false,'primary')}</div>`+panel('网站入口','域名、镜像与启用配置来自部署仓库',table(['域名 / 站点','类型 / 服务','配置状态','最近连接检查','操作'],siteRows(sites)))+`<p class="muted">新增网站、修改域名和升级镜像，请在本地修改配置，验证并推送 Git，再由服务器同步。</p>`;
  }else if(current==='certificates'){
    html=`<div class="intro"><div><h2>HTTPS 证书</h2><p class="muted">按证书组管理，共享 SAN 的域名一起续期。续期使用现有 Certbot 配置。</p></div></div>`+panel('证书清单','状态来自真实检查报告；检查时间显示数据是否新鲜',table(['证书 / 域名','状态','到期 / 剩余','最近证书检查','操作'],certs.map(c=>`<tr><td><strong>${esc(c.name)}</strong><small>${c.domains.map(esc).join('<br>')}</small></td><td>${certBadge(c)}</td><td>${c.expiry?date(c.expiry):'尚未签发 / 待检查'}<small>${c.days_remaining!==undefined?c.days_remaining+' 天':''}</small></td><td>${date(c.checked_at)}</td><td><div class="actions">${btn('检查',{certificate:c.name,action:'check'},!c.enabled)}${btn('续期测试',{certificate:c.name,action:'renew-test'},!c.enabled)}${btn('按需续期',{certificate:c.name,action:'renew'},!c.enabled)}</div></td></tr>`)))+`<p class="muted">续期测试使用 Let’s Encrypt 测试环境；按需续期遵守到期判断，不强制重复签发。尚未签发的证书请先按 Git 部署流程建立入口。</p>`;
  }else if(current==='frp'){
    const frp=services.find(s=>s.service==='frps');
    html=`<div class="intro"><div><h2>开发服务穿透</h2><p class="muted">公网域名通过 Nginx 和 FRPS 回到本地客户端。连接状态以端到端检查结果为准。</p></div>${frp?btn('FRPS 日志',{detail:frp.id}):''}</div>`+panel('连接路径',frp?'FRPS '+esc(frp.status):'FRPS 尚未部署',`<div class="topology"><div class="node">浏览器<small>HTTPS 域名</small></div><span class="arrow">→</span><div class="node">Nginx<small>证书 / 入口</small></div><span class="arrow">→</span><div class="node">FRPS<small>容器内部 HTTP</small></div><span class="arrow">⇄</span><div class="node">本地 FRPC<small>TLS 穿透连接</small></div><span class="arrow">→</span><div class="node">本地应用<small>/healthz 检查</small></div></div>`)+panel('穿透域名','客户端令牌和私钥不展示',table(['域名 / 站点','类型 / 服务','配置状态','端到端检查','操作'],siteRows(sites.filter(s=>s.kind==='frp_http'))));
  }else if(current==='audit'){
    html=panel('操作任务','操作执行期间自动刷新；点击查看结果',jobTable(jobs))+panel('审计记录','记录管理登录、操作请求与最终结果',table(['时间','操作','目标','结果'],audit.map(e=>`<tr><td>${date(e.at)}</td><td>${esc(e.action)}</td><td>${esc(e.target)}</td><td>${stateBadge(e.result)}</td></tr>`)));
  }
  $('content').innerHTML=html;
}
function jobTable(list){return table(['操作','目标','状态','开始时间','结果'],list.map(j=>`<tr class="job-row" data-job="${esc(j.id)}"><td>${esc({probe:'连接检查',check:'证书检查','renew-test':'续期测试',renew:'按需续期',start:'启动',stop:'停止',restart:'重启'}[j.action]||j.action)}</td><td>${esc(j.target)}</td><td>${stateBadge(j.state)}</td><td>${date(j.created)}</td><td>查看详情 ↗</td></tr>`));}
function askOperation(path, body, target, title, impact){pending={path,body,target};$('confirm-title').textContent=title;$('confirm-impact').textContent=impact;$('confirm-target').textContent=target;$('confirmation').value='';$('confirm-error').textContent='';$('confirm-dialog').showModal();$('confirmation').focus();}
async function submitOperation(path, body){const result=await api(path,body);trackedJob=result.id;notice('操作已提交，正在执行；实际结果将在操作记录中显示。');await refresh();current='audit';render();return result;}
async function detailService(id){const s=snapshot.services.find(s=>s.id===id);$('detail-title').textContent=s?.service||'服务详情';$('detail-content').innerHTML='<p class="muted">正在读取日志与资源…</p>';$('detail-dialog').showModal();const results=await Promise.allSettled([api('services/'+id+'/logs'),api('services/'+id+'/stats')]);const logs=results[0],stats=results[1];let html='';if(stats.status==='fulfilled'){const v=stats.value;html=`<div class="resource"><div><strong>${v.cpu_percent??'—'}%</strong><small>CPU 使用率（可超过单核 100%）</small></div><div><strong>${(v.memory_bytes/1048576).toFixed(1)} MB</strong><small>内存工作集</small></div><div><strong>${v.pids??'—'}</strong><small>进程数</small></div></div>`;}else html=`<p class="muted">${esc(stats.reason.message)}</p>`;html+=`<p class="muted">最近 200 行日志 · 已对常见凭据脱敏</p><pre>${esc(logs.status==='fulfilled'?logs.value.text:logs.reason.message)}</pre>`;$('detail-content').innerHTML=html;}
document.addEventListener('click',async event=>{const button=event.target.closest('[data-tab],[data-probe],[data-detail],[data-service],[data-certificate],[data-job]');if(!button||button.disabled||!snapshot)return;const d=button.dataset;try{
  if(d.tab){current=d.tab;render();}
  else if(d.probe){await submitOperation('sites/probe',{site:d.probe});}
  else if(d.detail){await detailService(d.detail);}
  else if(d.service){const s=snapshot.services.find(s=>s.id===d.service);if(!s)return;const action={start:'启动',stop:'停止',restart:'重启'}[d.action];askOperation('services/'+s.id+'/action',{action:d.action},s.service,action+' '+s.service,s.service==='nginx'?'此操作将影响所有网站入口。请确认影响范围。':'此操作可能短暂中断该服务及依赖它的访问。配置与持久化数据保留。');}
  else if(d.certificate){const action={check:'检查证书','renew-test':'测试续期',renew:'按需续期'}[d.action];askOperation('certificates/action',{name:d.certificate,action:d.action},d.certificate,action,d.action==='renew'?'使用当前续期配置检查到期情况；需要时签发并通知 Nginx 自动重载。共享 SAN 的域名一起处理。':d.action==='renew-test'?'访问测试 ACME 服务并验证所有 SAN 域名。测试不会替换正式证书。':'核验当前证书、私钥匹配、SAN 与有效期，不签发证书。');}
  else if(d.job){const j=jobs.find(j=>j.id===d.job);$('detail-title').textContent=j.action+' · '+j.target;$('detail-content').innerHTML=`<p>${stateBadge(j.state)} ${date(j.completed||j.created)}</p><pre>${esc(j.output||'任务正在执行…')}</pre>`;$('detail-dialog').showModal();}
}catch(error){notice(error.message,true);}});
$('confirm-form').addEventListener('submit',async event=>{event.preventDefault();if(!pending||$('confirmation').value!==pending.target){$('confirm-error').textContent='名称不匹配，请输入完整名称。';return;}$('confirm-submit').disabled=true;try{await submitOperation(pending.path,{...pending.body,confirmation:pending.target});$('confirm-dialog').close();pending=null;}catch(error){$('confirm-error').textContent=error.message;}finally{$('confirm-submit').disabled=false;}});
$('cancel-confirm').onclick=()=>$('confirm-dialog').close();$('close-detail').onclick=()=>$('detail-dialog').close();$('refresh').onclick=refresh;
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('login-error').textContent='';try{const result=await api('login',{token:$('token').value.trim()});csrf=result.csrf;$('token').value='';await showShell();}catch(error){$('login-error').textContent=error.message;}finally{button.disabled=false;}});
$('logout').onclick=async()=>{try{await api('logout',{});showLogin();}catch(error){notice(error.message,true);}};
setInterval(()=>{if(loggedIn)refresh();},5000);
(async()=>{try{csrf=(await api('session')).csrf;await showShell();}catch{showLogin();}})();
