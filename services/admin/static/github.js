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
    githubData.webhooks=data.webhooks||null;githubData.interval=data.interval_seconds;githubData.error=data.error||'';githubData.reachable=true;$('github-unread').textContent=data.unread?String(data.unread):'';
  }catch(error){githubData.error=error.message;githubData.reachable=false;}
  finally{githubData.loading=false;if(loggedIn&&current==='github')render();}
}
function renderGithub(){
  const labels={'blocked':'需处理','waiting':'检查执行中','attention':'审查待确认','observed-pass':'观测检查通过','unknown':'未确认'};
  let html=`<div class="intro"><div><h2>项目事件与 CI 观察</h2><p class="muted">六个仓库的提交、PR、检查与评论。每条信息保留来源和 SHA，观察结果不授权构建或发布。</p></div><button data-github-refresh="yes" ${githubData.loading?'disabled':''}>读取缓存</button><button data-github-sync="all" ${!githubWritable()||githubData.running?'disabled':''}>立即采集全部</button></div>`;
  html+=`<p class="muted">${githubData.autoEnabled?'自动轮询已启用，周期 '+githubData.interval+' 秒':'自动采集关闭；手动采集只读取 GitHub'} · ${githubData.running?'采集中':'当前空闲'} · 未读 ${githubData.unread}</p>`;
  const sections={};const hooks=githubData.webhooks;
  sections.hooks=panel('Webhook 接收队列','签名事件只导入通知；旧 SHA 或乱序 CI 事件需重新只读采集确认，不自动构建或发布',`<div class="panel-body"><p>${hooks?.ready?'队列可读':'队列未连接'} · ${hooks?.auto_import_enabled?'自动导入开启':'自动导入关闭'} · 待导入 ${hooks?.pending??'—'} · 历史 delivery ${hooks?.receipts??'—'}/${hooks?.receipt_limit??100000}</p><p class="muted">最近接收 ${date(hooks?.last_received_at)} · 最近导入 ${date(hooks?.last_import_at)}</p>${hooks?.error?`<p class="error">${esc(hooks.error)}</p>`:''}<button data-github-import="yes" ${!githubWritable()||!hooks?.ready?'disabled':''}>导入已接收事件（最多 50 条）</button></div>`);
  if(githubData.error)html+=`<p class="error" role="alert">${esc(githubData.error)}</p>`;
  sections.projects=panel('监控项目','缓存过期或采集异常时保持未确认；disabled/旧 SHA 不作为当前发布依据',table(['项目 / 仓库','观察状态','最近成功 / 当前 SHA','操作'],githubData.projects.map(p=>{
    const recent=p.success_at&&Date.now()-Date.parse(p.success_at)<Math.max(600000,githubData.interval*2000);
    const active=p.enabled&&p.status==='collected'&&recent;const state=active?p.snapshot?.state:'unknown';
    return `<tr><td><strong>${esc(p.project)}</strong><small>${githubLink('https://github.com/'+p.repository,p.repository)}</small></td><td>${badge(labels[state]||'未确认',state==='blocked'?'bad':state==='observed-pass'?'good':'warn')}<small>${p.enabled?'观察启用':'观察禁用'} · ${esc(p.status)}</small>${p.retry_at>Date.now()/1000?`<small>冷却至 ${date(new Date(p.retry_at*1000).toISOString())}</small>`:''}</td><td><small>${date(p.success_at)}</small>${(p.snapshot?.heads||[]).map(h=>`<small class="code-line">${esc(h.branch)} · ${esc(h.sha)}</small>`).join('')}${p.snapshot&&!active?'<small>仅保留历史快照，当前未确认</small>':''}</td><td><button data-github-sync="${esc(p.project)}" ${!githubWritable()||githubData.running||!p.enabled||p.retry_at>Date.now()/1000?'disabled':''}>采集</button><button data-github-detail="${esc(p.project)}">详情</button><button data-github-configure="${esc(p.project)}" ${!githubWritable()?'disabled':''}>观察配置</button></td></tr>`;
  })));
  sections.inbox=panel('通知收件箱','首轮为基线通知；已读仅清除提示，不解除 CI、审查或发布阻塞',`<div class="panel-body"><button data-github-read="visible" ${!githubWritable()?'disabled':''}>当前页标记已读</button></div>`+table(['项目 / 时间','事件','提交关联','操作'],githubData.notifications.map(n=>`<tr><td><strong>${esc(n.project)}</strong><small>${date(n.at)}</small>${badge(n.read?'已读':'未读',n.read?'':'blue')}</td><td>${githubLink(n.payload.url,n.payload.title||n.payload.kind)}${n.payload.body?`<pre>${esc(n.payload.body)}</pre>`:''}<small>${esc(n.payload.actor||'')}</small>${n.payload.source==='signed-webhook'?`<small>已验签 Webhook · ${esc(n.payload.event)} · ${esc(n.payload.action||'')}</small><small class="code-line">delivery ${esc(n.payload.delivery||'')}</small>${n.payload.attempt?`<small>尝试 ${esc(n.payload.attempt)}</small>`:''}`:''}${n.payload.path?`<small>${esc(n.payload.path)} ${n.payload.line?'· '+esc(n.payload.line):''}</small>`:''}</td><td><small class="code-line">${esc(n.payload.sha||'未绑定 SHA')}</small><small>${esc(n.payload.binding||'仅状态观察')}</small>${n.payload.conclusion?badge(n.payload.conclusion,n.payload.conclusion==='failure'?'bad':''):''}</td><td><button data-github-read="${n.id}" ${n.read||!githubWritable()?'disabled':''}>已读</button></td></tr>`)));
  if(githubData.nextBefore)sections.inbox+=`<button data-github-more="yes" ${githubData.loading?'disabled':''}>更早通知</button>`;
  const tabs=[{key:'projects',label:'项目与 CI',count:githubData.projects.length},{key:'hooks',label:'Webhook 接收',count:hooks?.pending??'—'},{key:'inbox',label:'通知收件箱',count:githubData.unread}];
  return html+verticalTabs('github','研发事件模块',tabs,moduleViews.github,sections[moduleViews.github]||sections.projects);
}
function openGithubDetail(project){
  const row=githubData.projects.find(p=>p.project===project);if(!row)return;
  const snap=row.snapshot;$('detail-title').textContent=project+' · 观察详情';
  $('detail-content').innerHTML=!snap?'<p>尚无成功快照；请核对权限与采集状态。</p>':`<p class="muted">${esc(snap.scope)} · ${date(snap.checked_at)}。状态过期后需重新采集，历史评论不自动视为当前提交阻塞。</p>`+
    table(['PR','头提交','审查'],snap.pulls.map(p=>`<tr><td>${githubLink(p.url,'#'+p.number+' '+p.title)}<small>${esc(p.state)} ${p.draft?'draft':''}</small></td><td class="code-line">${esc(p.sha)}</td><td>${esc(p.review)}<small>历史审查 ${p.historical_reviews}</small></td></tr>`))+
    table(['SHA','检查状态','检查'],snap.ci.map(c=>`<tr><td class="code-line">${esc(c.sha)}</td><td>${esc(c.state)}<small>${c.complete?'查询完整':'查询不完整'}</small></td><td>${c.checks.map(k=>`<small>${githubLink(k.url,k.name)} · ${esc(k.conclusion||k.status)}</small>${k.summary?`<pre>${esc(k.summary)}</pre>`:''}${(k.annotations||[]).map(a=>`<small>${esc(a.path)}:${esc(a.line||'')} · ${esc(a.level)} · ${esc(a.message)}</small>`).join('')}`).join('')}</td></tr>`))+
    table(['失败任务','步骤 / 提交'],snap.failed_jobs.map(j=>`<tr><td>${githubLink(j.url,j.name)}<small>尝试 ${j.attempt}</small></td><td>${j.steps.map(esc).join('<br>')}<small class="code-line">${esc(j.sha)}</small></td></tr>`));
  $('detail-dialog').showModal();
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
