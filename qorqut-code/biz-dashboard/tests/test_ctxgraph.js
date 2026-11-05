#!/usr/bin/env node
// Pure stored-relationship semantics and geometry, with no DOM or provider.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const {test} = require("node:test");
const sandbox = vm.createContext({});
for (const name of ["ctxmap", "ctxgraph"]) vm.runInContext(fs.readFileSync(path.join(__dirname, "../js", name + ".js"), "utf8"), sandbox);
const fixture = require("./fixtures/context-graph.json");
const plain = value => JSON.parse(JSON.stringify(value));
const graph = (data=fixture, topic=null) => plain(sandbox.ctxGraphData(data, {document_id:topic}, !topic));
const layout = (data, saved) => plain(sandbox.ctxGraphLayout(data, saved));

test("stored items and edges are authoritative, with no invented hubs or containment", () => {
  const before=JSON.stringify(fixture), g=graph();
  assert.equal(g.nodes.length,20);
  assert.equal(g.edges.length,19);
  assert.deepEqual(new Set(g.nodes.map(n=>n.kind)),new Set(['person','topic','task','story','table','agent','material']));
  assert.equal(g.nodes.some(n=>n.kind==='root'||n.kind==='group'),false);
  assert.equal(g.edges.filter(e=>e.kind==='mentions').length,6);
  assert.equal(g.edges.filter(e=>e.kind==='concerns').length,6);
  for(const e of g.edges)assert(g.nodes.some(n=>n.id===e.source)&&g.nodes.some(n=>n.id===e.target));
  assert.equal(JSON.stringify(fixture),before);
});

test("a scoped topic includes immediate neighbors, not an inferred transitive subtree", () => {
  const g=graph(fixture,'marketing/launch/autumn');
  assert.equal(g.rootId,'topic:marketing/launch/autumn');
  assert.equal(g.nodes.length,5);
  assert.equal(g.nodes.filter(n=>n.kind==='person').length,0);
  assert.equal(g.nodes.some(n=>n.id==='topic:research'),false);
  assert(g.edges.every(e=>e.source===g.rootId||e.target===g.rootId));
});

test("duplicates, reciprocal directions and self references remain separate", () => {
  const source={type:'story',id:'a'},target={type:'story',id:'b'};
  const data={graph:{nodes:[{...source,label:'A'},{...target,label:'B'}],edges:[
    {id:'one',source,target,role:'part_of',context:'First'},
    {id:'two',source,target,role:'part_of',context:'Second'},
    {id:'reverse',source:target,target:source,role:'custom_relation'},
    {id:'self',source,target:source,role:'related_to'}]}};
  const g=graph(data);
  assert.deepEqual(g.edges.map(e=>e.id),['one','two','reverse','self']);
  assert.equal(g.edges[0].source,'story:a');
  assert.equal(g.edges[2].source,'story:b');
  assert.equal(g.edges[3].source,g.edges[3].target);
  assert.equal(g.edges[1].context,'Second');
});

test("empty and disconnected maps do not manufacture edges", () => {
  assert.equal(graph({graph:{nodes:[],edges:[]}}).nodes.length,0);
  const nodes=fixture.graph.nodes.filter(n=>n.type==='person');
  assert.equal(graph({graph:{nodes,edges:[]}}).nodes.length,4);
  assert.equal(graph({graph:{nodes,edges:[]}}).edges.length,0);
});

test("layout is deterministic, finite and does not mutate input", () => {
  const g=graph(), before=JSON.stringify(g), first=layout(g);
  assert.deepEqual(first,layout(g));
  assert.equal(JSON.stringify(g),before);
  assert(first.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
  assert.equal(new Set(first.map(n=>n.x+','+n.y)).size,first.length);
});

test("saved positions survive updates and a new node starts near its linked item", () => {
  const saved=Object.fromEntries(layout(graph()).map(n=>[n.id,{x:n.x+5000,y:n.y-2500}]));
  const data=structuredClone(fixture);
  data.graph.nodes.push({type:'material',id:'new',label:'New material'});
  data.graph.edges.push({id:'new-edge',source:{type:'material',id:'new'},target:{type:'topic',id:'research'},role:'part_of'});
  const placed=layout(graph(data),saved);
  for(const n of placed.filter(n=>saved[n.id]))assert.deepEqual({x:n.x,y:n.y},saved[n.id]);
  const added=placed.find(n=>n.id==='material:new'),parent=saved['topic:research'];
  assert(Math.hypot(added.x-parent.x,added.y-parent.y)<400);
  assert(layout(graph(),{'topic:research':{x:Infinity,y:NaN}}).every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
});

test("large graphs with disconnected people settle to finite positions", () => {
  const data={graph:{nodes:[],edges:[]}};
  for(let i=0;i<8;i++){
    data.graph.nodes.push({type:'topic',id:'topic-'+i,label:'Topic '+i});
    for(let j=0;j<60;j++){
      const id=i+'-'+j;
      data.graph.nodes.push({type:'material',id,label:'Material '+id});
      data.graph.edges.push({id,source:{type:'material',id},target:{type:'topic',id:'topic-'+i},role:'part_of'});
    }
  }
  for(let i=0;i<50;i++)data.graph.nodes.push({type:'person',id:'person-'+i,label:'Person '+i});
  const placed=layout(graph(data));
  assert.equal(placed.length,538);
  assert(placed.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
});

test("edges meet the triangle boundary", () => {
  const n={sx:0,sy:0,radius:10,kind:'person'};
  const right=sandbox.ctxGraphAnchor(n,{sx:100,sy:0});
  assert(Math.abs(right.x-6)<.001);
  assert.equal(right.y,0);
  assert.equal(sandbox.ctxGraphAnchor(n,{sx:0,sy:-100}).y,-11);
});

test("Mermaid exports directional parallel edges without label injection", () => {
  const g=graph();
  g.nodes[0].label='Business"]\nmalicious-->node["';
  const text=sandbox.ctxGraphMermaid(g);
  assert(text.startsWith('graph LR\n'));
  assert.equal(text.split('\n').length,g.nodes.length+g.edges.length+2);
  assert.equal(text.split('-->|mentions|').length-1,6);
  assert(!text.includes('<script>'));
});


test("relationships attract across types; types cannot change geometry", () => {
  const g={nodes:[{id:'a',kind:'story',group:'story'},{id:'b',kind:'person',group:'person'},{id:'c',kind:'story',group:'story'}],edges:[{source:'a',target:'b'}]};
  const placed=layout(g),distance=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y);
  assert(distance(placed[0],placed[1])<distance(placed[0],placed[2]));
  const changed=layout({...g,nodes:g.nodes.map(n=>({...n,kind:'table',group:'table'}))});
  assert.deepEqual(placed.map(n=>[n.x,n.y]),changed.map(n=>[n.x,n.y]));
  const disconnected=layout({nodes:Array.from({length:100},(_,i)=>({id:String(i)})),edges:[]});
  const w=Math.max(...disconnected.map(n=>n.x))-Math.min(...disconnected.map(n=>n.x));
  const h=Math.max(...disconnected.map(n=>n.y))-Math.min(...disconnected.map(n=>n.y));
  assert(w/h<2 && h/w<2,'disconnected nodes fill a compact area');
});
