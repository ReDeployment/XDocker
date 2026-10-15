'use strict';
function githubWritable(){return loggedIn&&githubData.reachable&&navigator.onLine!==false&&!['verification','authentication','unexpected'].includes(connection.state);}
function githubLink(url,label){return /^https:\/\/github\.com\/(?:ArtisanCloud|Matrix-X)\/[A-Za-z0-9_.-]+(?:[\/#?]|$)/.test(url||'')?`<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(label)} ↗</a>`:esc(label);}
async function loadGithubSummary(){
  if(!loggedIn||githubSummaryLoading)return;githubSummaryLoading=true;const epoch=githubEpoch;
  try{const data=await api('github/summary');if(loggedIn&&epoch===githubEpoch){githubData.unread=data.unread;$('github-unread').textContent=data.unread?String(data.unread):'';}}
  catch(error){if(loggedIn)$('github-unread').textContent='?';}finally{githubSummaryLoading=false;}
}
async function loadGithub(more=false){
  if(!loggedIn||githubData.loading)return;githubData.loading=true;const epoch=githubEpoch;
  try{
    const data=await api('github'+(more&&githubData.nextBefore?'?before='+githubData.nextBefore:''));if(!loggedIn||epoch!==githubEpoch)return;
    githubData.projects=data.projects;githubData.notifications=more?[...githubData.notifications,...data.notifications.filter(n=>!githubData.notifications.some(old=>old.id===n.id))]:data.notifications;
    githubData.nextBefore=data.next_before;githubData.unread=data.unread;githubData.autoEnabled=data.auto_enabled;githubData.running=data.running;
    githubData.triage=data.triage||{issues:[],releases:[],diagnostics:[],pending:0};
    githubData.automation=data.automation||null;
    githubData.webhooks=data.webhooks||null;githubData.interval=data.interval_seconds;githubData.error=data.error||'';githubData.reachable=true;$('github-unread').textContent=data.unread?String(data.unread):'';
  }catch(error){githubData.error=error.message;githubData.reachable=false;}
  finally{githubData.loading=false;if(loggedIn&&current==='github')render();}
}
function renderGithub(){
  const labels={'blocked':'需处理','waiting':'检查执行中','attention':'审查待确认','observed-pass':'观测检查通过','unknown':'未确认'};
  let html=`<div class="intro"><div><h2>GitHub 项目动态</h2><p class="muted">先看待处理问题、失败检查和新发布。系统自动更新；需要时打开项目详情查看来源。</p></div><button data-github-refresh="yes" ${githubData.loading?'disabled':''}>刷新页面</button></div>`;
  const missing=githubData.projects.filter(p=>p.enabled&&p.access&&!p.access.configured).length;
  const automatic=!githubData.autoEnabled?'自动更新尚未启用：管理员完成一次连接配置后即可开启，无需逐个手动采集。':missing?'自动更新已开启；'+missing+' 个项目等待完成 GitHub 连接。':githubData.automation&&!githubData.automation.running?'自动更新服务尚未运行，当前只显示已保存的数据。':githubData.running?'正在检查 GitHub，完成后结果会自动更新。':'自动更新已开启：新事件触发核对，每 '+Math.round(githubData.interval/60)+' 分钟自动补漏。';
  html+=`<p class="notice">${esc(automatic)}</p>`;
  const sections={};const hooks=githubData.webhooks;const triage=githubData.triage||{issues:[],releases:[],diagnostics:[],pending:0};
  html+=`<p class="muted">${hooks?.auto_import_enabled?'通知自动接收已开启':'通知自动导入未开启'} · 未读消息 ${githubData.unread} · 待处理问题 ${triage.pending??0} · 检查与诊断不会自动修改项目代码。</p>`;
  sections.issues=githubIssues(triage);sections.releases=githubReleases(triage);sections.diagnostics=githubDiagnostics(triage);
  sections.hooks=panel('Webhook 接收队列','签名事件只导入通知；旧 SHA 或乱序 CI 事件需重新只读采集确认，不自动构建或发布',`<div class="panel-body"><p>${hooks?.ready?'队列可读':'队列未连接'} · ${hooks?.auto_import_enabled?'自动导入开启':'自动导入关闭'} · 待导入 ${hooks?.pending??'—'} · 历史 delivery ${hooks?.receipts??'—'}/${hooks?.receipt_limit??100000}</p><p class="muted">最近接收 ${date(hooks?.last_received_at)} · 最近导入 ${date(hooks?.last_import_at)}</p>${hooks?.error?`<p class="error">${esc(hooks.error)}</p>`:''}<button data-github-import="yes" ${!githubWritable()||!hooks?.ready?'disabled':''}>导入已接收事件（最多 50 条）</button></div>`);
  if(githubData.error)html+=`<p class="error" role="alert">${esc(githubData.error)}</p>`;
  sections.projects=panel('项目检查','这里回答每个项目是否已检查、有哪些问题。未完成连接或检查时，不代表代码通过。',table(['项目','现在是什么情况','最近检查','查看'],githubData.projects.map(p=>{
    const status=githubProjectStatus(p);
    return `<tr><td><strong>${esc(p.project)}</strong><small>${githubLink('https://github.com/'+p.repository,p.repository)}</small></td><td>${badge(status.title,status.type)}<small>${esc(status.detail)}</small><small>已保存消息 ${esc(p.event_count??'—')} 条</small></td><td><small>${p.success_at?date(p.success_at):'尚未完成首次检查'}</small></td><td><button data-github-detail="${esc(p.project)}">查看详情</button><details><summary>高级操作</summary><button data-github-backfill="${esc(p.project)}" ${!githubWritable()||githubData.running||!p.enabled?'disabled':''}>补采集历史</button><button data-github-sync="${esc(p.project)}" ${!githubWritable()||githubData.running||!p.enabled||p.retry_at>Date.now()/1000?'disabled':''}>手动重试</button><button data-github-configure="${esc(p.project)}" ${!githubWritable()?'disabled':''}>监控设置</button></details></td></tr>`;
  })));
  sections.hooks=githubConnections()+sections.hooks;
  sections.inbox=panel('通知收件箱','首次自动检查会补入近期记录；已读仅清除提示，不解除 CI、审查或发布阻塞',`<div class="panel-body"><button data-github-read="visible" ${!githubWritable()?'disabled':''}>当前页标记已读</button></div>`+table(['项目 / 时间','事件','提交关联','操作'],githubData.notifications.map(n=>`<tr><td><strong>${esc(n.project)}</strong><small>${date(n.at)}</small>${badge(n.read?'已读':'未读',n.read?'':'blue')}</td><td>${githubLink(n.payload.url,n.payload.title||n.payload.kind)}${n.payload.body?`<pre>${esc(n.payload.body)}</pre>`:''}<small>${esc(n.payload.actor||'')}</small>${n.payload.source==='signed-webhook'?`<small>已验签 Webhook · ${esc(n.payload.event)} · ${esc(n.payload.action||'')}</small><small class="code-line">delivery ${esc(n.payload.delivery||'')}</small>${n.payload.attempt?`<small>尝试 ${esc(n.payload.attempt)}</small>`:''}`:''}${n.payload.path?`<small>${esc(n.payload.path)} ${n.payload.line?'· '+esc(n.payload.line):''}</small>`:''}</td><td><small class="code-line">${esc(n.payload.sha||'未绑定 SHA')}</small><small>${esc(n.payload.binding||'仅状态观察')}</small>${n.payload.conclusion?badge(n.payload.conclusion,n.payload.conclusion==='failure'?'bad':''):''}</td><td><button data-github-read="${n.id}" ${n.read||!githubWritable()?'disabled':''}>已读</button></td></tr>`)));
  if(githubData.nextBefore)sections.inbox+=`<button data-github-more="yes" ${githubData.loading?'disabled':''}>更早通知</button>`;
  const tabs=[{key:'issues',label:'待处理问题',count:triage.pending??0},{key:'projects',label:'项目检查',count:githubData.projects.length},{key:'hooks',label:'连接与接收',count:hooks?.pending??'—'},{key:'inbox',label:'通知收件箱',count:githubData.unread},{key:'releases',label:'发布记录',count:triage.releases.length},{key:'diagnostics',label:'诊断记录',count:triage.diagnostics.length}];
  return html+verticalTabs('github','研发事件模块',tabs,moduleViews.github,sections[moduleViews.github]||sections.projects);
}
function githubProjectStatus(p){
 if(!p.enabled)return {title:'已暂停监控',detail:'此项目不会自动检查。',type:''};
 if(p.access&&!p.access.configured)return {title:'等待连接 GitHub',detail:githubConnectionProblem(p.access),type:'warn'};
 if(p.status==='collecting')return {title:'正在检查',detail:'完成后自动显示 PR 和 CI 结果。',type:'blue'};
 if(p.status==='webhook-needs-refresh'||p.status==='refresh-queued')return {title:'已收到新消息',detail:githubData.autoEnabled?'等待后台核对最新检查结果。':'完整检查尚未启用，请管理员完成一次连接配置。',type:'warn'};
 if(p.status==='collected'){
  const age=Date.now()-Date.parse(p.success_at||'');
  if(!(age>=0&&age<Math.max(600000,githubData.interval*2000)))return {title:'检查结果需要更新',detail:'保留上次结果，尚不能判断当前提交。',type:'warn'};
  const title={blocked:'检查失败，需要处理',waiting:'CI 正在运行',attention:'有审查意见待确认','observed-pass':'本次观察的检查通过',unknown:'检查信息尚不完整'}[p.snapshot?.state]||'检查信息尚不完整';
  return {title,detail:p.snapshot?.state==='observed-pass'?'仅表示所观察的检查通过，发布仍需验收。':'请打开详情查看失败步骤、审查意见或缺失的信息。',type:p.snapshot?.state==='blocked'?'bad':p.snapshot?.state==='observed-pass'?'good':'warn'};
 }
 if(p.status==='github-app-permissions-missing'||p.status==='github-app-permissions-unconfirmed')return {title:'GitHub App 权限需要补齐',detail:'请核对应用只读权限、仓库选择，以及安装账号是否批准最新权限。',type:'warn'};
 if(p.status==='github-app-installation-mismatch')return {title:'应用安装信息不匹配',detail:'请核对 App ID、安装 ID 与仓库所属方。',type:'warn'};
 if(p.status==='github-app-installation-suspended')return {title:'应用安装已暂停',detail:'需要在 GitHub 恢复此账号的应用安装。',type:'warn'};
 if(p.status?.startsWith('github-app-http-40')||p.status==='github-app-http-422')return {title:'GitHub App 连接需要检查',detail:'应用认证或仓库授权未确认；请核对私钥、安装账号及已选仓库。',type:'warn'};
 if(p.status?.startsWith('github-http-40'))return {title:'GitHub 连接需要检查',detail:'仓库访问或令牌授权未确认，不能据此判定代码有问题。',type:'warn'};
 if(p.retry_at>Date.now()/1000)return {title:'更新暂时失败',detail:githubData.autoEnabled?'将在 '+date(new Date(p.retry_at*1000).toISOString())+' 后自动重试。':'保留原数据，自动更新尚未启用。',type:'warn'};
 return {title:'尚未完成检查',detail:githubData.autoEnabled?'后台将自动读取检查结果。':'管理员启用自动更新后即可检查，无需每天逐个点击。',type:'warn'};
}
function githubConnectionProblem(access){
 return {'credentials-required':'服务器尚未配置此项目的只读连接；已收到的消息仍可查看。',
  'github-app-installation-required':'尚未配置此仓库所属方的 App 安装 ID。',
  'github-app-config-invalid':'应用配置格式不正确，请管理员重新检查。',
  'github-app-key-required':'应用私钥文件缺失，请管理员重新配置。',
  'github-app-key-invalid':'应用私钥无效或与配置不匹配，请管理员重新配置。',
  'github-app-crypto-unavailable':'管理服务缺少应用认证组件，需要更新 Admin 镜像。'}[access.state]||'服务器连接配置不可读取，需要管理员检查文件权限。';
}
function githubConnections(){
 const owners=[...new Set(githubData.projects.map(p=>p.access?.owner).filter(Boolean))];
 return panel('GitHub 连接状态','只需管理员配置一次服务器只读访问；之后系统自动检查。这里不接收或展示令牌。',table(['仓库所属方','连接状态','管理员配置位置'],owners.map(owner=>{
  const a=githubData.projects.find(p=>p.access?.owner===owner).access;
  return `<tr><td>${esc(owner)}</td><td>${badge(a.configured?(a.mode==='github-app'?'GitHub App 已配置，访问以实际检查为准':'令牌文件已配置，访问以实际检查为准'):'等待管理员连接',a.configured?'blue':'warn')}<small>${a.configured?(a.mode==='github-app'?'访问令牌由后台自动续取，无需手动更换。':'个人令牌到期需要替换。'):esc(githubConnectionProblem(a))}</small></td><td><code>${esc(a.file)}</code>${a.mode==='github-app'&&a.app_id?`<small>App ID ${esc(a.app_id)} · 安装 ID ${esc(a.installation_id)}</small>`:''}<small>私有文件权限 600；私钥和令牌不在网页显示。</small></td></tr>`;
 })))+`<p class="muted">自动更新总开关：${githubData.autoEnabled?'已启用':'尚未启用'}。已有事件可先在通知收件箱或项目详情查看。</p>`;
}
function githubProjectEvents(p){
 const events=p.recent_events||githubData.notifications.filter(n=>n.project===p.project).slice(0,10);
 return `<h3>已收到的消息</h3><p class="muted">共保存 ${esc(p.event_count??events.length)} 条，以下为最近 ${events.length} 条；收到消息不等于已完成当前检查。</p>`+table(['时间 / 事件','内容 / 来源'],events.map(n=>`<tr><td>${date(n.at)}<small>${esc(n.payload.actor||'')}</small></td><td>${githubLink(n.payload.url,n.payload.title||n.payload.kind||n.payload.event)}${n.payload.body?`<pre>${esc(n.payload.body)}</pre>`:''}<small>${esc(n.payload.conclusion||'')}</small></td></tr>`));
}
function openGithubDetail(project){
 const row=githubData.projects.find(p=>p.project===project);if(!row)return;
 const snap=row.snapshot,status=githubProjectStatus(row);$('detail-title').textContent=project+' · 项目检查详情';
 let html=`<p><strong>${esc(status.title)}</strong></p><p>${esc(status.detail)}</p><p class="muted">最近尝试：${date(row.attempted_at)} · 最近完成：${date(row.success_at)}</p>`;
 if(!snap)html+='<p class="notice">尚未拉取完整 PR 和 CI 结果，无法判断代码是否有问题。下方仍可查看已收到的消息。</p>';
 else html+=`<p class="muted">以下保留最近一次结果；请核对检查时间与对应提交。</p>`+
    table(['PR','对应提交','审查状态'],snap.pulls.map(p=>`<tr><td>${githubLink(p.url,'#'+p.number+' '+p.title)}<small>${esc(p.state)} ${p.draft?'draft':''}</small></td><td class="code-line">${esc(p.sha)}</td><td>${esc(p.review)}<small>历史审查 ${p.historical_reviews}</small></td></tr>`))+
    table(['对应提交','检查状态','检查结果'],snap.ci.map(c=>`<tr><td class="code-line">${esc(c.sha)}</td><td>${esc(c.state)}<small>${c.complete?'查询完整':'查询不完整'}</small></td><td>${c.checks.map(k=>`<small>${githubLink(k.url,k.name)} · ${esc(k.conclusion||k.status)}</small>${k.summary?`<pre>${esc(k.summary)}</pre>`:''}${(k.annotations||[]).map(a=>`<small>${esc(a.path)}:${esc(a.line||'')} · ${esc(a.level)} · ${esc(a.message)}</small>`).join('')}`).join('')}</td></tr>`))+
    table(['失败任务','失败步骤'],snap.failed_jobs.map(j=>`<tr><td>${githubLink(j.url,j.name)}<small>尝试 ${esc(j.attempt)}</small></td><td>${j.steps.map(esc).join('<br>')}<small class="code-line">${esc(j.sha)}</small></td></tr>`));
 $('detail-content').innerHTML=html+githubProjectEvents(row);$('detail-dialog').showModal();
}
function openGithubConfig(project){
  const row=githubData.projects.find(p=>p.project===project);if(!row)return;githubData.editing={project,revision:row.revision};
  $('detail-title').textContent=project+' · 本地观察配置';
  $('detail-content').innerHTML=`<p class="muted">只修改 XDocker 观察设置，不修改 GitHub 仓库、Actions 或分支保护。后台总开关 ${githubData.autoEnabled?'开启':'关闭'}。</p><label><input id="github-enabled" type="checkbox" ${row.enabled?'checked':''}>观察此项目</label><label>分支（逗号分隔；留空使用仓库默认分支）<input id="github-branches" value="${esc(row.branches.join(','))}"></label><label>需观察的检查名称（每行一项；留空观察全部）<textarea id="github-required">${esc(row.required_checks.join('\n'))}</textarea></label><p id="github-config-error" class="error"></p><button data-github-save="yes" ${!githubWritable()?'disabled':''}>保存配置</button>`;
  $('detail-dialog').showModal();
}
async function saveGithubConfig(){
  const edit=githubData.editing;if(!edit||!githubWritable())return;
  try{
    await api('github/action',{action:'configure',project:edit.project,revision:edit.revision,enabled:$('github-enabled').checked,
      branches:$('github-branches').value.split(',').map(s=>s.trim()).filter(Boolean),required_checks:$('github-required').value.split('\n').map(s=>s.trim()).filter(Boolean)});
    githubData.editing=null;$('detail-dialog').close();await loadGithub();notice('观察配置已保存；没有修改 GitHub 或启动镜像构建。');
  }catch(error){$('github-config-error').textContent=error.message;}
}

const githubIssueStates={new:'待处理',investigating:'处理中',resolved:'人工确认已解决',dismissed:'人工确认不处理'};
function githubIssues(data){
 return panel('待处理问题','按项目、PR／SHA 归并最近 2000 条通知；问题状态与 CI 证据分别保存，已读不等于已解决。',
 table(['项目 / 关联','问题 / 证据','处理状态','操作'],data.issues.map(i=>`<tr><td><strong>${esc(i.project)}</strong><small>${i.pr?'PR #'+esc(i.pr):'提交关联'}</small><small class="code-line">${esc(i.sha||'未绑定 SHA')}</small></td><td><strong>${esc(i.title)}</strong><small>${esc({blocked:'当前采集证据阻塞，需处理','needs-verification':'需核对当前提交与证据'}[i.publish_signal]||'未确认')}</small><small>${esc({historical:'历史提交证据','current-observed':'当前采集提交',unconfirmed:'尚未确认当前提交'}[i.head_binding])} · ${esc(i.current_ci)} · ${esc(i.evidence_count)} 条证据</small>${i.evidence.slice(0,3).map(e=>`<small>${githubLink(e.url,e.title||e.event)} · ${esc(e.actor)}</small>${e.path?`<small>${esc(e.path)}:${esc(e.line||'')}</small>`:''}`).join('')}</td><td>${badge(githubIssueStates[i.workflow_state]||'未确认',i.workflow_state==='new'?'warn':'')}<small>${esc(i.note||'尚无处理说明')}</small>${i.archived?'<small>保留的处理记录；已超观察窗口，需补采集核对</small>':''}${i.evidence_changed?'<small>出现新证据，旧处理结论需重新核对</small>':''}</td><td><button data-github-issue="${esc(i.id)}" ${!githubWritable()?'disabled':''}>证据与处理</button></td></tr>`))+
 (!data.issues.length?'<div class="panel-body"><p class="muted">当前观察窗口没有待核对证据；不代表代码已通过检查。可在「项目检查」确认连接和最近检查时间。</p></div>':''));
}
function githubReleases(data){
 return panel('Release 与预发布记录','按发布对象归并 Webhook／API 通知。发布存在不代表镜像已验收；分支名与短 SHA 不猜成完整提交。',
 table(['项目 / 发布','渠道 / 状态','提交 / 来源'],data.releases.map(r=>`<tr><td><strong>${esc(r.project)}</strong><small>${githubLink(r.url,r.title)}</small></td><td>${badge(r.action==='deleted'?'已删除':r.status==='prerelease'?'开发预发布':r.status==='draft'?'草稿':'正式发布')}<small>${esc(r.action||'API 观察')} · ${date(r.at)}</small></td><td><small class="code-line">${esc(r.sha||'完整 SHA 未确认')}</small><small>${esc(r.source)}</small></td></tr>`)));
}
function githubDiagnostics(data){
 return panel('只读证据诊断记录','基于已保存的事件、失败步骤和代码注解；不执行评论里的命令，不自动修复，不把推断当作代码根因。',
 table(['项目 / 时间','诊断','操作'],data.diagnostics.map(d=>`<tr><td><strong>${esc(d.project)}</strong><small>${date(d.at)}</small><small class="code-line">${esc(d.sha||'未绑定 SHA')}</small></td><td>${esc(d.summary)}<small>${d.stale?'新证据已到达，此报告需重新核对':'对应当前保存证据'}</small><small>${esc(d.root_cause)}</small></td><td><button data-github-report="${esc(d.id)}">查看报告</button></td></tr>`)));
}
function githubEvidence(item){
 return table(['事件 / 来源','正文 / 文件'],item.evidence.map(e=>`<tr><td>${githubLink(e.url,e.title||e.event)}<small>${esc(e.actor)} · ${date(e.at)}</small></td><td><pre>${esc(e.body||'无正文')}</pre><small>${esc(e.path||'')}${e.line?':'+esc(e.line):''}</small></td></tr>`));
}
function openGithubIssue(id){
 const item=githubData.triage?.issues.find(i=>i.id===id);if(!item)return;
 githubData.issueEditing={id,version:item.version,project:item.project};$('detail-title').textContent=item.project+' · '+item.title;
 $('detail-content').innerHTML=`<p class="muted">${esc(item.sha||'评论未绑定 SHA')} · ${esc(item.head_binding)}。手工处理状态不会改写 CI 或授权发布。</p>${githubEvidence(item)}<label>处理状态<select id="github-issue-state">${Object.entries(githubIssueStates).map(([k,v])=>`<option value="${k}" ${item.workflow_state===k?'selected':''}>${v}</option>`).join('')}</select></label><label>处理说明（至少 8 字）<textarea id="github-issue-note">${esc(item.note)}</textarea></label><label>输入项目名确认<input id="github-issue-confirm" autocomplete="off" placeholder="${esc(item.project)}"></label><p id="github-issue-error" class="error"></p><button data-github-issue-save="yes" ${!githubWritable()?'disabled':''}>保存本地处理状态</button><button data-github-diagnose="yes" ${!githubWritable()?'disabled':''}>生成只读证据诊断</button>`;
 $('detail-dialog').showModal();
}
function showGithubReport(report){
 $('detail-title').textContent=report.project+' · 只读证据诊断';
 $('detail-content').innerHTML=`<p>${esc(report.root_cause)}</p><p class="muted">${esc(report.sha||'未绑定 SHA')} · ${esc(report.head_binding)} · ${date(report.at)}</p>`+table(['失败任务','失败步骤'],report.failed_jobs.map(j=>`<tr><td>${githubLink(j.url,j.name)}</td><td>${j.steps.map(esc).join('<br>')}</td></tr>`))+table(['文件','位置 / GitHub 提示'],report.locations.map(p=>`<tr><td>${esc(p.path)}</td><td>${esc(p.line||'未提供行号')}<pre>${esc(p.message||'')}</pre></td></tr>`))+githubEvidence(report)+`<ol>${report.next_steps.map(s=>`<li>${esc(s)}</li>`).join('')}</ol>`;
 $('detail-dialog').showModal();
}
async function githubTriageAction(b){
 if(!b||b.disabled||!loggedIn)return;const d=b.dataset;
 try{
  if(d.githubIssue){openGithubIssue(d.githubIssue);return;}
  if(d.githubReport){const r=githubData.triage?.diagnostics.find(r=>r.id===d.githubReport);if(r)showGithubReport(r);return;}
  if(!githubWritable()||githubData.actionPending)return;
  if(d.githubBackfill){githubData.backfillProject=d.githubBackfill;$('detail-title').textContent=d.githubBackfill+' · 补采集历史';$('detail-content').innerHTML=`<p>使用固定 GitHub GET 查询补入最近 10 个 PR、每类 30 条评论与 10 个 Release 的观察记录；最多 100 条每次。不会读取邮件，不会启用轮询或修改 GitHub。</p><p class="muted">需服务器只读 GitHub 凭据；缺少私有仓库权限时保持未确认。</p><label>输入项目名确认<input id="github-backfill-confirm" autocomplete="off" placeholder="${esc(d.githubBackfill)}"></label><p id="github-backfill-error" class="error"></p><button data-github-backfill-submit="yes">开始补采集</button>`;$('detail-dialog').showModal();return;}
  let body;
  if(d.githubBackfillSubmit){body={action:'backfill',project:githubData.backfillProject,confirmation:$('github-backfill-confirm').value};if(body.confirmation!==body.project){$('github-backfill-error').textContent='项目名不匹配。';return;}}
  else{const i=githubData.issueEditing;if(!i)return;body={action:d.githubDiagnose?'diagnose':'issue-state',issue:i.id,version:i.version,confirmation:$('github-issue-confirm').value};if(body.confirmation!==i.project){$('github-issue-error').textContent='项目名不匹配。';return;}if(d.githubIssueSave){body.state=$('github-issue-state').value;body.note=$('github-issue-note').value;}}
  githubData.actionPending=true;b.disabled=true;
  const result=await api('github/action',body);
  $('detail-dialog').close();await loadGithub();await loadGithubSummary();
  if(d.githubDiagnose)showGithubReport(result);else notice(d.githubBackfillSubmit?'历史补采集已提交，结果以实际采集状态为准。':'本地问题状态已保存；CI 和发布授权保持独立。');
 }catch(error){const field=$('github-issue-error')||$('github-backfill-error');if(field)field.textContent=error.message;notice(error.message,true);}
 finally{githubData.actionPending=false;b.disabled=false;}
}
