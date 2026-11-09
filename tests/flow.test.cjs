const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync(require('path').join(__dirname,'../index.html'),'utf8');
const js=html.match(/<script>([\s\S]*?)<\/script>/)[1].split("$('overlay').addEventListener")[0];
const ctx=vm.createContext({});vm.runInContext(js,ctx);
vm.runInContext(`
state.requests=[{id:3,sender:'elder',handled:true},{id:2,sender:'family',handled:false},{id:1,sender:'elder',handled:false}];
if(priorityRequest().id!==2)throw Error('incoming priority');
state.mode='family';if(priorityRequest().id!==1)throw Error('older incoming priority');
if(incomingPending().length!==1)throw Error('directional count');
state.requests.forEach(r=>r.handled=true);if(priorityRequest().id!==3)throw Error('recent fallback');
state.query='退款';if(filteredCases().length!==1)throw Error('case search');
state.query='不存在的例子';if(filteredCases().length!==0)throw Error('empty search');
if(escapeHTML('<img>')!=='&lt;img&gt;')throw Error('escaping');
`,ctx);console.log('PASS: pending priority, sender counts, recent fallback, search and escaping');
