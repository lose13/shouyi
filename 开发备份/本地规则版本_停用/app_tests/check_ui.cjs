// Checks presentation logic using a small document stand-in, not a rendering engine.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const fixture=JSON.parse(fs.readFileSync('.test-tmp/ui-fixture.json','utf8'));
const nodes=new Map();
function makeNode(attrs={},tag='div'){
  const classes=new Set((attrs.class||'').split(/\s+/).filter(Boolean));
  const dataset=Object.fromEntries(Object.entries(attrs).filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase()),v]));
  const el={id:attrs.id,tagName:tag.toUpperCase(),dataset,value:'',style:{},textContent:'',innerHTML:'',hidden:false,disabled:false,open:false,events:{},
    classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c),toggle(c,on){const yes=on===undefined ? !classes.has(c):on;yes ? classes.add(c):classes.delete(c);return yes;}},
    setAttribute(k,v){attrs[k]=v;},getAttribute:k=>attrs[k],
    addEventListener(type,fn){this.events[type]=fn;},
    querySelector(){return null;},closest(){return null;},
    showModal(){this.open=true;},close(){this.open=false;},focus(){},scrollIntoView(){},append(){},click(){},remove(){}};
  if(attrs.id)nodes.set(attrs.id,el);return el;
}
const all=fixture.elements.map(e=>makeNode(e.attrs,e.tag));
const intro=makeNode({class:'intro-note'});
const document={getElementById(id){assert(nodes.has(id),'Missing HTML control: '+id);return nodes.get(id);},
  querySelector(selector){if(selector==='.intro-note')return intro;if(selector==='dialog[open]')return all.find(x=>x.tagName==='DIALOG'&&x.open)||null;return null;},
  querySelectorAll(selector){if(selector==='dialog')return all.filter(x=>x.tagName==='DIALOG');if(selector==='[data-close]')return all.filter(x=>x.dataset.close);if(selector==='[data-probe]')return all.filter(x=>x.dataset.probe);if(selector==='#memoryFilters button')return all.filter(x=>x.dataset.filter);return [];},
  addEventListener(){},body:{append(){}},createElement:tag=>makeNode({},tag)};
const context=vm.createContext({document,console,setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},clearInterval(){},AbortController,
  fetch:async path=>{
    if(path==='/api/bootstrap')return{ok:true,json:async()=>fixture.bootstrap};
    if(path.startsWith('/api/export')){const turn=new URL(path,'http://local.invalid').searchParams.get('turn');return{ok:true,text:async()=>fixture.exports[turn]};}
    throw new Error('Unexpected test request: '+path);
  }});
const code=fs.readFileSync('static/app.js','utf8');
(async()=>{
  await vm.runInContext(code,context);
  const run=s=>vm.runInContext(s,context);
  assert.strictEqual(nodes.get('currentTurn').textContent,6);
  assert(nodes.get('storyContent').innerHTML.includes('YY网络科技有限公司'));
  for(let i=0;i<28;i++){
    run(`chooseRound(${i})`);
    assert.strictEqual(nodes.get('currentTurn').textContent,i+1);
    assert(!nodes.get('storyContent').innerHTML.includes('undefined'));
  }
  assert.strictEqual(Number(nodes.get('countBlocked').textContent),4);
  run("state.filter='pending';renderMemory()");
  assert(nodes.get('memoryCards').innerHTML.includes('待核实'));
  assert(nodes.get('memoryCards').innerHTML.includes('待确认'));
  run("state.filter='history';renderMemory()");
  assert(nodes.get('memoryCards').innerHTML.includes('[已清除]'));
  assert(!nodes.get('memoryCards').innerHTML.includes('朋友转述的招聘机会'));
  await run("state.view='raw';renderMemory();loadRaw()");
  assert(nodes.get('memoryRaw').textContent.includes('已通过PMP考试并取得证书'));
  run("state.view='cards';state.filter='current';state.search='公司';renderMemory()");
  assert(nodes.get('memoryCards').innerHTML.includes('YY网络科技有限公司'));
  assert(!nodes.get('memoryCards').innerHTML.includes('银行卡标记'));
  run("state.search='';state.workspace.kind='sandbox';render()");
  assert.strictEqual(nodes.get('composer').hidden,false);
  assert.strictEqual(nodes.get('officialHint').hidden,true);
  assert(nodes.get('memoryCards').innerHTML.includes('删除这条记忆'));
  run("state.workspace.turns=[];state.workspace.records=[];state.index=0;render()");
  assert(nodes.get('storyContent').innerHTML.includes('从一句话开始'));
  assert.strictEqual(Number(nodes.get('countActive').textContent),0);
  assert.strictEqual(nodes.get('nextButton').disabled,true);
  console.log('PASS: 28轮页面内容逻辑、统计、待确认/历史筛选、删除标记、原文、搜索、沙盒入口和空白状态。');
  console.log('本检查不验证浏览器排版。');
})().catch(error=>{console.error(error);process.exitCode=1;});
