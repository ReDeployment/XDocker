/* Standalone acceptance: disposable loopback Flask, independent Chrome profile, no npm. */
'use strict';
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),cp=require('node:child_process'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),sleep=ms=>new Promise(r=>setTimeout(r,ms));
let fixture,chrome,ws,tmp;const pending=new Map(),exceptions=[];let serial=0,session;
const chromeBin=process.env.CHROME_BIN||['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','/usr/bin/google-chrome','/usr/bin/chromium'].find(p=>fs.existsSync(p));
function rpc(method,params={},sid=session){return new Promise((resolve,reject)=>{const id=++serial;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method));},15000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params,...(sid?{sessionId:sid}:{})}));});}
async function evaluate(expression){const r=await rpc('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.text+': '+r.exceptionDetails.exception?.description);return r.result.value;}
async function until(expression){for(let i=0;i<150;i++){if(await evaluate(expression))return;await sleep(100);}throw Error('UI timeout: '+expression);}
async function click(selector){assert(await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e||e.disabled)return false;e.click();return true})()`),'Missing active control: '+selector);}
async function pane(name){await click(`[data-view="service-hub"][data-view-tab="${name}"]`);await until(`document.querySelector('#view-service-hub-panel')?.getAttribute('aria-labelledby')==='view-service-hub-${name}'`);}
async function hubPage(name){await evaluate(`location.hash='service-hub/${name}'`);await until(`current==='service-hub'&&document.querySelector('#view-service-hub-panel')?.getAttribute('aria-labelledby')==='view-service-hub-${name}'`);}
async function sitesPage(name='overview'){await evaluate(`location.hash='sites/${name}'`);await until(`current==='sites'&&document.querySelector('#view-sites-panel')?.getAttribute('aria-labelledby')==='view-sites-${name}'`);}
async function form(values,operation=false){await evaluate(`(()=>{const f=document.querySelector(${operation?JSON.stringify('#hub-operation-form'):JSON.stringify('#hub-form')});for(const [k,v] of Object.entries(${JSON.stringify(values)})){const e=f.elements.namedItem(k);if(e.type==='checkbox')e.checked=v;else e.value=v;}f.requestSubmit(f.querySelector('button[type=submit]'));})()`);}
async function shot(name){if(process.env.HUB_UI_ARTIFACTS){fs.mkdirSync(process.env.HUB_UI_ARTIFACTS,{recursive:true});const r=await rpc('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(process.env.HUB_UI_ARTIFACTS,name+'.png'),Buffer.from(r.data,'base64'));}}
async function stop(child){if(!child||child.exitCode!==null)return;try{process.kill(-child.pid,'SIGINT');}catch{}for(let i=0;i<30&&child.exitCode===null;i++)await sleep(100);if(child.exitCode===null){try{process.kill(-child.pid,'SIGKILL');}catch{}}}
async function main(){
 assert(chromeBin,'Set CHROME_BIN');assert.equal(typeof WebSocket,'function','Node 22+ required');
 fixture=cp.spawn(process.env.PYTHON||'python3',['-B','tests/service_hub_browser_fixture.py'],{cwd:root,detached:true,stdio:['ignore','pipe','pipe'],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
 let lines='',errors='';fixture.stdout.on('data',b=>lines+=b);fixture.stderr.on('data',b=>errors+=b);
 for(let i=0;i<150&&!lines.includes('\n');i++){assert(fixture.exitCode===null,errors);await sleep(100);}assert(lines.includes('\n'),'Fixture failed: '+errors);
 const info=JSON.parse(lines.split('\n')[0]);tmp=fs.mkdtempSync(path.join(os.tmpdir(),'xdocker-resource-chrome-'));fs.chmodSync(tmp,0o700);
 chrome=cp.spawn(chromeBin,['--headless=new','--remote-debugging-port=0','--user-data-dir='+tmp,'--disable-background-networking','--disable-component-update','--no-first-run','--no-default-browser-check','about:blank'],{detached:true,stdio:'ignore'});
 const portFile=path.join(tmp,'DevToolsActivePort');for(let i=0;i<150&&!fs.existsSync(portFile);i++)await sleep(100);assert(fs.existsSync(portFile));
 const [port,wsPath]=fs.readFileSync(portFile,'utf8').trim().split('\n');ws=new WebSocket(`ws://127.0.0.1:${port}${wsPath}`);await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
 ws.onmessage=e=>{const r=JSON.parse(e.data);if(r.id){const p=pending.get(r.id);if(!p)return;clearTimeout(p.timer);pending.delete(r.id);r.error?p.reject(Error(JSON.stringify(r.error))):p.resolve(r.result);}else if(r.method==='Runtime.exceptionThrown')exceptions.push(r.params.exceptionDetails.exception?.description||r.params.exceptionDetails.text);};
 const target=await rpc('Target.createTarget',{url:'about:blank'},null);session=(await rpc('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
 await rpc('Page.enable');await rpc('Runtime.enable');await rpc('Network.enable');await rpc('Network.setBlockedURLs',{urls:['https://*']});
 await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await rpc('Page.navigate',{url:info.url});
 await until(`document.querySelector('#login-form')&&typeof renderServiceHubPage==='function'`);
 await evaluate(`document.querySelector('#token').value=${JSON.stringify(info.fixture_token)};document.querySelector('#login-form').requestSubmit(document.querySelector('#login-form button[type=submit]'))`);
 await until(`serviceHubUI.data&&location.hash==='#service-hub/projects'&&document.querySelector('#view-service-hub-projects[aria-selected=true]')`);
 for(const [tls,port,url] of [[null,80,'http://example.home.test'],['fixture',443,'https://example.home.test'],[null,8088,'http://example.home.test:8088'],['fixture',8443,'https://example.home.test:8443']]){
  assert(await evaluate(`hubEntry({domain:'example.home.test',tls:${JSON.stringify(tls)},enabled:true},{gateway:{http_port:${port},https_port:${port}}},true).includes('href="'+${JSON.stringify(url)}+'"')`),'Incorrect public entry port');
 }
 assert(await evaluate(`(()=>{const b=document.querySelectorAll('.hub-workspace [role=tab]');return b[1].getBoundingClientRect().top>b[0].getBoundingClientRect().bottom})()`));await shot('desktop-projects-empty');
 await pane('services');assert(await evaluate("document.querySelector('#view-service-hub-panel').textContent.includes('尚未登记项目。')"));
 await pane('projects');await click('[data-hub-edit="new"]');await evaluate(`document.querySelector('[name=id]').value='browser-project';document.querySelector('[name=name]').value='浏览器项目'`);await click('[data-hub-close]');
 await sitesPage();await hubPage('projects');await click('[data-hub-edit="new"]');assert.equal(await evaluate(`document.querySelector('[name=id]').value`),'browser-project');
 await form({id:'browser-project',name:'浏览器项目',endpoint:'example',domain:'browser.home.test',service:'example',monitor:'example-resources',enabled:true});
 await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.projects.length===1`);
 assert(await evaluate("document.querySelector('#view-service-hub-panel tbody tr').textContent.includes('尚未检查')"));
 await click('[data-hub-health="browser-project"]');await until(`serviceHubUI.data.projects[0].health?.state==='healthy'`);
 await click('[data-hub-edit="browser-project"]');await evaluate(`document.querySelector('[name=name]').value='保留中的草稿'`);await click('[data-hub-close]');await evaluate('loadServiceHub()');
 await sitesPage();
 assert(await evaluate(`document.querySelectorAll('[data-site-source]').length===2&&document.querySelector('.sites-workspace').textContent.includes('legacy.example.test')`));
 assert(await evaluate(`!document.querySelector('[data-view="service-hub"][data-view-tab="gateway"]')`));
 await click('[data-hub-entry="browser-project"]');await evaluate(`document.querySelector('[name=domain]').value='draft.home.test'`);await click('[data-hub-close]');await sitesPage('gateway');await sitesPage();await click('[data-hub-entry="browser-project"]');assert.equal(await evaluate(`document.querySelector('[name=domain]').value`),'draft.home.test');
 await form({domain:'browser.home.test'});await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.projects[0].revision===2`);
 await evaluate(`location.hash='service-hub/gateway'`);await until(`location.hash==='#sites/gateway'&&document.querySelector('#view-sites-panel')`);
 await click('[data-hub-preview]');await until(`document.querySelector('#detail-dialog').open`);assert(await evaluate(`document.querySelector('#detail-content').textContent.includes('proxy_pass http://127.0.0.1:3000;')`));await click('#close-detail');
 await click('[data-hub-gateway]');await until(`document.querySelector('#hub-operation-form')`);await form({confirmation:'xdocker-gateway'},true);await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.gateway.runtime_verified`);
 await hubPage('services');await click('[data-hub-logs="browser-project"]');await until(`document.querySelector('#detail-dialog').open`);assert(await evaluate(`!document.querySelector('#detail-content').textContent.includes('fixture-secret')`));await click('#close-detail');
 await click('[data-hub-action="browser-project"]');await until(`document.querySelector('#hub-dialog').open&&serviceHubUI.editor?.kind==='action'`);await form({confirmation:'example.service',action:'restart'},true);await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.operations.length===1`);
 await pane('records');assert(await evaluate(`document.querySelector('#view-service-hub-panel').textContent.includes('状态已核对')`));await shot('desktop-records');
 await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 for(const name of ['projects','services','records']){
  await pane(name);assert(await evaluate(`document.documentElement.scrollWidth<=innerWidth+2`),'Narrow overflow '+name);
  assert.equal(await evaluate(`document.querySelectorAll('.hub-workspace [role=tabpanel]').length`),1);
  assert(await evaluate(`(()=>{const n=document.querySelector('.hub-workspace [role=tablist]').getBoundingClientRect(),a=document.querySelector('.hub-workspace [aria-selected=true]').getBoundingClientRect();return a.left>=n.left-1&&a.right<=n.right+1})()`),'Active task outside narrow nav '+name);
 }
 await pane('projects');await click('[data-hub-edit="browser-project"]');assert.equal(await evaluate(`document.querySelector('[name=name]').value`),'保留中的草稿');assert(await evaluate(`document.querySelector('#hub-form button[type=submit]').getBoundingClientRect().bottom<=innerHeight`),'Primary save action is outside narrow viewport');await shot('mobile-editor');await click('[data-hub-close]');
 await pane('services');await shot('mobile-services');
 await evaluate(`serviceHubUI.error='fixture-visible-error';serviceHubUI.data.operations[0].state='unconfirmed';renderServiceHubPage()`);
 for(const name of ['projects','services','records']){await pane(name);assert(await evaluate(`document.querySelector('.hub-workspace').textContent.includes('fixture-visible-error')&&document.querySelector('.hub-workspace').textContent.includes('结果未确认')`));}
 await hubPage('projects');await click('[data-hub-edit="new"]');await form({id:'padelu',name:'PadelU',directory:info.padelu_directory,endpoint:'padelu',domain:'padelu-admin.home.test',service:'',monitor:'',enabled:true});await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.projects.length===2`);
 await sitesPage(); await click('[data-hub-health="padelu"]');await until(`document.querySelector('.sites-workspace [role=alert]')?.textContent.includes('HTTP 403')`);
 for(const view of ['overview','gateway']){
  await sitesPage(view);assert(await evaluate(`document.querySelector('.sites-workspace').textContent.includes('HTTP 403')`));
  assert(await evaluate(`document.documentElement.scrollWidth<=innerWidth+2`));assert.equal(await evaluate(`document.querySelectorAll('.sites-workspace [role=tabpanel]').length`),1);
 }
 await sitesPage();await evaluate(`document.querySelector('#sites-source').value='deployment';document.querySelector('#sites-source').dispatchEvent(new Event('change',{bubbles:true}))`);
 assert.equal(await evaluate(`document.querySelectorAll('[data-site-source]').length`),1);await sitesPage('gateway');await sitesPage();assert.equal(await evaluate(`document.querySelector('#sites-source').value`),'deployment');await shot('mobile-unified-sites');
 await evaluate(`document.querySelector('#sites-source').value='all';document.querySelector('#sites-source').dispatchEvent(new Event('change',{bubbles:true}))`);
 await rpc('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await shot('desktop-unified-sites');
 for(const width of [1440,390]){
  await rpc('Emulation.setDeviceMetricsOverride',{width,height:width===390?844:1000,deviceScaleFactor:1,mobile:width===390});await sitesPage('gateway');
  for(const [state,error,label] of [['waiting-address','XD_GATEWAY_ADDRESS_UNAVAILABLE','等待监听地址'],['retrying','XD_GATEWAY_PORT_IN_USE','自动重试'],['blocked','XD_GATEWAY_PENDING_CONFIGURATION','不会发布这些修改'],['stale',null,'检查未确认']]){
   await evaluate(`serviceHubUI.data.gateway.recovery={state:${JSON.stringify(state)},error:${JSON.stringify(error)},at:Date.now()/1000,next_check_at:Date.now()/1000+30,failures:1};renderSitesPage()`);
   assert(await evaluate(`document.querySelector('.sites-workspace').textContent.includes(${JSON.stringify(label)})`));
   await sitesPage('overview');assert(await evaluate(`document.querySelector('.sites-workspace').textContent.includes(${JSON.stringify(label)})`));await sitesPage('gateway');
   assert(await evaluate(`document.documentElement.scrollWidth<=innerWidth+2`));
  }
  await shot(width===390?'mobile-gateway-recovery':'desktop-gateway-recovery');
  await evaluate(`serviceHubUI.data.gateway.recovery=null;renderSitesPage()`);
 }
 for(const width of [1440,390]){
  await rpc('Emulation.setDeviceMetricsOverride',{width,height:width===390?844:1000,deviceScaleFactor:1,mobile:width===390});
  // Old bookmarks, direct entry and a second click in the sidebar all land on the list.
  await evaluate("location.hash='service-hub/overview'");
  await until("location.hash==='#service-hub/projects'&&document.querySelector('#view-service-hub-projects[aria-selected=true]')");
  await pane('records');
  await evaluate("window.scrollTo(0,0)");
  await click('[data-tab="service-hub"]');
  await until("location.hash==='#service-hub/projects'&&document.querySelector('#view-service-hub-projects[aria-selected=true]')");
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.hub-workspace [role=tab]')].map(b=>b.textContent)"),['项目登记','系统服务','操作记录']);
  assert(await evaluate("!document.querySelector('.hub-workspace .kpi,.hub-workspace .hub-status,.hub-workspace .intro')"));
  assert(await evaluate(`(()=>{
    const panel=document.querySelector('#view-service-hub-panel'),first=panel.querySelector('tbody tr');
    const button=first.querySelector('[data-hub-health="padelu"]');
    if(!button)return false;
    return [panel.querySelector('[data-hub-edit="new"]'),first.querySelector('strong'),button].every(e=>{
      const r=e.getBoundingClientRect();return r.width>0&&r.left>=0&&r.right<=innerWidth+1&&r.top>=0&&r.bottom<=innerHeight+1;
    });
  })()`),'Project, registration and connection check must be visible at '+width);
  await shot(width===390?'mobile-project-list':'desktop-project-list');
  assert.equal(await evaluate("document.querySelectorAll('#view-service-hub-panel tbody tr').length"),2);
  assert(await evaluate(`(()=>{
    const project=serviceHubUI.data.projects.find(p=>p.id==='padelu');
    const row=document.querySelector('[data-hub-health="padelu"]').closest('tr').textContent;
    return !project.health && row.includes('上次异常') && row.includes('HTTP 403') && row.includes(date(project.last_health.at*1000));
  })()`));
  await pane('services');
  assert.equal(await evaluate("document.querySelector('#view-service-hub-services').textContent.trim()"),'系统服务');
  await until("document.querySelector('[data-service-project=padelu]')?.textContent.includes('已发现 · 待授权')");
  assert.equal(await evaluate("document.querySelectorAll('[data-service-project]').length"),2);
  assert(await evaluate(`(()=>{
    const panel=document.querySelector('#view-service-hub-panel');
    return panel.textContent.includes('example.service') && panel.textContent.includes('可重启') &&
      panel.textContent.includes('PadelU') && !panel.textContent.includes('unrelated.service') && !!panel.querySelector('[data-hub-action="browser-project"]');
  })()`));
  const showFilter=value=>evaluate(`(()=>{const f=document.querySelector('[data-project-service-filter]');f.value=${JSON.stringify(value)};f.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await showFilter('discovered');
  assert.equal(await evaluate("document.querySelectorAll('[data-service-project]').length"),1);
  assert.equal(await evaluate("document.querySelector('[data-hub-grant=padelu]').textContent"),'授权');
  assert(!await evaluate("!!document.querySelector('[data-service-project=padelu] [data-hub-logs]')"));
  await click('[data-hub-batch-grant]');
  assert(await evaluate("document.querySelector('#hub-bulk-grant-form [type=submit]').disabled"));
  await click('[data-grant-group]');
  assert.equal(await evaluate("document.querySelector('#hub-bulk-grant-form [type=submit]').textContent"),'确认授权 3 项');
  await click('#hub-bulk-grant-form [name=grant]');
  assert(await evaluate("document.querySelector('[data-grant-group]').indeterminate&&document.querySelector('[data-grant-all]').indeterminate"));
  assert(await evaluate("document.querySelector('#hub-bulk-grant-form [type=submit]').getBoundingClientRect().bottom<=innerHeight && document.documentElement.scrollWidth<=innerWidth+2"));
  await shot(width===390?'mobile-batch-grants':'desktop-batch-grants');
  await click('#hub-bulk-grant-form [data-hub-close]');
  assert.equal(await evaluate("document.querySelector('[data-hub-batch-grant]').textContent"),'批量授权（3）');
  await click('[data-hub-grant="padelu"][data-hub-unit="padelu-web.service"]');
  assert(await evaluate(`(()=>{const form=document.querySelector('#hub-read-grant-form');const box=form.querySelector('[type=submit]').getBoundingClientRect();
    return form.querySelector('[type=submit]').textContent==='确认授权'&&form.textContent.includes('最近 100 行日志')&&
           form.textContent.includes('PadelU')&&form.textContent.includes('padelu-web.service')&&form.textContent.includes('127.0.0.1:15173')&&
           box.right<=innerWidth&&box.bottom<=innerHeight;})()`));
  await shot(width===390?'mobile-read-grant':'desktop-read-grant');
  await click('#hub-read-grant-form [data-hub-close]');
  assert(await evaluate("!!document.querySelector('[data-hub-grant=padelu]')"));
  await pane('records');await pane('services');
  assert.equal(await evaluate("document.querySelector('[data-project-service-filter]').value"),'discovered');
  await showFilter('review');
  assert(await evaluate("document.querySelector('.project-service-list').textContent.includes('browser-project-worker.service')&&!document.querySelector('.project-service-list').textContent.includes('browser-project-backend.service')"));
  await showFilter('empty');
  assert(await evaluate("document.querySelector('#view-service-hub-panel').textContent.includes('当前筛选没有项目')"));
  await showFilter('all');
  await shot(width===390?'mobile-project-services':'desktop-project-services');
  await click('[data-hub-bind-alias="browser-project-backend"]');
  await until("document.querySelectorAll('[data-bind-row]').length===1");
  await click('#hub-dialog [data-hub-close]');
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth+2"));
  await evaluate("location.hash='overview'");
  await until("document.querySelector('[data-overview-check]:enabled')&&document.querySelector('.kpi strong')?.textContent==='1'");
  assert(await evaluate("document.querySelector('#content').textContent.includes('demo / gateway')&&!document.querySelector('[data-probe=all]')"));
  await click('[data-overview-check]');
  await until("document.querySelector('[data-overview-progress]')?.textContent.includes('本轮完成 2/2 · 正常 1 · 异常 1')");
  assert(await evaluate("document.querySelector('#content').textContent.includes('HTTP 403')"));
  assert(await evaluate("document.documentElement.scrollWidth<=innerWidth+2"));
  await shot(width===390?'mobile-native-overview':'desktop-native-overview');
  await click('a[href="#services/logs"]');
  await until("document.querySelector('.container-log-row')?.textContent.includes('demo / gateway')");
  await evaluate("location.hash='services'");
  await until("document.querySelector('#view-services-operations')?.getAttribute('aria-selected')==='true'");
  await click('[data-view="services"][data-view-tab="logs"]');
  await until("document.querySelector('.container-log-row')?.textContent.includes('demo / gateway')");
  assert.equal(await evaluate("document.documentElement.scrollWidth<=innerWidth+2"),true);
  await click('.container-log-row [data-container-log]');
  await until("document.querySelector('#detail-content pre')?.textContent.includes('[REDACTED]')");
  assert(await evaluate("document.querySelector('#detail-content').textContent.includes('未设置大小上限')"));
  assert(!await evaluate("document.querySelector('#detail-content').textContent.includes('private-fixture')"));
  // Let Chrome deliver the previous dialog's queued close event during the next log read.
  await evaluate(`(() => {
    const dialog=document.querySelector('#detail-dialog');
    const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
    const fetchNow=window.fetch;
    window.fetch=async (...args)=>{const response=await fetchNow(...args);await closed;return response;};
    try{dialog.close();document.querySelector('.container-log-row [data-container-log]').click();}
    finally{window.fetch=fetchNow;}
  })()`);
  await until("document.querySelector('#detail-content pre')?.textContent.includes('[REDACTED]')");
  await click('#detail-content [data-container-log]');
  await until("document.querySelector('#detail-content pre')?.textContent.includes('fixture log')");
  await click('#close-detail');
  assert(await evaluate("document.querySelector('#view-services-logs').getAttribute('aria-selected')==='true'"));
  await shot(width===390?'mobile-container-logs':'desktop-container-logs');
 }
 await hubPage('services');
 for(const width of [1440,390]){
   await rpc('Emulation.setDeviceMetricsOverride',{width,height:width===390?844:1000,deviceScaleFactor:1,mobile:width===390});
   await click('[data-hub-batch]');await until("document.querySelector('#hub-batch-form')");
   const ready='[data-bind-row="browser-project-backend"]',pending='[data-bind-row="browser-project-worker"]';
   assert(await evaluate("document.querySelector('#hub-batch-form').textContent.includes('127.0.0.1:3000')"));
   assert.equal(await evaluate("document.querySelector('#hub-batch-form [type=submit]').textContent"),'确认绑定 1 项');
   assert(await evaluate("!document.querySelector('#hub-batch-form select')"));
   await click(ready+' input');
   assert(await evaluate("document.querySelector('#hub-batch-form [type=submit]').disabled"));
   await click(ready+' input');
   await click(pending+' [data-bind-edit]');
   assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-bind-pick]')].map(b=>b.dataset.bindPick)"),['browser-project','padelu']);
   assert(!await evaluate("document.querySelector('[data-bind-search]')!==null"));
   await click('[data-bind-pick="padelu"]');
   assert.equal(await evaluate("document.querySelector('#hub-batch-form [type=submit]').textContent"),'确认绑定 2 项');
   // Re-open only the manually chosen row. Keep the other selection, and restore its suggested candidate.
   await click(pending+' [data-bind-edit]');
   assert(await evaluate("document.querySelector('[data-bind-pick=padelu]').matches(':first-child[aria-current=true]')"));
   await click('[data-bind-pick="browser-project"]');
   assert(await evaluate("document.documentElement.scrollWidth<=innerWidth+2"));
   assert(await evaluate("document.querySelector('#hub-batch-form [type=submit]').getBoundingClientRect().bottom<=innerHeight"));
   await shot(width===390?'mobile-batch-bindings':'desktop-batch-bindings');
   if(width===1440){await click('#hub-dialog [data-hub-close]');continue;}
   await click('#hub-batch-form [type=submit]');
   await until("!document.querySelector('#hub-dialog').open&&document.querySelectorAll('[data-service-project] [data-hub-status]').length===3");
 }
 await click('[data-hub-status][data-hub-alias="browser-project-backend"]');
 await until("document.querySelector('#detail-dialog').open&&document.querySelector('#detail-title').textContent==='browser-project-backend.service'");
 await click('#close-detail');
 assert(await evaluate("!document.querySelector('[data-hub-action][data-hub-alias=browser-project-backend]')&&document.querySelector('[data-hub-batch]').disabled"));
 await click('[data-hub-grant="padelu"][data-hub-unit="padelu-web.service"]');
 await click('#hub-read-grant-form [type=submit]');
 await until("!serviceHubUI.busy&&document.querySelector('[data-project-service-filter]')?.value==='pending'&&document.querySelector('[data-service-project=padelu] [data-hub-bind-alias]')");
 assert(await evaluate("!serviceHubUI.data.projects.find(p=>p.id==='padelu').bound_services.length"));
 await click('[data-service-project=padelu] [data-hub-bind-alias]');
 await until("document.querySelector('#hub-batch-form [type=submit]:enabled')");
 await click('#hub-batch-form [type=submit]');
 await until("!serviceHubUI.busy&&document.querySelector('[data-hub-status=padelu]')&&document.querySelector('[data-project-service-filter]').value==='bound'");
 assert(!await evaluate("!!document.querySelector('[data-hub-action=padelu]')"));
 assert.equal(await evaluate("document.querySelector('[data-hub-status=padelu]').textContent"),'查看状态');
 assert.equal(await evaluate("document.querySelector('[data-hub-logs=padelu]').textContent"),'查看日志');
 await click('[data-hub-status=padelu]');
 await until("document.querySelector('#detail-dialog').open&&document.querySelector('#detail-title').textContent==='padelu-web.service'");
 await click('#close-detail');
 await click('[data-hub-logs=padelu]');
 await until("document.querySelector('#detail-dialog').open&&document.querySelector('#detail-content pre')?.textContent.includes('[REDACTED]')");
 await click('#close-detail');
 await evaluate("serviceHubUI.serviceFilter='all';renderServiceHubPage()");
 await click('[data-hub-batch-grant]');
 await click('[data-grant-all]');
 assert.equal(await evaluate("document.querySelector('#hub-bulk-grant-form [type=submit]').textContent"),'确认授权 2 项');
 await click('#hub-bulk-grant-form [type=submit]');
 await until("!serviceHubUI.busy&&!document.querySelector('#hub-dialog').open&&document.querySelector('[data-project-service-filter]').value==='pending'");
 assert(await evaluate("document.querySelector('[data-hub-batch-grant]').disabled&&document.querySelector('[data-hub-batch]').textContent==='批量绑定（2）'"));
 assert.equal(await evaluate("serviceHubUI.data.projects.find(p=>p.id==='padelu').bound_services.length"),1);
 await evaluate(`(async()=>{
   for(let i=0;i<6;i++){
     const id='extra-'+i;
     await api('service-hub/projects',{id,name:'Extra '+i,endpoint:'example',domain:id+'.home.test',
       directory:null,service:null,tls:null,monitor:null,delivery:'external',enabled:false,revision:1});
   }
   await loadServiceHub();
 })()`);
 for(const width of [1440,390]){
  await rpc('Emulation.setDeviceMetricsOverride',{width,height:width===390?844:1000,deviceScaleFactor:1,mobile:width===390});
  await click('[data-hub-batch]');
  await until("document.querySelectorAll('#hub-batch-form [data-bind-row]').length===2");
  assert.equal(await evaluate("document.querySelector('#hub-batch-form [type=submit]').textContent"),'确认绑定 2 项');
  assert(await evaluate(`[...document.querySelectorAll('#hub-batch-form [data-bind-row]')].every(row=>
    row.querySelector('input:checked')&&row.textContent.includes('PadelU')&&row.textContent.includes(${JSON.stringify(info.padelu_directory+'/jobs')}))`));
  assert(await evaluate("document.querySelector('#hub-batch-form [type=submit]').getBoundingClientRect().bottom<=innerHeight"));
  const lastRow='.hub-binding-row:last-child';
  await click(lastRow+' [data-bind-edit]');
  assert(await evaluate(`(()=>{
    const picker=document.querySelector('[data-bind-picker]'),first=picker.querySelector('[data-bind-pick]');
    const choice=first.getBoundingClientRect(),body=picker.closest('.hub-form-body').getBoundingClientRect();
    return picker.querySelectorAll('[data-bind-pick]').length===8
      &&first.dataset.bindPick==='padelu'&&first.getAttribute('aria-current')==='true'&&first.textContent.includes('推荐')
      &&document.activeElement===first&&choice.top>=body.top-1&&choice.bottom<=body.bottom+1;
  })()`),'Project choices must be visible when expanded at '+width);
  await shot(width===390?'mobile-project-picker':'desktop-project-picker');
  await evaluate(`(()=>{
    const search=document.querySelector('[data-bind-search]');
    search.value='extra-5.home.test';
    search.dispatchEvent(new Event('input',{bubbles:true}));
  })()`);
  assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-bind-pick]')].map(b=>b.dataset.bindPick)"),['extra-5']);
  await click('[data-bind-pick="extra-5"]');
  assert(await evaluate(`document.querySelector(${JSON.stringify(lastRow)}).textContent.includes('归属项目　Extra 5')`));
  assert.equal(await evaluate("document.querySelector('#hub-batch-form [type=submit]').textContent"),'确认绑定 2 项');
  await click('#hub-batch-form [data-hub-close]');
 }
 await evaluate("serviceHubUI.serviceFilter='all';renderServiceHubPage()");
 await evaluate(`(async()=>{
   for(const [id,endpoint] of [['review-web','padelu'],['review-api','example']]){
     await api('service-hub/projects',{id,name:id,endpoint,domain:id+'.home.test',directory:${JSON.stringify(info.review_directory)},
       service:null,tls:null,monitor:null,delivery:'external',enabled:false,revision:1});
   }
   serviceHubUI.serviceFilter='review';
   await loadServiceHub();
 })()`);
 for(const width of [1440,390]){
  await rpc('Emulation.setDeviceMetricsOverride',{width,height:width===390?844:1000,deviceScaleFactor:1,mobile:width===390});
  await click('[data-service-project="review-web"] [data-hub-review-grant="review-worker.service"]');
  assert(await evaluate(`(()=>{
   const form=document.querySelector('#hub-review-grant-form'),confirm=form.querySelector('[type=submit]');
   return confirm.disabled&&form.querySelectorAll('[name=project]').length===2&&
     !form.querySelector('[name=project]:checked')&&confirm.getBoundingClientRect().bottom<=innerHeight+1;
  })()`));
  await click('#hub-review-grant-form [value=review-api]');
  assert(await evaluate("!document.querySelector('#hub-review-grant-form [type=submit]').disabled"));
  await shot(width===390?'mobile-review-grant':'desktop-review-grant');
  if(width===1440){
   await click('#hub-review-grant-form [data-hub-close]');
   assert(await evaluate("!!document.querySelector('[data-hub-review-grant=\\\"review-worker.service\\\"]')"));
  }else{
   await click('#hub-review-grant-form [type=submit]');
   await until(`!serviceHubUI.busy&&!document.querySelector('#hub-dialog').open&&
     serviceHubUI.data.projects.find(p=>p.id==='review-api').bound_services.some(a=>serviceHubUI.data.services[a].unit==='review-worker.service')`);
  }
 }
 assert(await evaluate(`(()=>{
   const alias=serviceHubUI.data.projects.find(p=>p.id==='review-api').bound_services[0];
   return serviceHubUI.data.services[alias].actions.join(',')==='status,logs';
 })()`));
 await evaluate("serviceHubUI.serviceFilter='all';renderServiceHubPage()");
 await hubPage('projects');
 const waitForMergeCandidates=count=>until(`(async()=>{
   await loadServiceHub();
   return serviceHubUI.data.project_groups.find(p=>p.id==='review-web').merge_candidates.length===${count};
 })()`);
 await evaluate(`(async()=>{
   await api('service-hub/projects',{id:'review-alt',name:'Review Alt',endpoint:'example',domain:'review-alt.home.test',directory:${JSON.stringify(info.review_directory)},
     service:null,tls:null,monitor:null,delivery:'external',enabled:false,revision:1});
 })()`);
 await waitForMergeCandidates(2);
 await click('[data-hub-merge-project="review-web"]');
 assert(await evaluate(`(()=>{
   const form=document.querySelector('#hub-merge-project-form');
   return form.textContent.includes('选择要归并的旧登记')&&form.querySelectorAll('[name=source]').length===2&&
     !form.querySelector('[name=source]:checked')&&form.querySelector('[type=submit]').disabled;
 })()`),'Multiple merge candidates require choosing a source');
 await click('#hub-merge-project-form [value=review-alt]');
 await until("document.querySelector('[data-merge-preview]').textContent.includes('review-alt.home.test')");
 await click('#hub-merge-project-form [value=review-api]');
 await until("document.querySelector('[data-merge-preview]').textContent.includes('review-api.home.test')");
 assert(!await evaluate("document.querySelector('[data-merge-preview]').textContent.includes('review-alt.home.test')"));
 await click('#hub-merge-project-form [data-hub-close]');
 await evaluate(`(async()=>{
   await api('service-hub/projects/review-alt/remove',{revision:1,confirmation:'Review Alt'});
 })()`);
 await waitForMergeCandidates(1);
 for(const width of [1440,390]){
  await rpc('Emulation.setDeviceMetricsOverride',{width,height:width===390?844:1000,deviceScaleFactor:1,mobile:width===390});
  await click('[data-hub-merge-project="review-web"]');
  await until("document.querySelector('#hub-merge-project-form [type=submit]:enabled')");
  assert(await evaluate(`(()=>{
   const screen=document.querySelector('#hub-merge-project-form'),preview=screen.querySelector('[data-merge-preview]');
   return !screen.querySelector('[name=source]')&&
     screen.querySelector('[data-merge-summary]').textContent==='将 review-api 的网站入口归到 review-web。'&&
     preview.textContent.includes('review-api.home.test')&&preview.textContent.includes('review-web.home.test')&&
     preview.textContent.includes('review-worker.service')&&screen.querySelector('[type=submit]').getBoundingClientRect().bottom<=innerHeight+1;
  })()`),'Single-candidate merge shows direction and both routes at '+width);
  await shot(width===390?'mobile-project-merge':'desktop-project-merge');
  if(width===1440){await click('#hub-merge-project-form [data-hub-close]');continue;}
  await click('#hub-merge-project-form [type=submit]');
  await until("serviceHubUI.data.entry_owners['review-api']==='review-web'&&!serviceHubUI.busy");
 }
 assert(await evaluate("!document.querySelector('[data-hub-edit=review-api]')"));
 await click('[data-hub-site-project="review-web"]');
 await until("document.querySelector('#sites-project')?.value==='review-web'");
 assert.equal(await evaluate("document.querySelectorAll('[data-site-source=project]').length"),2);
 assert(await evaluate("document.querySelector('[data-site-id=review-api]').textContent.includes('review-web')"));
 await click('[data-hub-entry="new"]');
 await form({project:'review-web',endpoint:'padelu',domain:'review-extra.home.test',enabled:false});
 await until("document.querySelectorAll('[data-site-source=project]').length===3&&!serviceHubUI.busy");
 await hubPage('services');
 assert(await evaluate("!document.querySelector('[data-service-project=review-api]')&&document.querySelector('[data-service-project=review-web]').textContent.includes('review-worker.service')"));
 const mergedAlias=await evaluate("serviceHubUI.data.project_groups.find(p=>p.id==='review-web').bound_services[0]");
 await click('[data-hub-status="review-web"][data-hub-alias="'+mergedAlias+'"]');
 await until("document.querySelector('#detail-dialog').open&&document.querySelector('#detail-title').textContent==='review-worker.service'");
 await click('#close-detail');
 await hubPage('projects');
 await click('[data-hub-merge-project="review-web"][data-hub-merge-manual]');
 assert(await evaluate("document.querySelector('#hub-merge-project-form h2').textContent==='手动归并'"));
 assert(await evaluate("!document.querySelector('#hub-merge-project-form [name=source]:checked')"));
 await evaluate(`(()=>{
   const search=document.querySelector('[data-merge-search]');
   search.value='PadelU';search.dispatchEvent(new Event('input',{bubbles:true}));
 })()`);
 assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-merge-option]:not([hidden]) input')].map(o=>o.value)"),['padelu']);
 await click('#hub-merge-project-form [value=padelu]');
 await until("document.querySelector('#hub-merge-project-form [type=submit]:enabled')");
 assert(await evaluate(`(()=>{
   const preview=document.querySelector('[data-merge-preview]');
   return preview.querySelectorAll('article').length===4&&preview.textContent.includes(${JSON.stringify(info.padelu_directory)})&&
     preview.textContent.includes(${JSON.stringify(info.review_directory)});
 })()`),'Manual merge preview keeps directories for every route');
 await shot('mobile-manual-project-merge');
 await click('#hub-merge-project-form [type=submit]');
 await until("!serviceHubUI.busy&&serviceHubUI.data.entry_owners.padelu==='review-web'");
 await hubPage('services');
 await until("document.querySelector('[data-service-project=review-web]').textContent.includes('tasks.service')");
 assert(await evaluate("!document.querySelector('[data-service-project=padelu]')&&document.querySelector('[data-service-project=review-web]').textContent.includes('padelu-web.service')"));
 // Exercise restricted grants without entering any operation on the host.
 const permissions=await evaluate(`(()=>{
   const root=document.createElement('section');
   const project={id:'sample',name:'示例',service:'bound',endpoint_available:true};
   const data={projects:[project,{id:'revoked',service:'removed'}],services:{bound:{unit:'bound.service',scope:'user',actions:['status','logs']}}};
   const bindings=boundSystemServices(data);
   data.operations_enabled=true;root.innerHTML=projectServicePanel(data,'');
   const readOnly=root.textContent.includes('仅查看')&&!!root.querySelector('[data-hub-status]')&&!!root.querySelector('[data-hub-logs]')&&!root.querySelector('[data-hub-action]');
   data.services.bound.actions=['status','start','stop'];
   data.operations_enabled=false;root.innerHTML=projectServicePanel(data,'');
   const disabled=root.textContent.includes('运行操作未开启')&&!root.querySelector('[data-hub-action]')&&!root.querySelector('[data-hub-logs]');
   project.endpoint_available=false;
   data.operations_enabled=true;root.innerHTML=projectServicePanel(data,'');
   return {rows:bindings.length,readOnly,disabled,unavailable:root.textContent.includes('不可操作')&&!root.querySelector('button')};
 })()`);
 assert.deepEqual(permissions,{rows:1,readOnly:true,disabled:true,unavailable:true});
 assert.deepEqual(exceptions,[]);console.log(JSON.stringify({passed:true,desktop:'1440x1000',mobile:'390x844',hubPanes:3,sitePanes:2,unifiedSources:true,legacyGatewayRedirect:true,domain403Visible:true,gatewayRecoveryVisible:true,drafts:true,registerWithoutStart:true,health:true,gatewayPreviewAndApply:'isolated-adapter',serviceRestart:'one-fixed-grant',logRedaction:true,readGrantThenBind:true,batchReadGrants:true,reviewGrantAndBind:true,mergedProjectEntries:true,manualProjectMerge:true,dockerRequired:false,consoleExceptions:0}));
}
main().catch(async e=>{console.error(e.stack);if(ws&&session){try{console.error(JSON.stringify({exceptions,ui:await evaluate(`({login:document.querySelector('#login-error')?.textContent,content:document.querySelector('#content')?.innerText?.slice(0,1500),error:typeof serviceHubUI==='object'?serviceHubUI.error:'not loaded',formError:document.querySelector('#hub-form-error')?.textContent})`)}));await shot('failure');}catch{}}process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stop(chrome);await stop(fixture);if(fixture&&fixture.exitCode!==0){console.error('Fixture read-only assertion failed');process.exitCode=1;}if(tmp)fs.rmSync(tmp,{recursive:true,force:true});});
