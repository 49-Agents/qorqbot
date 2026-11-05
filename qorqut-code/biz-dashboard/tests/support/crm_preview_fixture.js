// Reusable local-only synthetic data for the two-pane CRM preview. It composes the
// real dashboard mounts with the disposable Operations server; no provider or UI is replaced.
const {startCrmServer} = require('./crm_server');

const IDS = {
  crm: '6a2c0000-0000-4000-8000-000000000001',
  table: '6a2c0000-0000-4000-8000-000000000002',
  scopedCrm: '6a2c0000-0000-4000-8000-000000000003',
  maya: '6a2c0000-0000-4000-8000-000000000101',
  leo: '6a2c0000-0000-4000-8000-000000000102',
  nora: '6a2c0000-0000-4000-8000-000000000103',
  scoped: '6a2c0000-0000-4000-8000-000000000104',
  story: '6a2c0000-0000-4000-8000-000000000105',
};

function createCrmPreview() {
  const server = startCrmServer(), api = server.api;
  const crm = api.addTable('global', {id: IDS.crm, title: 'Influencer partnerships', description: 'Creators and follow-ups for the November launch.',
    table_kind: 'crm', crm_config: {stages: [{id:'research',name:'Research'},{id:'contacted',name:'Contacted'},{id:'negotiating',name:'Negotiating'},{id:'confirmed',name:'Confirmed'}]},
    columns: [{id:'name',name:'Name',type:'text',record_field:'title'},{id:'company',name:'Company',type:'text',record_field:'customer_profile.company'},
      {id:'email',name:'Email',type:'text',record_field:'customer_profile.email'},{id:'followers',name:'Followers',type:'number'},{id:'next_step',name:'Next step',type:'text'}]});
  const ordinary = api.addTable('global', {id: IDS.table, title:'Campaign reference links', description:'A regular synthetic table for navigation checks.', table_kind:'general',
    columns:[{id:'label',name:'Label',type:'text'}]});
  const scopedCrm = api.addTable('alpha', {id:IDS.scopedCrm,title:'Atlas creator CRM',description:'Scoped synthetic records for Alpha Studio.',table_kind:'crm',
    crm_config:{stages:[{id:'contacted',name:'Contacted'},{id:'contracted',name:'Contracted'}]},columns:[{id:'name',name:'Name',type:'text',record_field:'title'},{id:'email',name:'Email',type:'text',record_field:'customer_profile.email'}]});
  const people = [
    {id:IDS.maya,title:'Maya Chen',email:'maya@example.test',company:'Maya Makes',stage:'negotiating',followers:85000,next:'Review media kit'},
    {id:IDS.leo,title:'Leo Martins',email:'leo@example.test',company:'Leo Outdoors',stage:'contacted',followers:42000,next:'Follow up Friday'},
    {id:IDS.nora,title:'Nora Davis',email:'nora@example.test',company:'Nora Studio',stage:'confirmed',followers:126000,next:'Send creative brief'},
  ];
  for (const person of people) {
    const task = api.addTask({id:person.id,title:person.title,task_type:'customer',customer_kind:'person',customer_state:'Lead',tags:['influencer'],
      overview:'Synthetic creator partnership record.',customer_profile:{email:person.email,company:person.company,website:'https://example.test/'+person.id,notes:'Synthetic preview data.'}});
    api.addEntry(crm.id,task,{stage_id:person.stage,cells:{followers:person.followers,next_step:person.next}});
  }
  const ordinaryTask = api.addTask({id:'6a2c0000-0000-4000-8000-000000000201',title:'Media kit checklist',task_type:'task',overview:'A regular table row.'});
  api.addEntry(ordinary.id,ordinaryTask,{cells:{label:'Media kit checklist'}});
  const scopedTask = api.addTask({id:IDS.scoped,business_id:'11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa',title:'Atlas Studio',task_type:'customer',customer_kind:'company',customer_state:'Lead',
    overview:'Synthetic Project scoped customer.',customer_profile:{email:'hello@atlas.example.test',company:'Atlas Research',notes:'Synthetic preview data.'}});
  api.addEntry(scopedCrm.id,scopedTask,{stage_id:'contacted'});
  const story = api.addTask({id:IDS.story,title:'Creator outreach plan',task_type:'story',overview:'Synthetic Focus Story for workspace mode regression.'});
  api.threads.get(crm.id).push({id:'synthetic-table-note',kind:'comment',author_id:'ordu',author_name:'Ordubashy',body:'Maya sent her media kit. Review it before Friday.',created_at:'2026-10-28T14:15:00Z'});
  api.activity.set('global:'+IDS.maya,[{id:'synthetic-activity',kind:'email',summary:'Media kit received',body:'Maya shared audience demographics and availability.',occurred_at:'2026-10-28T13:40:00Z',created_at:'2026-10-28T13:40:00Z',recorded_by:'ordu',source:{type:'task',id:IDS.maya,business_id:null}}]);
  server.pin({type:'table',id:crm.id,business_id:null});
  server.pin({type:'table',id:ordinary.id,business_id:null});
  server.pin({type:'task',id:IDS.story,business_id:null});

  const sourceKey = (type,id,businessId=null) => `${type}:${businessId ?? 'global'}:${id}`;
  for (const [type,id,body] of [
    ['table',crm.id,'Private Ordubashy thread for the influencer CRM.'],
    ['table',ordinary.id,'Private thread for the ordinary table.'],
    ['task',IDS.maya,'Private record thread for Maya Chen.'],
    ['task',IDS.leo,'Private record thread for Leo Martins.'],
    ['task',IDS.story,'Ordinary Focus Story conversation stays in the right workspace.'],
    ['table',scopedCrm.id,'Private thread for the scoped CRM.'],
  ]) server.privateThreads.set(sourceKey(type,id),{messages:[{id:'preview-'+type+'-'+id,sequence:1,body,created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',is_owner:false,created_at:'2026-10-28T14:00:00Z'}],read:0});
  server.privateThreads.set(sourceKey('task',IDS.scoped,'11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa'),{messages:[{id:'preview-scoped-atlas',sequence:1,
    body:'Private thread for the scoped Atlas Studio record.',created_by:'chief',created_by_name:'Ordubashy',author_kind:'agent',is_owner:false,created_at:'2026-10-28T14:00:00Z'}],read:0});

  const timestamp='2026-10-28T14:20:00Z';
  const refs=[
    {type:'table',id:crm.id,business_id:null,label:crm.title,available:true,table_kind:'crm'},
    {type:'table',id:ordinary.id,business_id:null,label:ordinary.title,available:true,table_kind:'general'},
    {type:'task',id:IDS.maya,business_id:null,label:'Maya Chen',available:true,task_type:'customer'},
    {type:'task',id:IDS.scoped,business_id:'11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa',business_slug:'alpha',label:'Atlas Studio',available:true,task_type:'customer'},
  ];
  server.agentChats.set('ordu',{agent_id:'ordu',name:'Ordubashy',session:{state:'live',reason:'live',can_send:true,working:false,provider:'codex',conversation_id:'synthetic-native-chat',transport:'terminal',detail:'Synthetic local preview',
      binding:{binding_id:'synthetic-binding',agent_id:'ordu',provider:'codex',conversation_id:'synthetic-native-chat',generation:1,transport:'terminal'}},revision:1,cursor:1,
    conversation:{conversation_id:'synthetic-native-chat',provider:'codex',complete:true,finalized:false,error:null,captured_length:1},
    conversations:[{conversation_id:'synthetic-native-chat',provider:'codex',updated_at:timestamp,current:true}],
    entries:[
      {id:'preview-owner-message',kind:'message',speaker:'owner',text:'Which creator should we follow up with next?',timestamp,position:100,partial:false,identity:'native_id',references:[]},
      {id:'preview-agent-message',kind:'message',speaker:'agent',text:`Maya sent her media kit. See the CRM: [${crm.title}](/tables/view?table_id=${crm.id}) and ordinary table [${ordinary.title}](/tables/view?table_id=${ordinary.id})`,timestamp,position:200,partial:false,identity:'native_id',completion_sequence:null,completion_id:null,system:null,references:refs},
    ],older:null,rendered_boundary:0,inputs:[],actions:[],last_read_sequence:0,newest_sequence:0});
  return {server,api,crm,ordinary,scopedCrm,people,ids:IDS};
}

module.exports={createCrmPreview,IDS};
