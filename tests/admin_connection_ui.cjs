// Exercise the shipped UI with controlled transport failures, without touching real SSH or Docker.
'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(require('path').join(__dirname,'../services/admin/static/app.js'),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
  const elements=new Map(),handlers={},requests=[],timers=new Map();let timerId=0;
  const state={authenticated:false,mode:'ok',csrf:'first-csrf',posts:0};
  state.preflightItems=[{key:'network-ghcr',title:'GHCR',group:'网络'},{key:'host-compose',title:'宿主机 Compose',group:'宿主机报告'}];
  state.plans=[{instance:'powerx-a',revision:'',enabled:false,frequency:'daily',weekday:6,time:'03:00',timezone:'Asia/Shanghai',grace_minutes:5,keep:7,auto_prune:false,next_run_at:null}];
  const refs={backend:'ghcr.io/artisancloud/powerx-backend@sha256:'+'a'.repeat(64),'web-admin':'ghcr.io/artisancloud/powerx-web-admin@sha256:'+'b'.repeat(64)};
  state.versionCandidate={id:'dev-fixture',channel:'dev',version:'sha-'+'c'.repeat(40),source_revision:'c'.repeat(40),images:refs,evidence:{run_id:12345,release_tag:null},migration:{status:'none',from_revisions:['d'.repeat(40)],notes:'fixture migration',rollback:'compatible'}};
  state.versionCandidates=[];state.versionProofs=[];
  state.githubProjects=['PowerX','PowerXPlugin','PowerXPluginSCRM','PowerXPluginECommerce','PowerXPluginMediaXStudio','PowerXPluginCRM'].map(project=>({project,
    repository:(['PowerXPluginMediaXStudio','PowerXPluginCRM'].includes(project)?'Matrix-X/':'ArtisanCloud/')+project,
    enabled:true,branches:[],required_checks:[],revision:'fixture-'+project,status:'not-collected',success_at:null,retry_at:0,snapshot:null}));
  state.githubWebhooks={ready:false,pending:null,auto_import_enabled:false,error:'webhook-queue-unavailable'};
  state.githubNotifications=[{id:1,project:'PowerX',at:new Date().toISOString(),read:false,payload:{title:'Review comment',kind:'comment',
    body:'<script>do not execute</script>',sha:'a'.repeat(40),binding:'historical',url:'https://github.com/ArtisanCloud/PowerX/pull/1'}}];
  const configured=Object.fromEntries(Object.entries(refs).map(([r,reference])=>[r,{reference,pin:'digest',tag_revision:null}]));
  const runtime=Object.fromEntries(Object.entries(refs).map(([r,reference],n)=>[r,{state:'running',health:'healthy',configuration_match:'matches',source_revision:'d'.repeat(40),image_id:'sha256:'+String(n+1).repeat(64),repo_digests:[reference]}]));
  state.versionCurrent={instance:'powerx-a',at:new Date().toISOString(),configured,runtime,pair:{state:'matched',source_revision:'d'.repeat(40)}};
  function element(id){
    if(!elements.has(id))elements.set(id,{id,value:'',type:'password',dataset:{},innerHTML:'',textContent:'',className:'',open:false,disabled:false,classList:{add(){},remove(){},toggle(){}},showModal(){this.open=true},close(){this.open=false},focus(){},addEventListener(type,fn){handlers[id+':'+type]=fn;}});
    return elements.get(id);
  }
  const context=vm.createContext({console,Map,Promise,Error,Date,AbortController,
    location:{hash:''},window:{addEventListener(type,fn){handlers['window:'+type]=fn;}},
    document:{hidden:false,getElementById:element,querySelectorAll(){return [];},addEventListener(type,fn){handlers['document:'+type]=fn;}},
    navigator:{onLine:true},setInterval(fn){handlers.poll=fn;},setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    async fetch(url,options={}){
      requests.push({url,method:options.method||'GET',body:options.body,csrf:options.headers?.['X-CSRF-Token']});
      if(options.method==='POST')state.posts++;
      if(state.mode==='timeout')return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const error=Error('aborted');error.name='AbortError';reject(error);}));
      if(state.mode==='bodytimeout')return {ok:true,status:200,json:()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const error=Error('aborted body');error.name='AbortError';reject(error);}))};
      if(state.mode==='unreachable')throw TypeError('Failed to fetch');
      if(state.mode==='server')return {ok:false,status:503,json:async()=>{throw Error('not JSON');}};
      if(state.mode==='unexpected')return {ok:true,status:200,json:async()=>['wrong service']};
      if(state.mode==='verification'&&options.method==='POST')return {ok:false,status:403,json:async()=>({error:'操作验证已失效'})};
      const path=url.slice(5);
      if(state.mode==='engineoffline'&&path==='snapshot')return {ok:false,status:502,json:async()=>({error:'Docker API unavailable'})};
      if(path==='login'){
        if(JSON.parse(options.body).token!=='fixture-token')return {ok:false,status:401,json:async()=>({error:'访问令牌不正确'})};
        state.authenticated=true;
      }
      if(!state.authenticated)return {ok:false,status:401,json:async()=>({error:'请先登录'})};
      if(path==='github/summary')return {ok:true,status:200,json:async()=>({unread:state.githubNotifications.filter(n=>!n.read).length})};
      if(path==='github')return {ok:true,status:200,json:async()=>({projects:state.githubProjects,notifications:state.githubNotifications,unread:state.githubNotifications.filter(n=>!n.read).length,
        auto_enabled:false,interval_seconds:300,running:false,next_before:null,error:null,webhooks:state.githubWebhooks,publish_authorized:false})};
      if(path==='github/action'){
        const body=JSON.parse(options.body);state.lastGithubBody=body;
        if(body.action==='read')state.githubNotifications.filter(n=>body.ids.includes(n.id)).forEach(n=>n.read=true);
        if(body.action==='configure'){
          const row=state.githubProjects.find(p=>p.project===body.project);
          assert.equal(body.revision,row.revision);Object.assign(row,{enabled:body.enabled,branches:body.branches,required_checks:body.required_checks});
        }
        if(body.action==='consume-webhooks'){state.githubWebhooks.pending=0;return {ok:true,status:200,json:async()=>({imported:1,acknowledged:1,github_writes:false})};}
        assert(['sync','read','configure'].includes(body.action));return {ok:true,status:body.action==='sync'?202:200,json:async()=>({saved:true,github_writes:false})};
      }
      if(path==='image-versions')return {ok:true,status:200,json:async()=>({instances:[{instance:'powerx-a',environment:'dev',configured}],candidates:state.versionCandidates,checks:state.versionProofs,catalog_error:null,catalog_notice:'fixture declared catalog'})};
      if(path.startsWith('image-versions/check/')){
        if(state.versionBusy)return {ok:false,status:409,json:async()=>({error:'另一个版本证据检查正在进行'})};
        const parts=path.split('/'),kind=parts[3],role=parts[4];
        const data={id:parts[2],kind,role,result:{at:new Date().toISOString(),...(kind==='evidence'?{state:'verified',detail:'fixture evidence'}:{status:'warn',detail:'私有清单权限待核对'})}};
        if(kind==='evidence')state.versionEvidence=true;
        return {ok:true,status:200,json:async()=>data};
      }
      if(path.startsWith('image-versions/powerx-a')){
        const local={instance:'powerx-a',id:'dev-fixture',at:new Date().toISOString(),images:{backend:{state:'verified'},'web-admin':{state:'verified'}}};
        const data=path.includes('/preview/')?{instance:'powerx-a',at:new Date().toISOString(),candidate:state.versionCandidate,current:state.versionCurrent,local,
          technical_preview:state.versionEvidence?'reviewable':'blocked',blockers:state.versionEvidence?[]:['发布与安装验收证据未核验'],warnings:['升级前必须新建备份并校验'],backup:null,deployment:'not-confirmed',applied:false,config_preview:Object.entries(refs).map(([r,ref])=>(r==='backend'?'POWERX_BACKEND_IMAGE':'POWERX_WEB_IMAGE')+'='+ref).join('\n'),scope:'只读预览，不保存配置'}
          :path.includes('/local/')?local:state.versionCurrent;
        return {ok:true,status:200,json:async()=>data};
      }
      if(path==='backup-plans')return {ok:true,status:200,json:async()=>({plans:state.plans,scheduler_enabled:false,scheduler_running:false,scheduler_error:null})};
      if(path.startsWith('backup-plans/')){
        if(state.planConflict)return {ok:false,status:409,json:async()=>({error:'计划已被其他页面修改，请重新读取后再确认。'})};
        const body=JSON.parse(options.body);state.lastPlanBody=body;
        if(body.action==='save')state.plans=[{...state.plans[0],...body.plan,revision:'a'.repeat(32),next_run_at:new Date(Date.now()+86400000).toISOString()}];
        else state.plans[0].enabled=false;
        return {ok:true,status:200,json:async()=>({plan:state.plans[0],scheduler_enabled:false})};
      }
      if(path==='preflight')return {ok:true,status:200,json:async()=>({items:state.preflightItems})};
      if(path.startsWith('preflight/')){
        if(state.checkBusy)return {ok:false,status:409,json:async()=>({error:'另一个只读检查正在进行'})};
        const key=path.slice(10),row={status:key==='network-ghcr'?'pass':'unknown',detail:key==='network-ghcr'?'HTTP 401，接口可达':'缺少宿主机报告',at:new Date().toISOString()};
        const response={ok:true,status:200,json:async()=>({key,result:row})};
        if(state.deferCheck)return new Promise(resolve=>{state.finishCheck=()=>resolve(response);});
        return response;
      }
      const data={session:{csrf:state.csrf},login:{csrf:state.csrf},snapshot:{at:new Date().toISOString(),revision:'a'.repeat(40),engine:{Version:'fixture'},services:[],sites:[],certificates:[],reports:[]},jobs:{jobs:[]},audit:{events:[]},backups:{instances:[],backups:[],application_centers:[]},instances:{instances:[]},'sites/probe':{id:'fixture-job'}}[path];
      assert(data,path);return {ok:true,status:200,json:async()=>data};
    }
  });
  vm.runInContext(source,context);
  vm.runInContext(fs.readFileSync(require('path').join(__dirname,'../services/admin/static/github.js'),'utf8'),context);
  return {state,context,element,handlers,requests,timers,run:code=>vm.runInContext(code,context)};
}
async function login(f){f.element('token').value='fixture-token';await f.handlers['login-form:submit']({preventDefault(){},submitter:f.element('login-submit')});}
(async()=>{
  const f=fixture();await flush();await flush();
  assert.equal(f.run('loggedIn'),false);assert.equal(f.state.posts,0);
  assert.match(f.element('login-connection').textContent,/管理服务可达/);
  // Network failures do not send the token again, even when a login was attempted explicitly.
  f.state.mode='unreachable';await login(f);assert.match(f.element('login-error').textContent,/不可达/);
  const loginAttempts=f.state.posts;
  f.handlers.poll();await flush();f.handlers['window:online']();await flush();
  assert.equal(f.state.posts,loginAttempts);assert.equal(f.element('token').value,'fixture-token');
  f.state.mode='ok';await f.element('login-recheck').onclick();assert.equal(f.run('loggedIn'),false);
  await login(f);assert.equal(f.run('loggedIn'),true);assert.equal(f.element('token').value,'');
  assert.equal(f.run('connection.state'),'connected');const lastRead=f.run('connection.lastRead');
  // A connection loss leaves the previous successful timestamp and disables mutation controls.
  f.state.mode='unreachable';f.handlers.poll();await flush();await flush();
  assert.equal(f.run('connection.ready'),false);assert.equal(f.run('connection.lastRead'),lastRead);
  assert.match(f.element('last-update').textContent,/上次数据/);
  assert.match(f.run("btn('停止',{action:'stop'})"),/disabled/);
  f.state.mode='ok';const postCount=f.state.posts;f.handlers.poll();await flush();await flush();
  assert.equal(f.run('connection.ready'),true);assert.equal(f.state.posts,postCount);
  // Mutation response loss is an unknown outcome, never an automatic retry.
  f.state.mode='unreachable';await assert.rejects(f.run("submitOperation('sites/probe',{site:'all'})"),/请求结果未确认.*操作记录/);
  const mutationCount=f.state.posts;f.state.mode='ok';await f.element('shell-recheck').onclick();
  assert.equal(f.state.posts,mutationCount);
  // Origin/CSRF errors are distinct; a manual GET updates CSRF without replaying the rejected POST.
  f.state.mode='verification';await assert.rejects(f.run("api('sites/probe',{site:'all'})"),/操作验证已失效/);
  assert.equal(f.run('connection.state'),'verification');f.state.csrf='new-csrf';f.state.mode='ok';
  await f.element('shell-recheck').onclick();assert.equal(f.run('csrf'),'new-csrf');
  await f.run("api('sites/probe',{site:'all'})");assert.equal(f.requests.at(-1).csrf,'new-csrf');
  f.state.mode='server';f.handlers.poll();await flush();await flush();
  assert.equal(f.run('connection.state'),'server');assert.match(f.element('shell-connection').textContent,/HTTP 503/);
  f.state.mode='unexpected';await f.element('shell-recheck').onclick();assert.equal(f.run('connection.state'),'unexpected');
  f.context.navigator.onLine=false;f.handlers['window:offline']();assert.equal(f.run('connection.state'),'offline');
  const before=f.requests.length;await f.element('shell-recheck').onclick();assert.equal(f.requests.length,before);
  f.context.navigator.onLine=true;f.state.mode='timeout';const timed=f.run("api('session')");await flush();
  for(const fn of [...f.timers.values()])fn();await assert.rejects(timed,/超时/);
  f.state.mode='bodytimeout';const slowBody=f.run("api('session')");await flush();
  for(const fn of [...f.timers.values()])fn();await assert.rejects(slowBody,/读取超时/);
  assert.equal(f.run('connection.state'),'unreachable');
  f.state.mode='ok';f.state.authenticated=false;await f.element('shell-recheck').onclick();assert.equal(f.run('loggedIn'),false);
  assert.equal(f.run('connection.state'),'authentication');assert.match(f.element('login-connection').textContent,/管理服务可达/);
  const beforeRecovery=f.state.posts;f.handlers.poll();await flush();assert.equal(f.state.posts,beforeRecovery);
  assert(f.requests.every(r=>!r.url.includes('fixture-token')));
  // Preflight remains usable when the Docker snapshot cannot be read.
  const g=fixture();await flush();g.state.mode='engineoffline';await login(g);
  assert.equal(g.run('loggedIn'),true);assert.equal(g.run('snapshot'),null);assert.equal(g.run('connection.ready'),false);
  const click=dataset=>g.handlers['document:click']({target:{closest(){return {dataset,disabled:false};}}});
  await click({tab:'preflight'});assert.match(g.element('page-title').textContent,/部署前检查/);assert.match(g.element('content').innerHTML,/aria-orientation="vertical"/);
  const moduleRequests=g.requests.length;await click({viewTab:'host',view:'preflight'});assert.equal(g.requests.length,moduleRequests);
  assert.match(g.element('content').innerHTML,/sh scripts\/preflight.sh --save/);
  await click({preflight:'all'});assert.equal(g.run('preflight.items[0].result.status'),'pass');assert.equal(g.run('preflight.items[1].result.status'),'unknown');
  const reads=g.requests.filter(r=>r.url.startsWith('/api/preflight/'));assert.equal(reads.length,2);assert(reads.every(r=>r.method==='GET'));
  await click({viewTab:'network',view:'preflight'});assert(!g.element('content').innerHTML.includes('sh scripts/preflight.sh --save'));
  g.run("preflight.items[0].result.at=new Date(Date.now()-960000).toISOString();render()");
  assert.match(g.element('content').innerHTML,/结果需重查/);
  const posts=g.state.posts;g.handlers.poll();await flush();await flush();
  assert.equal(g.requests.filter(r=>r.url.startsWith('/api/preflight/')).length,2);assert.equal(g.state.posts,posts);
  g.state.checkBusy=true;await click({preflight:'host-compose'});assert.match(g.element('content').innerHTML,/另一个只读检查正在进行/);
  assert.equal(g.run('preflight.items[0].result.status'),'pass');g.state.checkBusy=false;
  g.state.deferCheck=true;const checking=click({preflight:'all'});await flush();
  await click({preflightCancel:'yes'});g.state.finishCheck();await checking;
  assert.match(g.run('preflight.progress'),/已停止后续检查/);assert.equal(g.state.posts,posts);
  assert.equal(g.requests.filter(r=>r.url.startsWith('/api/preflight/')).length,4);
  const h=fixture();await flush();await login(h);
  const planClick=dataset=>h.handlers['document:click']({target:{closest(){return {dataset,disabled:false};}}});
  await planClick({tab:'backup-plans'});assert.match(h.element('content').innerHTML,/调度关闭/);assert.match(h.element('content').innerHTML,/计划禁用/);
  await planClick({planEdit:'powerx-a'});assert.equal(h.element('plan-enabled').checked,false);assert.equal(h.element('plan-prune').checked,false);
  h.element('plan-enabled').checked=true;h.element('plan-confirmation').value='wrong';
  const save=()=>h.handlers['plan-form:submit']({preventDefault(){}});const initialPosts=h.state.posts;
  await save();assert.equal(h.state.posts,initialPosts);assert.match(h.element('plan-error').textContent,/名称不匹配/);
  h.element('plan-confirmation').value='powerx-a';await save();assert.equal(h.state.posts,initialPosts);assert.match(h.element('plan-error').textContent,/维护影响/);
  h.element('plan-maintenance-ack').checked=true;h.element('plan-prune').checked=true;await save();assert.equal(h.state.posts,initialPosts);assert.match(h.element('plan-error').textContent,/删除旧成功备份/);
  h.element('plan-prune-ack').checked=true;await save();assert.equal(h.state.lastPlanBody.maintenance_ack,true);assert.equal(h.state.lastPlanBody.prune_ack,true);
  assert.match(h.element('notice').textContent,/服务器调度开关关闭/);assert.equal(h.element('plan-dialog').open,false);
  const savedPosts=h.state.posts;h.handlers.poll();await flush();await flush();assert.equal(h.state.posts,savedPosts);
  await planClick({planEdit:'powerx-a'});h.element('plan-confirmation').value='powerx-a';h.element('plan-maintenance-ack').checked=true;h.element('plan-prune-ack').checked=true;
  h.state.planConflict=true;await save();assert.match(h.element('plan-error').textContent,/其他页面修改/);assert.equal(h.element('plan-dialog').open,true);h.state.planConflict=false;
  h.element('cancel-plan').onclick();await planClick({planDisable:'powerx-a'});assert.equal(h.run('pending.kind'),'plan-disable');
  h.element('confirmation').value='powerx-a';await h.handlers['confirm-form:submit']({preventDefault(){}});assert.equal(h.state.plans[0].enabled,false);
  assert(h.requests.filter(r=>r.method==='POST').every(r=>r.url==='/api/login'||r.url.startsWith('/api/backup-plans/')));
  const i=fixture();await flush();i.state.mode='engineoffline';await login(i);
  const metadataClick=dataset=>i.handlers['document:click']({target:{closest(){return {dataset,disabled:false};}}});
  await metadataClick({tab:'backup-plans'});assert.equal(i.run('connection.ready'),false);assert.equal(i.run('planWritable()'),true);
  await metadataClick({planEdit:'powerx-a'});i.element('plan-confirmation').value='powerx-a';
  await i.handlers['plan-form:submit']({preventDefault(){}});assert.equal(i.state.lastPlanBody.plan.enabled,false);
  const metadataPosts=i.state.posts;await assert.rejects(i.run("api('sites/probe',{site:'all'})"),/未就绪/);assert.equal(i.state.posts,metadataPosts);
  i.context.navigator.onLine=false;i.handlers['window:offline']();assert.equal(i.run('planWritable()'),false);
  const v=fixture();await flush();v.state.mode='engineoffline';await login(v);
  const versionClick=dataset=>v.handlers['document:click']({target:{closest(){return {dataset,disabled:false};}}});
  await versionClick({tab:'image-versions'});assert.match(v.element('content').innerHTML,/尚无有效候选记录/);assert.equal(v.run('snapshot'),null);
  v.state.versionCandidates=[v.state.versionCandidate];await versionClick({versionRefresh:'yes'});
  assert.match(v.element('content').innerHTML,/开发候选/);await versionClick({versionCurrent:'yes'});assert.match(v.element('content').innerHTML,/成对匹配/);
  await versionClick({versionPreview:'dev-fixture'});assert.match(v.element('content').innerHTML,/升级准备未通过/);assert.match(v.element('content').innerHTML,/发布与安装验收证据未核验/);
  await versionClick({versionCheck:'dev-fixture'});assert.match(v.element('content').innerHTML,/权限待核对/);assert.match(v.element('content').innerHTML,/证据匹配/);
  await versionClick({versionLocal:'dev-fixture'});assert.match(v.element('content').innerHTML,/本机镜像已核验/);
  await versionClick({versionPreview:'dev-fixture'});assert.match(v.element('content').innerHTML,/技术项可供审查，仍需备份及授权/);assert.match(v.element('content').innerHTML,/POWERX_BACKEND_IMAGE=/);
  const versionRequests=v.requests.filter(r=>r.url.startsWith('/api/image-versions'));assert(versionRequests.every(r=>r.method==='GET'));
  const readCount=versionRequests.length;v.handlers.poll();await flush();await flush();assert.equal(v.requests.filter(r=>r.url.startsWith('/api/image-versions')).length,readCount);
  v.state.versionBusy=true;await versionClick({versionCheck:'dev-fixture'});assert.match(v.element('content').innerHTML,/另一个版本证据检查/);
  assert.equal(v.run('imageVersions.preview.applied'),false);
  v.element('version-channel').value='stable';v.handlers['document:change']({target:v.element('version-channel')});assert.match(v.element('content').innerHTML,/尚无有效候选记录/);
  const z=fixture();await flush();z.state.mode='engineoffline';await login(z);
  const githubClick=dataset=>z.handlers['document:click']({target:{closest(){return {dataset,disabled:false};}}});
  await githubClick({tab:'github'});assert.match(z.element('content').innerHTML,/PowerXPluginCRM/);assert.match(z.element('content').innerHTML,/PowerXPluginMediaXStudio/);
  assert.match(z.element('content').innerHTML,/自动采集关闭/);await githubClick({viewTab:'inbox',view:'github'});assert.match(z.element('content').innerHTML,/&lt;script&gt;/);await githubClick({viewTab:'projects',view:'github'});
  assert.equal(z.run('githubWritable()'),true);const githubPosts=z.state.posts;
  z.handlers.poll();await flush();await flush();assert.equal(z.state.posts,githubPosts);
  await githubClick({githubSync:'PowerXPluginCRM'});assert.equal(z.state.lastGithubBody.project,'PowerXPluginCRM');
  await githubClick({githubRead:'visible'});assert.equal(z.run('githubData.unread'),0);
  await githubClick({githubConfigure:'PowerX'});z.element('github-enabled').checked=false;z.element('github-branches').value='develop';z.element('github-required').value='lint\nunit-tests';
  await githubClick({githubSave:'yes'});assert.equal(z.state.lastGithubBody.enabled,false);assert.equal(z.state.githubProjects[0].enabled,false);
  z.state.githubWebhooks={ready:true,pending:1,auto_import_enabled:false,last_received_at:'2026-10-12T00:00:00Z',error:''};await githubClick({githubRefresh:'yes'});
  const viewRequests=z.requests.length;await githubClick({viewTab:'hooks',view:'github'});assert.equal(z.requests.length,viewRequests);
  assert.match(z.element('content').innerHTML,/Webhook 接收队列/);assert.match(z.element('content').innerHTML,/自动导入关闭/);
  const hookBefore=z.state.posts;await githubClick({githubImport:'yes'});assert.equal(z.state.posts,hookBefore+1);assert.equal(z.state.lastGithubBody.action,'consume-webhooks');assert.match(z.element('notice').textContent,/已导入 1 条/);assert.equal(z.run('githubData.webhooks.pending'),0);
  z.handlers.poll();await flush();await flush();assert.equal(z.state.posts,hookBefore+1);
  z.run("githubData.notifications=[{id:93,project:'PowerX',at:'2026-10-12T00:00:00Z',read:false,payload:{source:'signed-webhook',event:'workflow_run',title:'<script>hook</script>',delivery:'<svg>',sha:'b'.repeat(40),attempt:2,binding:'event-sha-needs-refresh'}}]");z.run('render()');await githubClick({viewTab:'inbox',view:'github'});
  assert.match(z.element('content').innerHTML,/已验签 Webhook/);assert.match(z.element('content').innerHTML,/&lt;svg&gt;/);assert.match(z.element('content').innerHTML,/尝试 2/);
  const gitControlPosts=z.state.posts;z.context.navigator.onLine=false;z.handlers['window:offline']();assert.equal(z.run('githubWritable()'),false);
  await assert.rejects(z.run("api('github/action',{action:'sync',project:'all'})"));await assert.rejects(z.run("api('github/action',{action:'consume-webhooks'})"));assert.equal(z.state.posts,gitControlPosts);
  assert(!z.run("githubLink('javascript:alert(1)','unsafe')").includes('href='));
  const tabKeys=['projects','hooks','inbox'];const keyboardTabs=tabKeys.map(viewTab=>({dataset:{view:'github',viewTab}}));
  const keyboardList={querySelectorAll(){return keyboardTabs;}};keyboardTabs.forEach(tab=>tab.closest=()=>keyboardList);
  z.context.navigator.onLine=true;let prevented=false;const viewPosts=z.state.posts;
  z.handlers['document:keydown']({target:{closest(){return keyboardTabs[0];}},key:'ArrowDown',preventDefault(){prevented=true;}});
  assert(prevented);assert.equal(z.run('moduleViews.github'),'hooks');assert.equal(z.state.posts,viewPosts);
  z.context.location.hash='#github/inbox';z.handlers['window:hashchange']();assert.equal(z.run('current'),'github');assert.equal(z.run('moduleViews.github'),'inbox');
  assert.match(z.element('content').innerHTML,/aria-labelledby="view-github-inbox"/);
  console.log('Vertical module tabs passed: independent panels, read-only switching and content isolation.');
  console.log('GitHub observer UI passed: six projects, Docker-independent metadata controls, text-only comments, local configuration, read receipts, GET-only polling and offline refusal.');
  console.log('Admin UI connection checks passed: offline/timeout/unreachable/5xx/401/403, stale data, GET-only recovery, no POST or token replay.');
  console.log('Preflight UI checks passed: missing snapshot, explicit GET-only checks, unknown host report, partial results, busy response, cancellation, no polling retries.');
  console.log('Backup plan UI checks passed: disabled defaults, explicit maintenance/prune acknowledgments, target confirmation, global gate warning, optimistic conflict, disable, GET-only polling.');
  console.log('Image version UI checks passed: missing snapshot/empty catalog, explicit current/local/evidence reads, private uncertainty, blocked/reviewable preview, GET-only actions, no polling, channel filtering.');
})().catch(error=>{console.error(error);process.exitCode=1;});
