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
async function pane(name){await click(`[data-view="resources"][data-view-tab="${name}"]`);await until(`document.querySelector('#view-resources-panel')?.getAttribute('aria-labelledby')==='view-resources-${name}'`);}
async function form(values){await evaluate(`(()=>{const f=document.querySelector('#resource-form');for(const [k,v] of Object.entries(${JSON.stringify(values)}))f.elements.namedItem(k).value=v;f.requestSubmit(f.querySelector('button[type=submit]'));})()`);}
async function shot(name){if(process.env.RESOURCE_UI_ARTIFACTS){fs.mkdirSync(process.env.RESOURCE_UI_ARTIFACTS,{recursive:true});const r=await rpc('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(process.env.RESOURCE_UI_ARTIFACTS,name+'.png'),Buffer.from(r.data,'base64'));}}
async function stop(child){if(!child||child.exitCode!==null)return;try{process.kill(-child.pid,'SIGINT');}catch{}for(let i=0;i<30&&child.exitCode===null;i++)await sleep(100);if(child.exitCode===null){try{process.kill(-child.pid,'SIGKILL');}catch{}}}
async function main(){
 assert(chromeBin,'Set CHROME_BIN');assert.equal(typeof WebSocket,'function','Node 22+ required');
 fixture=cp.spawn(process.env.PYTHON||'python3',['-B','tests/resource_browser_fixture.py'],{cwd:root,detached:true,stdio:['ignore','pipe','pipe'],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
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
 await until(`document.querySelector('#login-form')&&typeof renderResourcesPage==='function'`);
 await evaluate(`document.querySelector('#token').value=${JSON.stringify(info.fixture_token)};document.querySelector('#login-form').requestSubmit(document.querySelector('#login-form button[type=submit]'))`);
 await until(`resourcesUI.data?.report&&document.querySelector('.resource-kpis')`);
 assert(await evaluate(`(()=>{const b=document.querySelectorAll('.resource-workspace [role=tab]');return b[1].getBoundingClientRect().top>b[0].getBoundingClientRect().bottom})()`));
 assert.equal(await evaluate(`document.querySelectorAll('.resource-workspace [role=tabpanel]').length`),1);await shot('desktop-overview');
 assert(await evaluate(`document.querySelector('.resource-kpis').textContent.includes('GPU')`));
 await click('[data-resource-gpu-view]');await until(`location.hash==='#resources/gpu'&&document.querySelector('.resource-gpu-name')`);
 assert(await evaluate(`document.querySelector('#view-resources-panel').textContent.includes('512.0 MiB')&&document.querySelector('#view-resources-panel').textContent.includes('39.0 °C')&&document.querySelector('#view-resources-panel').textContent.includes('0.0%')&&document.querySelector('#view-resources-panel').textContent.includes('预留显存')`));
 await shot('desktop-gpu');
 await evaluate(`window.originalGPU=structuredClone(resourcesUI.data.report.gpu);const second=structuredClone(originalGPU.devices[0]);second.id='f'.repeat(64);second.name='AMD 第二张显卡';second.bus_id='0000:06:00.0';resourcesUI.data.report.gpu.devices.push(second);renderResourcesPage();const select=document.querySelector('#resource-gpu-select');select.value=second.id;select.dispatchEvent(new Event('change',{bubbles:true}));`);
 await pane('overview');await pane('gpu');assert.equal(await evaluate(`document.querySelector('#resource-gpu-select').value`),'f'.repeat(64));
 await evaluate(`resourcesUI.data.report.gpu.devices[1].temperature_celsius=null;resourcesUI.data.report.gpu.devices[1].state='partial';renderResourcesPage()`);
 assert(await evaluate(`document.querySelector('#view-resources-panel').textContent.includes('不可用')&&document.querySelector('.resource-alert').textContent.includes('部分 GPU')`));
 await pane('apps');assert(await evaluate(`document.querySelector('.resource-workspace').textContent.includes('部分 GPU')`));
 await evaluate(`resourcesUI.data.report.gpu=originalGPU;renderResourcesPage()`);
 await pane('discover');await click('[data-resource-bind]');await evaluate(`document.querySelector('[name=id]').value='browser-app';document.querySelector('[name=name]').value='浏览器应用'`);await click('[data-resource-close]');
 await pane('apps');await pane('discover');await click('[data-resource-bind]');assert.equal(await evaluate(`document.querySelector('[name=id]').value`),'browser-app');
 await form({id:'browser-app',name:'浏览器应用'});await until(`!document.querySelector('#resource-dialog').open&&resourcesUI.data.apps.length===1`);
 await pane('apps');assert(await evaluate(`document.querySelector('#view-resources-panel').textContent.includes('浏览器应用')`));
 await click('[data-resource-edit="browser-app"]');await evaluate(`document.querySelector('[name=name]').value='保留中的草稿'`);await click('[data-resource-close]');await loadRefresh();
 await pane('history');await until('!resourcesUI.historyLoading');await click('[data-resource-history]');await until(`document.querySelector('.resource-chart')`);await shot('desktop-history');
 await pane('apps');await click('[data-resource-edit="browser-app"]');assert.equal(await evaluate(`document.querySelector('[name=name]').value`),'保留中的草稿');await click('[data-resource-close]');
 await rpc('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
 for(const name of ['overview','gpu','apps','discover','history']){
  await pane(name);assert(await evaluate(`document.documentElement.scrollWidth<=innerWidth+2`),'Narrow overflow '+name);
  assert(await evaluate(`document.querySelectorAll('.resource-workspace [role=tabpanel]').length===1`));
  assert(await evaluate(`(()=>{const list=document.querySelector('.resource-workspace [role=tablist]').getBoundingClientRect(),a=document.querySelector('.resource-workspace [role=tab][aria-selected=true]').getBoundingClientRect();return a.left>=list.left-1&&a.right<=list.right+1})()`),'Active tab outside navigation '+name);
 }
 assert(await evaluate(`(()=>{const b=document.querySelectorAll('.resource-workspace [role=tab]');return Math.abs(b[1].getBoundingClientRect().top-b[0].getBoundingClientRect().top)<2})()`));
 await pane('gpu');await shot('mobile-gpu-navigation');await evaluate(`document.querySelector('#view-resources-panel').scrollIntoView({block:'start'})`);await shot('mobile-gpu');
 for(const state of ['not-collected','not-detected','unavailable']){await evaluate(`resourcesUI.data.report.gpu={state:${JSON.stringify(state)},devices:[],limit_reached:false};renderResourcesPage()`);assert(await evaluate(`!document.querySelector('#view-resources-panel').textContent.includes('0.0%')&&!!document.querySelector('.resource-alert')`));}
 await evaluate(`resourcesUI.data.report.gpu=originalGPU;renderResourcesPage()`);
 await pane('apps');await shot('mobile-apps');await click('[data-resource-edit="browser-app"]');assert.equal(await evaluate(`document.querySelector('[name=name]').value`),'保留中的草稿');await shot('mobile-editor');await click('[data-resource-close]');
 await evaluate(`resourcesUI.data.report.at-=200;resourcesUI.error='fixture-visible-error';renderResourcesPage()`);
 for(const name of ['overview','gpu','apps','discover','history']){await pane(name);assert(await evaluate(`document.querySelector('.resource-workspace').textContent.includes('采集数据已过期')&&document.querySelector('.resource-workspace').textContent.includes('fixture-visible-error')`));}
 assert.equal(await evaluate(`resourcesUI.data.runtime_control`),false);assert.deepEqual(exceptions,[]);
 console.log(JSON.stringify({passed:true,desktop:'1440x1000',mobile:'390x844',panes:5,gpu:true,registration:true,history:true,drafts:true,staleVisible:true,runtimeMutation:false,consoleExceptions:0}));
}
async function loadRefresh(){await evaluate('loadResources()');}
main().catch(async e=>{console.error(e.stack);if(ws&&session){try{console.error(JSON.stringify({exceptions,ui:await evaluate(`({login:document.querySelector('#login-error')?.textContent,content:document.querySelector('#content')?.innerText?.slice(0,1500),error:typeof resourcesUI==='object'?resourcesUI.error:'not loaded',formError:document.querySelector('#resource-form-error')?.textContent})`)}));await shot('failure');}catch{}}process.exitCode=1;}).finally(async()=>{if(ws)ws.close();await stop(chrome);await stop(fixture);if(fixture&&fixture.exitCode!==0){console.error('Fixture read-only assertion failed');process.exitCode=1;}if(tmp)fs.rmSync(tmp,{recursive:true,force:true});});
