'use strict';
let state, draft, dirty = false, polling = false;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = value => value == null ? '—' : Number(value).toLocaleString(undefined, {maximumFractionDigits: 1});
function message(text, error = false) {
  const box = $(error ? 'error' : 'notice'); box.textContent = text; box.hidden = false;
  if (!error) setTimeout(() => { if (box.textContent === text) box.hidden = true; }, 6000);
}
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache: 'no-store'} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  if (!response.ok) {
    let reason = `HTTP ${response.status}`;
    try { reason = (await response.json()).error || reason; } catch (_) {}
    throw new Error(reason);
  }
  return response.json();
}
function markDirty() { dirty = true; $('dirty').hidden = false; }
function renderEditors() {
  const names = [...new Set([...state.sensors.filter(s => s.unit.toLowerCase() === 'degrees c').map(s => s.name), ...draft.zones.flatMap(z => z.sensors)])];
  $('zones').innerHTML = draft.zones.map(z => `<article class="zone"><div class="zone-head"><div><h3>${esc(z.name)}</h3><small>IPMI zone ${z.id} · hottest selected sensor</small></div><div class="zone-status"><strong id="zone-bmc-${z.id}">BMC duty: —</strong><span class="badge" id="zone-live-${z.id}">MONITORING</span></div></div><div class="zone-content"><div class="zone-controls"><label>Reference temperature sensors<select multiple aria-label="Zone ${z.id+1} reference sensors" data-zone="${z.id}" data-field="sensors">${names.map(n=>`<option ${z.sensors.includes(n)?'selected':''} value="${esc(n)}">${esc(n)}</option>`).join('')}</select></label><label>Critical override (°C)<input type="number" min="20" max="110" data-zone="${z.id}" data-field="critical" value="${z.critical}"></label></div><p class="help">Select one sensor, or hold Ctrl / Command to select several. Critical temperature requests 100% duty.</p><div id="graph-${z.id}"></div><div class="graph-key"><span>— Preview curve</span><span>┄ Cooling hysteresis</span><span>● Live BMC state</span></div><table class="curve-table"><thead><tr><th>From temperature (°C)</th><th>Duty (%)</th><th></th></tr></thead><tbody id="points-${z.id}"></tbody></table><button data-add="${z.id}">Add temperature step</button><p class="help">The first duty also applies below the first step. Preview changes take effect after Apply.</p></div></article>`).join('');
  for (const z of draft.zones) renderPoints(z.id);
  for (const key of ['poll_seconds','threshold_check_seconds','startup_seconds','hysteresis']) $(key).value = draft[key];
  $('enabled').checked = draft.enabled;
  $('thresholds').innerHTML = Object.entries(draft.fans).map(([name,f],i) => `<tr><td>${esc(name)}<small id="threshold-status-${i}">Waiting</small></td><td id="threshold-actual-${i}">—</td>${f.desired.map((n,j)=>`<td><input type="number" min="0" max="50000" step="1" aria-label="${esc(name)} ${['LNR','LCR','LNC'][j]}" data-fan="${esc(name)}" data-rpm="${j}" value="${n}"></td>`).join('')}<td>${f.factory ? f.factory.map(fmt).join(' / ') : 'Not recorded'}</td><td><input type="checkbox" aria-label="Auto restore ${esc(name)}" data-fan="${esc(name)}" data-restore ${f.restore?'checked':''}></td><td><button class="threshold-action" data-threshold="apply" data-fan="${esc(name)}">Apply now</button><button class="threshold-action" data-threshold="capture" data-fan="${esc(name)}">Record defaults</button></td></tr>`).join('');
}
function renderPoints(id) {
  const z = draft.zones[id];
  $(`points-${id}`).innerHTML = z.points.map((p,i)=>`<tr><td><input type="number" min="-40" max="110" aria-label="Zone ${id+1} step ${i+1} temperature" data-zone="${id}" data-point="${i}" data-key="temp" value="${p.temp}"></td><td><input type="number" min="10" max="100" step="1" aria-label="Zone ${id+1} step ${i+1} duty" data-zone="${id}" data-point="${i}" data-key="duty" value="${p.duty}"></td><td><button data-remove="${i}" data-zone="${id}" ${z.points.length===1?'disabled':''}>Remove</button></td></tr>`).join('');
  graph(id);
}
function graph(id) {
  const z = draft.zones[id], points = [...z.points].filter(p => Number.isFinite(p.temp) && Number.isFinite(p.duty)).sort((a,b)=>a.temp-b.temp);
  if (!points.length) return;
  const left=45, right=560, top=20, bottom=205, min=Math.min(0,points[0].temp), max=110;
  const x = t => left + (Math.max(min,Math.min(max,t))-min)/(max-min)*(right-left);
  const y = d => bottom - Math.max(0,Math.min(100,d))/100*(bottom-top);
  function path(cooling) {
    let duty=points[0].duty, d=`M ${x(min)} ${y(duty)}`;
    for (const p of points.slice(1)) {
      if(p.temp >= z.critical) continue;
      const t = p.temp - (cooling ? draft.hysteresis : 0);
      d += ` H ${x(t)} V ${y(p.duty)}`; duty=p.duty;
    }
    d += ` H ${x(z.critical)} V ${y(100)} H ${x(max)}`;
    return d;
  }
  let grid='';
  for(let d=0;d<=100;d+=25) grid+=`<line class="grid" x1="${left}" x2="${right}" y1="${y(d)}" y2="${y(d)}"/><text x="${left-9}" y="${y(d)+4}" text-anchor="end">${d}%</text>`;
  for(let t=0;t<=100;t+=20) grid+=`<text x="${x(t)}" y="${bottom+22}" text-anchor="middle">${t}°</text>`;
  const live = state.zones.find(v=>v.id===id);
  const marker = live && live.readback != null ? `<circle class="marker" cx="${x(live.temperature)}" cy="${y(live.readback)}" r="6"><title>${esc(live.temperature)}°C · BMC reports ${esc(live.readback)}%</title></circle>` : '';
  $(`graph-${id}`).innerHTML=`<svg viewBox="0 0 580 250" role="img" aria-label="Zone ${id+1} temperature versus duty cycle preview"><title>Temperature on the horizontal axis; duty cycle on the vertical axis</title>${grid}<path class="hysteresis" d="${path(true)}"/><path class="curve" d="${path(false)}"/>${marker}<text x="${(left+right)/2}" y="247" text-anchor="middle">TEMPERATURE (°C)</text></svg>`;
}
function telemetry() {
  const knownSensors = state.sensors.filter(s=>s.unit.toLowerCase()==='degrees c').map(s=>s.name);
  document.querySelectorAll('select[data-field="sensors"]').forEach(select=>{
    const existing = new Set([...select.options].map(o=>o.value));
    for(const name of knownSensors) if(!existing.has(name)) {
      const option=document.createElement('option');option.value=name;option.textContent=name;select.append(option);
    }
  });
  $('mode').textContent=state.demo?'DEMO MODE':'LIVE IPMI';
  $('demo-panel').hidden=!state.demo;
  $('connection').textContent=state.connected?'BMC reachable':'BMC connection unavailable';
  $('power').textContent=state.power==='on'?'On':state.power==='off'?'Off':'Unknown';
  $('host').textContent=state.host; $('phase').textContent=state.phase;
  $('control-state').textContent=state.config.enabled?'Automatic control enabled':'Fan control disabled';
  const fresh = state.connected && state.power==='on' && !['fault','starting'].includes(state.phase);
  const temps=state.sensors.filter(s=>s.unit.toLowerCase()==='degrees c'&&s.value!==null).map(s=>s.value);
  $('hottest').textContent=fresh&&temps.length?`${fmt(Math.max(...temps))}°C`:'—';
  const age=state.last_success ? Math.max(0, Math.round(Date.now()/1000-state.last_success)) : null;
  $('last-update').textContent=age===null?'—':`${age}s ago`;
  $('freshness').textContent=fresh?'Telemetry available':state.power==='off'?'Server is powered off':'Readings are stale or settling';
  $('sensor-status').textContent=fresh?'LIVE':state.power==='off'?'POWERED OFF':'STALE / SETTLING';
  $('sensors').classList.toggle('stale',!fresh);
  $('sensors').innerHTML=state.sensors.filter(s=>['degrees c','rpm'].includes(s.unit.toLowerCase())).map(s=>`<div class="sensor"><span>${esc(s.name)}</span><strong>${fmt(s.value)}${s.value!==null?(s.unit.toLowerCase()==='rpm'?' <span>RPM</span>':'°C'):''}</strong><span class="${s.status!=='ok'||(fresh&&s.unit.toLowerCase()==='rpm'&&s.value===0)?'bad':''}">${esc(s.status)}${fresh&&s.unit.toLowerCase()==='rpm'&&s.value===0?' · zero RPM: check fan / connection':''}${!fresh?' · stale / inactive':''}</span></div>`).join('') || '<p>No active sensor readings. Waiting for a running server.</p>';
  for(const z of draft.zones) {
    const live=state.zones.find(v=>v.id===z.id);
    const reported=state.bmc_duties?.find(v=>v.id===z.id);
    const dutyLabel=$(`zone-bmc-${z.id}`);
    dutyLabel.textContent=reported?.duty!=null?`BMC duty: ${reported.duty}%`:'BMC duty: —';
    dutyLabel.title=reported?.error || (reported?.updated_at ? `Read from BMC at ${new Date(reported.updated_at*1000).toLocaleTimeString()}` : 'No current BMC duty reading');
    $(`zone-live-${z.id}`).textContent=live?`${fmt(live.temperature)}°C · target ${live.requested}%${live.critical?' · CRITICAL':''}`:'MONITORING / PAUSED';
    graph(z.id);
  }
  Object.keys(draft.fans).forEach((name,i)=>{
    const row=state.thresholds[name];
    const skipped = row?.actual && row.status !== 'unavailable' ? ['LNR','LCR','LNC'].filter((_,j)=>row.actual[j]===null) : [];
    $(`threshold-status-${i}`).textContent=row ? row.status + (row.reason ? ` · ${row.reason}` : '') + (skipped.length ? ` · ${skipped.join(', ')} not reported; skipped` : '') : 'Not checked';
    const cell=$(`threshold-actual-${i}`);
    cell.textContent=row?.actual?.map(fmt).join(' / ') || '—';
    if(row?.expected) {
      const target=document.createElement('small');
      target.textContent='Effective target: '+row.expected.map(fmt).join(' / ');
      cell.append(target);
    }
    document.querySelectorAll('input[data-rpm]').forEach(input=>{
      if(input.dataset.fan!==name) return;
      const unsupported=!!(row?.actual && row.status!=='unavailable' && row.actual[Number(input.dataset.rpm)]===null);
      input.disabled=unsupported;
      input.title=unsupported?'Not reported by this fan sensor; this threshold is skipped':'';
    });
  });
  $('events').innerHTML=state.events.map(e=>`<div class="event ${e.level==='error'?'error':''}"><time>${esc(new Date(e.time*1000).toLocaleString())}</time>${esc(e.message)}</div>`).join('') || '<p>No events yet.</p>';
  if(state.error) message(state.error,true); else if(!$('error').dataset.local) $('error').hidden=true;
}
async function refresh(rebuild=false) {
  state=await api('/api/state');
  if(!draft || rebuild) {draft=structuredClone(state.config);dirty=false;$('dirty').hidden=true;renderEditors();}
  telemetry();
}
document.addEventListener('input', e=>{
  const el=e.target, d=el.dataset;
  if(d.zone!==undefined) {
    const z=draft.zones[Number(d.zone)];
    if(d.point!==undefined) z.points[Number(d.point)][d.key]=el.value===''?NaN:Number(el.value);
    else if(d.field==='sensors') z.sensors=[...el.selectedOptions].map(o=>o.value);
    else if(d.field==='critical') z.critical=Number(el.value);
    graph(z.id);
  } else if(d.fan) {
    if(d.rpm!==undefined) draft.fans[d.fan].desired[Number(d.rpm)]=el.value===''?NaN:Number(el.value);
    else if('restore' in d) draft.fans[d.fan].restore=el.checked;
  } else if(el.id==='enabled') draft[el.id]=el.checked;
  else if(['poll_seconds','threshold_check_seconds','startup_seconds','hysteresis'].includes(el.id)) {draft[el.id]=Number(el.value); draft.zones.forEach(z=>graph(z.id));}
  else return;
  markDirty();
});
document.addEventListener('click', async e=>{
  const button=e.target.closest('button'); if(!button || !draft) return;
  const d=button.dataset;
  try {
    $('error').hidden=true; delete $('error').dataset.local;
    if(button.id==='save') {
      button.disabled=true;
      await api('/api/config',draft); await refresh(true); message('Configuration saved. The controller will apply it on its next cycle.');
    } else if(d.add!==undefined) {
      const z=draft.zones[Number(d.add)], last=z.points.at(-1);
      z.points.push({temp:Math.min(110,last.temp+10),duty:Math.min(100,last.duty+10)});renderPoints(z.id);markDirty();
    } else if(d.remove!==undefined) {
      const z=draft.zones[Number(d.zone)];z.points.splice(Number(d.remove),1);renderPoints(z.id);markDirty();
    } else if(button.id==='copy-thresholds') {
      const values=[...draft.fans.FAN1.desired];Object.values(draft.fans).forEach(f=>f.desired=[...values]);
      renderEditors();markDirty();
    } else if(d.threshold) {
      if(dirty) throw new Error('Apply configuration before performing threshold actions.');
      if(d.threshold==='capture'&&!confirm(`Record the current ${d.fan} readings as FACTORY defaults? Only continue if you know these are factory values.`)) return;
      button.disabled=true;await api(`/api/thresholds/${d.threshold}`,{fan:d.fan});await refresh(true);message('Threshold action completed and verified.');
    } else if(d.demo) {
      button.disabled=true;await api('/api/demo',{power:d.demo});message('Demo action queued. Watch the next controller update.');
    }
  } catch(err) { $('error').dataset.local='true';message(err.message,true); }
  finally {button.disabled=false;}
});
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
async function poll() { if(polling)return;polling=true;try{await refresh();}catch(err){message(`Dashboard disconnected: ${err.message}. Displayed readings may be stale.`,true);$('connection').textContent='Dashboard disconnected';$('sensor-status').textContent='STALE';$('sensors').classList.add('stale'); document.querySelectorAll('[id^="zone-bmc-"]').forEach(el=>{el.textContent='BMC duty: — (disconnected)';});}finally{polling=false;} }
poll();setInterval(poll,3000);
