let SNAP = null;
let CURRENT_PAGE = 'runtimecenter';
let SELECTED_DEVICE = {kind:'', name:''};
let ANALYZER_FILES = [];
let CURRENT_ANALYZER_TAB = 'upload';
const $ = (id) => document.getElementById(id);
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pill(v){ const c=String(v||'unknown').toLowerCase(); return `<span class="pill ${esc(c)}">${esc(v||'unknown')}</span>`; }
function fmtTs(ts){ if(!ts) return '-'; try { return new Date(ts*1000).toLocaleTimeString(); } catch(e){ return '-'; } }
function showPage(p){ CURRENT_PAGE=p; document.querySelectorAll('nav button, .topnav button').forEach(b=>b.classList.toggle('active', b.dataset.page===p)); document.querySelectorAll('.page').forEach(x=>x.classList.remove('active')); const pageEl=$(`page-${p}`); if(!pageEl){ console.warn('Missing page', p); return; } pageEl.classList.add('active'); renderActive(); if(p==='runtime'){ loadMetrics(); loadRestorePlan(); } if(p==='health'){ loadHealthMonitor(); } if(p==='parity'){ loadParityAudit(); } if(p==='uiactions'){ loadUiActionMatrix(); } if(p==='lts'){ loadLtsAudit(); } if(p==='commands'){ loadCommandAudit(); } if(p==='settings'){ loadRuntimeSettings(); } if(p==='clusters'){ loadPowerMapStatus(); loadPowerMapEditor(); } if(p==='project'){ loadProjectProfiles(); loadProjectConfig(); } if(p==='alarms'){ populateAlarmDevices(); } if(p==='strategy'){ populateStrategyClusters(); } if(p==='ops'){ populateOpsBmsDevices(); loadCsvStatus(); } }
function cardsHtml(summary, s){
  const rec = s.recording || {}; const soaking = (s.soak_test||{}).running;
  return [
    ['BMS online', `${summary.bms_online||0}/${summary.bms_total||0}`, (summary.bms_error||0)?'warn':'ok'],
    ['PCS online', `${summary.pcs_online||0}/${summary.pcs_total||0}`, (summary.pcs_error||0)?'warn':'ok'],
    ['Strategies', summary.strategy_count||0, 'accent'],
    ['CSV', (rec.bms_recording||rec.pcs_recording) ? 'Recording' : 'Idle', (rec.bms_recording||rec.pcs_recording) ? 'ok' : ''],
    ['Commands', (s.command_acks||[]).length, ''],
    ['Soak test', soaking ? 'Running' : 'Idle', soaking ? 'ok' : ''],
  ].map(([l,v,c]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join('');
}

function numVal(v){ const n=Number(v); return Number.isFinite(n)?n:null; }
function fmtMetric(v, suffix='', digits=1){ const n=numVal(v); if(n===null) return esc(v ?? '-'); return esc(n.toFixed(digits).replace(/\.0$/,'')) + esc(suffix); }
function bmsStatusLabel(raw){
  const v = String(raw ?? '').trim();
  const n = Number(v);
  if(Number.isFinite(n)){
    const map = {1:'Normal',2:'Full Charge',3:'Full Discharge',4:'Warning',5:'Fault'};
    return map[n] || `Unknown(${n})`;
  }
  const l=v.toLowerCase();
  if(!l) return '-';
  if(l.includes('fault') || l.includes('alarm')) return 'Fault';
  if(l.includes('warn')) return 'Warning';
  if(l.includes('full') && l.includes('charge')) return 'Full Charge';
  if(l.includes('full') && l.includes('discharge')) return 'Full Discharge';
  if(l.includes('normal') || l.includes('ready') || l.includes('online') || l.includes('running')) return 'Normal';
  return v;
}
function bmsStatusClass(label){
  const l=String(label||'').toLowerCase();
  if(l.includes('fault')) return 'bad';
  if(l.includes('warning')) return 'warn';
  if(l.includes('full')) return 'accent';
  if(l.includes('normal')) return 'ok';
  return '';
}
function bmsStatusFromDevice(d, vals){
  return bmsStatusLabel(bmsMetric(vals,['bms_status','system_status','status','state','work_status','running_status']) || d.bms_status || d.work_status || d.state || d.status_code || d.status);
}
function metricFromDevice(d, vals, keys){ return bmsMetric(vals, keys) ?? d[keys[0]] ?? '-'; }
function dashboardBar(label, value, total, cls=''){
  const t=Math.max(Number(total)||0,0); const v=Math.max(Number(value)||0,0); const pct=t?Math.max(0,Math.min(100,(v/t)*100)):0;
  return `<div class="bar-row"><div class="bar-top"><span>${esc(label)}</span><b>${esc(v)}/${esc(t)}</b></div><div class="bar-track"><div class="bar-fill ${esc(cls)}" style="width:${pct}%"></div></div></div>`;
}
function dashboardSeverityBar(label, count, total, cls=''){
  const t=Math.max(Number(total)||0,0); const v=Math.max(Number(count)||0,0); const pct=t?Math.max(4,Math.min(100,(v/t)*100)):0;
  return `<div class="bar-row"><div class="bar-top"><span>${esc(label)}</span><b>${esc(v)}</b></div><div class="bar-track"><div class="bar-fill ${esc(cls)}" style="width:${pct}%"></div></div></div>`;
}

function deviceRows(){ const ds=(SNAP||{}).device_states||{}; const rows=[]; for(const [typ, items] of Object.entries({BMS:ds.bms||{}, PCS:ds.pcs||{}})){ Object.values(items).forEach(d=>rows.push({...d, _type:typ})); } return rows.sort((a,b)=>String(a._type+a.name).localeCompare(String(b._type+b.name))); }


let PROJECT=null;
async function loadProjectProfiles(){
  try{
    const r=await fetch('/api/project/profiles',{cache:'no-store'}); const data=await r.json();
    const setOptions=(id, arr, fallback)=>{ const el=$(id); if(!el) return; const old=el.value||fallback; const opts=(arr&&arr.length?arr:[fallback]); el.innerHTML=opts.map(x=>`<option value="${esc(x)}">${esc(x)}</option>`).join(''); if(opts.includes(old)) el.value=old; };
    setOptions('bmsCfgProfile', data.bms_profiles, 'catl_v22');
    setOptions('pcsCfgProfile', data.pcs_profiles, 'kehua_bcs1250');
  }catch(e){ console.warn('profile options unavailable', e); }
}
async function loadProjectConfig(){ const box=$('projectConfigResult'); if(box) box.textContent='Loading...'; try{ const r=await fetch('/api/project/config',{cache:'no-store'}); PROJECT=await r.json(); renderProjectConfig(); if(box) box.innerHTML='<span class="ok">Loaded.</span>'; }catch(e){ if(box) box.innerHTML=`<span class="bad">${esc(e)}</span>`; } }
function projectBmsNames(){ return (PROJECT?.bms_devices||[]).map(x=>String(x.name||'').trim()).filter(Boolean).sort(); }
function projectPcsNames(){ return Object.keys(PROJECT?.pcs_configs||{}).filter(Boolean).sort(); }
function setMultiSelectOptions(id, names, selected){
  const el=$(id); if(!el) return;
  const sel=new Set((selected||[]).map(String));
  el.innerHTML=(names||[]).map(n=>`<option value="${esc(n)}" ${sel.has(String(n))?'selected':''}>${esc(n)}</option>`).join('');
}
function getMultiSelectValues(id){ const el=$(id); if(!el) return []; return Array.from(el.selectedOptions||[]).map(o=>String(o.value||'').trim()).filter(Boolean); }
function refreshClusterBindingSelectors(selectedBms=null, selectedPcs=null){
  if(!PROJECT || !$('clusterCfgBms') || !$('clusterCfgPcs')) return;
  const curBms = selectedBms || getMultiSelectValues('clusterCfgBms');
  const curPcs = selectedPcs || getMultiSelectValues('clusterCfgPcs');
  setMultiSelectOptions('clusterCfgBms', projectBmsNames(), curBms);
  setMultiSelectOptions('clusterCfgPcs', projectPcsNames(), curPcs);
  refreshClusterPowerMapEditor();
}
function selectedClusterPowerMap(){
  const mode=$('clusterPowerMapMode')?.value || 'even';
  const pcs=getMultiSelectValues('clusterCfgPcs');
  if(mode==='keep') return null;
  if(mode==='even'){
    if(!pcs.length) return {};
    const share=Number((1/pcs.length).toFixed(6));
    const m={}; pcs.forEach(n=>m[n]=share); return m;
  }
  const txt=String($('clusterPowerMapJson')?.value||'').trim();
  if(!txt) return {};
  try{
    const parsed=JSON.parse(txt);
    const allowed=new Set(pcs);
    const out={};
    Object.entries(parsed||{}).forEach(([k,v])=>{ if(allowed.has(String(k))) out[String(k)] = Number(v); });
    return out;
  }catch(e){ throw new Error('Power Map JSON is invalid: '+e); }
}
function refreshClusterPowerMapEditor(){ if(!$('clusterPowerMapJson') || !$('clusterCfgPcs')) return;
  const mode=$('clusterPowerMapMode')?.value || 'even';
  const box=$('clusterPowerMapJson'); if(!box) return;
  if(mode==='keep'){ box.disabled=true; return; }
  box.disabled=false;
  if(mode==='even'){
    const pcs=getMultiSelectValues('clusterCfgPcs');
    const m={}; if(pcs.length){ const share=Number((1/pcs.length).toFixed(6)); pcs.forEach(n=>m[n]=share); }
    box.value=JSON.stringify(m,null,2);
  }
}
function autoEvenPowerMap(){ const rows=((PROJECT&&PROJECT.site_config&&PROJECT.site_config.clusters)||[]); rows.forEach((_,i)=>autoEvenRowPowerMap(i)); }
function renderProjectConfig(){
  if(!PROJECT) return;
  const bms=PROJECT.bms_devices||[]; const pcs=PROJECT.pcs_configs||{}; const clusters=((PROJECT.site_config||{}).clusters)||[];
  $('projectBmsCount').textContent=`${bms.length} BMS`; $('projectPcsCount').textContent=`${Object.keys(pcs).length} PCS`; if($('projectClusterCount')) $('projectClusterCount').textContent=`${clusters.length} Cluster`;
  $('projectBmsRows').innerHTML=bms.map(d=>{ const name=String(d.name||''); return `<tr><td>${esc(name)}</td><td>${esc(d.host)}</td><td>${esc(d.port)}</td><td>${esc(d.unit_id)}</td><td>${esc(d.interval||d.poll_interval)}</td><td>${esc(d.profile||d.driver||'')}</td><td><button data-action="fill-bms" data-name="${esc(name)}" onclick="return actionClick(event)">Edit</button> <button class="danger" data-action="remove-bms" data-name="${esc(name)}" onclick="return actionClick(event)">Remove</button></td></tr>`; }).join('') || '<tr><td colspan="7" class="muted">No BMS configured</td></tr>';
  $('projectPcsRows').innerHTML=Object.entries(pcs).sort().map(([name,c])=>`<tr><td>${esc(name)}</td><td>${esc(c.host)}</td><td>${esc(c.port)}</td><td>${esc(c.unit_id)}</td><td>${esc(c.profile||c.driver||'')}</td><td>${esc(c.enabled)}</td><td><button data-action="fill-pcs" data-name="${esc(name)}" onclick="return actionClick(event)">Edit</button> <button class="danger" data-action="remove-pcs" data-name="${esc(name)}" onclick="return actionClick(event)">Remove</button></td></tr>`).join('') || '<tr><td colspan="7" class="muted">No PCS configured</td></tr>';
  renderClusterBindingRows(clusters);
  $('projectConfigRaw').textContent=JSON.stringify(PROJECT,null,2);
}

function configuredBmsNames(){ return (PROJECT?.bms_devices||[]).map(x=>String(x.name||'')).filter(Boolean).sort(); }
function configuredPcsNames(){ return Object.keys(PROJECT?.pcs_configs||{}).sort(); }
let CLUSTER_EDIT = {};
let CLUSTER_BINDING_DIRTY = false;
let CLUSTER_BINDING_INTERACTIVE_UNTIL = 0;
function touchClusterBindingEditor(ms=30000){ CLUSTER_BINDING_INTERACTIVE_UNTIL = Date.now() + ms; }
function isClusterBindingEditing(){ return Date.now() < CLUSTER_BINDING_INTERACTIVE_UNTIL; }
function markClusterBindingDirty(){ CLUSTER_BINDING_DIRTY = true; touchClusterBindingEditor(60000); const el=$('projectConfigResult'); if(el) el.innerHTML='<span class="warn">Cluster binding has unsaved changes. Click Save on the row you changed.</span>'; }
function normalizedClusterBms(c){ return [...(c?.bms_devices||c?.bms||[])].map(String).filter(Boolean); }
function normalizedClusterPcs(c){ return [...(c?.pcs_devices||c?.pcs||(c?.pcs_device?[c.pcs_device]:[])||[])].map(String).filter(Boolean); }
function normalizePowerMap(raw, bmsList=[], pcsList=[]){
  const allowedBms=new Set((bmsList||[]).map(String));
  const allowedPcs=new Set((pcsList||[]).map(String));
  const out={};
  if(!raw || typeof raw!=='object' || Array.isArray(raw)) return out;
  Object.entries(raw).forEach(([pcs, weights])=>{
    const pc=String(pcs);
    if(allowedPcs.size && !allowedPcs.has(pc)) return;
    if(weights && typeof weights==='object' && !Array.isArray(weights)){
      const row={};
      Object.entries(weights).forEach(([bms,w])=>{
        const bm=String(bms);
        if(allowedBms.size && !allowedBms.has(bm)) return;
        const n=Number(w);
        if(Number.isFinite(n) && n>=0) row[bm]=n;
      });
      if(Object.keys(row).length) out[pc]=row;
    } else {
      // Backward compatibility with the old flat map: {"PCS-1": 0.5}
      // Interpret it as this PCS can use every selected BMS with that weight.
      const n=Number(weights);
      if(Number.isFinite(n) && n>=0){
        const row={};
        (bmsList||[]).forEach(bm=>row[String(bm)]=n);
        if(Object.keys(row).length) out[pc]=row;
      }
    }
  });
  return out;
}
function evenMapForRow(row){
  const bms=(row?.bms||[]).map(String).filter(Boolean);
  const pcs=(row?.pcs||[]).map(String).filter(Boolean);
  const out={};
  if(!bms.length || !pcs.length) return out;
  const share=Number((1/bms.length).toFixed(6));
  pcs.forEach(pc=>{ out[pc]={}; bms.forEach(bm=>out[pc][bm]=share); });
  return out;
}
function clusterEditFromProject(clusters){
  const next={};
  (clusters||[]).forEach((c,idx)=>{
    const name=String(c.name||`Cluster-${idx+1}`);
    const bms=normalizedClusterBms(c);
    const pcs=normalizedClusterPcs(c);
    next[name]={bms, pcs, power_map:normalizePowerMap(c.power_map||{}, bms, pcs)};
  });
  return next;
}
function sameClusterNames(a,b){ const ak=Object.keys(a||{}).sort().join('|'); const bk=Object.keys(b||{}).sort().join('|'); return ak===bk; }
function usedByOtherClusters(kind, clusterName){
  const used=new Set();
  Object.entries(CLUSTER_EDIT||{}).forEach(([name,row])=>{
    if(String(name)===String(clusterName)) return;
    (row[kind]||[]).forEach(x=>used.add(String(x)));
  });
  return used;
}
function clusterAvailable(kind, clusterName){
  const all = kind==='bms' ? configuredBmsNames() : configuredPcsNames();
  const current = new Set(((CLUSTER_EDIT[clusterName]||{})[kind]||[]).map(String));
  const used = usedByOtherClusters(kind, clusterName);
  return all.filter(n => current.has(n) || !used.has(n));
}
function bindSelectOptions(names){ return ['<option value="">Select device...</option>'].concat((names||[]).map(n=>`<option value="${esc(n)}">${esc(n)}</option>`)).join(''); }
function jsArg(v){ return JSON.stringify(String(v)); }
function encArg(v){ return encodeURIComponent(String(v)); }
function decArg(v){ return decodeURIComponent(String(v||'')); }
function clusterDomId(name){ return 'cl_' + String(name||'').replace(/[^a-zA-Z0-9_-]/g, '_'); }
function chipHtml(clusterName, kind, values){
  return (values||[]).map(v=>`<span class="chip">${esc(v)} <button title="Remove" data-action="remove-bind-chip" data-cluster="${esc(clusterName)}" data-kind="${esc(kind)}" data-value="${esc(v)}" onclick="return actionClick(event)">×</button></span>`).join('') || '<span class="muted">None</span>';
}
function refreshBindRow(clusterName){
  const row=CLUSTER_EDIT[clusterName]||{bms:[],pcs:[],power_map:{}};
  const cid=clusterDomId(clusterName);
  const bmsBox=$(`bindBmsChips_${cid}`), pcsBox=$(`bindPcsChips_${cid}`);
  if(bmsBox) bmsBox.innerHTML=chipHtml(clusterName,'bms',row.bms);
  if(pcsBox) pcsBox.innerHTML=chipHtml(clusterName,'pcs',row.pcs);
  const bmsSel=$(`bindBmsPick_${cid}`), pcsSel=$(`bindPcsPick_${cid}`);
  if(bmsSel) bmsSel.innerHTML=bindSelectOptions(clusterAvailable('bms', clusterName).filter(n=>!(row.bms||[]).includes(n)));
  if(pcsSel) pcsSel.innerHTML=bindSelectOptions(clusterAvailable('pcs', clusterName).filter(n=>!(row.pcs||[]).includes(n)));
  const pm=$(`clusterRowPower_${cid}`); if(pm) pm.textContent=JSON.stringify(row.power_map||{});
}
function addBindPick(clusterName, kind){
  touchClusterBindingEditor(60000);
  const cid=clusterDomId(clusterName); const sel=$(`bind${kind==='bms'?'Bms':'Pcs'}Pick_${cid}`); const v=String(sel?.value||'').trim();
  if(!v) return;
  const row=CLUSTER_EDIT[clusterName]||(CLUSTER_EDIT[clusterName]={bms:[],pcs:[],power_map:{}});
  const arr=row[kind]||(row[kind]=[]);
  if(!arr.includes(v)) arr.push(v);
  row.power_map=normalizePowerMap(row.power_map||{}, row.bms||[], row.pcs||[]);
  markClusterBindingDirty();
  refreshAllBindRows();
}
function removeBindChip(clusterName, kind, value){
  touchClusterBindingEditor(60000);
  const row=CLUSTER_EDIT[clusterName]; if(!row) return;
  row[kind]=(row[kind]||[]).filter(x=>String(x)!==String(value));
  row.power_map=normalizePowerMap(row.power_map||{}, row.bms||[], row.pcs||[]);
  markClusterBindingDirty();
  refreshAllBindRows();
}
function evenMap(pcs){ return {}; /* deprecated flat map helper; use evenMapForRow(row) */ }
function refreshAllBindRows(){ Object.keys(CLUSTER_EDIT||{}).forEach(refreshBindRow); }
function renderClusterBindingRows(clusters){
  const bmsNames=configuredBmsNames(), pcsNames=configuredPcsNames();
  if($('availableBmsCount')) $('availableBmsCount').textContent=String(bmsNames.length);
  if($('availablePcsCount')) $('availablePcsCount').textContent=String(pcsNames.length);
  const freshEdit = clusterEditFromProject(clusters||[]);
  // While the user is selecting from a dropdown, do not redraw this table.
  if(isClusterBindingEditing() && $('projectClusterRows') && $('projectClusterRows').children.length){ return; }
  if(!CLUSTER_BINDING_DIRTY || !sameClusterNames(CLUSTER_EDIT, freshEdit)) CLUSTER_EDIT = freshEdit;
  const rows=(clusters||[]).map((c,idx)=>{
    const name=String(c.name||`Cluster-${idx+1}`);
    const cid=clusterDomId(name);
    const arg=jsArg(name);
    return `<tr data-cluster="${esc(name)}">
      <td><b>${esc(name)}</b></td>
      <td>
        <div id="bindBmsChips_${cid}" class="chip-list"></div>
        <div class="bind-picker"><select id="bindBmsPick_${cid}" onfocus="touchClusterBindingEditor(60000)" onchange="touchClusterBindingEditor(60000)"></select><button data-action="add-bind-pick" data-cluster="${esc(name)}" data-kind="bms" onmousedown="touchClusterBindingEditor(60000)" onclick="return actionClick(event)">Add</button></div>
      </td>
      <td>
        <div id="bindPcsChips_${cid}" class="chip-list"></div>
        <div class="bind-picker"><select id="bindPcsPick_${cid}" onfocus="touchClusterBindingEditor(60000)" onchange="touchClusterBindingEditor(60000)"></select><button data-action="add-bind-pick" data-cluster="${esc(name)}" data-kind="pcs" onmousedown="touchClusterBindingEditor(60000)" onclick="return actionClick(event)">Add</button></div>
      </td>
      <td>
        <code id="clusterRowPower_${cid}" class="power-map-code" title="Click Edit to configure power map">{}</code>
        <div class="row-actions"><button data-action="edit-power-map" data-cluster="${esc(name)}" onclick="return actionClick(event)">Edit</button><button data-action="auto-even-power-map" data-cluster="${esc(name)}" onclick="return actionClick(event)">Auto Even</button><button class="danger" data-action="clear-power-map" data-cluster="${esc(name)}" onclick="return actionClick(event)">Clear Power Map</button></div>
      </td>
      <td><button data-action="save-cluster-binding" data-cluster="${esc(name)}" onclick="return actionClick(event)">Save</button> <button class="danger" data-action="remove-cluster" data-cluster="${esc(name)}" onclick="return actionClick(event)">Remove Cluster</button></td>
    </tr>`;
  }).join('');
  if($('projectClusterRows')) $('projectClusterRows').innerHTML=rows || '<tr><td colspan="5" class="muted">No clusters configured. Add a cluster below.</td></tr>';
  refreshAllBindRows();
}
function autoEvenPowerMap(){ Object.values(CLUSTER_EDIT||{}).forEach(row=>{ row.power_map=evenMapForRow(row); }); markClusterBindingDirty(); refreshAllBindRows(); }
function autoEvenPowerMapForCluster(name){
  touchClusterBindingEditor(60000);
  const row=CLUSTER_EDIT[name]||(CLUSTER_EDIT[name]={bms:[],pcs:[],power_map:{}});
  row.power_map=evenMapForRow(row);
  markClusterBindingDirty(); refreshBindRow(name);
}
function clearPowerMapForCluster(name){
  touchClusterBindingEditor(60000);
  const row=CLUSTER_EDIT[name]||(CLUSTER_EDIT[name]={bms:[],pcs:[],power_map:{}});
  row.power_map={};
  markClusterBindingDirty(); refreshBindRow(name);
}
async function clearPowerMapAndSave(name){
  if(!confirm(`Clear Power Map for ${name}? This will keep the BMS/PCS binding but remove all PCS→BMS weights.`)) return;
  clearPowerMapForCluster(name);
  await saveClusterBindingByName(name);
}
function editRowPowerMapByName(name){
  touchClusterBindingEditor(120000);
  // Prefer the human-friendly table editor instead of a JSON prompt.
  showPage('clusters');
  setTimeout(()=>{
    const sel=$('pmClusterSelect');
    if(sel){ sel.value=String(name||''); renderPowerMapEditorForSelected(); }
    if($('pmEditorResult')) $('pmEditorResult').innerHTML='<span class="warn">Editing Power Map for '+esc(name)+'. Change weights in the table and click Save Power Map.</span>';
  }, 50);
}
function editRowPowerMap(idx){ const clusters=((PROJECT&&PROJECT.site_config&&PROJECT.site_config.clusters)||[]); const name=String((clusters[idx]||{}).name||''); if(name) editRowPowerMapByName(name); }
async function saveClusterBindingByName(name){
  if(!PROJECT) await loadProjectConfig();
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  const oldByName={};
  (data.clusters||[]).forEach(c=>{ oldByName[String(c.name||'')]=c; });
  // Save the whole visible binding editor, not only the clicked row. This avoids losing other pending row edits.
  data.clusters=Object.entries(CLUSTER_EDIT||{}).map(([clusterName,row])=>{
    const bms=[...(row.bms||[])].map(String).filter(Boolean);
    const pcs=[...(row.pcs||[])].map(String).filter(Boolean);
    const power_map=normalizePowerMap(row.power_map||{}, bms, pcs);
    return Object.assign({}, oldByName[String(clusterName)]||{}, {
      name:String(clusterName),
      // Native keys used by the PySide/runtime site loader:
      bms_devices:bms,
      pcs_devices:pcs,
      pcs_device:pcs[0]||'',
      // Backward-compatible aliases used by some Web helpers:
      bms:bms,
      pcs:pcs,
      power_map:power_map
    });
  });
  await postJson('/api/site/config', data, 'projectConfigResult');
  CLUSTER_BINDING_DIRTY=false; CLUSTER_BINDING_INTERACTIVE_UNTIL=0;
  await loadProjectConfig(); await validateProjectConfig();
}
async function saveClusterBindingRow(idx, name){ await saveClusterBindingByName(name); }
async function addClusterBindingRow(){
  if(!PROJECT) await loadProjectConfig();
  const name=String($('newClusterName')?.value||'').trim() || prompt('New cluster name:');
  if(!name) return;
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  if(!Array.isArray(data.clusters)) data.clusters=[];
  if(data.clusters.some(x=>String(x.name)===String(name))){ alert('Cluster already exists.'); return; }
  data.clusters.push({name, bms:[], pcs:[], power_map:{}});
  await postJson('/api/site/config', data, 'projectConfigResult'); if($('newClusterName')) $('newClusterName').value=''; await loadProjectConfig();
}
async function removeClusterBindingRow(name){
  if(!confirm(`Remove cluster ${name}?`)) return;
  if(!PROJECT) await loadProjectConfig();
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  data.clusters=(data.clusters||[]).filter(x=>String(x.name)!==String(name));
  await postJson('/api/site/config', data, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig();
}

function setCfgValue(id, value){ const el=$(id); if(el) el.value = value ?? ''; }
function showConfigEditMessage(kind, name){
  const box=$('projectConfigResult');
  if(box) box.innerHTML = `<span class="ok">Loaded ${esc(kind)} config for <b>${esc(name)}</b>. Edit the form above, then click Save.</span>`;
  const first = kind === 'BMS' ? $('bmsCfgName') : $('pcsCfgName');
  const form = first ? first.closest('.section,.card,.panel,div') : null;
  try{ (form || first)?.scrollIntoView({behavior:'smooth', block:'center'}); }catch(e){ try{ (form || first)?.scrollIntoView(); }catch(_){} }
  if(first){ try{ first.focus({preventScroll:true}); }catch(e){ try{ first.focus(); }catch(_){} } }
}
function getBmsConfigByName(name){
  const target=String(name||'');
  const lists=[PROJECT?.bms_devices, PROJECT?.devices?.bms, PROJECT?.bms];
  for(const list of lists){
    if(Array.isArray(list)){ const d=list.find(x=>String(x?.name||x?.id||'')===target); if(d) return d; }
    else if(list && typeof list==='object'){ const d=list[target]; if(d) return Object.assign({name:target}, d); }
  }
  return null;
}
function getPcsConfigByName(name){
  const target=String(name||'');
  const sources=[PROJECT?.pcs_configs, PROJECT?.pcs_devices, PROJECT?.devices?.pcs, PROJECT?.pcs];
  for(const src of sources){
    if(Array.isArray(src)){ const c=src.find(x=>String(x?.name||x?.id||'')===target); if(c) return c; }
    else if(src && typeof src==='object'){ const c=src[target]; if(c) return Object.assign({name:target}, c); }
  }
  return null;
}
function fillBmsConfig(name){
  const d=getBmsConfigByName(name);
  if(!d){ const box=$('projectConfigResult'); if(box) box.innerHTML=`<span class="bad">BMS config not found: ${esc(name)}</span>`; return false; }
  setCfgValue('bmsCfgName', d.name||d.id||name);
  setCfgValue('bmsCfgHost', d.host||d.ip||'');
  setCfgValue('bmsCfgPort', d.port||502);
  setCfgValue('bmsCfgUnit', d.unit_id ?? d.slave_id ?? d.device_id ?? 1);
  setCfgValue('bmsCfgInterval', d.interval ?? d.poll_interval ?? 2);
  setCfgValue('bmsCfgProfile', d.profile||d.driver||'catl_v22');
  showConfigEditMessage('BMS', d.name||d.id||name);
  return false;
}
function fillPcsConfig(name){
  const c=getPcsConfigByName(name);
  if(!c){ const box=$('projectConfigResult'); if(box) box.innerHTML=`<span class="bad">PCS config not found: ${esc(name)}</span>`; return false; }
  setCfgValue('pcsCfgName', c.name||c.id||name);
  setCfgValue('pcsCfgHost', c.host||c.ip||'');
  setCfgValue('pcsCfgPort', c.port||502);
  setCfgValue('pcsCfgUnit', c.unit_id ?? c.slave_id ?? c.device_id ?? 1);
  setCfgValue('pcsCfgProfile', c.profile||c.driver||'kehua_bcs1250');
  showConfigEditMessage('PCS', c.name||c.id||name);
  return false;
}
function splitNames(v){ return String(v||'').split(',').map(x=>x.trim()).filter(Boolean); }
function fillClusterBinding(name){
  const clusters=((PROJECT&&PROJECT.site_config&&PROJECT.site_config.clusters)||[]); const c=clusters.find(x=>String(x.name)===String(name))||{};
  $('clusterCfgName').value=c.name||name||'';
  refreshClusterBindingSelectors(normalizedClusterBms(c), normalizedClusterPcs(c));
  if($('clusterPowerMapMode')) $('clusterPowerMapMode').value = c.power_map ? 'manual' : 'even';
  if($('clusterPowerMapJson')) $('clusterPowerMapJson').value=JSON.stringify(c.power_map||{},null,2);
  refreshClusterPowerMapEditor();
}
async function saveClusterBinding(){
  if(!PROJECT) await loadProjectConfig();
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  if(!Array.isArray(data.clusters)) data.clusters=[];
  const name=String($('clusterCfgName').value||'').trim(); if(!name){ $('projectConfigResult').innerHTML='<span class="bad">Cluster name is required.</span>'; return; }
  let pmap=null;
  try{ pmap=selectedClusterPowerMap(); }catch(e){ $('projectConfigResult').innerHTML=`<span class="bad">${esc(e.message||e)}</span>`; return; }
  const _bms=getMultiSelectValues('clusterCfgBms'), _pcs=getMultiSelectValues('clusterCfgPcs'); const next={name, bms_devices:_bms, pcs_devices:_pcs, pcs_device:_pcs[0]||'', bms:_bms, pcs:_pcs};
  const old=data.clusters.find(x=>String(x.name)===name)||{};
  if(pmap===null){ if(old.power_map) next.power_map=old.power_map; }
  else { next.power_map=pmap; }
  const idx=data.clusters.findIndex(x=>String(x.name)===name); if(idx>=0) data.clusters[idx]=Object.assign({}, old, next); else data.clusters.push(next);
  await postJson('/api/site/config', data, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig();
}
async function saveBmsConfig(){ const payload={name:$('bmsCfgName').value,host:$('bmsCfgHost').value,port:Number($('bmsCfgPort').value||502),unit_id:Number($('bmsCfgUnit').value||1),interval:Number($('bmsCfgInterval').value||2),profile:$('bmsCfgProfile').value}; await postJson('/api/project/bms/upsert', payload, 'projectConfigResult'); await loadProjectConfig(); }
async function removeBmsByName(name){ if(!name || !confirm(`Remove BMS ${name}? This also removes it from cluster binding and power map references.`)) return; await postJson('/api/project/bms/remove', {name}, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig(); }
async function removeBmsConfig(){ const name=$('bmsCfgName').value; await removeBmsByName(name); }
async function savePcsConfig(){ const payload={name:$('pcsCfgName').value,host:$('pcsCfgHost').value,port:Number($('pcsCfgPort').value||502),unit_id:Number($('pcsCfgUnit').value||1),profile:$('pcsCfgProfile').value,enabled:true}; await postJson('/api/project/pcs/upsert', payload, 'projectConfigResult'); await loadProjectConfig(); }
async function removePcsByName(name){ if(!name || !confirm(`Remove PCS ${name}? This also removes it from cluster binding and power map references.`)) return; await postJson('/api/project/pcs/remove', {name}, 'projectConfigResult'); await loadProjectConfig(); await validateProjectConfig(); }
async function removePcsConfig(){ const name=$('pcsCfgName').value; await removePcsByName(name); }
function downloadProjectConfig(){ const text=JSON.stringify(PROJECT||{}, null, 2); const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([text], {type:'application/json'})); a.download='ess_aio_project_config_export.json'; a.click(); URL.revokeObjectURL(a.href); }
async function validateProjectConfig(){ const box=$('projectValidationBox'); if(box) box.textContent='Validating...'; try{ const r=await fetch('/api/project/validate',{cache:'no-store'}); const data=await r.json(); if(box) box.textContent=JSON.stringify(data,null,2); const target=$('projectConfigResult'); if(target) target.innerHTML=data.ok?'<span class="ok">Project validation passed.</span>':`<span class="bad">Project validation found ${esc((data.issues||[]).length)} error(s).</span>`; return data; }catch(e){ if(box) box.textContent=String(e); return {ok:false,error:String(e)}; } }


function pcsRows(){ const ds=(SNAP||{}).device_states||{}; return Object.values((ds.pcs||{})).sort((a,b)=>String(a.name).localeCompare(String(b.name))); }
function requireExecute(label){
  return confirm(`${label}\n\nThis may write to equipment or change dispatch state. Continue?`);
}
async function postJson(url, payload, targetId, method='POST'){
  const target = targetId ? $(targetId) : null;
  if(target) target.innerHTML = '<span class="muted">Sending...</span>';
  try{
    const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload||{})});
    const data = await r.json();
    const ok = r.ok && data.ok !== false;
    const risk = data.risk ? ` risk=${esc(data.risk)}` : '';
    const html = `${ok ? '<span class="ok">OK</span>' : '<span class="bad">BLOCKED/ERROR</span>'} <code>${esc(data.command_id||data.status||r.status)}</code>${risk} ${esc(data.message||data.detail||data.error||'')}`;
    if(target) target.innerHTML = html;
    await refreshNow(true);
    return data;
  }catch(e){ if(target) target.innerHTML = `<span class="bad">${esc(e)}</span>`; return {ok:false,error:String(e)}; }
}

function decodeDatasetValue(v){ return String(v ?? '').trim(); }
function actionButton(el){
  // Robustly resolve dynamic action buttons. In some browsers the click target can
  // be a text node inside the button; Text does not have closest(), which made
  // Edit/Clear/Remove look like they did nothing.
  let node = el;
  if(node && node.nodeType === 3) node = node.parentElement;
  while(node && node !== document){
    if(node.matches && node.matches('button[data-action]')) return node;
    node = node.parentElement;
  }
  return null;
}
function actionClick(ev){
  // Central dispatcher for dynamically rendered table buttons. Only cancel the
  // browser event after a real action button is found; otherwise normal clicks
  // must continue to work.
  const btn = actionButton(ev ? ev.target : null);
  if(!btn) return true;
  try{
    if(ev){
      ev.preventDefault();
      ev.stopPropagation();
      if(ev.stopImmediatePropagation) ev.stopImmediatePropagation();
    }
    dispatchActionButton(btn).catch(err=>{
      console.error('Action button failed', err);
      const target=$('projectConfigResult') || $('pmEditorResult');
      if(target) target.innerHTML = `<span class="bad">Action failed: ${esc(err && err.message ? err.message : err)}</span>`;
    });
  }catch(err){ console.error('Action click failed', err); }
  return false;
}
async function dispatchActionButton(btn){
  const action=btn.dataset.action||'';
  const name=decodeDatasetValue(btn.dataset.name);
  const cluster=decodeDatasetValue(btn.dataset.cluster);
  const kind=decodeDatasetValue(btn.dataset.kind);
  const value=decodeDatasetValue(btn.dataset.value);
  if(action==='fill-bms') return fillBmsConfig(name);
  if(action==='remove-bms') return removeBmsByName(name);
  if(action==='fill-pcs') return fillPcsConfig(name);
  if(action==='remove-pcs') return removePcsByName(name);
  if(action==='add-bind-pick') return addBindPick(cluster, kind);
  if(action==='remove-bind-chip') return removeBindChip(cluster, kind, value);
  if(action==='edit-power-map') return editRowPowerMapByName(cluster);
  if(action==='auto-even-power-map') return autoEvenPowerMapForCluster(cluster);
  if(action==='clear-power-map') return clearPowerMapAndSave(cluster);
  if(action==='save-cluster-binding') return saveClusterBindingByName(cluster);
  if(action==='remove-cluster') return removeClusterBindingRow(cluster);
  console.warn('Unknown action button', action, btn);
}
document.addEventListener('click', function(ev){
  const btn=actionButton(ev.target);
  if(btn) actionClick(ev);
}, false);

function bmsRows(){ return (((SNAP||{}).device_states||{}).bms) ? Object.entries(SNAP.device_states.bms).map(([name,v])=>Object.assign({name},v||{})) : []; }
let RACK_DATA=null; let RACK_ACTIONS={};
function populateOpsBmsDevices(){ if(!SNAP) return; const sel=$('opsBmsDevice'); if(!sel) return; const old=sel.value; const bms=bmsRows().map(x=>x.name).sort(); sel.innerHTML=bms.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(bms.includes(old)) sel.value=old; renderBmsControl(); renderBmsPresetRegisters(); }
function opsBmsName(){ return $('opsBmsDevice') ? $('opsBmsDevice').value : ''; }
function bmsScope(){ return $('bmsControlScope') ? $('bmsControlScope').value : 'single'; }
function syncBmsWriteScope(){ if($('bmsWriteScope') && $('bmsControlScope')) $('bmsWriteScope').value=$('bmsControlScope').value; }
function bmsStartAll(){ postJson('/api/bms/start-all', {}, 'opsCommandResult'); }
function bmsStopAll(){ if(!confirm('Stop all BMS polling?')) return; postJson('/api/bms/stop-all', {}, 'opsCommandResult'); }
function bmsByName(device, action, target='opsCommandResult'){ if(!device) return; const url=action==='start'?'/api/bms/start':'/api/bms/stop'; postJson(url, {device}, target); }
function bmsSingle(action){ const device=opsBmsName(); return bmsByName(device, action, 'opsCommandResult'); }
function bmsHvOptions(){ return {timeout:parseFloat($('bmsHvTimeout')?.value||'30'), poll_interval:parseFloat($('bmsHvPoll')?.value||'1'), ignore_pcs_precheck: !!($('bmsHvIgnorePcsPrecheck')?.checked), confirm_text:'EXECUTE'}; }
function bmsHvByName(device, mode){ if(!device) return; const opts=bmsHvOptions(); const note=opts.ignore_pcs_precheck?' (BMS-only / ignore PCS precheck)':''; if(!requireExecute(`Send HV ${mode.toUpperCase()} workflow to ${device}${note}?`)) return; postJson('/api/bms/hv', {device, mode, ...opts}, 'opsCommandResult'); }
function bmsHv(mode){ const device=opsBmsName(); if(!device){ $('opsCommandResult').innerHTML='<span class="bad">Select a BMS first.</span>'; return; } return bmsHvByName(device, mode); }
function bmsHvAll(mode){ const opts=bmsHvOptions(); const note=opts.ignore_pcs_precheck?' (BMS-only / ignore PCS precheck)':''; if(!requireExecute(`Send HV ${mode.toUpperCase()} workflow to all online BMS${note}?`)) return; postJson('/api/bms/hv-all', {mode, ...opts}, 'opsCommandResult'); }
function bmsHvScoped(mode){ return bmsScope()==='single' ? bmsHv(mode) : bmsHvAll(mode); }
async function bmsHeartbeat(start){ const box=$('bmsHeartbeatStatus'); if(box) box.innerHTML=start?'<span class="ok">Heartbeat start command sent. Periodic heartbeat is being queued by Runtime.</span>':'<span class="warn">Heartbeat stop command sent.</span>'; const data=await postJson(start?'/api/bms/heartbeat/start-all':'/api/bms/heartbeat/stop-all', {}, 'opsCommandResult'); if(box) box.innerHTML=(data.ok!==false?(start?'<span class="ok">Heartbeat active / start acknowledged.</span>':'<span class="warn">Heartbeat stopped / stop acknowledged.</span>'):'<span class="bad">Heartbeat command failed.</span>')+' <code>'+esc(data.command_id||data.status||'')+'</code>'; }
function bmsClearFaultAll(){ if(!requireExecute('Clear fault on all online BMS?')) return; postJson('/api/bms/command', {scope:'all_online', command:'clear_fault', confirm_text:'EXECUTE'}, 'opsCommandResult'); }
function bms038b(start){ postJson(start?'/api/bms/038b/start':'/api/bms/038b/stop', {}, 'opsCommandResult'); }
function parseAddrForApi(v){ const t=String(v||'').trim(); return t.toLowerCase().startsWith('0x') ? t : Number(t); }
function fillRtcNow(){ const d=new Date(); $('rtcYear').value=d.getFullYear(); $('rtcMonth').value=d.getMonth()+1; $('rtcDay').value=d.getDate(); $('rtcHour').value=d.getHours(); $('rtcMinute').value=d.getMinutes(); $('rtcSecond').value=d.getSeconds(); }
function bmsRegisterWrite(){ syncBmsWriteScope(); const scope=$('bmsWriteScope').value; const device=opsBmsName(); const address=parseAddrForApi($('bmsWriteAddress').value); const value=parseInt($('bmsWriteValue').value||'0'); if(scope==='single'&&!device) return; if(!requireExecute(`Write BMS register ${$('bmsWriteAddress').value}=${value} scope=${scope}?`)) return; postJson('/api/bms/register-write', {device, scope, address, value, confirm_text:'EXECUTE'}, 'opsCommandResult'); }
function bmsRtcWrite(){ syncBmsWriteScope(); const scope=$('bmsWriteScope').value; const device=opsBmsName(); if(scope==='single'&&!device) return; const payload={device, scope, year:+$('rtcYear').value, month:+$('rtcMonth').value, day:+$('rtcDay').value, hour:+$('rtcHour').value, minute:+$('rtcMinute').value, second:+$('rtcSecond').value, confirm_text:'EXECUTE'}; if(!requireExecute(`Write RTC to ${scope==='single'?device:'all online BMS'}?`)) return; postJson('/api/bms/rtc-write', payload, 'opsCommandResult'); }
function bmsQuickCommand(command){ syncBmsWriteScope(); const scope=bmsScope(); const device=opsBmsName(); if(scope==='single'&&!device) return; if(!requireExecute(`Send BMS command ${command} to ${scope==='single'?device:scope}?`)) return; postJson('/api/bms/command', {device, scope, command, confirm_text:'EXECUTE'}, 'opsCommandResult'); }
const BMS_PRESET_REGS=[
  ['0x0381','EMS command / HV request','1'],['0x038B','Insulation monitor disable','2'],['0x0380','EMS heartbeat manual value','1'],['0x0382','RTC year','2026'],['0x0383','RTC month','1'],['0x0384','RTC day','1'],['0x0385','RTC hour','0'],['0x0386','RTC minute','0'],['0x0387','RTC second','0'],['0x0388','Reserved control 0388','0'],['0x0389','Reserved control 0389','0'],['0x038A','Reserved control 038A','0'],['0x038C','Fault Clear cmd (pulse 1 then 0)','1'],['0x038D','Reserved control 038D','0'],['0x038E','Reserved control 038E','0'],['0x038F','Reserved control 038F','0'],['0x0390','Reserved control 0390','0'],['0x0391','Reserved control 0391','0'],['0x0392','Reserved control 0392','0'],['0x0393','Reserved control 0393','0'],['0x0394','Reserved control 0394','0']
];
function renderBmsPresetRegisters(){ const tb=$('bmsPresetRows'); if(!tb) return; tb.innerHTML=BMS_PRESET_REGS.map((r,i)=>`<tr><td><code>${r[0]}</code></td><td>${esc(r[1])}</td><td><input id="bmsPresetVal${i}" type="number" value="${esc(r[2])}" /></td><td><button onclick="setBmsManual('${r[0]}','bmsPresetVal${i}')">Use</button></td><td><button onclick="bmsPresetWrite('${r[0]}','bmsPresetVal${i}')">Write</button></td></tr>`).join(''); }
function setBmsManual(addr,inputId){ $('bmsWriteAddress').value=addr; $('bmsWriteValue').value=$(inputId).value; }
function bmsPresetWrite(addr,inputId){ $('bmsWriteAddress').value=addr; $('bmsWriteValue').value=$(inputId).value; bmsRegisterWrite(); }
function bmsMetric(vals, keys){ vals=vals||{}; for(const k of keys){ if(vals[k]!==undefined&&vals[k]!==null&&vals[k]!=='' ){ return vals[k]; } } return '-'; }
function renderBmsControl(){
  if(!SNAP) return;
  const rows=bmsRows();
  const online=rows.filter(d=>d.online || d.connection==='online').length;
  const statusCounts={normal:0,fullCharge:0,fullDischarge:0,warning:0,fault:0,unknown:0};
  rows.forEach(d=>{ const vals=d.latest_values||d.snapshot||{}; const label=bmsStatusFromDevice(d, vals); const l=String(label).toLowerCase(); if(l.includes('fault')) statusCounts.fault++; else if(l.includes('warning')) statusCounts.warning++; else if(l.includes('full charge')) statusCounts.fullCharge++; else if(l.includes('full discharge')) statusCounts.fullDischarge++; else if(l.includes('normal')) statusCounts.normal++; else statusCounts.unknown++; });
  if($('bmsControlCards')) $('bmsControlCards').innerHTML=[
    ['BMS Total',rows.length,''],['Online',online,online===rows.length?'ok':'warn'],['Normal',statusCounts.normal,'ok'],['Warning/Fault',statusCounts.warning+statusCounts.fault,(statusCounts.warning+statusCounts.fault)?'bad':'ok']
  ].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join('');
  if($('bmsControlCount')) $('bmsControlCount').textContent=`${rows.length} BMS`;
  if($('bmsControlRows')) $('bmsControlRows').innerHTML=rows.map(d=>{
    const vals=d.latest_values||d.snapshot||{};
    const isSel=$('opsBmsDevice') && $('opsBmsDevice').value===d.name;
    const soc=metricFromDevice(d, vals, ['soc','SOC','soc_value','system_soc']);
    const voltage=metricFromDevice(d, vals, ['voltage','system_voltage','total_voltage','dc_voltage','pack_voltage']);
    const current=metricFromDevice(d, vals, ['current','system_current','dc_current','pack_current']);
    const power=metricFromDevice(d, vals, ['power','active_power','dc_power','system_power']);
    const statusLabel=bmsStatusFromDevice(d, vals);
    const stClass=bmsStatusClass(statusLabel);
    return `<tr class="${isSel?'selected-row':''}" onclick="if($('opsBmsDevice')){$('opsBmsDevice').value='${esc(d.name)}'; renderBmsControl(); renderBmsPresetRegisters();}"><td>${esc(d.name)}</td><td>${pill(d.connection||'')}</td><td>${fmtMetric(soc,'%',1)}</td><td>${fmtMetric(voltage,' V',1)}</td><td>${fmtMetric(current,' A',1)}</td><td>${fmtMetric(power,' kW',1)}</td><td><span class="pill ${esc(stClass)}">${esc(statusLabel)}</span></td><td>${esc(d.updated_at||d.last_update||d.last_seen||'-')}</td><td>${esc(d.last_message||'')}</td><td><button onclick="event.stopPropagation(); bmsByName('${esc(d.name)}','start')">Connect</button> <button onclick="event.stopPropagation(); bmsByName('${esc(d.name)}','stop')">Disconnect</button> <button onclick="event.stopPropagation(); bmsHvByName('${esc(d.name)}','on')">HV ON</button> <button onclick="event.stopPropagation(); bmsHvByName('${esc(d.name)}','off')">HV OFF</button></td></tr>`;
  }).join('') || '<tr><td colspan="10" class="muted">No BMS devices</td></tr>';
}
async function csvBms(start){ const device=opsBmsName(); const payload=device?{devices:[device]}:{devices:[]}; const data=await postJson(start?'/api/csv/bms/start':'/api/csv/bms/stop', payload, 'opsCommandResult'); $('opsCsvStatus').textContent=JSON.stringify(data.recording||data, null, 2); }
async function csvPcs(start){ const data=await postJson(start?'/api/csv/pcs/start':'/api/csv/pcs/stop', {devices:[]}, 'opsCommandResult'); $('opsCsvStatus').textContent=JSON.stringify(data.recording||data, null, 2); }
async function soakStart(){ const label=$('soakLabel').value||'field-soak'; const interval_s=parseFloat($('soakInterval').value||'60'); const data=await postJson('/api/soak/start', {label, interval_s}, 'opsCommandResult'); $('soakOpsBox').textContent=JSON.stringify(data, null, 2); }
async function soakStop(){ const data=await postJson('/api/soak/stop', {}, 'opsCommandResult'); $('soakOpsBox').textContent=JSON.stringify(data, null, 2); }
async function soakReport(){ try{ const r=await fetch('/api/soak/report', {cache:'no-store'}); $('soakOpsBox').textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ $('soakOpsBox').textContent=String(e); } }
async function loadOperationLog(){ try{ const r=await fetch('/api/logs/operation/recent?max_lines=120', {cache:'no-store'}); const data=await r.json(); $('operationLogBox').textContent=(data.lines||[]).join('\n') || JSON.stringify(data,null,2); }catch(e){ $('operationLogBox').textContent=String(e); } }
function populatePcsSelected(){ if(!SNAP) return; const sel=$('pcsSelected'); if(!sel) return; const old=sel.value; const names=pcsRows().map(x=>x.name).filter(Boolean).sort(); sel.innerHTML=names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(names.includes(old)) sel.value=old; renderPcsCards(); }
function selectedPcs(){ return $('pcsSelected') ? $('pcsSelected').value : ''; }
function renderPcsCards(){ const rows=pcsRows(); const online=rows.filter(d=>d.online || d.connection==='online').length; if($('pcsControlCards')) $('pcsControlCards').innerHTML=[['PCS Total',rows.length,''],['Online',online,online===rows.length?'ok':'warn'],['Running',((SNAP?.workers||{}).pcs_running||[]).length,'accent'],['Errors',rows.filter(d=>d.error||d.errors).length,'bad']].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join(''); }
function renderPCS(){ if(!SNAP) return; populatePcsSelected(); const rows=pcsRows(); $('pcsCount').textContent=`${rows.length} PCS`; $('pcsRows').innerHTML=rows.map(d=>{ const vals=d.latest_values||{}; const isSel=selectedPcs()===d.name; return `<tr class="${isSel?'selected-row':''}" onclick="if($('pcsSelected')){$('pcsSelected').value='${esc(d.name)}'; renderPCS();}"><td>${esc(d.name)}</td><td>${pill(d.connection)}</td><td>${esc(d.status||'')}</td><td>${esc(d.errors||0)}</td><td><div><b>SOC</b> ${esc(bmsMetric(vals,['soc','SOC','soc_value']))}</div><div><b>V</b> ${esc(bmsMetric(vals,['voltage','system_voltage','total_voltage','dc_voltage']))}</div><div><b>I</b> ${esc(bmsMetric(vals,['current','system_current','dc_current']))}</div><div><b>Status</b> ${esc(d.status||bmsMetric(vals,['status','state']))}</div></td><td>${esc(d.last_message||'')}</td><td><button onclick="event.stopPropagation(); pcsSingle('${esc(d.name)}','connect')">Connect</button> <button onclick="event.stopPropagation(); pcsSingle('${esc(d.name)}','stop')">Disconnect</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','start')">Start</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','stop')">Stop</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','close_dc_breaker')">Close DC</button> <button onclick="event.stopPropagation(); pcsOneCommand('${esc(d.name)}','open_dc_breaker')">Open DC</button></td></tr>`}).join('') || '<tr><td colspan="7" class="muted">No PCS devices</td></tr>'; renderPcsCards(); }

function renderPcsAlarms(){
  const tb=$('pcsAlarmRows'); if(!tb) return;
  const rows=pcsRows().flatMap(d=>{
    const vals=d.latest_values||d.snapshot||{}; const items=[];
    if(d.error||d.errors) items.push({name:d.name,severity:'alarm',status:d.status||d.connection||'',message:d.last_message||d.error||`${d.errors} error(s)`});
    for(const [k,v] of Object.entries(vals||{})){ const kl=String(k).toLowerCase(); if((kl.includes('alarm')||kl.includes('fault')||kl.includes('warning')) && String(v)!=='0' && String(v)!=='false' && String(v)!=='') items.push({name:d.name,severity:kl.includes('alarm')||kl.includes('fault')?'alarm':'warning',status:k,message:String(v)}); }
    return items;
  });
  tb.innerHTML=rows.map(a=>`<tr><td>${esc(a.name)}</td><td>${esc(a.severity)}</td><td>${esc(a.status)}</td><td>${esc(a.message)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">No PCS alarm/fault data detected.</td></tr>';
}
function pcsConnectAll(){ postJson('/api/pcs/connect-all', {}, 'pcsCommandResult'); }
function pcsStopAll(){ postJson('/api/pcs/stop-all', {}, 'pcsCommandResult'); }
function pcsFleetCommand(method){ if(!requireExecute(`Send ${method} to all online PCS?`)) return; postJson('/api/pcs/fleet-command', {method, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsFleetPower(method,inputId){ const value=parseFloat($(inputId).value||'0'); if(!requireExecute(`Send ${method}=${value} to fleet?`)) return; postJson('/api/pcs/fleet-command', {method,value, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsSingle(pcs, action, target='pcsCommandResult'){ const url= action==='connect' ? '/api/pcs/connect' : '/api/pcs/stop'; postJson(url, {pcs}, target); }
function pcsSingleSelected(action){ const pcs=selectedPcs(); if(!pcs) return; pcsSingle(pcs, action); }
function pcsOneCommand(pcs, method){ if(!requireExecute(`Send ${method} to ${pcs}?`)) return; postJson('/api/pcs/command', {pcs, method, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsOneCommandSelected(method){ const pcs=selectedPcs(); if(!pcs) return; pcsOneCommand(pcs, method); }
function pcsPowerSelected(method,inputId){ const pcs=selectedPcs(); if(!pcs) return; const value=parseFloat($(inputId).value||'0'); if(!requireExecute(`Send ${method}=${value} to ${pcs}?`)) return; postJson('/api/pcs/command', {pcs, method, value, confirm_text:'EXECUTE'}, 'pcsCommandResult'); }
function pcsCustomSelected(){ const pcs=selectedPcs(); const method=($('pcsCustomMethod').value||'').trim(); if(!pcs||!method) return; const raw=($('pcsCustomValue').value||'').trim(); const payload={pcs, method, confirm_text:'EXECUTE'}; if(raw!=='') payload.value=parseFloat(raw); if(!requireExecute(`Send custom PCS command ${method} to ${pcs}?`)) return; postJson('/api/pcs/command', payload, 'pcsCommandResult'); }
let STRATEGY_CENTER=null;
function populateStrategyClusters(){ if(!SNAP) return; const sel=$('strategyCluster'); const old=sel.value; const clusters=(SNAP.clusters||[]).map(c=>c.name).sort(); sel.innerHTML=clusters.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(clusters.includes(old)) sel.value=old; renderStrategy(); loadStrategyCenter(); }
function populateStrategyClustersOnce(){ const sel=$('strategyCluster'); if(!sel || sel.options.length || !SNAP) return; const clusters=(SNAP.clusters||[]).map(c=>c.name).sort(); sel.innerHTML=clusters.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); }
async function loadStrategyCenter(){ try{ const r=await fetch('/api/strategy-center',{cache:'no-store'}); STRATEGY_CENTER=await r.json(); renderStrategy(); }catch(e){ const box=$('strategyIssuesBox'); if(box) box.textContent=String(e); } }
function renderStrategy(){ if(!SNAP) return; populateStrategyClustersOnce(); const running=new Set(((SNAP.workers||{}).strategies)||[]); const sc=STRATEGY_CENTER||{}; const rows=(sc.clusters||SNAP.clusters||[]); if($('strategyCards')){ const prof=sc.profile_strategy||{}; $('strategyCards').innerHTML=[['Profile', prof.enabled===false?'Disabled':'Enabled', prof.enabled===false?'warn':'ok'],['Running', sc.strategies_running??running.size, 'accent'],['Ready clusters', `${sc.clusters_ready??'-'}/${sc.clusters_total??rows.length}`, ''],['Issues', (sc.issues||[]).length, (sc.issues||[]).length?'warn':'ok']].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join(''); } if($('strategyIssuesBox')){ $('strategyIssuesBox').textContent=JSON.stringify({profile:sc.profile_strategy||{}, issues:sc.issues||[], safety_note:sc.safety_note||''}, null, 2); } $('strategyRows').innerHTML=rows.map(c=>{ const name=c.cluster||c.name||''; return `<tr><td>${esc(name)}</td><td>${pill(c.health||'ready')}</td><td>${esc((c.bms_devices||[]).join(', '))}</td><td>${esc((c.pcs_devices||[]).join(', '))}</td><td>${esc(c.allocation_mode||'')}</td><td><code>${esc(JSON.stringify(c.power_map||{}))}</code></td><td>${(c.running||running.has(name))?pill('running'):pill('stopped')}</td></tr>` }).join('') || '<tr><td colspan="7" class="muted">No clusters</td></tr>'; if($('strategyCommandRows')){ $('strategyCommandRows').innerHTML=(sc.recent_strategy_commands||[]).map(c=>`<tr><td>${fmtTs(c.updated_ts||c.created_ts)}</td><td>${esc(c.name)}</td><td>${esc(c.risk||'low')}</td><td>${pill(c.status)}</td><td>${esc(c.ok)}</td><td>${esc(c.message||'')}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">No strategy commands</td></tr>'; } }
function strategyClusterName(){ return $('strategyCluster').value; }
async function loadStrategyConfig(){ const box=$('strategyConfigResult'); if(box) box.textContent='Loading...'; try{ const r=await fetch('/api/strategy/config',{cache:'no-store'}); const data=await r.json(); $('strategyConfigEditor').value=JSON.stringify(data.config||{}, null, 2); if(box) box.innerHTML=data.ok?`<span class="ok">Loaded ${esc(data.path||'strategy.json')}</span>`:`<span class="bad">${esc(data.error||'load failed')}</span>`; }catch(e){ if(box) box.innerHTML=`<span class="bad">${esc(e)}</span>`; } }
async function saveStrategyConfig(){ const box=$('strategyConfigResult'); try{ const config=JSON.parse($('strategyConfigEditor').value||'{}'); await postJson('/api/strategy/config', {config}, 'strategyConfigResult', 'PUT'); await loadStrategyCenter(); }catch(e){ if(box) box.innerHTML=`<span class="bad">Invalid JSON: ${esc(e)}</span>`; } }
async function strategyApplySettings(){ const cluster=strategyClusterName(); if(!cluster) return; const payload={cluster, mode:$('strategyMode').value, target_power_kw:parseFloat($('strategyTargetPower').value||'0'), ramp_step_kw:parseFloat($('strategyRampStep').value||'50'), ramp_interval_s:parseFloat($('strategyRampInterval').value||'5'), bms_timeout_s:parseFloat($('strategyTimeout').value||'5')}; await postJson('/api/cluster/strategy-settings', payload, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStart(){ const cluster=strategyClusterName(); if(!cluster) return; await strategyApplySettings(); if(!requireExecute(`Start strategy for ${cluster}?`)) return; await postJson('/api/strategy/start', {cluster, confirm_text:'EXECUTE'}, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStop(){ const cluster=strategyClusterName(); if(!cluster) return; if(!confirm(`Stop strategy for ${cluster}?`)) return; await postJson('/api/strategy/stop', {cluster}, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStartAll(){ if(!requireExecute('Start strategy for all configured clusters?')) return; await postJson('/api/strategy/start-all', {confirm_text:'EXECUTE'}, 'strategyCommandResult'); await loadStrategyCenter(); }
async function strategyStopAll(){ if(!confirm('Stop strategy for all configured clusters?')) return; await postJson('/api/strategy/stop-all', {}, 'strategyCommandResult'); await loadStrategyCenter(); }
function renderOverview(){
  if(!SNAP) return;
  const summary=SNAP.summary||{};
  if($('cards')) $('cards').innerHTML=cardsHtml(summary,SNAP);
  const alarms=overviewAlarmItems();
  const workers=SNAP.workers||{};
  const bmsTotal=Number(summary.bms_total||0), bmsOnline=Number(summary.bms_online||0);
  const pcsTotal=Number(summary.pcs_total||0), pcsOnline=Number(summary.pcs_online||0);
  const alarmCount=alarms.filter(a=>String(a.severity||'').toLowerCase().includes('alarm')).length;
  const warnCount=alarms.filter(a=>String(a.severity||'').toLowerCase().includes('warn')).length;
  const siteState = alarmCount ? 'Fault' : (warnCount ? 'Warning' : (bmsOnline+pcsOnline>0 ? 'Running' : 'Standby'));
  const stateClass = siteState==='Fault'?'bad':(siteState==='Warning'?'warn':(siteState==='Running'?'ok':''));
  const dashboardHtml = `
    <div class="dashboard-cards">
      <div class="metric-card hero"><div class="metric-label">Site state</div><div class="metric-value ${stateClass}">${esc(siteState)}</div><div class="metric-foot">Uptime ${esc(SNAP.uptime_s||0)}s</div></div>
      <div class="metric-card"><div class="metric-label">BMS online</div><div class="metric-value ${bmsOnline===bmsTotal?'ok':'warn'}">${esc(bmsOnline)}/${esc(bmsTotal)}</div><div class="metric-foot">Running ${(workers.bms_running||[]).length}</div></div>
      <div class="metric-card"><div class="metric-label">PCS online</div><div class="metric-value ${pcsOnline===pcsTotal?'ok':'warn'}">${esc(pcsOnline)}/${esc(pcsTotal)}</div><div class="metric-foot">Running ${(workers.pcs_running||[]).length}</div></div>
      <div class="metric-card"><div class="metric-label">Active issues</div><div class="metric-value ${alarms.length?'bad':'ok'}">${esc(alarms.length)}</div><div class="metric-foot">Alarm ${alarmCount} · Warning ${warnCount}</div></div>
      <div class="metric-card"><div class="metric-label">CSV</div><div class="metric-value">${esc(csvStatusText())}</div><div class="metric-foot">Recorder status</div></div>
      <div class="metric-card"><div class="metric-label">Strategy</div><div class="metric-value accent">${esc((workers.strategies||[]).length)}</div><div class="metric-foot">Active strategy workers</div></div>
    </div>
    <div class="dashboard-charts">
      <div class="chart-card"><div class="chart-title">Device online distribution</div>${dashboardBar('BMS online', bmsOnline, bmsTotal, 'ok')}${dashboardBar('PCS online', pcsOnline, pcsTotal, 'accent')}</div>
      <div class="chart-card"><div class="chart-title">Alarm / warning distribution</div>${dashboardSeverityBar('Alarm', alarmCount, Math.max(alarms.length,1), 'bad')}${dashboardSeverityBar('Warning', warnCount, Math.max(alarms.length,1), 'warn')}${dashboardSeverityBar('Normal', Math.max((bmsTotal+pcsTotal)-alarms.length,0), Math.max(bmsTotal+pcsTotal,1), 'ok')}</div>
      <div class="chart-card"><div class="chart-title">Runtime activity</div>${dashboardSeverityBar('BMS workers', (workers.bms_running||[]).length, Math.max(bmsTotal,1), 'ok')}${dashboardSeverityBar('PCS workers', (workers.pcs_running||[]).length, Math.max(pcsTotal,1), 'accent')}${dashboardSeverityBar('Strategies', (workers.strategies||[]).length, Math.max((SNAP.clusters||[]).length,1), 'warn')}</div>
    </div>`;
  if($('overviewDashboard')) $('overviewDashboard').innerHTML=dashboardHtml;
  if($('overviewAlarmRows')) $('overviewAlarmRows').innerHTML=alarms.slice(0,12).map(a=>`<tr><td>${esc(a.severity||'')}</td><td>${esc(a.area||a.kind||'')}</td><td>${esc(a.device||'')}</td><td>${esc(a.message||a.key||a.status||'')}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">No active runtime alarms or issues.</td></tr>';
  if($('runtimeSummary')) $('runtimeSummary').textContent=JSON.stringify({api_schema:SNAP.api_schema, uptime_s:SNAP.uptime_s, workers:SNAP.workers, summary:SNAP.summary, recording:SNAP.recording}, null, 2);
  if($('soakSummary')) $('soakSummary').textContent=JSON.stringify(SNAP.soak_test||{}, null, 2);
}
function csvStatusText(){ const r=SNAP?.recording||{}; const on=[]; if(r.bms_csv) on.push('BMS'); if(r.pcs_csv) on.push('PCS'); return on.length?on.join('+'):'idle'; }
function overviewAlarmItems(){ const out=[]; const ds=(SNAP?.device_states)||{}; for(const [kind,map] of Object.entries({bms:ds.bms||{}, pcs:ds.pcs||{}})){ for(const [name,d] of Object.entries(map||{})){ if(d.error||d.errors){ out.push({severity:'alarm',area:kind,device:name,message:d.last_message||d.error||`${d.errors} error(s)`}); } if(d.online===false||d.connection==='offline'){ out.push({severity:'warning',area:kind,device:name,message:'offline'}); } } } return out; }
function renderDevices(){ if(!SNAP) return; const q=($('deviceFilter')?.value||'').toLowerCase(); const tf=$('typeFilter')?.value||'all'; const rows=deviceRows().filter(d=>(tf==='all'||d._type===tf) && (!q || String(d.name).toLowerCase().includes(q) || String(d.connection).toLowerCase().includes(q) || String(d.last_message).toLowerCase().includes(q))); $('devices').innerHTML=rows.map(d=>{ const isSel=SELECTED_DEVICE.kind===d._type && SELECTED_DEVICE.name===d.name; return `<tr class="clickable-row ${isSel?'selected-row':''}" onclick="selectDevice('${esc(d._type)}','${esc(d.name)}')"><td>${esc(d._type)}</td><td>${esc(d.name)}</td><td>${pill(d.connection)}</td><td>${esc(d.status)}</td><td>${esc(d.errors||0)}</td><td>${esc(d.last_latency_ms||0)}</td><td>${esc(d.last_message||'')}</td></tr>`; }).join('') || '<tr><td colspan="7" class="muted">No devices</td></tr>'; $('deviceCount').textContent=`${rows.length} rows`; }
async function selectDevice(kind,name){ SELECTED_DEVICE={kind,name}; renderDevices(); if($('selectedDeviceTitle')) $('selectedDeviceTitle').textContent=`${kind} ${name}`; if($('deviceSnapshot')) $('deviceSnapshot').textContent='Loading...'; try{ const r=await fetch(`/api/device/${kind.toLowerCase()}/${encodeURIComponent(name)}/snapshot`, {cache:'no-store'}); if($('deviceSnapshot')) $('deviceSnapshot').textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ if($('deviceSnapshot')) $('deviceSnapshot').textContent=String(e); } }
function populateAlarmDevices(){ if(!SNAP) return; const sel=$('alarmDevice'); const old=sel.value; const bms=Object.keys(((SNAP.device_states||{}).bms)||{}).sort(); sel.innerHTML=bms.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(bms.includes(old)) sel.value=old; }
async function loadAlarms(){ const name=$('alarmDevice').value; if(!name){ $('alarms').innerHTML='<tr><td colspan="3" class="muted">No BMS selected</td></tr>'; return; } $('alarms').innerHTML='<tr><td colspan="3" class="muted">Loading...</td></tr>'; try{ const r=await fetch(`/api/device/bms/${encodeURIComponent(name)}/alarms`, {cache:'no-store'}); const data=await r.json(); const active=(data.alarms||[]).filter(a=>(a.active||[]).length); $('alarms').innerHTML=(active.length?active:(data.alarms||[]).slice(0,32)).map(a=>`<tr><td>${esc(a.address)}</td><td>${esc(a.raw)}</td><td>${esc((a.active||[]).join('\n'))}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">No alarm data</td></tr>'; }catch(e){ $('alarms').innerHTML=`<tr><td colspan="3" class="bad">${esc(e)}</td></tr>`; } }
function renderClusters(){ if(!SNAP) return; $('clusters').innerHTML=(SNAP.clusters||[]).map(c=>`<tr><td>${esc(c.name)}</td><td>${esc((c.bms_devices||[]).join(', '))}</td><td>${esc((c.pcs_devices||[]).join(', '))}</td><td>${esc(c.allocation_mode||'')}</td><td><code>${esc(JSON.stringify(c.power_map||{}))}</code></td></tr>`).join('') || '<tr><td colspan="5" class="muted">No clusters</td></tr>'; }
function renderCommands(){ if(!SNAP) return; $('commands').innerHTML=(SNAP.command_acks||[]).map(c=>`<tr><td>${fmtTs(c.updated_ts||c.created_ts)}</td><td><code>${esc(c.command_id)}</code></td><td>${esc(c.name)}</td><td>${esc(c.risk||'low')}</td><td>${esc(c.confirmed)}</td><td>${pill(c.status)}</td><td>${esc(c.ok)}</td><td>${esc(c.message||'')}</td></tr>`).join('') || '<tr><td colspan="8" class="muted">No commands</td></tr>'; }
async function loadCommandAudit(){ const box=$('commandAuditBox'); if(!box) return; box.textContent='Loading...'; try{ const r=await fetch('/api/commands/audit-summary',{cache:'no-store'}); box.textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ box.textContent=String(e); } }
async function loadMetrics(){ try{ const r=await fetch('/api/runtime/metrics', {cache:'no-store'}); const data=await r.json(); if($('metricsBox')) $('metricsBox').textContent=JSON.stringify(data, null, 2); if($('settingsMetrics')) $('settingsMetrics').textContent=JSON.stringify(data, null, 2); }catch(e){ if($('metricsBox')) $('metricsBox').textContent=String(e); if($('settingsMetrics')) $('settingsMetrics').textContent=String(e); } }
async function loadRestorePlan(){ try{ const r=await fetch('/api/runtime/restore-plan', {cache:'no-store'}); $('restoreBox').textContent=JSON.stringify(await r.json(), null, 2); }catch(e){ $('restoreBox').textContent=String(e); } }
async function loadLogsStatus(){ try{ const r=await fetch('/api/logs/status', {cache:'no-store'}); const data=await r.json(); const old=$('settingsCsvLogs').textContent||''; $('settingsCsvLogs').textContent=`Logs:\n${JSON.stringify(data,null,2)}\n\n${old}`; }catch(e){ $('settingsCsvLogs').textContent=String(e); } }
async function loadCsvStatus(){ try{ const r=await fetch('/api/csv/status', {cache:'no-store'}); const data=await r.json(); if($('settingsCsvLogs')) $('settingsCsvLogs').textContent=`CSV:\n${JSON.stringify(data,null,2)}\n\n${$('settingsCsvLogs').textContent||''}`; if($('opsCsvStatus')) $('opsCsvStatus').textContent=JSON.stringify(data,null,2); }catch(e){ if($('settingsCsvLogs')) $('settingsCsvLogs').textContent=String(e); if($('opsCsvStatus')) $('opsCsvStatus').textContent=String(e); } }
function renderSettings(){ if(!SNAP) return; if($('settingsSchema')) $('settingsSchema').textContent=SNAP.api_schema||'-'; if($('settingsPid')) $('settingsPid').textContent=((SNAP.runtime||{}).pid || SNAP.pid || '-'); if($('settingsUptime')) $('settingsUptime').textContent=(SNAP.uptime_s||0)+'s'; if(SNAP.metrics && $('settingsMetrics') && $('settingsMetrics').textContent==='-') $('settingsMetrics').textContent=JSON.stringify(SNAP.metrics,null,2); }
async function loadRuntimeSettings(){
  renderSettings();
  const target=$('runtimeSettingsResult'); if(target) target.textContent='Loading runtime settings...';
  try{
    const r=await fetch('/api/runtime/settings',{cache:'no-store'}); const data=await r.json();
    if(target) target.innerHTML=data.ok?`<span class="ok">Loaded from ${esc(data.path||'-')}</span>`:`<span class="bad">${esc(data.error||'failed')}</span>`;
    const rows=(data.rows||[]).map(row=>{
      const key=String(row.key||''); const typ=String(row.type||'string'); const editable=!!row.editable;
      let input='';
      if(editable){
        if(typ==='boolean') input=`<select data-runtime-key="${esc(key)}"><option value="true" ${row.value===true?'selected':''}>true</option><option value="false" ${row.value===false?'selected':''}>false</option></select>`;
        else input=`<input data-runtime-key="${esc(key)}" value="${esc(row.value??'')}" />`;
      } else input=`<code>${esc(row.value??'')}</code>`;
      return `<tr><td><code>${esc(key)}</code></td><td>${input}</td><td>${esc(row.source||'')}</td><td>${editable?'yes':'no'}</td><td>${row.restart_required?'required':'no'}</td><td class="muted">${esc(row.description||'')}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No runtime settings found.</td></tr>';
    if($('runtimeSettingsRows')) $('runtimeSettingsRows').innerHTML=rows;
  }catch(e){ if(target) target.innerHTML=`<span class="bad">${esc(e)}</span>`; }
}
async function saveRuntimeSettingsFromTable(){
  const settings={};
  document.querySelectorAll('[data-runtime-key]').forEach(el=>{ const key=el.getAttribute('data-runtime-key'); settings[key]=el.value; });
  if(!confirm('Save editable runtime settings? Some values may require Runtime restart.')) return;
  await postJson('/api/runtime/settings', {settings}, 'runtimeSettingsResult');
  await loadRuntimeSettings();
}

function powerMapProjectClusters(){
  return ((PROJECT && PROJECT.site_config && PROJECT.site_config.clusters) || []).map((c,idx)=>{
    const name=String(c.name||`Cluster-${idx+1}`);
    const bms=normalizedClusterBms(c);
    const pcs=normalizedClusterPcs(c);
    const pmap=normalizePowerMap(c.power_map||{}, bms, pcs);
    return {name,bms,pcs,power_map:pmap, raw:c};
  });
}
async function loadPowerMapEditor(){
  try{
    const r=await fetch('/api/project/config',{cache:'no-store'});
    PROJECT=await r.json();
    const clusters=powerMapProjectClusters();
    const sel=$('pmClusterSelect');
    if(sel){
      const old=sel.value;
      sel.innerHTML=clusters.map(c=>`<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');
      if(clusters.some(c=>c.name===old)) sel.value=old;
    }
    renderPowerMapEditorForSelected();
  }catch(e){ if($('pmEditorResult')) $('pmEditorResult').innerHTML=`<span class="bad">${esc(e)}</span>`; }
}
function selectedPowerMapCluster(){
  const name=String($('pmClusterSelect')?.value||'');
  return powerMapProjectClusters().find(c=>c.name===name) || powerMapProjectClusters()[0] || null;
}
function renderPowerMapEditorForSelected(){
  const c=selectedPowerMapCluster();
  const head=$('pmEditorHead'), rows=$('pmEditorRows'), summary=$('pmEditorSummary');
  if(!head || !rows) return;
  if(!c){
    head.innerHTML=''; rows.innerHTML='<tr><td class="muted">No clusters configured. Add clusters and bind BMS/PCS in Project → Cluster Binding first.</td></tr>';
    if(summary) summary.textContent='No cluster.';
    return;
  }
  if(summary) summary.textContent=`${c.name}: ${c.pcs.length} PCS × ${c.bms.length} BMS`;
  if(!c.bms.length || !c.pcs.length){
    head.innerHTML=''; rows.innerHTML='<tr><td class="warn">Bind at least one BMS and one PCS before editing Power Map.</td></tr>';
    return;
  }
  const pm=normalizePowerMap(c.power_map||{}, c.bms, c.pcs);
  head.innerHTML=`<tr><th>PCS \ BMS</th>${c.bms.map(b=>`<th>${esc(b)}</th>`).join('')}<th>Row Sum</th></tr>`;
  rows.innerHTML=c.pcs.map(pc=>{
    const row=pm[pc]||{};
    const sum=c.bms.reduce((a,b)=>a+Number(row[b]||0),0);
    return `<tr data-pm-pcs="${esc(pc)}"><td><b>${esc(pc)}</b></td>${c.bms.map(b=>`<td><input class="pm-weight" data-pm-bms="${esc(b)}" value="${esc(Number(row[b]??0).toFixed(6).replace(/\.0+$/,'').replace(/(\.\d*?)0+$/,'$1'))}" onfocus="touchPowerMapEditor()" oninput="updatePowerMapRowSums()" /></td>`).join('')}<td class="pm-row-sum ${Math.abs(sum-1)<=0.001?'oksum':'badsum'}">${sum.toFixed(6)}</td></tr>`;
  }).join('');
  updatePowerMapRowSums();
}
function touchPowerMapEditor(){ touchClusterBindingEditor(90000); }
function collectPowerMapEditor(){
  const c=selectedPowerMapCluster();
  if(!c) return null;
  const out={};
  document.querySelectorAll('#pmEditorRows tr[data-pm-pcs]').forEach(tr=>{
    const pc=tr.getAttribute('data-pm-pcs');
    const row={};
    tr.querySelectorAll('input[data-pm-bms]').forEach(inp=>{
      const bm=inp.getAttribute('data-pm-bms');
      const n=Number(inp.value);
      if(Number.isFinite(n) && n>=0) row[bm]=n;
    });
    out[pc]=row;
  });
  return {cluster:c, power_map:normalizePowerMap(out, c.bms, c.pcs)};
}
function updatePowerMapRowSums(){
  document.querySelectorAll('#pmEditorRows tr[data-pm-pcs]').forEach(tr=>{
    let sum=0;
    tr.querySelectorAll('input[data-pm-bms]').forEach(inp=>{ const n=Number(inp.value); if(Number.isFinite(n)) sum+=n; });
    const cell=tr.querySelector('.pm-row-sum');
    if(cell){ cell.textContent=sum.toFixed(6); cell.classList.toggle('oksum', Math.abs(sum-1)<=0.001); cell.classList.toggle('badsum', Math.abs(sum-1)>0.001); }
  });
}
function powerMapAutoEvenSelected(){
  const c=selectedPowerMapCluster(); if(!c) return;
  if(!c.bms.length || !c.pcs.length){ alert('Bind BMS and PCS first.'); return; }
  const share=Number((1/c.bms.length).toFixed(6));
  document.querySelectorAll('#pmEditorRows input[data-pm-bms]').forEach(inp=>{ inp.value=String(share); });
  updatePowerMapRowSums(); touchPowerMapEditor();
  if($('pmEditorResult')) $('pmEditorResult').innerHTML='<span class="ok">Auto Even generated. Click Save Power Map to persist it.</span>';
}
function powerMapNormalizeSelected(){
  document.querySelectorAll('#pmEditorRows tr[data-pm-pcs]').forEach(tr=>{
    const inputs=Array.from(tr.querySelectorAll('input[data-pm-bms]'));
    const vals=inputs.map(inp=>{ const n=Number(inp.value); return Number.isFinite(n)&&n>=0?n:0; });
    const sum=vals.reduce((a,b)=>a+b,0);
    if(sum>0){ inputs.forEach((inp,i)=>{ inp.value=String(Number((vals[i]/sum).toFixed(6))); }); }
  });
  updatePowerMapRowSums(); touchPowerMapEditor();
  if($('pmEditorResult')) $('pmEditorResult').innerHTML='<span class="ok">Rows normalized. Click Save Power Map to persist it.</span>';
}
async function savePowerMapEditor(){
  if(!PROJECT) await loadPowerMapEditor();
  const collected=collectPowerMapEditor();
  if(!collected){ alert('No cluster selected.'); return; }
  const sums=Object.fromEntries(Object.entries(collected.power_map||{}).map(([pc,row])=>[pc,Object.values(row||{}).reduce((a,b)=>a+Number(b||0),0)]));
  const bad=Object.entries(sums).filter(([_,sum])=>Math.abs(sum-1)>0.001);
  if(bad.length && !confirm('Some PCS rows do not sum to 1.000. Save anyway?')) return;
  const data=JSON.parse(JSON.stringify((PROJECT&&PROJECT.site_config)||{site:'ESS Site',clusters:[]}));
  data.clusters=(data.clusters||[]).map(c=>{
    const name=String(c.name||c.id||'');
    if(name!==collected.cluster.name) return c;
    const bms=normalizedClusterBms(c);
    const pcs=normalizedClusterPcs(c);
    return Object.assign({}, c, {bms_devices:bms, pcs_devices:pcs, pcs_device:pcs[0]||'', bms:bms, pcs:pcs, power_map:normalizePowerMap(collected.power_map, bms, pcs)});
  });
  await postJson('/api/site/config', data, 'pmEditorResult');
  CLUSTER_BINDING_DIRTY=false; CLUSTER_BINDING_INTERACTIVE_UNTIL=0;
  await loadPowerMapEditor();
  await loadPowerMapStatus();
}
async function loadPowerMapStatus(){
  const issues=$('powerMapIssues'); if(issues) issues.textContent='Loading...';
  try{
    const r=await fetch('/api/power-map/status',{cache:'no-store'}); const data=await r.json();
    if($('powerMapStatusRows')) $('powerMapStatusRows').innerHTML=(data.clusters||[]).map(c=>`<tr><td>${esc(c.cluster)}</td><td>${c.ready_for_dispatch?'<span class="ok">ready</span>':'<span class="warn">not ready</span>'}</td><td><code>${esc(JSON.stringify(c.pcs_weight_sums||{}))}</code></td><td><pre>${esc(JSON.stringify(c.power_map||{},null,2))}</pre></td></tr>`).join('') || '<tr><td colspan="4" class="muted">No clusters.</td></tr>';
    if(issues) issues.textContent=JSON.stringify({runtime_use:data.runtime_use, issues:data.issues||[]}, null, 2);
  }catch(e){ if(issues) issues.textContent=String(e); }
}
async function loadSiteConfig(){ $('siteConfigResult').textContent='Loading...'; try{ const r=await fetch('/api/site/config', {cache:'no-store'}); const data=await r.json(); $('siteConfigEditor').value=JSON.stringify(data.config||data, null, 2); $('siteConfigResult').innerHTML='<span class="ok">Loaded from runtime.</span>'; }catch(e){ $('siteConfigResult').innerHTML=`<span class="bad">${esc(e)}</span>`; } }
function downloadSiteConfig(){ const text=$('siteConfigEditor').value||'{}'; const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([text], {type:'application/json'})); a.download='site_config.runtime_export.json'; a.click(); URL.revokeObjectURL(a.href); }
async function saveSiteConfigFromEditor(){ if(!confirm('Save the JSON editor content to Runtime site config?')) return; try{ const cfg=JSON.parse($('siteConfigEditor').value||'{}'); await postJson('/api/site/config', cfg, 'siteConfigResult'); }catch(e){ $('siteConfigResult').innerHTML=`<span class="bad">Invalid JSON: ${esc(e)}</span>`; } }
async function siteSaveRuntime(){ if(!confirm('Persist current runtime site config to disk?')) return; await postJson('/api/site/save', {}, 'siteConfigResult'); }
let CURVES={};
let CURVE_PLAYBACK=null;
function resetCurveBuffer(){ for(const k of Object.keys(CURVES)) delete CURVES[k]; CURVE_PLAYBACK=null; if($('curveCsvStatus')) $('curveCsvStatus').textContent='Cleared.'; renderCurve(); }
function curveValueFromSnapshot(snap,sig){ if(!snap) return NaN; const aliases={soc:['soc','SOC','soc_value'], voltage:['voltage','system_voltage','total_voltage','dc_voltage'], current:['current','system_current','dc_current'], power:['power','power_kw','actual_power','active_power'], actual_power:['actual_power','active_power','power_kw','power'], reactive_power:['reactive_power','q','q_kvar'], temperature:['temperature','max_temperature','temp']}; for(const k of (aliases[sig]||[sig])){ if(snap[k]!==undefined){ const v=Number(snap[k]); if(Number.isFinite(v)) return v; } } return NaN; }
function curveSeriesList(){ if(CURVE_PLAYBACK) return CURVE_PLAYBACK; return Object.values(CURVES).map(x=>x).filter(Boolean); }
async function loadLiveCurves(){ if(CURVE_PLAYBACK) return; const type=$('curveDeviceType')?.value||'all'; const dev=$('curveDevice')?.value||''; const sig=$('curveSignal')?.value||'soc'; const multi=$('curveMulti')?.checked; const maxSamples=Math.max(60, Math.min(10000, parseInt($('curveMaxSamples')?.value||'600'))); try{ const url=`/api/curves/live?signal=${encodeURIComponent(sig)}&device_type=${encodeURIComponent(String(type).toLowerCase())}&device=${encodeURIComponent(dev)}&multi=${multi?'true':'false'}&limit=${maxSamples}`; const r=await fetch(url,{cache:'no-store'}); const j=await r.json(); CURVES={}; (j.series||[]).forEach(s=>{ CURVES[s.name]={name:s.name,data:s.data||[]}; }); }catch(e){ /* fallback to compact snapshot below */ } }
async function renderCurve(){ if(!SNAP) return; const sel=$('curveDevice'); if(!sel) return; const type=$('curveDeviceType')?.value||'all'; const names=deviceRows().filter(d=>type==='all'||d._type===type).map(d=>d.name).sort(); const old=sel.value; if(!sel.options.length || names.indexOf(old)<0){ sel.innerHTML=names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(names.includes(old)) sel.value=old; }
  await loadLiveCurves();
  const c=$('curveCanvas'), ctx=c.getContext('2d'); let series=curveSeriesList().filter(s=>(s.data||[]).length); ctx.clearRect(0,0,c.width,c.height); ctx.strokeStyle='#374151'; ctx.lineWidth=1; for(let i=0;i<6;i++){ const y=i*c.height/5; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(c.width,y); ctx.stroke(); }
  if(!series.length && !CURVE_PLAYBACK){ const sig=$('curveSignal')?.value||'soc'; const type=$('curveDeviceType')?.value||'all'; const dev=$('curveDevice')?.value||''; const multi=$('curveMulti')?.checked; const rows=deviceRows().filter(d=>(type==='all'||d._type===type) && (multi||!dev||d.name===dev)); rows.forEach(d=>{ const y=curveValueFromSnapshot(d.latest_values||d.snapshot||d, sig); if(Number.isFinite(y)) CURVES[d.name]={name:d.name,data:[{t:Date.now(),y}]}; }); series=curveSeriesList().filter(s=>(s.data||[]).length); } const all=series.flatMap(s=>s.data.map(p=>Number(p.y))).filter(Number.isFinite); ctx.fillStyle='#9ca3af'; ctx.fillText(`server-cache series=${series.length} samples=${all.length}`, 14, 20); if(all.length<1){ if($('curveStatsRows')) $('curveStatsRows').innerHTML='<tr><td colspan="5" class="muted">No curve samples yet.</td></tr>'; return; }
  let min=Math.min(...all), max=Math.max(...all); if(min===max){min-=1;max+=1;} const palette=['#60a5fa','#34d399','#fbbf24','#f87171','#c084fc','#22d3ee','#fb7185','#a3e635'];
  series.forEach((s,si)=>{ const arr=s.data; if(arr.length<1) return; ctx.strokeStyle=palette[si%palette.length]; ctx.lineWidth=2; ctx.beginPath(); arr.forEach((p,i)=>{ const x=arr.length===1?10:i*(c.width-30)/(arr.length-1)+15; const y=c.height-25-(Number(p.y)-min)*(c.height-55)/(max-min); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); }); ctx.stroke(); ctx.fillStyle=palette[si%palette.length]; ctx.fillText(s.name, 14+(si%4)*230, 40+Math.floor(si/4)*16); });
  ctx.fillStyle='#e5e7eb'; ctx.fillText(`min=${min.toFixed(2)} max=${max.toFixed(2)}`, 14, c.height-8); if($('curveStatsRows')) $('curveStatsRows').innerHTML=series.map(s=>{ const ys=s.data.map(p=>Number(p.y)).filter(Number.isFinite); const mn=Math.min(...ys), mx=Math.max(...ys), last=ys[ys.length-1]; return `<tr><td>${esc(s.name)}</td><td>${ys.length}</td><td>${mn.toFixed(3)}</td><td>${mx.toFixed(3)}</td><td>${Number(last).toFixed(3)}</td></tr>`; }).join(''); }
async function loadCurveCsvFile(){
  const inp=$('curveCsvFile');
  if(!inp || !inp.files || !inp.files[0]){ alert('Please choose a CSV file first.'); return; }
  try{
    const text=await inp.files[0].text();
    if($('curveCsvText')) $('curveCsvText').value=text;
    if($('curveCsvStatus')) $('curveCsvStatus').textContent='Loaded file '+inp.files[0].name+' into playback buffer.';
    loadCurveCsvPlayback();
  }catch(e){ if($('curveCsvStatus')) $('curveCsvStatus').textContent='CSV upload error: '+e; }
}
function loadCurveCsvPlayback(){ const text=$('curveCsvText').value||''; const lines=text.split(/\r?\n/).filter(x=>x.trim()); if(lines.length<2){ $('curveCsvStatus').textContent='Need CSV header and at least one row.'; return; } const head=lines[0].split(',').map(x=>x.trim().toLowerCase()); const idx=(names)=>names.map(n=>head.indexOf(n)).find(i=>i>=0); const ti=idx(['time','timestamp','ts']); const di=idx(['device','name','dev']); const si=idx(['signal','key','field']); const vi=idx(['value','val','scaled','raw']); if(di<0||vi<0){ $('curveCsvStatus').textContent='CSV needs device/name and value columns.'; return; } const map={}; for(const line of lines.slice(1)){ const cols=line.split(','); const dev=(cols[di]||'device').trim(); const sig=si>=0?(cols[si]||'value').trim():($('curveSignal')?.value||'value'); const v=Number(cols[vi]); if(!Number.isFinite(v)) continue; const key=dev+':'+sig; if(!map[key]) map[key]=[]; map[key].push({t:ti>=0?Date.parse(cols[ti])||map[key].length:map[key].length,y:v}); } CURVE_PLAYBACK=Object.entries(map).map(([name,data])=>({name,data})); $('curveCsvStatus').textContent=`Loaded ${CURVE_PLAYBACK.length} playback series.`; renderCurve(); }
function clearCurveCsvPlayback(){ CURVE_PLAYBACK=null; $('curveCsvStatus').textContent='Playback cleared; back to live snapshot curves.'; renderCurve(); }
function exportCurveCsv(){ const series=curveSeriesList(); let out='series,t,value\n'; for(const s of series){ for(const p of (s.data||[])) out += `${s.name},${p.t},${p.y}\n`; } const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([out],{type:'text/csv'})); a.download='ess_aio_web_curves.csv'; a.click(); URL.revokeObjectURL(a.href); }

function showAnalyzerTab(tab){
  CURRENT_ANALYZER_TAB = tab || 'upload';
  const tabs=['upload','modbus','can','joint','history'];
  for(const t of tabs){ const p=$('analyzer-'+t); if(p) p.classList.toggle('active', t===tab); }
  document.querySelectorAll('#analyzerTabs button').forEach(b=>b.classList.toggle('active', b.dataset.tab===tab));
  if(tab==='history') loadDiagnosisHistory();
  if(tab==='modbus'||tab==='can'||tab==='joint'||tab==='upload') loadAnalyzerFiles();
}
function analyzerFilesByKind(kind){
  if(kind==='modbus') return ANALYZER_FILES.filter(f=>['modbus','csv','pcap','pcapng'].includes(String(f.kind||'').toLowerCase()));
  if(kind==='asc') return ANALYZER_FILES.filter(f=>String(f.kind||'').toLowerCase()==='asc');
  if(kind==='dbc') return ANALYZER_FILES.filter(f=>String(f.kind||'').toLowerCase()==='dbc');
  if(kind==='mapping') return ANALYZER_FILES.filter(f=>String(f.kind||'').toLowerCase()==='mapping');
  return [];
}
function setSelectOptionsKeep(id, files, placeholder){
  const el=$(id); if(!el) return; const old=el.value;
  el.innerHTML=`<option value="">${esc(placeholder||'Select file')}</option>` + (files||[]).map(f=>`<option value="${esc(String(f.path||''))}">${esc(f.name||f.path||'')}</option>`).join('');
  if(old && Array.from(el.options).some(o=>o.value===old)) el.value=old;
}
function refreshAnalyzerFileSelects(){
  const modbus=analyzerFilesByKind('modbus'), asc=analyzerFilesByKind('asc'), dbc=analyzerFilesByKind('dbc'), mapping=analyzerFilesByKind('mapping');
  setSelectOptionsKeep('anModbusSelect', modbus, 'Select uploaded Modbus capture');
  setSelectOptionsKeep('anJointModbusSelect', modbus, 'Select uploaded Modbus capture');
  setSelectOptionsKeep('anAscSelect', asc, 'Select uploaded ASC');
  setSelectOptionsKeep('anJointAscSelect', asc, 'Select uploaded ASC');
  setSelectOptionsKeep('anDbcSelect', dbc, 'Select uploaded DBC');
  setSelectOptionsKeep('anJointDbcSelect', dbc, 'Select uploaded DBC');
  setSelectOptionsKeep('anMappingSelect', mapping, 'Select uploaded mapping.json');
  setSelectOptionsKeep('anJointMappingSelect', mapping, 'Select uploaded mapping.json');
  updateAnalyzerSelectedLabels();
}
function selectAnalyzerPath(kind,path){
  if(!path) return;
  if(kind==='asc'){ if($('anAscPath')) $('anAscPath').value=path; ['anAscSelect','anJointAscSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='dbc'){ if($('anDbcPath')) $('anDbcPath').value=path; ['anDbcSelect','anJointDbcSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='mapping'){ if($('anMappingPath')) $('anMappingPath').value=path; ['anMappingSelect','anJointMappingSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='modbus'){ if($('anModbusPath')) $('anModbusPath').value=path; if($('anJointModbusPath')) $('anJointModbusPath').value=path; ['anModbusSelect','anJointModbusSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  else if(kind==='joint_modbus'){ if($('anJointModbusPath')) $('anJointModbusPath').value=path; if($('anModbusPath')) $('anModbusPath').value=path; ['anModbusSelect','anJointModbusSelect'].forEach(id=>{if($(id)) $(id).value=path;}); }
  updateAnalyzerSelectedLabels();
}
function updateAnalyzerSelectedLabels(){
  const mod=$('anModbusPath')?.value||'';
  if($('anModbusSelectedFile')) $('anModbusSelectedFile').innerHTML = mod ? `<b>Selected:</b> <code>${esc(mod)}</code>` : '<span>No Modbus capture selected.</span>';
  const asc=$('anAscPath')?.value||'', dbc=$('anDbcPath')?.value||'', map=$('anMappingPath')?.value||'';
  if($('anCanSelectedFiles')) $('anCanSelectedFiles').innerHTML = [asc&&`ASC: <code>${esc(asc)}</code>`, dbc&&`DBC: <code>${esc(dbc)}</code>`, map&&`Mapping: <code>${esc(map)}</code>`].filter(Boolean).join('<br>') || 'No CAN-related files selected.';
}
async function uploadAnalyzerFile(kind,inputId,targetId){
  const inp=$(inputId); if(!inp || !inp.files || !inp.files[0]){ alert('Please choose a file first.'); return; }
  const fd=new FormData(); fd.append('file', inp.files[0]); fd.append('kind', kind);
  $('anUploadStatus').textContent='Uploading '+inp.files[0].name+'...';
  try{
    const r=await fetch('/api/analyzer/upload',{method:'POST',body:fd}); const j=await r.json();
    if(!j.ok){ $('anUploadStatus').textContent='Upload failed: '+(j.error||JSON.stringify(j)); return; }
    if(targetId && $(targetId)) $(targetId).value=j.path||'';
    selectAnalyzerPath(kind, j.path||'');
    $('anUploadStatus').textContent='Uploaded '+(j.filename||'file')+' → '+(j.path||'');
    await loadAnalyzerFiles();
  }catch(e){ $('anUploadStatus').textContent='Upload error: '+e; }
}
async function uploadAnalyzerModbusFile(){ await uploadAnalyzerModbusFileFrom('anModbusFile'); }
async function uploadAnalyzerModbusFileFrom(inputId){
  const inp=$(inputId); if(!inp || !inp.files || !inp.files[0]){ alert('Please choose a file first.'); return; }
  const fd=new FormData(); fd.append('file', inp.files[0]); fd.append('kind', 'modbus');
  if($('anUploadStatus')) $('anUploadStatus').textContent='Uploading '+inp.files[0].name+'...';
  try{
    const r=await fetch('/api/analyzer/upload',{method:'POST',body:fd}); const j=await r.json();
    if(!j.ok){ if($('anUploadStatus')) $('anUploadStatus').textContent='Upload failed: '+(j.error||JSON.stringify(j)); alert('Upload failed: '+(j.error||JSON.stringify(j))); return; }
    await loadAnalyzerFiles();
    selectAnalyzerPath('modbus', j.path||'');
    if($('anUploadStatus')) $('anUploadStatus').textContent='Uploaded '+(j.filename||'capture')+' → '+(j.path||'');
    showAnalyzerTab(CURRENT_ANALYZER_TAB||'modbus');
  }catch(e){ if($('anUploadStatus')) $('anUploadStatus').textContent='Upload error: '+e; alert('Upload error: '+e); }
}
function useAnalyzerFile(kind,path){
  if(kind==='asc') selectAnalyzerPath('asc', path);
  else if(kind==='dbc') selectAnalyzerPath('dbc', path);
  else if(kind==='mapping') selectAnalyzerPath('mapping', path);
  else if(kind==='modbus' || kind==='csv' || kind==='pcap' || kind==='pcapng') selectAnalyzerPath('modbus', path);
}
async function loadAnalyzerFiles(){
  const el=$('analyzerFileRows'); if(!el) return;
  el.innerHTML='<tr><td colspan="5" class="muted">Loading...</td></tr>';
  try{
    const r=await fetch('/api/analyzer/files',{cache:'no-store'}); const j=await r.json(); const files=j.files||[]; ANALYZER_FILES=files; refreshAnalyzerFileSelects();
    el.innerHTML=files.map(f=>`<tr><td>${esc(f.kind||'')}</td><td>${esc(f.name||'')}</td><td>${esc(formatBytes(f.size_bytes||0))}</td><td>${esc(f.modified_at||'')}</td><td><button data-kind="${esc(f.kind||'')}" data-path="${esc(String(f.path||''))}" onclick="useAnalyzerFile(this.dataset.kind,this.dataset.path)">Use</button></td></tr>`).join('') || '<tr><td colspan="5" class="muted">No uploaded files yet.</td></tr>';
  }catch(e){ el.innerHTML=`<tr><td colspan="5" class="bad">${esc(e)}</td></tr>`; }
}
function formatBytes(n){ n=Number(n)||0; if(n<1024) return n+' B'; if(n<1048576) return (n/1024).toFixed(1)+' KB'; if(n<1073741824) return (n/1048576).toFixed(1)+' MB'; return (n/1073741824).toFixed(2)+' GB'; }
async function analyzeModbusCapture(){
  const body={path:$('anModbusPath').value, timeout_seconds:parseFloat($('anTimeout').value||'2'), limit:200};
  $('anModbusResult').textContent='Analyzing...';
  try{ const r=await fetch('/api/analyzer/modbus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); $('anModbusResult').textContent=JSON.stringify(j,null,2); await loadDiagnosisHistory(); }catch(e){ $('anModbusResult').textContent=String(e); }
}
async function analyzeJoint(){
  const body={asc_path:$('anAscPath').value, modbus_path:$('anJointModbusPath').value, dbc_path:$('anDbcPath').value, mapping_path:$('anMappingPath').value, tolerance_s:parseFloat($('anTolerance').value||'0.5'), limit:200};
  $('anJointResult').textContent='Correlating...';
  try{ const r=await fetch('/api/analyzer/joint',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); $('anJointResult').textContent=JSON.stringify(j,null,2); await loadDiagnosisHistory(); }catch(e){ $('anJointResult').textContent=String(e); }
}
async function loadDiagnosisHistory(){
  const el=$('diagnosisHistory'); if(!el) return;
  el.innerHTML='<tr><td colspan="6" class="muted">Loading...</td></tr>';
  try{
    const r=await fetch('/api/diagnosis/history?limit=100',{cache:'no-store'}); const data=await r.json(); const jobs=data.jobs||[];
    el.innerHTML=jobs.map(j=>`<tr><td>${esc(j.created_at||'')}</td><td>${esc(j.kind||'')}</td><td>${pill(j.status||'')}</td><td>${esc(j.count??'')}</td><td><code>${esc(JSON.stringify(j.summary||{}))}</code></td><td>${esc((j.evidence||[]).map(e=>e.message||e.type).join('\n'))}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">No diagnosis history.</td></tr>';
  }catch(e){ el.innerHTML=`<tr><td colspan="6" class="bad">${esc(e)}</td></tr>`; }
}
async function clearDiagnosisHistory(){
  if(!confirm('Clear Diagnosis Center history?')) return;
  await postJson('/api/diagnosis/history/clear', {}, null);
  await loadDiagnosisHistory();
}


const UI_ACTION_MATRIX = [
  ['Overview','Start All','start_all','normal'], ['Overview','Stop All','stop_all','normal'], ['Overview','Clear Log View','clear_log_view','normal'], ['Overview','Open Output Folder','open_output_folder','file-dialog'],
  ['Devices','Browse BMS Output','browse_bms_output','file-dialog'], ['Devices','Add device','add_device','normal'], ['Devices','Remove selected BMS','remove_selected_bms','normal'], ['Devices','Browse PCS Output','browse_pcs_output','file-dialog'], ['Devices','Add / Update PCS','add_update_pcs','normal'], ['Devices','Remove PCS','remove_pcs','normal'], ['Devices','Set Current PCS','set_current_pcs','normal'], ['Devices','Import PCS Profile','import_pcs_profile','file-dialog'], ['Devices','Save PCS List','save_pcs_list','normal'], ['Devices','Connect selected PCS','connect_selected_pcs','normal'], ['Devices','Disconnect selected PCS','disconnect_selected_pcs','normal'],
  ['BMS Control','Clear Fault All Online','clear_fault_all_online','high'], ['BMS Control','Power On All Online','power_on_all_online','high'], ['BMS Control','Power Off All Online','power_off_all_online','high'], ['BMS Control','Stay All Online','stay_all_online','high'], ['BMS Control','Read BMS Debug','read_bms_debug','normal'], ['BMS Control','Read BMS Version','read_bms_version','normal'], ['BMS Control','HV ON All Online','hv_on_all_online','high'], ['BMS Control','HV OFF All Online','hv_off_all_online','high'], ['BMS Control','Cancel HV Workflow','cancel_hv_workflow','normal'],
  ['PCS Control','Refresh PCS List','refresh_pcs_list','normal'], ['PCS Control','Refresh PCS Status','refresh_pcs_status','normal'], ['PCS Control','Test PCS Config','test_pcs_config','normal'], ['PCS Control','Read PCS Debug','read_pcs_debug','normal'], ['PCS Control','Stop Debug','stop_debug','high'], ['PCS Control','Start Debug','start_debug','high'], ['PCS Control','HV On Debug','hv_on_debug','high'], ['PCS Control','HV Off Debug','hv_off_debug','high'], ['PCS Control','Refresh PCS Live Registers','refresh_pcs_live_registers','normal'], ['PCS Control','Fleet Status','fleet_status','normal'],
  ['Curves','Load History CSV','load_history_csv','file-dialog'], ['Curves','Clear History','clear_history','normal'], ['Curves','Apply Time Filter','apply_time_filter','normal'], ['Curves','Add Point','add_point','normal'], ['Curves','Clear Dynamic','clear_dynamic','normal'], ['Curves','Toggle Favorite','toggle_favorite','normal'], ['Curves','Add to Curve','add_to_curve','normal'],
  ['Replay','Load Main CSV','load_main_csv','file-dialog'], ['Replay','Replay Next Row','replay_next_row','normal'], ['Replay','Start Replay','start_replay','normal'], ['Replay','Stop Replay','stop_replay','normal'],
  ['Packet Analyzer','Load Capture','load_capture','file-dialog'], ['Packet Analyzer','Clear','clear_packet','normal'], ['Packet Analyzer','Export CSV','export_packet_csv','normal'], ['Packet Analyzer','Analyze Issues','analyze_issues','normal'], ['Packet Analyzer','Send to Register Tool','send_to_register_tool','normal'], ['Packet Analyzer','Apply','packet_apply','normal'], ['Packet Analyzer','First Page','packet_first','normal'], ['Packet Analyzer','Prev Page','packet_prev','normal'], ['Packet Analyzer','Next Page','packet_next','normal'], ['Packet Analyzer','Last Page','packet_last','normal'],
  ['CAN','Load CAN Log','load_can_log','file-dialog'], ['CAN','Clear CAN','clear_can','normal'], ['CAN','DBC / Mapping','select_mapping','file-dialog'], ['CAN','Clear DBC','clear_dbc','normal'], ['CAN','Export Frames','export_frames','normal'], ['CAN','Export Stats','export_stats','normal'], ['CAN','Apply','can_apply','normal'], ['CAN','Add Signal','add_signal','normal'], ['CAN','Clear Plot','clear_plot','normal'], ['CAN','Export Signal CSV','export_signal_csv','normal'],
  ['Joint Analysis','Select ASC','select_asc','file-dialog'], ['Joint Analysis','Select Modbus Capture','select_modbus_capture','file-dialog'], ['Joint Analysis','Select DBC','select_dbc','file-dialog'], ['Joint Analysis','Select Mapping','select_mapping','file-dialog'], ['Diagnosis','Run Diagnosis','run_packet_diagnosis','normal'], ['Diagnosis','Clear All Evidence','clear_all_evidence','normal'], ['Diagnosis','Export CSV','export_diagnosis_csv','normal'], ['Diagnosis','Export Markdown','export_diagnosis_markdown','normal'],
  ['Release','Run Self Check','run_self_check','normal'], ['Release','Open Crash Logs','open_crash_logs','file-dialog'], ['Release','About ESS-AIO','about','normal'], ['Report','Start Session','start_session','normal'], ['Report','End Session','end_session','normal'], ['Report','Generate HTML Report','generate_html_report','normal'], ['Report','Export Debug Package','export_debug_package','normal'], ['Report','Open Reports Folder','open_reports_folder','file-dialog'],
  ['Settings','Apply Runtime Params','apply_runtime_params','normal'], ['Site','Apply Site','apply_site','normal'], ['Site','Refresh','refresh_site','normal'], ['Site','Save Site','save_site','normal'], ['Site','Import Site','import_site','file-dialog'], ['Site','Export Site','export_site','file-dialog'], ['Site','Rename Cluster','rename_cluster','normal'], ['Site','Add Cluster','add_cluster','normal'], ['Site','Delete Selected Cluster','delete_selected_cluster','normal'], ['Site','Add PCS to Cluster','add_pcs_to_cluster','normal'], ['Site','Remove PCS from Cluster','remove_pcs_from_cluster','normal'], ['Site','Move BMS','move_bms','normal'], ['Site','Refresh Power Map','refresh_power_map','normal'], ['Site','Auto Even Map','auto_even_map','normal'], ['Site','Apply Power Map','apply_power_map','normal'], ['Site','Clear Power Map','clear_power_map','normal'],
  ['Strategy','Reload Strategy','reload_strategy','normal'], ['Strategy','Save Strategy','save_strategy','normal'], ['Strategy','Import Strategy JSON','import_strategy_json','file-dialog'], ['Strategy','Export Strategy JSON','export_strategy_json','file-dialog'], ['Strategy','Reset Default','reset_default_strategy','normal'], ['Strategy','Refresh Clusters','refresh_clusters','normal'], ['Strategy','Apply Fake Scenario','apply_fake_scenario','normal'], ['Strategy','Reset Fake Scenarios','reset_fake_scenarios','normal'],
  ['Templates','Import Template Package','import_template_package','file-dialog'], ['Templates','Validate','validate_template','normal'], ['Templates','Apply to Current Profile','apply_template','normal'], ['Templates','Export Template Package','export_template_package','file-dialog'], ['Templates','Refresh','refresh_templates','normal'], ['Templates','Apply Driver Binding','apply_driver_binding','normal'], ['Templates','Import Point Table JSON','import_point_table_json','file-dialog'], ['Templates','Set Selected As Active','set_point_table_active','normal'], ['Templates','Refresh Point Tables','refresh_point_tables','normal'], ['Templates','Open Point Tables Folder','open_output_folder','file-dialog'],
  ['Timeline','Refresh Timeline','refresh_timeline','normal'], ['Timeline','Export CSV','export_timeline_csv','normal']
];
function loadUiActionMatrix(){
  const el=$('uiActionRows'); if(!el) return;
  el.innerHTML=UI_ACTION_MATRIX.map(([group,label,action,risk])=>`<tr><td>${esc(group)}</td><td>${esc(label)}</td><td><code>${esc(action)}</code></td><td>${pill(risk)}</td><td><button onclick="runUiAction('${esc(action)}','${esc(label)}','${esc(risk)}')">Run</button></td></tr>`).join('');
  if($('uiActionResult')) $('uiActionResult').textContent=`${UI_ACTION_MATRIX.length} PySide buttons listed for Web parity.`;
}
async function runUiAction(action,label,risk){
  let confirm_text='';
  if(risk==='high'){
    if(!requireExecute(`${label} is a high-risk action. Type EXECUTE to continue.`)) return;
    confirm_text='EXECUTE';
  }
  await postJson('/api/ui-action', {action, params:{}, confirm_text}, 'uiActionResult');
}


async function loadRuntimeCenter(){
  try{
    const r=await fetch('/api/runtime-center',{cache:'no-store'}); const data=await r.json();
    const site=data.site||{};
    const state=data.site_state||'Unknown';
    const cls=state==='Fault'?'bad':(state==='Warning'?'warn':(state==='Running'?'ok':'muted'));
    if($('runtimeCenterCards')) $('runtimeCenterCards').innerHTML=`
      <div class="card"><div class="label">Site State</div><div class="value ${cls}">${esc(state)}</div></div>
      <div class="card"><div class="label">BMS Online</div><div class="value">${esc(site.bms_online||0)} / ${esc(site.bms_total||0)}</div></div>
      <div class="card"><div class="label">PCS Online</div><div class="value">${esc(site.pcs_online||0)} / ${esc(site.pcs_total||0)}</div></div>
      <div class="card"><div class="label">Strategies</div><div class="value">${esc(site.strategies_running||0)}</div></div>
      <div class="card"><div class="label">Recent Failed Cmd</div><div class="value ${site.commands_failed_recent?'bad':''}">${esc(site.commands_failed_recent||0)}</div></div>`;
    if($('runtimeCenterClusters')) $('runtimeCenterClusters').innerHTML=(data.clusters||[]).map(c=>`<tr><td>${esc(c.name)}</td><td>${esc((c.bms_devices||[]).join(', '))}</td><td>${esc((c.pcs_devices||[]).join(', '))}</td><td>${esc(c.allocation_mode||'')}</td><td>${(data.workers?.strategies||[]).includes(c.name)?pill('running'):pill('stopped')}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No clusters configured</td></tr>';
    if($('runtimeCenterCommands')) $('runtimeCenterCommands').textContent=JSON.stringify((data.recent_commands||[]).slice(0,8),null,2);
    if($('runtimeCenterRaw')) $('runtimeCenterRaw').textContent=JSON.stringify(data,null,2);
  }catch(e){ if($('runtimeCenterRaw')) $('runtimeCenterRaw').textContent=String(e); }
}
let ALARM_TIMER=null;
function loadAlarmCenterDebounced(){ clearTimeout(ALARM_TIMER); ALARM_TIMER=setTimeout(loadAlarmCenter, 250); }
async function loadAlarmCenter(){
  try{
    const q=$('alarmFilterText')?$('alarmFilterText').value:'';
    const sev=$('alarmFilterSeverity')?$('alarmFilterSeverity').value:'';
    const includeAck=$('alarmIncludeAck')?$('alarmIncludeAck').checked:true;
    const url=`/api/alarm-center?limit=500&query=${encodeURIComponent(q)}&severity=${encodeURIComponent(sev)}&include_ack=${includeAck?'true':'false'}`;
    const r=await fetch(url,{cache:'no-store'}); const data=await r.json();
    if($('alarmCenterCards')) $('alarmCenterCards').innerHTML=`
      <div class="card"><div class="label">Active Count</div><div class="value ${data.active_count?'bad':'ok'}">${esc(data.active_count||0)}</div></div>
      <div class="card"><div class="label">Filtered</div><div class="value">${esc(data.filtered_count??(data.active||[]).length)}</div></div>
      <div class="card"><div class="label">Acknowledged</div><div class="value">${esc(data.ack_count||0)}</div></div>
      <div class="card"><div class="label">Top Devices</div><div class="value">${esc((data.top_by_device||[]).length)}</div></div>`;
    if($('alarmCenterRows')) $('alarmCenterRows').innerHTML=(data.active||[]).map(a=>{
      const sig=a.key||a.message||''; const ack=a.acknowledged?`<span class="pill ok">ACK</span><br><small>${esc((a.ack||{}).iso||'')}</small>`:'<span class="muted">-</span>';
      const btn=a.acknowledged?`<button onclick="clearAlarmAck('${encodeURIComponent(a.alarm_id||'')}')">Clear</button>`:`<button onclick="ackAlarm('${encodeURIComponent(a.alarm_id||'')}')">Acknowledge</button>`;
      return `<tr><td>${esc(a.device)}</td><td>${pill(a.severity||'alarm')}</td><td>${esc(sig)}</td><td><code>${esc(a.value??'')}</code></td><td>${ack}</td><td>${btn}</td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No active alarms/errors detected from runtime snapshot.</td></tr>';
    if($('alarmCenterStats')) $('alarmCenterStats').textContent=JSON.stringify({top_by_device:data.top_by_device, top_by_signal:data.top_by_signal, ack_path:data.ack_path, note:data.note},null,2);
    if($('alarmCenterRaw')) $('alarmCenterRaw').textContent=JSON.stringify(data,null,2);
  }catch(e){ if($('alarmCenterRaw')) $('alarmCenterRaw').textContent=String(e); }
}
async function ackAlarm(encodedId){ const alarm_id=decodeURIComponent(encodedId||''); const note=prompt('ACK note / operator remark:', '') || ''; await postJson('/api/alarm-center/ack', {alarm_id, note}, 'alarmCenterRaw'); await loadAlarmCenter(); }
async function clearAlarmAck(encodedId){ const alarm_id=decodeURIComponent(encodedId||''); await postJson('/api/alarm-center/ack/clear', {alarm_id}, 'alarmCenterRaw'); await loadAlarmCenter(); }
async function clearAlarmAckAll(){ if(!confirm('Clear all Alarm Center ACK records?')) return; await postJson('/api/alarm-center/ack/clear', {}, 'alarmCenterRaw'); await loadAlarmCenter(); }

function registerDeviceNames(type){
  const cfgBms=(PROJECT?.bms_devices||PROJECT?.devices||[]).map(x=>x.name||x.id).filter(Boolean);
  const cfgPcs=Object.keys(PROJECT?.pcs_configs||{}).concat((PROJECT?.pcs_devices||[]).map(x=>x.name||x.id)).filter(Boolean);
  if(type==='pcs') return Array.from(new Set([...pcsRows().map(x=>x.name).filter(Boolean), ...cfgPcs])).sort();
  const live=Object.keys(((SNAP?.device_states||{}).bms)||{}).concat(bmsRows().map(x=>x.name).filter(Boolean));
  return Array.from(new Set([...live,...cfgBms])).sort();
}
function populateRegisterDevices(){
  if(!SNAP) return;
  const type=$('regDeviceType')?.value || 'bms';
  const names=registerDeviceNames(type);
  const old=$('regDevice')?.value || '';
  if($('regDevice')){ $('regDevice').innerHTML=names.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(names.includes(old)) $('regDevice').value=old; }
  const bms=registerDeviceNames('bms');
  const oldw=$('regWriteDevice')?.value || '';
  if($('regWriteDevice')){ $('regWriteDevice').innerHTML=bms.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join(''); if(bms.includes(oldw)) $('regWriteDevice').value=oldw; }
}
function toggleRegisterMode(){
  const m=$('regReadMode')?.value || 'continuous';
  if($('regContinuousFields')) $('regContinuousFields').style.display = m==='continuous' ? '' : 'none';
  if($('regStridedFields')) $('regStridedFields').style.display = m==='continuous' ? 'none' : '';
}
function renderRegisterRows(data){
  const rows=data.rows||[];
  if($('regRows')) $('regRows').innerHTML=rows.map(r=>{ const p=r.point||{}; return `<tr><td><code>${esc(r.address_hex)}</code></td><td>${esc(r.raw)}</td><td>${esc(r.scaled)}</td><td>${esc(p.name||p.key||'')}</td><td>${esc(p.unit||'')}</td><td>${esc(p.access||'')}</td></tr>`; }).join('') || '<tr><td colspan="6" class="muted">No data</td></tr>';
  if($('regReadJson')) $('regReadJson').textContent=JSON.stringify(data,null,2);
}
async function registerRead(){
  const body={device_type:$('regDeviceType').value, device:$('regDevice').value, register_type:$('regType').value, mode:$('regReadMode').value, start:$('regStart').value, count:parseInt($('regCount').value||'1'), step:$('regStep').value, quantity:parseInt($('regQuantity').value||'1'), length:parseInt($('regLength').value||'1')};
  if($('regReadJson')) $('regReadJson').textContent='Reading...';
  try{ const r=await fetch('/api/register/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); renderRegisterRows(j); }catch(e){ if($('regReadJson')) $('regReadJson').textContent=String(e); }
}
async function registerLookup(){
  const body={device_type:$('regDeviceType').value, device:$('regDevice').value, address:$('regStart').value};
  try{ const r=await fetch('/api/register/lookup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const j=await r.json(); $('regLookupResult').textContent=JSON.stringify(j,null,2); }catch(e){ $('regLookupResult').textContent=String(e); }
}
async function registerWriteBms(){
  const device=$('regWriteDevice').value; const address=parseAddrForApi($('regWriteAddress').value); const value=parseInt($('regWriteValue').value||'0');
  if(!device) return;
  if(!requireExecute(`Write BMS register ${$('regWriteAddress').value}=${value} on ${device}?`)) return;
  await postJson('/api/bms/register-write', {device, scope:'single', address, value, confirm_text:'EXECUTE'}, 'regWriteResult');
}
function copyRegisterResult(){ try{ navigator.clipboard.writeText($('regReadJson').textContent||''); }catch(e){} }

async function loadReleaseCenter(){
  const box=$('releaseNotesBox'); if(box) box.textContent='Loading...';
  try{
    const r=await fetch('/api/release/manifest',{cache:'no-store'}); const data=await r.json();
    if($('releaseCards')) $('releaseCards').innerHTML=[['Schema',data.api_schema||'-','accent'],['Files',(data.files||[]).filter(f=>f.exists).length+'/'+(data.files||[]).length,''],['BMS',((data.snapshot||{}).summary||{}).bms_total??'-',''],['PCS',((data.snapshot||{}).summary||{}).pcs_total??'-','']].map(([l,v,c])=>`<div class="card"><div class="label">${esc(l)}</div><div class="value ${c}">${esc(v)}</div></div>`).join('');
    if($('releaseFileRows')) $('releaseFileRows').innerHTML=(data.files||[]).map(f=>`<tr><td>${esc(f.kind)}</td><td><code>${esc(f.path)}</code></td><td>${f.exists?pill('ok'):pill('missing')}</td><td>${esc(f.size_bytes||0)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">No files</td></tr>';
    if(box) box.textContent=data.release_notes || JSON.stringify(data,null,2);
  }catch(e){ if(box) box.textContent=String(e); }
}
function renderActive(){ if(!SNAP) return; if(CURRENT_PAGE==='runtimecenter') loadRuntimeCenter(); else if(CURRENT_PAGE==='overview') renderOverview(); else if(CURRENT_PAGE==='devices') renderDevices(); else if(CURRENT_PAGE==='project') renderProjectConfig(); else if(CURRENT_PAGE==='ops'){ populateOpsBmsDevices(); renderBmsControl(); renderBmsPresetRegisters(); } else if(CURRENT_PAGE==='pcs'){ populatePcsSelected(); renderPCS(); } else if(CURRENT_PAGE==='strategy') loadStrategyCenter(); else if(CURRENT_PAGE==='analyzer'){ loadAnalyzerFiles(); if(document.querySelector('#analyzer-history.active')) loadDiagnosisHistory(); } else if(CURRENT_PAGE==='registerdebug'){ populateRegisterDevices(); } else if(CURRENT_PAGE==='release'){ loadReleaseCenter(); } else if(CURRENT_PAGE==='health'){ loadHealthMonitor(); } else if(CURRENT_PAGE==='parity'){ loadParityAudit(); } else if(CURRENT_PAGE==='uiactions'){ loadUiActionMatrix(); } else if(CURRENT_PAGE==='lts'){ loadLtsAudit(); } else if(CURRENT_PAGE==='curves') renderCurve(); else if(CURRENT_PAGE==='settings'){ renderSettings(); if(!$('runtimeSettingsRows')?.children.length) loadRuntimeSettings(); } else if(CURRENT_PAGE==='site' && !$('siteConfigEditor').value) loadSiteConfig(); else if(CURRENT_PAGE==='alarmcenter') loadAlarmCenter(); else if(CURRENT_PAGE==='alarms') populateAlarmDevices(); else if(CURRENT_PAGE==='clusters'){ renderClusters(); if(!$('powerMapStatusRows')?.children.length) loadPowerMapStatus(); if(!$('pmEditorRows')?.children.length) loadPowerMapEditor(); } else if(CURRENT_PAGE==='commands') renderCommands(); updateRuntimeFooter(); }


async function loadLtsAudit(){
  const raw=$('ltsRaw'); if(raw) raw.textContent='Running 9.x LTS final audit...';
  try{
    const r=await fetch('/api/lts/final',{cache:'no-store'}); const data=await r.json();
    const cls=data.readiness==='lts_ready'?'ok':'warn'; const sum=data.summary||{};
    if($('ltsCards')) $('ltsCards').innerHTML=`
      <div class="card"><div class="label">Readiness</div><div class="value ${cls}">${esc(data.readiness||'-')}</div></div>
      <div class="card"><div class="label">Health</div><div class="value ${String(sum.health_status)==='fault'?'bad':(String(sum.health_status)==='warning'?'warn':'ok')}">${esc(sum.health_status||'-')} ${esc(sum.health_score??'')}</div></div>
      <div class="card"><div class="label">Parity</div><div class="value ${Number(sum.parity_percent||0)>=95?'ok':'warn'}">${esc(sum.parity_percent??'-')}%</div></div>
      <div class="card"><div class="label">Packaging Missing</div><div class="value ${(sum.missing_packaging_items||0)?'bad':'ok'}">${esc(sum.missing_packaging_items??0)}</div></div>
      <div class="card"><div class="label">Consistency Issues</div><div class="value ${(sum.consistency_issues||0)?'warn':'ok'}">${esc(sum.consistency_issues??0)}</div></div>`;
    const control=((data.control_closure||{}).checklist)||[];
    if($('ltsControlRows')) $('ltsControlRows').innerHTML=control.map(x=>`<tr><td>${esc(x.area||'')}</td><td>${esc(x.feature||'')}</td><td>${pill(x.status||'unknown')}</td><td><code>${esc((x.api||[]).join(' · '))}</code></td><td>${esc(x.safety||'')}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No rows</td></tr>';
    const files=((data.packaging||{}).files)||[];
    if($('ltsPackagingRows')) $('ltsPackagingRows').innerHTML=files.map(x=>`<tr><td>${esc(x.relative_path||x.kind||'')}</td><td>${x.exists?pill('ok'):pill('missing')}</td><td><code>${esc(x.path||((x.checked||[])[0])||'')}</code></td></tr>`).join('') || '<tr><td colspan="3" class="muted">No rows</td></tr>';
    if(raw) raw.textContent=JSON.stringify(data,null,2);
  }catch(e){ if(raw) raw.textContent=String(e); }
}

async function loadParityAudit(){
  const raw=$('parityRaw'); if(raw) raw.textContent='Running parity audit...';
  try{
    const r=await fetch('/api/ui-web-parity',{cache:'no-store'}); const data=await r.json();
    const cov=data.coverage||{}; const cls=(data.readiness==='ready'?'ok':'warn');
    if($('parityCards')) $('parityCards').innerHTML=`
      <div class="card"><div class="label">Readiness</div><div class="value ${cls}">${esc(data.readiness||'-')}</div></div>
      <div class="card"><div class="label">Coverage</div><div class="value ${cls}">${esc(cov.percent??'-')}%</div></div>
      <div class="card"><div class="label">Covered</div><div class="value ok">${esc(cov.covered??0)} / ${esc(cov.total??0)}</div></div>
      <div class="card"><div class="label">Missing</div><div class="value ${(cov.missing||0)?'bad':'ok'}">${esc(cov.missing??0)}</div></div>`;
    if($('parityRows')) $('parityRows').innerHTML=(data.checklist||[]).map(x=>`<tr><td>${esc(x.area||'')}</td><td>${esc(x.ui_feature||'')}</td><td>${esc(x.web_page||'')}</td><td>${pill(x.status||'unknown')}</td><td><code>${esc((x.api||[]).join(' · '))}</code>${x.safety?`<br><span class="warn">${esc(x.safety)}</span>`:''}${x.note?`<br><span class="muted">${esc(x.note)}</span>`:''}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No checklist rows.</td></tr>';
    if(raw) raw.textContent=JSON.stringify(data,null,2);
  }catch(e){ if(raw) raw.textContent=String(e); }
}

async function loadHealthMonitor(){
  try{
    const r=await fetch('/api/health-monitor',{cache:'no-store'}); const data=await r.json();
    const status=data.status||'unknown';
    const cls=status==='fault'?'bad':(status==='warning'?'warn':'ok');
    const sum=data.summary||{}, proc=data.process||{}, queues=data.queues||{}, curves=data.curve_cache||{};
    if($('healthCards')) $('healthCards').innerHTML=`
      <div class="card"><div class="label">Health</div><div class="value ${cls}">${esc(status)}</div></div>
      <div class="card"><div class="label">Score</div><div class="value ${cls}">${esc(data.score??'-')}</div></div>
      <div class="card"><div class="label">Workers</div><div class="value">BMS ${esc((data.workers||{}).bms_running??0)}/${esc((data.workers||{}).bms_total??0)} · PCS ${esc((data.workers||{}).pcs_running??0)}/${esc((data.workers||{}).pcs_total??0)}</div></div>
      <div class="card"><div class="label">Command Queue</div><div class="value ${queues.runtime_command_queue?'warn':''}">${esc(queues.runtime_command_queue??0)}</div></div>
      <div class="card"><div class="label">Threads / Memory</div><div class="value">${esc(proc.thread_count??'-')} / ${esc(proc.memory_mb??'-')} MB</div></div>
      <div class="card"><div class="label">Curve Cache</div><div class="value">${esc(curves.series??0)} series / ${esc(curves.samples??0)} samples</div></div>
      <div class="card"><div class="label">BMS Online</div><div class="value">${esc(sum.bms_online??0)} / ${esc(sum.bms_total??0)}</div></div>
      <div class="card"><div class="label">PCS Online</div><div class="value">${esc(sum.pcs_online??0)} / ${esc(sum.pcs_total??0)}</div></div>`;
    if($('healthIssues')) $('healthIssues').innerHTML=(data.issues||[]).map(i=>`<tr><td>${pill(i.severity||'info')}</td><td>${esc(i.area||'')}</td><td>${esc(i.message||'')}</td></tr>`).join('') || '<tr><td colspan="3" class="ok">No health issues detected</td></tr>';
    const fr=data.freshness||{}; const rows=[...(fr.error_devices||[]), ...(fr.offline_devices||[]), ...(fr.stale_devices||[])];
    if($('healthFreshness')) $('healthFreshness').innerHTML=rows.map(x=>`<tr><td>${esc(x.kind||'')}</td><td>${esc(x.device||'')}</td><td>${x.online?pill('online'):pill('offline')}</td><td>${esc(x.age_s??'')}</td><td>${esc(x.error||'')}</td></tr>`).join('') || '<tr><td colspan="5" class="ok">No stale/offline/error device rows</td></tr>';
    if($('healthRaw')) $('healthRaw').textContent=JSON.stringify(data,null,2);
  }catch(e){ if($('healthRaw')) $('healthRaw').textContent=String(e); }
}

async function shutdownRuntimeFromWeb(){
  if(!confirm('Shutdown ESS-AIO Runtime now? This stops polling, CSV recording, workers, strategy and Web API.')) return;
  const box=$('runtimeShutdownBox'); if(box) box.textContent='Requesting shutdown...';
  try{
    const r=await fetch('/api/runtime/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'web-runtime-page',confirmed:true})});
    const j=await r.json();
    if(box) box.textContent=JSON.stringify(j,null,2)+'\n\nRuntime is stopping. Closing this browser tab alone would not stop Runtime; this shutdown request does.';
    setTimeout(()=>{ refreshNow(true); }, 1000);
  }catch(e){ if(box) box.textContent=String(e); }
}


function updateRuntimeFooter(){
  const active=document.querySelector('.page.active'); if(!active) return;
  document.querySelectorAll('.runtime-page-footer').forEach(x=>x.remove());
  const summary=SNAP?.summary||{}; const div=document.createElement('div'); div.className='runtime-page-footer';
  div.innerHTML=`<span><b>Runtime</b> ${esc(SNAP?.api_schema||'-')}</span><span><b>Uptime</b> ${esc(SNAP?.uptime_s||0)}s</span><span><b>BMS</b> ${esc(summary.bms_online??0)}/${esc(summary.bms_total??0)}</span><span><b>PCS</b> ${esc(summary.pcs_online??0)}/${esc(summary.pcs_total??0)}</span><span><b>Commands</b> ${esc((SNAP?.command_acks||[]).length)}</span>`;
  active.appendChild(div);
}
async function refreshNow(force=false){ if(!force && document.hidden) return; try{ const endpoint = force && CURRENT_PAGE==='release' ? '/api/snapshot' : '/api/snapshot/compact'; const r=await fetch(endpoint, {cache:'no-store'}); const s=await r.json(); if(!force && SNAP && SNAP.snapshot_id===s.snapshot_id) return; SNAP=s; $('subtitle').innerHTML=`${esc(s.api_schema)} · uptime ${esc(s.uptime_s)}s · ${s.compact?'compact':'full'} snapshot <code>${esc(s.snapshot_id||'-')}</code>`; if(CURRENT_PAGE==='overview') renderOverview(); else renderActive(); }catch(e){ $('subtitle').innerHTML=`<span class="bad">Runtime unavailable: ${esc(e)}</span>`; } }
setInterval(()=>{ if($('auto').checked && !document.hidden) refreshNow(false); }, 2500);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden && $('auto').checked) refreshNow(true); });
refreshNow(true);
