// Explicitly invoked, synthetic-only UI preview. Nothing is proxied to the live app.
const {startCrmServer}=require('./support/crm_server');
const fixture=startCrmServer({htmlTransform:html=>html
  .replace('</head>','<link rel="stylesheet" href="/css/chat-review-reference.css"><link rel="stylesheet" href="/tests/fixtures/reference-review-preview.css"></head>')
  .replace('<script src="/js/main.js"></script>','<script src="/js/chat-review-reference.js"></script><script src="/tests/fixtures/reference-review-preview.js"></script>')});
fixture.api.addTask({id:'preview-person',title:'Maya Chen',task_type:'customer',customer_kind:'person',customer_profile:{}});
fixture.api.addTask({id:'preview-task',title:'Prepare the launch page',task_type:'task',status:'open'});
fixture.api.addTable('global',{id:'preview-table',title:'Influencer partnerships',description:'Creators and launch follow-ups',table_kind:'crm',columns:[]});
fixture.listen().then(url=>console.log(JSON.stringify({url,fixture:'Synthetic reference editing; saves and decisions remain in browser memory.'})));
