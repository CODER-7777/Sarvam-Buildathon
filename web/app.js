const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const e = escapeHtml;
let config, currentRun, history = [], polling = null, busy = false;
const names = {sarvam:'Sarvam AI',repository:'Repository',posthog:'PostHog',sentry:'Sentry',linear:'Linear',slack:'Slack'};
const symbols = {sarvam:'S',repository:'⌘',posthog:'P',sentry:'△',linear:'◒',slack:'#'};
const labels = {configured:'Configured',not_configured:'Not connected',disabled:'Disabled',not_implemented:'Not included'};
const statusLabel = state => labels[state] || state;
const date = value => new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const safeLink = value => {try {const u=new URL(value);return u.protocol==='https:' && (u.hostname==='linear.app'||u.hostname.endsWith('.linear.app'))?u.href:null;}catch{return null;}};

function showError(message) {$('error').textContent=message;$('error').hidden=false;}
function clearError() {$('error').hidden=true;}
async function api(path, options={}) {
  const response = await fetch(path,{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':config?.csrf || '',...options.headers}});
  const data = await response.json();
  if(!response.ok) throw new Error(data.error || 'The request failed.');
  return data;
}
function switchView(view) {
  for (const name of ['investigate','history','integrations']) {$(`${name}-view`).hidden=name!==view;$(`nav-${name}`).classList.toggle('active',name===view);}
  $('breadcrumb').textContent=view==='investigate'?'Investigations':view[0].toUpperCase()+view.slice(1);
  clearError();
  if(view==='history')refreshHistory().catch(err=>showError(err.message));
}
function setMode() {
  const mode=document.querySelector('input[name="mode"]:checked').value;
  $('demo-options').hidden=mode!=='demo';$('connected-options').hidden=mode==='demo';
  $('mode-badge').textContent=mode==='demo'?'DEMO MODE':'CONNECTED MODE';
  $('mode-note').textContent=mode==='demo'?'A deterministic sample with synthetic logs and source code. No external services are queried.':'Uses your configured repository and services. When enabled, Sarvam receives the report and redacted evidence excerpts for analysis. Blank time windows use the last 24 hours.';
  if(mode==='demo')loadSample();
  else if(Object.values(config.samples).includes($('description').value))$('description').value='';
}
function loadSample() {$('description').value=config.samples[$('sample').value];}
function renderIntegrations() {
  $('integration-list').innerHTML=Object.entries(config.integrations).filter(([key])=>key!=='slack').map(([key,{state}])=>`<div class="integration-row"><span class="service-icon">${symbols[key]}</span><span class="service-name">${names[key]}</span><span class="service-status ${e(state)}">${e(statusLabel(state))}</span></div>`).join('');
  const descriptions={sarvam:['Analyzes source and failure evidence, returning a validated JSON report. Live analysis is an explicit opt-in.','SARVAM_API_KEY · SARVAM_ENABLED=true · SARVAM_MODEL'],repository:['Searches source files inside the configured root. The report can select a relative subfolder; files are never modified.','REPO_ROOT'],posthog:['Retrieves up to 20 targeted events with distinct IDs, session IDs, timestamps, and properties. Use a personal API key with query access.','POSTHOG_API_KEY · POSTHOG_PROJECT_ID · POSTHOG_HOST'],sentry:['Retrieves up to 10 error events including available stack traces and breadcrumbs for the chosen user and time window.','SENTRY_TOKEN · SENTRY_ORG · SENTRY_PROJECT · SENTRY_HOST'],linear:['Creates an issue only after you review and confirm the report. Choose a team ID when your workspace has multiple teams.','LINEAR_API_KEY · LINEAR_TEAM_ID'],slack:['There was no existing Slack integration to retain. The local website and process-once CLI provide ingestion in this MVP.','Reserved: SLACK_BOT_TOKEN · SLACK_CHANNEL_ID']};
  $('config-cards').innerHTML=Object.entries(descriptions).map(([key,[description,vars]])=>`<article class="card config-card"><div class="card-heading"><h2>${names[key]}</h2><span class="badge">${e(statusLabel(config.integrations[key].state))}</span></div><p>${description}</p><code>${vars}</code></article>`).join('');
}
async function refreshHistory() {
  history=await api('/api/runs');$('history-count').textContent=history.length;
  $('recent-list').innerHTML=history.length?history.slice(0,7).map(run=>`<button class="recent-item" data-run="${e(run.id)}"><span>${run.diagnosis?.outcome==='DIAGNOSED'?'✓':'·'}</span><span>${e(run.report.text)}</span></button>`).join(''):'<p class="muted">Your investigations will appear here.</p>';
  $('history-table').innerHTML=history.length?`<div class="card history-card"><table><thead><tr><th>INVESTIGATION</th><th>OUTCOME</th><th>CONFIDENCE</th><th>CREATED</th><th>DURATION</th></tr></thead><tbody>${history.map(run=>`<tr><td><button class="history-open" data-run="${e(run.id)}">${e(run.report.text)}</button><div class="run-meta">${e(run.report.mode.toUpperCase())}</div></td><td><span class="badge">${e(run.diagnosis?.outcome || run.status.toUpperCase())}</span></td><td>${run.diagnosis?.outcome==='DIAGNOSED'?Math.round(run.diagnosis.confidence*100)+'%':'—'}</td><td>${e(date(run.created_at))}</td><td>${e(run.duration)}s</td></tr>`).join('')}</tbody></table></div>`:'<div class="card history-empty">No investigations yet. Start with a sample investigation.</div>';
}
function renderRun(run) {
  currentRun=run;
  $('empty-state').hidden=true;
  const running=run.status==='running';$('progress').hidden=!running;$('result').hidden=running;
  if(running) {
    const stages={'Queued':5,'Parsing report':12,'Loading synthetic evidence':25,'Collecting service evidence':30,'Inspecting sample source':48,'Searching source code':48,'Analyzing collected evidence':70,'Validating evidence':90};
    $('progress-stage').textContent=run.stage;$('elapsed').textContent=`${run.duration}s`;$('progress-bar').value=stages[run.stage]||10;
    return;
  }
  const d=run.diagnosis;if(!d){showError('This record does not contain a completed report.');return;}
  const diagnosed=d.outcome==='DIAGNOSED';
  const cited=[...new Set(d.citations.map(c=>c.evidence_id))];
  const linearUrl=safeLink(run.linear?.url);
  $('result').innerHTML=`<div class="result-heading"><div><h2>Investigation report</h2><div class="run-meta">${e(date(run.created_at))} &nbsp; / &nbsp; ${e(run.duration)}s &nbsp; / &nbsp; ${e(run.report.mode.toUpperCase())}</div></div><div class="result-actions"><button class="secondary" id="export-report">Export report ↓</button>${linearUrl?`<a class="secondary" href="${e(linearUrl)}" target="_blank" rel="noopener noreferrer">${e(run.linear.identifier)} ↗</a>`:`<button class="secondary" id="create-linear" ${config.integrations.linear.state!=='configured'?'disabled title="Configure Linear in .env first"':''}>Create Linear issue ↗</button>`}</div></div>
  ${run.report.mode==='demo'?'<div class="warning">DEMO · All evidence below is synthetic. No PostHog, Sentry, or Sarvam service was queried.</div>':''}
  <div class="result-grid"><article class="card diagnosis-card ${diagnosed?'':'abstention'}"><div class="outcome"><span class="outcome-label">${e(d.outcome.replaceAll('_',' '))}</span><span class="confidence">${diagnosed?Math.round(d.confidence*100)+'% confidence estimate':'Abstained'}</span></div><span class="inference-label">${d.basis==='deterministic_fixture'?'DETERMINISTIC SAMPLE CONCLUSION':d.basis==='model_inference'?'AI INFERENCE · REVIEW AGAINST THE EVIDENCE':'INSUFFICIENT SUPPORT FOR A CAUSE'}</span><h3>${e(d.root_cause)}</h3><p>${e(d.summary)}</p><div class="citation-chips">${cited.map(id=>`<a href="#${e(id)}">↗ ${e(id)}</a>`).join('')}</div></article><aside class="card next-card"><h3>Recommended next steps</h3><ol>${d.next_steps.map(s=>`<li>${e(s)}</li>`).join('')}</ol></aside></div>
  ${(run.observations||[]).length?`<section class="card unknowns"><h3>Observed excerpts</h3><p class="muted">Verbatim source text. Matching a quote does not prove the AI's causal interpretation.</p><ul>${run.observations.map(o=>`<li><a href="#${e(o.evidence_id)}">${e(o.evidence_id)}</a> · ${e(o.origin.replace('_',' '))}<blockquote>${e(o.quote)}</blockquote>${o.location?`<code>${e(o.location)}</code>`:''}</li>`).join('')}</ul></section>`:''}
  <div class="section-heading"><h3>Evidence trail</h3><span>${run.evidence.length} RECORDS</span></div>
  ${run.evidence.length?run.evidence.map(item=>`<article class="card evidence-card" id="${e(item.id)}"><div class="evidence-top"><span class="service-icon">${symbols[item.source]||'≡'}</span><span class="evidence-title">${e(item.title)}</span><span class="evidence-id">${e(item.id)}</span><span class="origin ${e(item.origin)}">${e(item.origin.replace('_',' ').toUpperCase())}</span></div><div class="evidence-meta">${e(names[item.source]||'User diagnostics')}${item.timestamp?' · '+e(item.timestamp):''}${item.location?' · '+e(item.location):''}</div><details ${item.source==='repository'?'open':''}><summary>Inspect collected excerpt</summary><pre>${e(item.content)}</pre></details></article>`).join(''):'<p class="muted">No matching evidence was collected. A diagnosis would be speculation.</p>'}
  ${d.missing.length?`<section class="card unknowns"><h3>Still unknown</h3><ul>${d.missing.map(s=>`<li>${e(s)}</li>`).join('')}</ul></section>`:''}
  ${run.warnings.map(w=>`<div class="warning">${e(w)}</div>`).join('')}
  <details class="audit"><summary>Tool-call history · ${run.tools.length} steps</summary>${run.tools.map(t=>`<div class="tool"><div class="tool-head"><strong>${e(t.tool)}</strong><span>${e(t.duration)}s</span><span class="status">${e(t.status.toUpperCase())}</span></div><details><summary>Inputs and redacted output</summary><pre>${e(typeof t.input==='string'?t.input:JSON.stringify(t.input,null,2))}\n\n${e(t.output)}</pre></details></div>`).join('')}</details>`;
  $('export-report').onclick=exportReport;
  if($('create-linear'))$('create-linear').onclick=previewLinear;
}
function setBusy(value) {busy=value;$('submit').disabled=value;$('submit').innerHTML=value?'Investigating…':'Start investigation <span>↗</span>';}
async function pollRun(id) {
  clearTimeout(polling);
  try {
    const run=await api(`/api/runs/${id}`);renderRun(run);
    if(run.status==='running'){polling=setTimeout(()=>pollRun(id),650);}
    else {setBusy(false);await refreshHistory();}
  }catch(err){setBusy(false);$('progress').hidden=true;showError(err.message+' Your record remains in History; reopen it to resume checking.');}
}
async function openRun(id){clearTimeout(polling);switchView('investigate');setBusy(true);await pollRun(id);$('result').scrollIntoView({behavior:'smooth',block:'start'});}
async function exportReport(){try{const response=await fetch(`/api/runs/${currentRun.id}/markdown`);if(!response.ok)throw new Error('Report export failed.');const blob=await response.blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`investigation-${currentRun.id}.md`;a.click();URL.revokeObjectURL(url);}catch(err){showError(err.message);}}
async function previewLinear(){try{const response=await fetch(`/api/runs/${currentRun.id}/markdown`);if(!response.ok)throw new Error('Unable to prepare the report.');$('linear-preview').textContent=await response.text();$('linear-dialog').showModal();}catch(err){showError(err.message);}}
$('confirm-linear').onclick=async()=>{const button=$('confirm-linear');button.disabled=true;try{await api(`/api/runs/${currentRun.id}/linear`,{method:'POST',body:JSON.stringify({confirm:true})});$('linear-dialog').close();await pollRun(currentRun.id);}catch(err){$('linear-dialog').close();showError(err.message);}finally{button.disabled=false;}};
$('cancel-linear').onclick=()=>$('linear-dialog').close();
$('bug-form').onsubmit=async event=>{event.preventDefault();if(busy)return;clearError();setBusy(true);$('result').hidden=true;$('empty-state').hidden=true;$('progress').hidden=false;$('progress-stage').textContent='Queued';$('progress-bar').value=5;
  const mode=document.querySelector('input[name="mode"]:checked').value;
  try{const run=await api('/api/runs',{method:'POST',body:JSON.stringify({text:$('description').value,mode,sample:$('sample').value,user_id:mode==='connected'?$('user-id').value:'',repository:mode==='connected'?$('repository').value:'.',diagnostics:mode==='connected'?$('diagnostics').value:'',start:mode==='connected'&&$('start').value?new Date($('start').value).toISOString():'',end:mode==='connected'&&$('end').value?new Date($('end').value).toISOString():''})});await pollRun(run.id);}catch(err){setBusy(false);$('progress').hidden=true;showError(err.message);}
};
document.addEventListener('click',event=>{const button=event.target.closest('[data-run]');if(button)openRun(button.dataset.run);});
for(const view of ['investigate','history','integrations'])$(`nav-${view}`).onclick=()=>switchView(view);
$('manage-integrations').onclick=()=>switchView('integrations');$('new-investigation').onclick=()=>{switchView('investigate');$('description').focus();};
document.querySelectorAll('input[name="mode"]').forEach(input=>input.onchange=setMode);$('sample').onchange=loadSample;
async function init(){try{config=await api('/api/config');renderIntegrations();loadSample();await refreshHistory();}catch(err){showError('Could not connect to the local server. '+err.message);$('submit').disabled=true;}}
init();
