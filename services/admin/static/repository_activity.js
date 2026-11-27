'use strict';

const repositoryView = {scope:'all', detail:null, section:'branches', filter:'all', treeBranch:'', scopeDraft:null, request:0};
const branchFilters = {all:'全部分支',parallel:'并行开发',pending:'待合入'};
const observationDate = stamp => stamp ? date(stamp*1000) : '未取得';
const activityOptions = (values,selected) => Object.entries(values).map(([key,label]) =>
  `<option value="${esc(key)}" ${key===selected?'selected':''}>${esc(label)}</option>`).join('');
const activityCell = (label,html) => `<td data-label="${esc(label)}">${html}</td>`;
function resetRepositoryView(){
  repositoryView.request++;
  repositoryView.scope='all';
  repositoryView.detail=null;
  repositoryView.scopeDraft=null;
  $('repo-dialog')?.remove();
}
function repoButton(row, filter, label, description){
  return `<button class="repo-link" type="button" data-repo-open="${esc(row.id)}" data-repo-filter="${filter}" aria-label="${esc(description)}">${esc(label)}</button>`;
}
function repositoryOverview(activity){
  if(!activity)return '<p class="empty">等待仓库检查数据</p>';
  const all=activity.repositories;
  const available=all.filter(r=>r.branch_count!=null);
  const missing=all.length-available.length;
  const scopes={all:'全部登记',observed:'已识别仓库',missing:'目录待核对'};
  const list=all.filter(r=>repositoryView.scope==='all'||(repositoryView.scope==='missing')===(r.branch_count==null))
    .sort((a,b)=>(b.parallel??b.active??-1)-(a.parallel??a.active??-1)||(b.latest||0)-(a.latest||0)||a.name.localeCompare(b.name));
  const progress=activity.progress;
  const running=progress.state==='running';
  const state=running?`正在检查 ${progress.done} / ${progress.total} · ${esc(progress.name)}`:
    activity.pending?'等待检查':progress.state==='failed'?'检查失败，等待重试':
    activity.at?'上次检查 '+observationDate(activity.at):'等待首次检查';
  const header=`<div class="repo-tools"><div><strong>${available.length} 个仓库 · ${missing} 项目录待核对</strong><small role="status">${state} · 每日 02:00（上海时间）</small></div>
    <div class="actions"><select aria-label="筛选仓库" id="repo-scope">${activityOptions(scopes,repositoryView.scope)}</select>
    <button type="button" data-repo-refresh ${!activity.available||running||activity.pending?'disabled':''}>立即检查</button></div></div>`;
  const rows=list.map(r=>{
    const identity=repoButton(r,'all',r.name,r.name+' · 查看仓库详情')+`<small>${esc(r.repository||r.notes[0]||'目录待核对')}</small>`;
    const branch=r.branch?`<code title="${esc(r.directory)}">${esc(r.branch)}</code>`:'未检测到';
    const development=r.target?'<strong>'+esc(r.target)+'</strong><small>'+
      repoButton(r,'pending',r.pending==null?'待核实':r.pending+' 条待合入',r.name+' · 查看待合入分支')+'</small>'+
      (r.pending!=null&&r.unknown_merges?'<small>'+r.unknown_merges+' 条待核实</small>':''):'待核实';
    const release=r.release?'<strong>'+esc(r.release.branch)+'</strong><small>'+
      repoButton(r,'release',r.release.count==null?'待核实':r.release.count+' 个提交待发布',
        r.name+' · 查看待发布提交')+'</small>':r.branch_count==null?'待核实':'未设置';
    const parallel=r.parallel==null?'待检查':repoButton(r,'parallel',(r.partial?'已发现 ':'')+r.parallel+' 条分支',r.name+' · 查看并行开发分支');
    const basis=r.partial&&r.branch_count!=null?'<small>'+esc(r.basis==='GitHub'?'部分数据待核实':'按本机记录')+'</small>':'';
    return '<tr>'+activityCell('仓库',identity)+activityCell('当前开发分支',branch)+
      activityCell('开发主干',development)+activityCell('发布分支',release)+activityCell('并行开发',parallel+basis)+'</tr>';
  });
  return `<section class="panel repo-panel">${header}${table(['仓库','当前开发分支','开发主干','发布分支','并行开发'],rows)}</section>`;
}

async function openRepository(id,filter){
  const seq=++repositoryView.request;
  try{
    const detail=await api('code-sync/activity/'+id);
    if(seq!==repositoryView.request||!loggedIn||current!=='code-sync')return;
    const section=filter==='release'?'release':'branches';
    Object.assign(repositoryView,{detail,section,treeBranch:'',filter:section==='release'?'all':filter,scopeDraft:detail.scope});
    let dialog=$('repo-dialog');
    if(!dialog){
      dialog=document.createElement('dialog');dialog.id='repo-dialog';dialog.setAttribute('aria-labelledby','repo-title');
      document.body.append(dialog);
    }
    renderRepositoryDetail();
    if(!dialog.open)dialog.showModal();
  }catch(error){if(seq===repositoryView.request)notice(error.message,true);}
}
function branchDifference(branch){
  const comparisons=branch.differences||[];
  if(!comparisons.length)return '待核实';
  return '<div>'+comparisons.map(({sha,scope,ahead,behind})=>{
    const counts=ahead==null?'待核实':ahead===0&&behind===0?'与主干一致':`独有 ${ahead} · 落后 ${behind}`;
    return comparisons.length===1?counts:`<small>${esc(scope)} · ${esc(sha.slice(0,10))}</small><span>${counts}</span>`;
  }).join('')+'</div>';
}
function branchDecision(branch,summary){
  if(branch.merge==='target')return '开发主干';
  if(branch.merge==='release')return '发布分支';
  if(branch.merge==='merged'){
    return branch.differences?.some(d=>d.ahead>0)?'已有 PR 合入记录':'无需合入';
  }
  if(branch.merge==='unknown')return '合入状态待核实';
  if(branch.prs.some(pr=>pr.base===summary.target))return '查看合入 PR';
  return branch.parallel||branch.name===summary.branch?'核对后合入':'核对是否仍需合入';
}
function branchTable(detail){
  const s=detail.summary;
  const list=detail.branches.filter(b=>repositoryView.filter==='all'||(repositoryView.filter==='parallel'?b.parallel:b.merge==='pending'));
  list.sort((a,b)=>Number(b.name===s.branch)-Number(a.name===s.branch)||Number(Boolean(b.parallel))-Number(Boolean(a.parallel)));
  const rows=list.map(b=>{
    const workspaces=b.directories.length?`<button class="repo-link" type="button" data-repo-worktrees="${esc(b.name)}">${b.directories.length} 个工作目录</button>`:'无检出目录';
    const links=b.prs.map(pr=>`<a href="https://github.com/${esc(s.repository)}/pull/${pr.number}" target="_blank" rel="noreferrer">PR #${pr.number} → ${esc(pr.base)}</a>`).join(' · ');
    const latest=b.sha.map(sha=>esc(sha.slice(0,10))+(b.tip_dates?.[sha]?' · '+observationDate(b.tip_dates[sha]):'')).join(' / ');
    const role=b.name===s.branch?'当前开发':b.parallel?'并行开发':'';
    const status=`<span class="${b.merge==='pending'?'sync-source-issue':''}">${branchDecision(b,s)}</span>`;
    const activity=b.reasons.map(esc).join(' · ');
    return '<tr>'+activityCell('分支',`<strong>${esc(b.name)}</strong>${role?`<small>${role}</small>`:''}<small>${latest}</small>`)+
      activityCell('与开发主干差异',branchDifference(b))+
      activityCell('合入建议',status+`<small>${esc(b.push)}</small>`)+
      activityCell('开发活动',(activity?`<span>${activity}</span>`:'')+`<small>${workspaces}</small>`+
        `<small>${links||(b.prs_known?'无开放 PR':'PR 待核实')}</small>`)+'</tr>';
  });
  const heading=s.target?'与 '+esc(s.target)+' 的差异':'与开发主干的差异';
  return `<div class="repo-filter"><label>显示 <select id="repo-branch-filter">${activityOptions(branchFilters,repositoryView.filter)}</select></label><span>${list.length} 条分支</span></div>
    ${table(['分支',heading,'合入建议','开发活动'],rows)}
    <p class="sync-help">独有是该分支还有多少提交没进主干。落后是主干还有多少提交没进该分支。并行开发按未提交改动、开放 PR 和近 7 天的待合入提交判断。</p>`;
}
function worktreeTable(detail){
  const branchFilter=repositoryView.treeBranch;
  const rows=detail.worktrees.filter(w=>!branchFilter||w.branch===branchFilter).map(w=>{
    const branch=w.branch?esc(w.branch):'固定提交（detached HEAD）';
    const state=w.dirty==null?esc(w.note||'待核实'):w.dirty?'有未提交改动':'工作区干净';
    const changes=w.dirty?[['已暂存',w.staged],['未暂存',w.modified],['新增',w.new]].filter(([,n])=>n).map(([k,n])=>k+' '+n).join(' · '):'';
    return '<tr>'+activityCell('工作目录',esc(w.directory)+(w.directory===detail.summary.directory?'<small>概览所示目录</small>':''))+
      activityCell('检出分支',branch+`<small>${esc(w.head.slice(0,12))}</small>`)+activityCell('工作区',state+`<small>${esc(changes)}</small>`)+'</tr>';
  });
  const controls=branchFilter?`<div class="repo-filter">${esc(branchFilter)} <button data-repo-all-trees>查看全部目录</button></div>`:'';
  return controls+table(['本机工作目录','检出分支 / 提交','工作区'],rows);
}
function releaseTable(detail){
  const release=detail.summary.release;
  const commits=detail.release_commits;
  const direction=`<p class="repo-direction"><strong>${esc(release.development||'开发主干待核实')} → ${esc(release.branch)}</strong><small>${esc(release.basis)}</small></p>`;
  if(release.count==null)return direction+`<p>${esc(release.note)}</p>`;
  if(!release.count)return direction+'<p>开发主干的提交均已进入发布分支。</p>';
  const rows=commits.map(commit=>'<tr>'+activityCell('提交',`<code>${esc(commit.sha.slice(0,12))}</code>`)+
    activityCell('描述',esc(commit.subject||'—'))+activityCell('提交时间',observationDate(commit.at))+'</tr>');
  const count=release.count>commits.length?`，显示最近 ${commits.length} 个`:'';
  return direction+`<p>${release.count} 个提交待进入发布分支${count}</p>`+
    table(['提交','描述','提交时间'],rows)+
    '<p class="sync-help">按 Git 提交历史比较。通过 cherry-pick 或 squash 进入发布分支的改动可能需要核对。</p>';
}
function scopeOptions(branches,selected){
  const names=[...new Set(branches.map(b=>b.name))].sort((a,b)=>a.localeCompare(b));
  const missing=selected&&!names.includes(selected)?
    `<option value="${esc(selected)}" selected>${esc(selected)}（本轮未发现）</option>`:'';
  return missing+names.map(name=>`<option value="${esc(name)}" ${name===selected?'selected':''}>${esc(name)}</option>`).join('');
}
function renderRepositoryDetail(){
  const d=repositoryView.detail,r=d.summary;
  const sections={branches:'分支差异',trees:'工作目录 '+(r.worktree_count??'')};
  if(repositoryView.section==='release')sections.release='发布差异';
  const controls=Object.entries(sections).map(([id,label])=>`<button type="button" data-repo-tab="${id}" aria-pressed="${id===repositoryView.section}">${esc(label)}</button>`).join('');
  let body;
  if(repositoryView.section==='settings'){
    body=`<dl class="sync-source-facts"><dt>登记项目</dt><dd>${r.projects.map(esc).join('、')}</dd><dt>登记目录</dt><dd>${r.directories.map(path=>esc(path)+(r.directory_issues?.[path]?' · '+esc(r.directory_issues[path]):'')).join('<br>')}</dd><dt>分支数据来源</dt><dd>${esc(r.basis||'待核对目录')}</dd><dt>检查时间</dt><dd>${observationDate(r.at)}</dd></dl>`;
    if(d.branches.length&&codeSyncUI.data?.activity.available){
      const scope=repositoryView.scopeDraft??{branch:r.override||'',release:r.release?.branch||''};
      const defaultName=r.default_branch?' · '+esc(r.default_branch):'';
      const form=`<form id="repo-target-form">
        <label>开发主干 <select name="branch"><option value="">仓库默认分支${defaultName}</option>${scopeOptions(d.branches,scope.branch)}</select></label>
        <label>发布分支 <select name="release"><option value="">不跟踪发布</option>${scopeOptions(d.branches,scope.release)}</select></label>
        <button type="submit">保存并检查</button><p class="error" id="repo-scope-error" role="alert"></p>
      </form>`;
      body=form+body;
    }
    body+='<p class="sync-help">待合入按分支提交与目标分支的历史关系核对，并参考最近 100 个已关闭 PR 的合入记录。更早的压缩或变基合入可能需要人工核对。</p>';
  }else if(r.branch_count==null)body='<p>请核对登记目录和本机读取范围。</p>';
  else if(repositoryView.section==='release')body=releaseTable(d);
  else body=repositoryView.section==='branches'?branchTable(d):worktreeTable(d);
  const notes=r.notes.map(esc).join(' · ');
  const configButton=`<button type="button" data-repo-tab="settings" aria-pressed="${repositoryView.section==='settings'}">配置分支</button>`;
  const coverage=r.partial&&r.branch_count!=null?' · '+esc(r.basis||'部分数据待核实'):'';
  $('repo-dialog').innerHTML=`<div class="detail-head"><div><h2 id="repo-title">${esc(r.name)}</h2><small>开发主干 ${esc(r.target||'待核实')}${coverage}</small></div><div class="repo-head-actions">${configButton}<button type="button" data-repo-close>关闭</button></div></div>
    <div class="repo-tabs" role="group" aria-label="仓库详情分区">${controls}</div>${notes?`<p class="repo-note">${notes}</p>`:''}<div class="repo-body">${body}</div>`;
}
document.addEventListener('change',event=>{
  if(event.target.form?.id==='repo-target-form')repositoryView.scopeDraft=Object.fromEntries(new FormData(event.target.form));
  if(event.target.id==='repo-scope'){repositoryView.scope=event.target.value;renderCodeSync();$('repo-scope').focus();}
  if(event.target.id==='repo-branch-filter'){repositoryView.filter=event.target.value;renderRepositoryDetail();$('repo-branch-filter').focus();}
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-repo-open],[data-repo-close],[data-repo-tab],[data-repo-refresh],[data-repo-worktrees],[data-repo-all-trees]');
  if(!button||button.disabled||!loggedIn)return;
  const data=button.dataset;
  if(data.repoOpen){await openRepository(data.repoOpen,data.repoFilter);return;}
  if('repoClose' in data){$('repo-dialog').close();return;}
  if(data.repoWorktrees||'repoAllTrees' in data){
    repositoryView.section='trees';repositoryView.treeBranch=data.repoWorktrees||'';renderRepositoryDetail();$('repo-dialog').querySelector('[aria-pressed="true"]').focus();return;
  }
  if(data.repoTab){repositoryView.section=data.repoTab;renderRepositoryDetail();$('repo-dialog').querySelector('[aria-pressed="true"]').focus();return;}
  button.disabled=true;
  try{await api('code-sync/activity/refresh',{});await loadCodeSync();}
  catch(error){notice(error.message,true);button.disabled=false;}
});
document.addEventListener('submit',async event=>{
  if(event.target.id!=='repo-target-form')return;
  event.preventDefault();const form=event.target,button=event.submitter;button.disabled=true;
  const fields=Object.fromEntries(new FormData(form));
  try{
    await api('code-sync/activity/'+repositoryView.detail.summary.id+'/target',fields);
    $('repo-dialog').close();notice('检查范围已保存，正在排队重新检查。');await loadCodeSync();
  }catch(error){
    const message=form.querySelector('#repo-scope-error');
    if(form.isConnected)message.textContent=error.message;
    else notice(error.message,true);
    button.disabled=false;
  }
});
