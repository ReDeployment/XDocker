'use strict';

const repositoryView = {scope:'all', detail:null, section:'branches', filter:'all', treeBranch:'', request:0};
const branchFilters = {all:'全部分支',active:'推进中',pending:'待合入'};
const mergeLabels = {target:'目标分支',pending:'有待合入提交',merged:'已合入',unknown:'待核实'};
const observationDate = stamp => stamp ? date(stamp*1000) : '未取得';
const activityOptions = (values,selected) => Object.entries(values).map(([key,label]) =>
  `<option value="${esc(key)}" ${key===selected?'selected':''}>${esc(label)}</option>`).join('');
const activityCell = (label,html) => `<td data-label="${esc(label)}">${html}</td>`;
function resetRepositoryView(){
  repositoryView.request++;
  repositoryView.scope='all';
  repositoryView.detail=null;
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
    .sort((a,b)=>(b.active??-1)-(a.active??-1)||(b.latest||0)-(a.latest||0)||a.name.localeCompare(b.name));
  const progress=activity.progress;
  const running=progress.state==='running';
  const state=running?`正在检查 ${progress.done} / ${progress.total} · ${esc(progress.name)}`:
    activity.pending?'等待检查':progress.state==='failed'?'检查失败，等待重试':
    activity.at?'上次检查 '+observationDate(activity.at):'等待首次检查';
  const header=`<div class="repo-tools"><div><strong>${available.length} 个仓库 · ${missing} 项目录待核对</strong><small role="status">${state} · 每日 02:00（上海时间）</small></div>
    <div class="actions"><select aria-label="筛选仓库" id="repo-scope">${activityOptions(scopes,repositoryView.scope)}</select>
    <button type="button" data-repo-refresh ${!activity.available||running||activity.pending?'disabled':''}>立即检查</button></div></div>`;
  function metric(row,key){
    if(row[key]==null)return '待核实';
    const label=(key==='active'&&row.partial)||(key==='pending'&&row.unknown_merges)?'已见 '+row[key]:row[key];
    return repoButton(row,key,label,row.name+' · '+branchFilters[key]+' '+label);
  }
  const rows=list.map(r=>{
    const identity=repoButton(r,'all',r.name,r.name+' · 查看仓库详情')+`<small>${esc(r.repository||r.notes[0]||'目录待核对')}</small>`;
    const branch=r.branch?`<code title="${esc(r.directory)}">${esc(r.branch)}</code>`:'未检测到';
    return '<tr>'+activityCell('仓库',identity)+activityCell('当前检出分支',branch)+activityCell('推进中',metric(r,'active'))+
      activityCell('待合入',metric(r,'pending')+(r.target?'<small>→ '+esc(r.target)+(r.target_push==='本地有待推送提交'?' · 本地待推送':'')+'</small>':''))+activityCell('最近活动',observationDate(r.latest)+(r.partial&&r.branch_count!=null?'<small>'+esc(r.basis==='GitHub'?'部分数据待核实':r.basis)+'</small>':''))+'</tr>';
  });
  return `<section class="panel repo-panel">${header}${table(['仓库','当前检出分支','推进中','待合入','最近活动'],rows)}</section>`;
}

async function openRepository(id,filter){
  const seq=++repositoryView.request;
  try{
    const detail=await api('code-sync/activity/'+id);
    if(seq!==repositoryView.request||!loggedIn||current!=='code-sync')return;
    Object.assign(repositoryView,{detail,section:'branches',treeBranch:'',filter});
    let dialog=$('repo-dialog');
    if(!dialog){
      dialog=document.createElement('dialog');dialog.id='repo-dialog';dialog.setAttribute('aria-labelledby','repo-title');
      document.body.append(dialog);
    }
    renderRepositoryDetail();
    if(!dialog.open)dialog.showModal();
  }catch(error){if(seq===repositoryView.request)notice(error.message,true);}
}
function branchTable(detail){
  const list=detail.branches.filter(b=>repositoryView.filter==='all'||(repositoryView.filter==='active'?b.reasons.length:b.merge==='pending'));
  const rows=list.map(b=>{
    const workspaces=b.directories.length?`<button class="repo-link" type="button" data-repo-worktrees="${esc(b.name)}">${b.directories.length} 个工作目录</button>`:'无检出目录';
    const links=b.prs.map(pr=>`<a href="https://github.com/${esc(detail.summary.repository)}/pull/${pr.number}" target="_blank" rel="noreferrer">#${pr.number} → ${esc(pr.base)}</a>`).join(' · ');
    const latest=b.sha.map(s=>esc(s.slice(0,10))+(b.tip_dates?.[s]?' · '+observationDate(b.tip_dates[s]):'')).join(' / ');
    return '<tr>'+activityCell('分支',`<strong>${esc(b.name)}</strong><small>${latest}</small>`)+
      activityCell('开发活动',(b.reasons.map(esc).join(' · ')||'暂无推进信号')+`<small>${workspaces}</small>`)+
      activityCell('推送与合入',esc(b.push)+'<small>'+mergeLabels[b.merge]+'</small>')+
      activityCell('开放 PR',links||(b.prs_known?'无':'未核实'))+'</tr>';
  });
  return `<div class="repo-filter"><label>显示 <select id="repo-branch-filter">${activityOptions(branchFilters,repositoryView.filter)}</select></label><span>${list.length} 条</span></div>
    ${table(['分支 / 最新提交','开发活动','推送与合入','开放 PR'],rows)}
    <p class="sync-help">推进中按分支去重。除目标分支外，工作区有改动、有开放 PR，或近 7 天有待合入提交，均计入。</p>`;
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
function renderRepositoryDetail(){
  const d=repositoryView.detail,r=d.summary;
  const sections={branches:'分支 '+(r.branch_count??'—'),trees:'工作目录（worktree） '+(r.worktree_count??'—'),settings:'检查范围'};
  const controls=Object.entries(sections).map(([id,label])=>`<button type="button" data-repo-tab="${id}" aria-pressed="${id===repositoryView.section}">${esc(label)}</button>`).join('');
  const targets=activityOptions(Object.fromEntries(d.branches.map(b=>[b.name,b.name])),r.override);
  let body;
  if(repositoryView.section==='settings'){
    body=`<dl class="sync-source-facts"><dt>登记项目</dt><dd>${r.projects.map(esc).join('、')}</dd><dt>登记目录</dt><dd>${r.directories.map(path=>esc(path)+(r.directory_issues?.[path]?' · '+esc(r.directory_issues[path]):'')).join('<br>')}</dd><dt>分支数据来源</dt><dd>${esc(r.basis||'待核对目录')}</dd><dt>检查时间</dt><dd>${observationDate(r.at)}</dd></dl>`;
    if(d.branches.length&&codeSyncUI.data?.activity.available)body+=`<form id="repo-target-form"><label>合入目标 <select name="branch"><option value="">仓库默认分支</option>${targets}</select></label><button type="submit">保存并检查</button></form>`;
    body+='<p class="sync-help">待合入按分支提交与目标分支的历史关系核对，并参考最近 100 个已关闭 PR 的合入记录。更早的压缩或变基合入可能需要人工核对。</p>';
  }else if(r.branch_count==null)body='<p>请核对登记目录和本机读取范围。</p>';
  else body=repositoryView.section==='branches'?branchTable(d):worktreeTable(d);
  const notes=r.notes.map(esc).join(' · ');
  $('repo-dialog').innerHTML=`<div class="detail-head"><div><h2 id="repo-title">${esc(r.name)}</h2><small>${esc(r.repository||r.notes[0]||'目录待核对')} · 合入目标 ${esc(r.target||'待核实')}</small></div><button type="button" data-repo-close>关闭</button></div>
    <div class="repo-tabs" role="group" aria-label="仓库详情分区">${controls}</div>${notes?`<p class="repo-note">${notes}</p>`:''}<div class="repo-body">${body}</div>`;
}
document.addEventListener('change',event=>{
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
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try{
    await api('code-sync/activity/'+repositoryView.detail.summary.id+'/target',{branch:event.target.elements.branch.value});
    $('repo-dialog').close();notice('合入目标已保存，正在排队重新检查。');await loadCodeSync();
  }catch(error){notice(error.message,true);button.disabled=false;}
});
