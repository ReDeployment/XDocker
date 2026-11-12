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
 await until(`serviceHubUI.data&&document.querySelector('.hub-kpis')`);
 for(const [tls,port,url] of [[null,80,'http://example.home.test'],['fixture',443,'https://example.home.test'],[null,8088,'http://example.home.test:8088'],['fixture',8443,'https://example.home.test:8443']]){
  assert(await evaluate(`hubEntry({domain:'example.home.test',tls:${JSON.stringify(tls)},enabled:true},{gateway:{http_port:${port},https_port:${port}}},true).includes('href="'+${JSON.stringify(url)}+'"')`),'Incorrect public entry port');
 }
 assert(await evaluate(`(()=>{const b=document.querySelectorAll('.hub-workspace [role=tab]');return b[1].getBoundingClientRect().top>b[0].getBoundingClientRect().bottom})()`));await shot('desktop-overview');
 await pane('projects');await click('[data-hub-edit="new"]');await evaluate(`document.querySelector('[name=id]').value='browser-project';document.querySelector('[name=name]').value='浏览器项目'`);await click('[data-hub-close]');
 await pane('gateway');await pane('projects');await click('[data-hub-edit="new"]');assert.equal(await evaluate(`document.querySelector('[name=id]').value`),'browser-project');
 await form({id:'browser-project',name:'浏览器项目',endpoint:'example',domain:'browser.home.test',service:'example',monitor:'example-resources',enabled:true});
 await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.projects.length===1`);
 await click('[data-hub-health="browser-project"]');await until(`serviceHubUI.data.projects[0].health?.state==='healthy'`);
 await click('[data-hub-edit="browser-project"]');await evaluate(`document.querySelector('[name=name]').value='保留中的草稿'`);await click('[data-hub-close]');await evaluate('loadServiceHub()');
 await pane('gateway');await click('[data-hub-preview]');await until(`document.querySelector('#detail-dialog').open`);assert(await evaluate(`document.querySelector('#detail-content').textContent.includes('proxy_pass http://127.0.0.1:3000;')`));await click('#close-detail');
 await click('[data-hub-gateway]');await until(`document.querySelector('#hub-operation-form')`);await form({confirmation:'xdocker-gateway'},true);await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.gateway.runtime_verified`);
 await pane('services');await click('[data-hub-logs="browser-project"]');await until(`document.querySelector('#detail-dialog').open`);assert(await evaluate(`!document.querySelector('#detail-content').textContent.includes('fixture-secret')`));await click('#close-detail');
 await click('[data-hub-action="browser-project"]');await until(`document.querySelector('#hub-dialog').open&&serviceHubUI.editor?.kind==='action'`);await form({confirmation:'浏览器项目',action:'restart'},true);await until(`!document.querySelector('#hub-dialog').open&&serviceHubUI.data.operations.length===1`);
 await pane('records');assert(await evaluate(`document.querySelector('#view-service-hub-panel').textContent.includes('状态已核对')`));await shot('desktop-records');
 await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 for(const name of ['overview','projects','gateway','services','records']){
  await pane(name);assert(await evaluate(`document.documentElement.scrollWidth<=innerWidth+2`),'Narrow overflow '+name);
  assert.equal(await evaluate(`document.querySelectorAll('.hub-workspace [role=tabpanel]').length`),1);
  assert(await evaluate(`(()=>{const n=document.querySelector('.hub-workspace [role=tablist]').getBoundingClientRect(),a=document.querySelector('.hub-workspace [aria-selected=true]').getBoundingClientRect();return a.left>=n.left-1&&a.right<=n.right+1})()`),'Active task outside narrow nav '+name);
 }
 await pane('projects');await click('[data-hub-edit="browser-project"]');assert.equal(await evaluate(`document.querySelector('[name=name]').value`),'保留中的草稿');assert(await evaluate(`document.querySelector('#hub-form button[type=submit]').getBoundingClientRect().bottom<=innerHeight`),'Primary save action is outside narrow viewport');await shot('mobile-editor');await click('[data-hub-close]');
 await pane('services');await shot('mobile-services');
 await evaluate(`serviceHubUI.error='fixture-visible-error';serviceHubUI.data.operations[0].state='unconfirmed';renderServiceHubPage()`);
 for(const name of ['overview','projects','gateway','services','records']){await pane(name);assert(await evaluate(`document.querySelector('.hub-workspace').textContent.includes('fixture-visible-error')&&document.querySelector('.hub-workspace').textContent.includes('结果未确认')`));}
 assert.deepEqual(exceptions,[]);console.log(JSON.stringify({passed:true,desktop:'1440x1000',mobile:'390x844',panes:5,drafts:true,registerWithoutStart:true,health:true,gatewayPreviewAndApply:'isolated-adapter',serviceRestart:'one-fixed-grant',logRedaction:true,dockerRequired:false,consoleExceptions:0}));
}
main().catch(async e=>{console.error(e.stack);if(ws&&session){try{console.error(JSON.stringify({exceptions,ui:await evaluate(`({login:document.querySelector('#login-error')?.textContent,content:document.querySelector('#content')?.innerText?.slice(0,1500),error:typeof serviceHubUI==='object'?serviceHubUI.error:'not loaded',formError:document.querySelector('#hub-form-error')?.textContent})`)}));await shot('failure');}catch{}}process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stop(chrome);await stop(fixture);if(fixture&&fixture.exitCode!==0){console.error('Fixture read-only assertion failed');process.exitCode=1;}if(tmp)fs.rmSync(tmp,{recursive:true,force:true});});
