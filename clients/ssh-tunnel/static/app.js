'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={connecting:'连接中',connected:'管理接口可达',reconnecting:'自动重连中',stopped:'已停止','not-started':'未启动','port-conflict':'端口占用','host-key-error':'主机身份需核对','authentication-error':'SSH 认证失败','api-unreachable':'管理接口不可达','service-error':'管理服务异常','unexpected-response':'响应需核对','invalid-config':'配置需排查'};
const operations={connect:'连接',disconnect:'断开',reconnect:'重连'};
let token='',data=null,loading=false,posting=false,reachable=false,pending=null,epoch=0;
function notice(message,error=false){$('notice').textContent=message;$('notice').className=error?'error':'';}
function logout(){epoch++;token='';data=null;reachable=false;pending=null;$('token').value='';$('login').hidden=false;$('workspace').hidden=true;$('confirm-dialog').close();}
async function request(path,body){
 const c=new AbortController(),timer=setTimeout(()=>c.abort(),body?12000:75000);
 try{
  const response=await fetch('/api/'+path,{method:body?'POST':'GET',headers:{'X-XDocker-Local-Token':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{}),cache:'no-store',credentials:'omit',signal:c.signal});
  const result=await response.json();
  if(!response.ok){if(response.status===401)logout();throw Error(result.error||'本机管理请求未成功。');}
  return result;
 }catch(e){reachable=false;if(body)throw Error('操作结果未确认，请先读取状态和操作记录；不会自动重试。');throw Error(e.name==='AbortError'?'本机管理请求超时。':e.message||'本机管理服务不可达。');}
 finally{clearTimeout(timer);}
}
const badge=value=>`<span class="badge ${value==='connected'?'good':['connecting','reconnecting','stopped','not-started'].includes(value)?'warn':'bad'}">${esc(labels[value]||value)}</span>`;
function render(){
 if(!data)return;
 const disabled=posting||!reachable||data.busy||!data.writes_supported;
 $('profiles').innerHTML=data.profiles.map(p=>`<tr><td><strong>${esc(p.destination||'配置待核对')}</strong><small>本机 127.0.0.1:${esc(p.local_port)} → 服务器 127.0.0.1:${esc(p.server_port||'—')}</small></td><td>${badge(p.state)}<small>${p.registered?'任务已登记':'任务未登记'} · ${p.supervisor_active?'监督进程运行':'监督进程未运行'}</small><small>${esc(p.message)}</small></td><td>${badge(p.current_http||'未检查')}<small>${p.at?esc(new Date(p.at).toLocaleString('zh-CN')):'尚无托管状态记录'}</small></td><td><div class="actions">${p.url?`<a class="button" href="${esc(p.url)}" target="_blank" rel="noreferrer">打开控制台 ↗</a>`:''}${['connect','disconnect','reconnect'].map(action=>`<button type="button" data-operation="${action}" data-port="${esc(p.local_port)}" ${disabled||p.manageable===false||(action==='connect'&&(p.registered||p.supervisor_active))?'disabled':''} class="${action==='disconnect'?'danger':''}">${operations[action]}</button>`).join('')}</div></td></tr>`).join('')||'<tr><td colspan="4">尚无本仓库登记的隧道，可在下方新增连接。</td></tr>';
 $('jobs').innerHTML=data.jobs.map(j=>`<tr><td>${esc(new Date(j.at*1000).toLocaleString('zh-CN'))}<small>${esc(j.destination)} · 本机 ${esc(j.local_port)}</small></td><td>${esc(operations[j.action]||j.action)}</td><td><strong>${esc({running:'执行中',requested:'已登记，连接待确认',completed:'已完成',failed:'失败',unconfirmed:'结果未确认'}[j.state]||j.state)}</strong><small>${esc(j.message)}</small></td></tr>`).join('')||'<tr><td colspan="3">暂无网页操作记录。</td></tr>';
 $('new-connection').querySelector('button').disabled=disabled;
 $('refresh').disabled=loading;
}
async function refresh(){
 if(!token||loading)return;loading=true;const generation=epoch;
 try{const result=await request('tunnels');if(generation!==epoch)return;data=result;reachable=true;$('login').hidden=true;$('workspace').hidden=false;$('last-read').textContent='读取于 '+new Date().toLocaleTimeString('zh-CN');}
 catch(e){notice(e.message,true);}finally{loading=false;render();}
}
function confirm(action,profile){
 if(!reachable||posting||data?.busy||!data?.writes_supported)return;
 pending={action,...profile};$('confirm-title').textContent=operations[action]+' SSH 隧道';
 $('confirm-target').textContent=profile.destination+' · 本机 '+profile.local_port+' → 服务器 '+profile.server_port;
 $('confirm-impact').textContent=action==='connect'?'将登记本仓库的自动重连任务，不修改 SSH 配置或接管已有端口。':'此操作会中断所选端口的控制台访问；本机管理器继续可用。只操作本仓库管理的隧道。';
 $('confirmation').value='';$('confirm-error').textContent='';$('confirm-dialog').showModal();$('confirmation').focus();
}
$('login-form').addEventListener('submit',async e=>{e.preventDefault();epoch++;token=$('token').value.trim();$('token').value='';notice('');await refresh();});
$('logout').onclick=logout;$('refresh').onclick=refresh;$('cancel').onclick=()=>{$('confirm-dialog').close();pending=null;};
document.addEventListener('click',e=>{const b=e.target.closest('[data-operation]');if(!b||b.disabled)return;const p=data?.profiles.find(p=>p.local_port===Number(b.dataset.port));if(p)confirm(b.dataset.operation,p);});
$('new-connection').addEventListener('submit',e=>{
 e.preventDefault();const destination=$('destination').value.trim(),local_port=Number($('local-port').value),server_port=Number($('server-port').value);
 if(!/^(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(destination)||![local_port,server_port].every(n=>Number.isInteger(n)&&n>=1&&n<=65535)){notice('请填写 SSH 别名及有效端口。',true);return;}
 if(data?.profiles.some(p=>p.local_port===local_port)){notice('本机端口已有登记，请使用对应连接行或另选端口。',true);return;}
 confirm('connect',{destination,local_port,server_port,version:null});
});
$('confirm-form').addEventListener('submit',async e=>{
 e.preventDefault();if(!pending||posting||!reachable)return;
 if($('confirmation').value!==pending.destination){$('confirm-error').textContent='SSH 目标不匹配。';return;}
 const p=pending;posting=true;$('submit').disabled=true;render();
 try{await request('tunnels/action',{action:p.action,destination:p.destination,local_port:p.local_port,server_port:p.server_port,version:p.version??null,confirmation:p.destination});$('confirm-dialog').close();pending=null;notice('请求已登记，请查看操作结果与实际 SSH/HTTP 状态。');}
 catch(e){$('confirm-error').textContent=e.message;notice(e.message,true);}finally{posting=false;$('submit').disabled=false;await refresh();}
});
setInterval(()=>{if(token&&!document.hidden)refresh();},5000);
window.addEventListener('online',refresh);
window.addEventListener('offline',()=>{reachable=false;notice('本机管理连接待恢复；运行操作不会自动重试。',true);render();});
// Entering or refreshing the page never starts, stops or reconnects a tunnel.
