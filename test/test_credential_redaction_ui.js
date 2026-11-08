// Request credential redaction UI: node test/test_credential_redaction_ui.js [index.html]
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const {spawnSync}=require('node:child_process');
const src=fs.readFileSync(process.argv[2]||path.join(__dirname,'..','static','index.html'),'utf8');
let passed=0;
async function test(name,fn){await fn();passed++;console.log('OK: '+name);}
function extract(name){
  const match=src.match(new RegExp('(?:async )?function '+name+'\\([^\\n]*\\)\\{[\\s\\S]*?\\n\\}'));
  assert.ok(match,'function '+name+' exists');
  return match[0];
}
function deferred(){
  let resolve;
  const promise=new Promise(r=>{resolve=r;});
  return {promise,resolve};
}
function reply(enabled){return {credential_redaction:{enabled}};}
function harness(responses){
  const nodes=new Map(),calls=[],toasts=[];
  function createElement(tagName){
    const el={tagName:tagName.toUpperCase(),innerHTML:'',className:'',children:[],disabled:false,value:'',textContent:''};
    el.remove=()=>{
      nodes.delete(el.id);
      for(const child of el.children)nodes.delete(child.id);
    };
    return el;
  }
  const document={
    createElement,
    getElementById:id=>nodes.get(id)||null,
    body:{appendChild(el){
      nodes.set(el.id,el);
      for(const match of el.innerHTML.matchAll(/<(select|button)\b([^>]*)>([\s\S]*?)<\/\1>/g)){
        const [,tag,attrs,body]=match,id=attrs.match(/\bid="([^"]+)"/);
        if(!id)continue;
        const child=createElement(tag);child.id=id[1];child.disabled=/\bdisabled\b/.test(attrs);child.textContent=body;
        if(tag==='select'){
          const options=[...body.matchAll(/<option\b([^>]*)>([^<]*)<\/option>/g)];
          const selected=options.find(m=>/\bselected\b/.test(m[1]))||options[0];
          child.value=selected?selected[1].match(/\bvalue="([^"]*)"/)[1]:'';
        }
        nodes.set(child.id,child);el.children.push(child);
      }
    }}
  };
  const context={document,toast:(message,type)=>toasts.push({message,type}),fetch:async(url,opts)=>{
    calls.push({url,method:opts.method,headers:opts.headers,body:opts.body===undefined?undefined:JSON.parse(opts.body)});
    assert.ok(responses.length,'an expected response is queued');
    let item=responses.shift();
    if(typeof item==='function')item=await item();
    if(item instanceof Error)throw item;
    const status=item.status||200;
    return {ok:status>=200&&status<300,status,text:async()=>item.raw===undefined?JSON.stringify(item.data):item.raw};
  }};
  const names=['api','credentialRedactionEnabled','openCredentialRedactionManager','renderCredentialRedactionManager','closeCredentialRedactionManager','saveCredentialRedaction'];
  const state=src.match(/^let credentialRedactionLoading=.*;$/m);
  assert.ok(state,'credential redaction request state exists');
  vm.runInNewContext("const API='';\n"+state[0]+'\n'+names.map(extract).join('\n'),context);
  return {context,calls,toasts,nodes,get:id=>document.getElementById(id)};
}
async function main(){
  await test('header entry is adjacent to sensitive-word filtering',()=>{
    assert.match(src,/<button class="btn btn-ghost" onclick="openContentFilterManager\(\)">🛡 敏感词过滤<\/button>\s*<button class="btn btn-ghost" onclick="openCredentialRedactionManager\(\)">请求脱敏<\/button>/,'request redaction entry follows sensitive-word filtering');
  });
  for(const enabled of [true,false])await test('GET initializes '+enabled+' from the server',async()=>{
    const h=harness([{data:reply(enabled)}]);
    await h.context.openCredentialRedactionManager();
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].method,'GET');assert.equal(h.calls[0].url,'/api/credential-redaction');
    assert.equal(h.calls[0].body,undefined);assert.equal(h.get('credentialRedactionEnabled').value,String(enabled));
    const overlay=h.get('credentialRedactionOverlay');assert.equal(overlay.className,'modal-overlay show');
    assert.match(overlay.innerHTML,/<div class="modal">\s*<h2>请求凭据脱敏<\/h2>/);
    assert.match(overlay.innerHTML,/<div class="form-row">\s*<div class="form-group">\s*<label for="credentialRedactionEnabled">开启（全局）<\/label>\s*<select id="credentialRedactionEnabled">/);
    assert.match(overlay.innerHTML,/<option value="false"[^>]*>否<\/option>/);
    assert.match(overlay.innerHTML,/<option value="true"[^>]*>是<\/option>/);
    assert.equal((overlay.innerHTML.match(/\bselected\b/g)||[]).length,1);
    assert.match(overlay.innerHTML,/<div class="form-actions">/);
    assert.match(overlay.innerHTML,/id="credentialRedactionClose"[^>]*onclick="closeCredentialRedactionManager\(\)">关闭<\/button>/);
    assert.match(overlay.innerHTML,/id="credentialRedactionSave"[^>]*onclick="saveCredentialRedaction\(\)">保存<\/button>/);
    assert.doesNotMatch(overlay.innerHTML,/checkbox|\.checked/);
    for(const text of ['发往模型前','账号、密码、API Key','单向替换','本地日志保持原样','下一次出站','重试、换端点','已发出的请求','本地工具','从 .env 取值','裸值','图片','漏识别'])assert.ok(overlay.innerHTML.includes(text),text);
    assert.equal(h.toasts.length,0);
  });
  await test('loading waits for GET and coalesces repeated opens',async()=>{
    const pending=deferred(),h=harness([()=>pending.promise]);
    const first=h.context.openCredentialRedactionManager();
    await h.context.openCredentialRedactionManager();
    assert.equal(h.calls.length,1);assert.equal(h.get('credentialRedactionOverlay'),null);assert.equal(h.get('credentialRedactionEnabled'),null);
    pending.resolve({data:reply(true)});await first;
    await h.context.openCredentialRedactionManager();assert.equal(h.calls.length,1);
  });
  await test('closing and reopening fetches current global state',async()=>{
    const h=harness([{data:reply(true)},{data:reply(false)}]);
    await h.context.openCredentialRedactionManager();h.context.closeCredentialRedactionManager();
    assert.equal(h.get('credentialRedactionOverlay'),null);
    await h.context.openCredentialRedactionManager();assert.equal(h.get('credentialRedactionEnabled').value,'false');assert.equal(h.calls.length,2);
  });
  const loadFailures=[
    ['HTTP error',{status:500,data:{error:'load write-store unavailable'}}],
    ['network error',new Error('offline')],
    ['empty response',{raw:''}],
    ['missing state',{data:{credential_redaction:{}}}],
    ['string bool',{data:reply('false')}],
    ['non-JSON response',{raw:'<html>bad gateway</html>'}]
  ];
  for(const [name,response] of loadFailures)await test('GET '+name+' keeps state unknown and allows retry',async()=>{
    const h=harness([response,{data:reply(true)}]);await h.context.openCredentialRedactionManager();
    assert.equal(h.get('credentialRedactionOverlay'),null);assert.equal(h.get('credentialRedactionEnabled'),null);
    assert.equal(h.toasts.length,1);assert.equal(h.toasts[0].type,'error');assert.match(h.toasts[0].message,/加载失败/);
    await h.context.openCredentialRedactionManager();assert.equal(h.get('credentialRedactionEnabled').value,'true');
  });
  for(const enabled of [true,false])await test('PUT sends boolean '+enabled+' and confirms returned state',async()=>{
    const h=harness([{data:reply(!enabled)},{data:reply(!enabled)}]);
    await h.context.openCredentialRedactionManager();
    const select=h.get('credentialRedactionEnabled');select.value=String(enabled);
    await h.context.saveCredentialRedaction();
    const call=h.calls[1];assert.equal(call.method,'PUT');assert.equal(call.url,'/api/credential-redaction');
    assert.deepEqual(call.body,reply(enabled));assert.equal(typeof call.body.credential_redaction.enabled,'boolean');
    assert.equal(call.headers['Content-Type'],'application/json');
    assert.equal(select.value,String(!enabled));assert.equal(h.get('credentialRedactionOverlay'),null);
    assert.equal(h.toasts.length,1);assert.equal(h.toasts[0].type,'success');
    assert.ok(h.toasts[0].message.includes(enabled?'已关闭':'已开启'));assert.match(h.toasts[0].message,/下一次出站生效/);
  });
  const saveFailures=[
    ['HTTP 400',{status:400,data:{error:'enabled must be bool'}}],
    ['HTTP 500',{status:500,data:{error:'disk write failed'}}],
    ['network error',new Error('offline')],
    ['empty response',{raw:''}],
    ['string bool',{data:reply('true')}]
  ];
  for(const [name,response] of saveFailures)await test('PUT '+name+' preserves editing and releases save lock',async()=>{
    const h=harness([{data:reply(false)},response,{data:reply(true)}]);await h.context.openCredentialRedactionManager();
    const overlay=h.get('credentialRedactionOverlay'),select=h.get('credentialRedactionEnabled');select.value='true';
    await h.context.saveCredentialRedaction();
    assert.equal(h.get('credentialRedactionOverlay'),overlay);assert.equal(select.value,'true');assert.equal(select.disabled,false);
    assert.equal(h.get('credentialRedactionSave').disabled,false);assert.equal(h.get('credentialRedactionClose').disabled,false);
    assert.equal(h.toasts.length,1);assert.equal(h.toasts[0].type,'error');assert.match(h.toasts[0].message,/保存失败/);
    await h.context.saveCredentialRedaction();assert.equal(h.calls.length,3);assert.equal(h.toasts[1].type,'success');
  });
  await test('pending PUT disables controls and blocks duplicate save/open/close',async()=>{
    const pending=deferred(),h=harness([{data:reply(false)},()=>pending.promise]);
    await h.context.openCredentialRedactionManager();const overlay=h.get('credentialRedactionOverlay');h.get('credentialRedactionEnabled').value='true';
    const saving=h.context.saveCredentialRedaction();
    for(const id of ['credentialRedactionSave','credentialRedactionClose','credentialRedactionEnabled'])assert.equal(h.get(id).disabled,true,id);
    await h.context.saveCredentialRedaction();await h.context.openCredentialRedactionManager();h.context.closeCredentialRedactionManager();
    assert.equal(h.calls.length,2);assert.equal(h.get('credentialRedactionOverlay'),overlay);assert.equal(h.toasts.length,0);
    pending.resolve({data:reply(true)});await saving;
    assert.equal(h.toasts.length,1);assert.equal(h.toasts[0].type,'success');
  });
  await test('thrown API errors are surfaced for both load and save',async()=>{
    const h=harness([{data:reply(false)}]),api=h.context.api;
    h.context.api=async()=>{throw new Error('unexpected failure');};
    await h.context.openCredentialRedactionManager();assert.equal(h.get('credentialRedactionOverlay'),null);assert.match(h.toasts[0].message,/加载失败.*unexpected failure/);
    h.context.api=api;await h.context.openCredentialRedactionManager();h.get('credentialRedactionEnabled').value='true';
    h.context.api=async()=>{throw new Error('unexpected save failure');};
    await h.context.saveCredentialRedaction();assert.equal(h.get('credentialRedactionEnabled').value,'true');assert.equal(h.get('credentialRedactionSave').disabled,false);
    assert.match(h.toasts[1].message,/保存失败.*unexpected save failure/);
  });
  await test('inline script passes node --check',()=>{
    const scripts=[...src.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];assert.ok(scripts.length);
    for(const [,script] of scripts){
      const result=spawnSync(process.execPath,['--check'],{input:script,encoding:'utf8'});
      assert.equal(result.status,0,result.stderr||String(result.error));
    }
  });
  await test('CSS has balanced delimiters, CSS comments and house modal rules',()=>{
    const styles=[...src.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/g)];assert.ok(styles.length);
    const css=styles.map(m=>m[1]).join('\n');assert.doesNotMatch(css,/^\s*\/\//m);
    assert.equal((css.match(/\/\*/g)||[]).length,(css.match(/\*\//g)||[]).length,'balanced comments');
    const clean=css.replace(/\/\*[\s\S]*?\*\//g,'').replace(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g,'');
    const stack=[],pairs={'}':'{',')':'(',']':'['};
    for(const c of clean){if('{(['.includes(c))stack.push(c);else if('})]'.includes(c))assert.equal(stack.pop(),pairs[c],'CSS delimiter '+c);}
    assert.equal(stack.length,0);
    for(const rule of ['.modal-overlay{','.modal{','.modal h2{','.form-group input,.form-group select{','.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}','.form-actions{'])assert.ok(clean.includes(rule),rule);
  });
  console.log('PASS: '+passed+' credential redaction UI checks');
}
main().catch(error=>{console.error('FAIL: '+error.message);process.exitCode=1;});
