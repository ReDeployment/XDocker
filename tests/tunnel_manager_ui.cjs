'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../clients/ssh-tunnel/static/app.js'),'utf8');
const flush=()=>new Promise(r=>setImmediate(r));
function fixture(){
 const elements=new Map(),handlers={},requests=[];
 const state={mode:'ok',posts:0,profiles:[],jobs:[]};
 function el(id){if(!elements.has(id))elements.set(id,{id,value:'',hidden:false,disabled:false,textContent:'',innerHTML:'',className:'',open:false,addEventListener(type,fn){handlers[id+':'+type]=fn;},querySelector(){return el(id+'-button');},showModal(){this.open=true;},close(){this.open=false;},focus(){}});return elements.get(id);}
 const context=vm.createContext({console,Map,Error,Promise,Date,Number,AbortController,
  document:{hidden:false,getElementById:el,addEventListener(t,f){handlers['document:'+t]=f;}},window:{addEventListener(t,f){handlers['window:'+t]=f;}},
  setTimeout(){return 1;},clearTimeout(){},setInterval(fn){handlers.poll=fn;},
  async fetch(url,options){requests.push({url,...options});if(options.method==='POST')state.posts++;
   if(state.mode==='offline')throw TypeError('offline');
   if(state.mode==='unauthorized')return {ok:false,status:401,json:async()=>({error:'token rejected'})};
   return {ok:true,status:options.method==='POST'?202:200,json:async()=>options.method==='POST'?{id:'job'}:{profiles:state.profiles,jobs:state.jobs,busy:false,writes_supported:true}};
  }});
 vm.runInContext(source,context);
 async function login(){el('token').value='fixture-local-token';await handlers['login-form:submit']({preventDefault(){}});}
 async function prepare(){el('destination').value='test-server';el('local-port').value='19181';el('server-port').value='9080';handlers['new-connection:submit']({preventDefault(){}});}
 return {state,requests,handlers,el,context,login,prepare};
}
(async()=>{
 const f=fixture();assert.equal(f.requests.length,0,'page entry must not initiate SSH');await f.login();
 assert.equal(f.state.posts,0);assert.equal(f.el('token').value,'');assert.equal(f.el('workspace').hidden,false);
 await f.prepare();assert.equal(f.el('confirm-dialog').open,true);assert.equal(f.state.posts,0);
 f.el('confirmation').value='wrong';await f.handlers['confirm-form:submit']({preventDefault(){}});assert.equal(f.state.posts,0);
 f.el('confirmation').value='test-server';f.state.mode='offline';await f.handlers['confirm-form:submit']({preventDefault(){}});
 assert.equal(f.state.posts,1);assert.match(f.el('confirm-error').textContent,/结果未确认/);
 f.state.mode='ok';await f.handlers['window:online']();await f.handlers.poll();await flush();assert.equal(f.state.posts,1,'read recovery must not replay mutations');
 f.state.profiles=[{destination:'<script>unsafe</script>',local_port:19181,server_port:9080,state:'connected',current_http:'connected',registered:true,supervisor_active:true,version:'v',url:'http://127.0.0.1:19181'}];
 await f.el('refresh').onclick();assert.match(f.el('profiles').innerHTML,/&lt;script&gt;/);assert.doesNotMatch(f.el('profiles').innerHTML,/<script>unsafe/);
 f.state.mode='unauthorized';await f.el('refresh').onclick();assert.equal(f.el('workspace').hidden,true);assert.equal(vm.runInContext('token',f.context),'');
 console.log('Local SSH UI passed: explicit target confirmation, escaped metadata, offline refusal, GET-only recovery and no mutation replay.');
})().catch(e=>{console.error(e);process.exit(1);});
