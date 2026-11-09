'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', cy, snapshot, scope = '', ticket = 0, selected = '', saved = null, nextCursor = null;
  let lastStrengthRefresh = 0, traces = [], timer, loaded = 0, detailTicket = 0;
  const colors = {M0:'#ce9467',M1:'#8bb9ac',M2:'#aa9bce',M3:'#6ba8d9',M4:'#d5a2bd',M5:'#929eae'};
  async function api(path, options = {}) {
    options.headers = {...options.headers, 'Content-Type':'application/json', 'X-CSRF-Token':csrf};
    const response = await fetch(path, {...options, cache:'no-store', credentials:'same-origin'});
    if (response.status === 401) { clearData(); $('login').hidden=false; $('workspace').hidden=true; throw Error('需要重新登录'); }
    if (!response.ok) throw Error('请求失败 '+response.status);
    return response.json();
  }
  function clearData() { ticket++; detailTicket++; selected='';snapshot=null;saved=null;traces=[];nextCursor=null;loaded=0;if(cy)cy.elements().remove();$('details').replaceChildren(text('h2','选择一条记忆'),text('p','查看来源、主体、强度和遗忘曲线。'));$('list').replaceChildren();$('summary').textContent='';$('trace').replaceChildren(new Option('没有召回轨迹','')); }
  function offline(error) { $('connection').textContent='离线，数据截至 '+(snapshot?new Date(snapshot.generated_at*1000).toLocaleTimeString():'尚无快照');$('notice').textContent=error.message; }
  function text(tag, value) { const el=document.createElement(tag);el.textContent=value;return el; }
  function graphInit() {
    cy=cytoscape({container:$('graph'), elements:[], minZoom:.05,maxZoom:10,wheelSensitivity:.2,
      style:[{selector:'node',style:{label:'data(label)','background-color':el=>colors[el.data('layer')]||'#96a4b5','background-opacity':el=>Math.max(.18,el.data('strength_now')),width:el=>26+el.data('importance')*22,height:el=>26+el.data('importance')*22,'font-size':10,'text-max-width':110,'text-wrap':'ellipsis','text-valign':'bottom','text-margin-y':7,color:'#69809a','border-width':el=>el.data('protected')?3:1,'border-color':'#adc5df'}},
      {selector:'edge',style:{width:el=>.8+el.data('weight')*3,'line-color':'#9aaebf', 'curve-style':'bezier', 'target-arrow-shape':'triangle','target-arrow-color':'#9aaebf',opacity:.5}},
      {selector:'edge[edge_class="structure"]',style:{'line-style':'dashed','line-color':'#c69c73'}},
      {selector:'.seed',style:{'border-width':3,'border-color':'#4ea0eb'}},{selector:'.trace',style:{'border-width':4,'border-color':'#edac43','line-color':'#edac43',opacity:1}},
      {selector:':selected',style:{'border-width':4,'border-color':'#3b8fda'}}]});
    let dragStart=0,dragFrames=0,dragging=false;cy.on('grab','node',()=>{dragStart=performance.now();dragFrames=0;dragging=true;const count=()=>{if(dragging){dragFrames++;requestAnimationFrame(count);}};requestAnimationFrame(count);});cy.on('free','node',()=>{dragging=false;const elapsed=performance.now()-dragStart;$('graph').dataset.dragDurationMs=elapsed.toFixed(1);$('graph').dataset.dragFps=(1000*dragFrames/Math.max(1,elapsed)).toFixed(1);$('graph').dataset.dragFrames=String(dragFrames);});
    let graphWidth=$('graph').clientWidth;new ResizeObserver(()=>{const width=$('graph').clientWidth;if(Math.abs(width-graphWidth)>60){graphWidth=width;cy.resize();if(cy.nodes().length)cy.fit(undefined,25);}}).observe($('graph'));
    cy.on('tap','node',event=>showDetail(event.target.id()));
    cy.on('dragfree','node',event=>{event.target.lock();$('notice').textContent='节点已固定；点击“保存布局”可保留位置。';});
  }
  function filterQuery() {return new URLSearchParams({scope_id:scope,layer:$('layer').value,status:$('status').value,subject:$('subject').value,search:$('search').value,future_days:$('future').value});}
  function listRender() { $('list').replaceChildren();for(const node of cy.nodes()){const b=text('button',`${node.data('layer')} · ${node.data('status')} · ${node.data('label')}`);b.onclick=()=>showDetail(node.id());$('list').append(b);} }
  async function loadGraph(append=false) {
    const mine=++ticket, scopeAtStart=scope, query=filterQuery();
    if(append && nextCursor)query.set('cursor',nextCursor);
    const data=await api('/memory-ui/graph?'+query);
    if(mine!==ticket||scopeAtStart!==scope)return;
    if(append && snapshot && data.graph_revision!==snapshot.graph_revision){clearData();return loadGraph();}
    if(!append&&snapshot&&(data.scope_epoch!==snapshot.scope_epoch||data.policy_version!==snapshot.policy_version)){selected='';detailTicket++;$('details').replaceChildren(text('p','记忆/策略已更新，请重新选择。'));}
    if(append && (loaded+data.nodes.length>1000 || cy.edges().length+data.edges.length>3000)){ $('notice').textContent='已到视图资源边界，请缩小筛选范围。';return; }
    const previous={};if(!append)cy.nodes().forEach(n=>previous[n.id()]={...n.position(),locked:n.locked()});
    cy.batch(()=>{if(!append)cy.elements().remove();for(const n of data.nodes){if(cy.getElementById(n.id).length)continue;const pos=previous[n.id]||saved?.positions?.[n.id];cy.add({data:n,...(pos?{position:{x:pos.x,y:pos.y},locked:pos.locked}: {})});}for(const e of data.edges){if(!cy.getElementById(e.id).length)cy.add({data:e});}});
    if(!append && !Object.keys(previous).length){if(saved?.positions && Object.keys(saved.positions).length){const unplaced=cy.nodes().filter(n=>!saved.positions[n.id()]);unplaced.layout({name:'circle',animate:false}).run();if(saved.zoom)cy.viewport({zoom:saved.zoom,pan:saved.pan});if($('graph').clientWidth<600)cy.fit(undefined,25);}else cy.layout({name:'cose',animate:false,randomize:false,numIter:180,fit:true}).run();}
    if(!performance.getEntriesByName('memory-first-graph-ready').length)requestAnimationFrame(()=>{performance.mark('memory-first-graph-ready');$('graph').dataset.firstReadyMs=performance.now().toFixed(1);});snapshot=data;loaded=cy.nodes().length;nextCursor=data.next_cursor;$('more').hidden=!nextCursor;
    $('empty').hidden=loaded>0;$('summary').textContent=`${loaded} 条记忆 · ${cy.edges().length} 条真实关联/结构边 · ${data.association_status}${data.truncated?' · 视图已截断，可筛选或继续分页':''}`;
    $('connection').textContent='在线 · '+new Date(data.generated_at*1000).toLocaleTimeString();$('notice').textContent='';lastStrengthRefresh=Date.now();listRender();
    if(selected&&!cy.getElementById(selected).length){selected='';detailTicket++;$('details').replaceChildren(text('p','原节点已删除、到期或不在当前筛选范围。'));}
    const traceResult=await api('/memory-ui/traces?scope_id='+encodeURIComponent(scope));if(mine!==ticket)return;traces=traceResult.traces;
    $('trace').replaceChildren(new Option(traces.length?'选择一次真实召回':'没有召回轨迹',''));for(const trace of traces)$('trace').add(new Option(new Date(trace.created*1000).toLocaleTimeString()+(trace.delivered?' · 已投递':' · 未投递'),trace.id));
  }
  async function showDetail(id) {
    const mine=++detailTicket, graphTicket=ticket, fromScope=scope;
    const data=await api('/memory-ui/memories/'+encodeURIComponent(id)+'?scope_id='+encodeURIComponent(scope));
    if(mine!==detailTicket||graphTicket!==ticket||fromScope!==scope||!cy.getElementById(id).length||data.scope_epoch!==snapshot.scope_epoch||data.graph_revision!==snapshot.graph_revision)return;
    selected=id;cy.nodes().unselect();cy.getElementById(id).select();const box=$('details');box.replaceChildren(text('h2',data.value));
    const dl=document.createElement('dl');for(const [label,value]of [['编号',data.id],['层/状态',data.layer+' / '+data.status_now],['主体/范围',data.subjects.join('、')+' / '+data.scope_id],['有效时间',String(data.valid_from)+' → '+(data.valid_to??'当前')],['强度 / 可信质量',data.strength_now.toFixed(3)+' / '+data.confidence_score],['锚点S0 / H',data.strength_at_anchor+' / '+(data.half_life_days??'自然衰减豁免')+'天'],['独立证据 / revision',data.independent_evidence_count+' / '+data.revision],['保护 / 到期 / 复审',data.protected+' / '+data.expires_at+' / '+data.review_at]]){dl.append(text('dt',label),text('dd',value));}box.append(dl);
    const canvas=document.createElement('canvas');canvas.className='curve';canvas.width=560;canvas.height=260;box.append(text('h3','假设无新证据的曲线'),canvas);const ctx=canvas.getContext('2d');ctx.strokeStyle='#8c9fb3';for(const s of [.35,.15]){ctx.beginPath();ctx.moveTo(25,230-s*200);ctx.lineTo(540,230-s*200);ctx.stroke();ctx.fillStyle='#8c9fb3';ctx.fillText(String(s),0,230-s*200);}ctx.strokeStyle='#4194df';ctx.lineWidth=4;ctx.beginPath();data.curve.forEach((p,i)=>{const x=25+p.days/180*515,y=230-(p.state==='expired'?0:p.strength)*200;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();
    box.append(text('h3','为什么记住 / 来源'));for(const e of data.evidence)box.append(text('p',e.source_state==='available'?(e.excerpt??'来源暂不可见'):'原文已到期，仅保留来源标识 '+e.source_id));if(!data.evidence.length)box.append(text('p','旧条目缺少可核验来源，不能假装知道原话。'));box.append(text('h3','结构依赖'),text('pre',JSON.stringify(data.dependencies,null,2)));
  }
  async function chooseScope() {clearData();scope=$('scope').value;await api('/memory-ui/preferences',{method:'PUT',body:JSON.stringify({scope_id:scope})});const current=scope, mine=ticket;const data=await api('/memory-ui/layout?scope_id='+encodeURIComponent(scope));if(current!==scope||mine!==ticket)return;saved=data.layout;if(saved?.filters){$('layer').value=saved.filters.layer||'';$('status').value=saved.filters.status||'';}await loadGraph();}
  async function start() {const info=await api('/memory-ui/session');csrf=info.csrf;$('login').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;if(!cy)graphInit();const data=await api('/memory-ui/scopes');$('scope').replaceChildren();for(const item of data.scopes)$('scope').add(new Option(item.label,item.id));if(data.default_scope)$('scope').value=data.default_scope;await chooseScope();clearInterval(timer);timer=setInterval(async()=>{try{const mine=ticket, current=scope;const update=await api('/memory-ui/updates?scope_id='+encodeURIComponent(scope));if(mine!==ticket||current!==scope)return;if(!snapshot||update.scope_epoch!==snapshot.scope_epoch||update.policy_version!==snapshot.policy_version||update.graph_revision!==snapshot.graph_revision||Date.now()-lastStrengthRefresh>60000)await loadGraph();else $('connection').textContent='在线 · '+new Date().toLocaleTimeString();}catch(error){offline(error);}},10000);}
  $('loginForm').onsubmit=async event=>{event.preventDefault();try{await api('/login',{method:'POST',body:JSON.stringify({viewer_id:$('viewer').value,password:$('password').value})});$('password').value='';await start();}catch(error){$('loginError').textContent=error.message;}};
  $('logout').onclick=async()=>{try{await api('/logout',{method:'POST',body:'{}'});}finally{clearInterval(timer);clearData();csrf='';$('workspace').hidden=true;$('login').hidden=false;$('logout').hidden=true;}};
  $('scope').onchange=()=>chooseScope().catch(offline);for(const id of ['layer','status'])$(id).onchange=()=>loadGraph().catch(offline);
  let debounce;for(const id of ['search','subject','future'])$(id).oninput=()=>{clearTimeout(debounce);$('days').textContent=$('future').value==='0'?'当前状态':'未来 +'+$('future').value+' 天';debounce=setTimeout(()=>loadGraph().catch(offline),300);};
  $('fit').onclick=()=>cy.fit(undefined,35);$('relayout').onclick=()=>{cy.nodes().unlock();cy.layout({name:'cose',animate:false,numIter:180}).run();};$('lock').onclick=()=>cy.nodes(':selected').forEach(n=>n.locked()?n.unlock():n.lock());$('listToggle').onclick=()=>{$('list').hidden=!$('list').hidden;};
  $('save').onclick=async()=>{try{const positions={};cy.nodes().forEach(n=>positions[n.id()]={...n.position(),locked:n.locked()});await api('/memory-ui/layout',{method:'PUT',body:JSON.stringify({scope_id:scope,positions,zoom:cy.zoom(),pan:cy.pan(),filters:{layer:$('layer').value,status:$('status').value}})});$('notice').textContent='布局已保存；记忆强度、证据和关联未改变。';}catch(error){offline(error);}};
  $('more').onclick=()=>loadGraph(true).catch(offline);$('trace').onchange=()=>{cy.elements().removeClass('trace seed');const trace=traces.find(t=>t.id===$('trace').value);if(trace){for(const id of trace.packet.seeds)cy.getElementById(id).addClass('seed');for(const path of Object.values(trace.packet.paths)){for(let i=1;i<path.length;i++)cy.edges().filter(e=>e.source().id()===path[i-1]&&e.target().id()===path[i]).addClass('trace');}for(const id of trace.packet.selected)cy.getElementById(id).addClass('trace');}};
  $('voiceStatus').onclick=async()=>{try{const data=await api('/memory-ui/voice?scope_id='+encodeURIComponent(scope));$('voiceState').textContent=JSON.stringify(data);}catch(e){$('voiceState').textContent='语音状态读取失败';}};
  $('voicePreview').onclick=async()=>{try{$('voiceState').textContent='正在准备本地试听…';const data=await api('/memory-ui/voice/preview',{method:'POST',body:JSON.stringify({scope_id:scope,text:$('voiceText').value})});$('voiceAudio').src=data.audio_url;$('voiceState').textContent='合成语音，仅本机试听，未发送QQ。';}catch(e){$('voiceState').textContent='语音尚未就绪或合成失败，未发送QQ。';}};
  start().catch(()=>{});
})();
