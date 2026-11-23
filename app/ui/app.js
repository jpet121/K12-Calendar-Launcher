'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
  const display = value => value === null || value === undefined || value === '' ? '—' : typeof value === 'boolean' ? (value ? 'Yes' : 'No') : String(value);
  const warningStatus = value => /fail|pending|unknown|conflict|reject|block|limit|stop|skip|partial/i.test(String(value || ''));
  const tokenKey = 'k12-calendar-launcher-session';
  let token = location.hash.slice(1);
  try { if (token) sessionStorage.setItem(tokenKey, token); else token = sessionStorage.getItem(tokenKey) || ''; } catch (_) { /* Token remains in this window's memory. */ }
  if (location.hash) history.replaceState(null, '', location.pathname);
  let state = {signed_in:false,busy:false}, posting = false, polling = false, lastRendered = '', localErrorText = '', snapshot = null, selectedCalendar = '', previews = {}, receiptHistory = [];
  let currentEnvironmentKey = '', receiptStorageKey = '', urlEdited = false; // Never read the old unscoped receipt cache.
  const friendlyJobs = {select_environment:'Switching Oracle environment',connect:'Connecting to Oracle',read:'Reading calendar data',export:'Exporting calendar data',preview_create:'Validating workbook',apply_create:'Building calendars',preview_calendar_edit:'Reviewing calendar correction',apply_calendar_edit:'Applying calendar correction',preview_event_edit:'Reviewing shared event correction',apply_event_edit:'Applying event correction',check_delete:'Checking deletion requirements',reconcile_calendar:'Reconciling calendar correction',reconcile_event:'Reconciling event correction',prepare_restore:'Reviewing event restoration',disconnect:'Signing out',prepare_acceptance:'Preparing live acceptance test',run_acceptance:'Running live acceptance test'};

  function tab(name) {
    document.querySelectorAll('.panel').forEach(node => node.hidden = node.id !== 'panel-' + name);
    document.querySelectorAll('.tab').forEach(node => { const selected = node.dataset.tab === name; node.classList.toggle('active', selected); node.setAttribute('aria-selected', String(selected)); });
  }
  document.querySelectorAll('.tab').forEach(node => node.addEventListener('click', () => tab(node.dataset.tab)));
  $('recovery-create').addEventListener('click', () => tab('create'));

  function notice(container, text, kind='') { const node = el('div', text, 'notice' + (kind ? ' '+kind : '')); container.append(node); return node; }
  function typedOrigin() {
    try { const parsed=new URL($('oracle-url').value.trim()); return parsed.protocol==='https:' && !parsed.username && !parsed.password ? parsed.origin : ''; } catch (_) { return ''; }
  }
  function controls() {
    const busy = state.busy || posting;
    const pendingTarget = urlEdited;
    const allowedWhileChanging = ['connect-button','switch-environment','disconnect','close-app'];
    document.querySelectorAll('button').forEach(button => {
      if (button.classList.contains('tab') || button.id === 'recovery-create') return;
      button.disabled = !token || busy || ((pendingTarget || !state.environment) && !allowedWhileChanging.includes(button.id)) || (button.hasAttribute('data-auth') && !state.signed_in) || button.dataset.blocked === 'true';
      if (button.dataset.previewKind) button.disabled = button.disabled || !previews[button.dataset.previewKind] || !state.signed_in || (button.dataset.checkId && !$(button.dataset.checkId).checked);
    });
    const needsCredentials = !state.signed_in || pendingTarget;
    $('credential-fields').hidden = !needsCredentials;
    $('disconnect').hidden = !state.signed_in;
    $('connection-label').textContent = pendingTarget ? 'Connect to the pasted environment' : state.signed_in ? 'Connected to '+display(state.profile_name) : state.environment ? 'Not connected' : 'Choose environment';
    $('connection-note').textContent = pendingTarget ? 'Enter credentials for this URL and click Connect. Calendar actions are paused until connection succeeds.' : state.signed_in ? 'Credentials stay in this launcher process until sign-out or close. Paste another URL to switch environments.' : 'Paste your Oracle URL, then enter your approved Oracle REST credentials.';
    $('connection-dot').className = 'dot' + (state.signed_in && !pendingTarget ? ' connected' : '');
    $('oracle-url').disabled = !token || busy;
    $('username').disabled = !token || busy || !needsCredentials;
    $('password').disabled = !token || busy || !needsCredentials;
    $('environment-select').disabled = !token || busy;
    $('switch-environment').disabled = !token || busy || !$('environment-select').value || ($('environment-select').value===state.profile_id && !pendingTarget);
    $('connect-button').disabled = !token || busy || !typedOrigin() || !$('username').value.trim() || !$('password').value || !needsCredentials;
    $('environment-target').textContent = pendingTarget ? (typedOrigin() ? typedOrigin()+' — connect to verify this target' : 'Enter a full HTTPS Oracle URL.') : state.environment || 'Choose environment';
    for(const [id,path] of [['open-oracle',''],['open-calendar-events','/fscmUI/redwood/org-calendar-event/calendar-event/home']]){
      const link=$(id);if(pendingTarget || !state.environment){link.removeAttribute('href');link.setAttribute('aria-disabled','true');link.title='Connect to the selected Oracle URL first.';}else{link.href=state.environment+path;link.removeAttribute('aria-disabled');link.removeAttribute('title');}
    }
  }
  function markURLChanged() {
    if (!urlEdited) {
      $('username').value=''; previews={}; snapshot=null; selectedCalendar='';
      for(const id of ['create-preview','edit-preview','delete-preview','recovery-preview','acceptance-preview','selected-calendar'])$(id).hidden=true;
      for(const id of ['calendar-edit-select','event-edit-select','delete-select','inventory-counts','operation-result'])$(id).replaceChildren();
      renderInventory();
    }
    urlEdited=true; $('username').value=''; $('password').value=''; controls();
  }
  function localError(message) { localErrorText=message;$('operation-error').textContent = message; $('operation-error').hidden = false; $('activity-label').textContent = 'Action needed'; $('activity-dot').className = 'dot failed'; }

  async function api(path, data) {
    const options = {method:data === undefined ? 'GET' : 'POST', headers:{'X-Launcher-Token':token}, credentials:'same-origin', cache:'no-store', redirect:'error'};
    if (data !== undefined) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
    const response = await fetch(path, options);
    let value;
    try { value = await response.json(); } catch (_) { throw new Error('The local launcher returned an unreadable response. Keep the console open and check its status.'); }
    if (!response.ok) throw new Error(value.error || 'The local request could not be completed.');
    return value;
  }

  async function action(name, data={}) {
    if (posting || state.busy) return;
    if (!state.environment && !['connect','select_environment','disconnect'].includes(name)) { localError('Choose an Oracle environment and connect before continuing.'); return; }
    if (urlEdited && !['connect','select_environment','disconnect'].includes(name)) { localError('Connect to the pasted Oracle URL before continuing.'); return; }
    posting = true; localErrorText='';controls(); $('operation-error').hidden = true; $('activity-label').textContent = friendlyJobs[name] || name; $('activity-dot').className = 'dot running';
    try {
      const result = await api('/api/action', {action:name,expected_profile_id:state.profile_id,expected_environment:state.environment,...data});
      state.busy = true; state.job_name = name; state.job_id = result.job_id;
      $('progress-details').open = true;
      if (name.startsWith('apply_')) { previews = {}; document.querySelectorAll('[data-preview-kind]').forEach(button => button.disabled = true); }
      await poll();
    } catch (error) { localError(error.message + ' No request was automatically retried.'); }
    finally { if (data.password) data.password = ''; posting = false; controls(); }
  }

  function table(headers, rows, actionColumn) {
    const node = el('table'), head = el('thead'), header = el('tr');
    headers.forEach(name => header.append(el('th', name))); if (actionColumn) header.append(el('th',''));
    head.append(header); node.append(head); const body = el('tbody');
    rows.forEach((row,index) => { const tr = el('tr'); row.forEach((value,column) => { const td = el('td', display(value)); if (column === 0 || column === 1) td.classList.add('wrap'); tr.append(td); }); if (actionColumn) { const td=el('td'); const button=el('button',actionColumn.label,'secondary row-button'); button.addEventListener('click',()=>actionColumn.run(index)); td.append(button);tr.append(td); } body.append(tr); });
    node.append(body); return node;
  }
  function tableInto(container, headers, rows, actionColumn) { container.replaceChildren(); if (!rows.length) container.append(el('p','No records to display.','empty')); else container.append(table(headers,rows,actionColumn)); }
  function stats(container, counts) { container.replaceChildren(); for (const [label,value] of counts) { const item=el('div',undefined,'stat'); item.append(el('strong',display(value)),el('span',label)); container.append(item); } }
  function selectOptions(select, rows, value, label) {
    const current=select.value; select.replaceChildren(); const blank=el('option','Choose a record'); blank.value=''; select.append(blank);
    rows.forEach(row=>{const option=el('option',label(row));option.value=String(row[value]);select.append(option);});
    if (rows.some(row=>String(row[value])===current)) select.value=current;
  }
  function acceptSnapshot(value) {
    snapshot=value; const calendars=snapshot.calendars || [];
    const counts=snapshot.counts || {}; stats($('inventory-counts'),[['Calendars',counts.calendars ?? calendars.length],['Distinct events',counts.events],['Event links',counts.links]]);
    for(const id of ['calendar-edit-select','delete-select']) selectOptions($(id),calendars,'CalendarId',row=>row.Name+' · '+row.CalendarId);
    const unique=new Map();calendars.forEach(calendar=>(calendar.events || []).forEach(event=>unique.set(String(event.CalendarEventId),event)));
    selectOptions($('event-edit-select'),[...unique.values()],'CalendarEventId',row=>(row.ShortCode || row.Name)+' · '+row.CalendarEventId);
    renderInventory(); fillCalendar(); fillEvent();
    if (selectedCalendar) showCalendar(selectedCalendar);
  }
  function renderInventory() {
    if(!snapshot){$('calendar-table').replaceChildren(el('p','Connect to Oracle to read your calendar inventory.','empty'));return;}
    const query=$('search-calendars').value.trim().toLowerCase();
    const rows=(snapshot.calendars || []).filter(row=>(row.Name+' '+row.CalendarId).toLowerCase().includes(query));
    tableInto($('calendar-table'),['Calendar','Oracle ID','Start date','End date','Events'],rows.map(row=>[row.Name,row.CalendarId,row.StartDate,row.EndDate,(row.events || []).length]),{label:'View events',run:index=>showCalendar(String(rows[index].CalendarId))});
  }
  function showCalendar(id) {
    const calendar=(snapshot?.calendars || []).find(row=>String(row.CalendarId)===id);if(!calendar){$('selected-calendar').hidden=true;return;}
    selectedCalendar=id;$('selected-calendar').hidden=false;$('selected-name').textContent=calendar.Name;$('selected-meta').textContent='Oracle calendar ID '+id+' · '+display(calendar.StartDate)+' to '+display(calendar.EndDate);
    const rows=calendar.events || [];tableInto($('event-table'),['Event','Code','Start timestamp','End timestamp','All day','Half day'],rows.map(row=>[row.Name,row.ShortCode,row.StartDateTime,row.EndDateTime,row.AllDayFlag,row.HalfDayForElapsed]),{label:'Edit dates',run:index=>{ $('event-edit-select').value=String(rows[index].CalendarEventId);fillEvent();tab('edit'); }});
    $('calendar-edit-select').value=id;$('delete-select').value=id;fillCalendar();
  }
  function fillCalendar(){const calendar=(snapshot?.calendars || []).find(row=>String(row.CalendarId)===$('calendar-edit-select').value);for(const[field,id]of[['Name','calendar-name'],['Description','calendar-description'],['StartDate','calendar-start'],['EndDate','calendar-end']])$(id).value=calendar?.[field] || '';previews.calendar_edit=null;controls();}
  function fillEvent(){const id=$('event-edit-select').value;let selected;for(const calendar of snapshot?.calendars || [])for(const event of calendar.events || [])if(String(event.CalendarEventId)===id)selected=event;$('event-start').value=selected?.StartDateTime || '';$('event-end').value=selected?.EndDateTime || '';previews.event_edit=null;controls();}
  $('search-calendars').addEventListener('input',renderInventory);$('calendar-edit-select').addEventListener('change',fillCalendar);$('event-edit-select').addEventListener('change',fillEvent);

  function previewCard(target,title){const container=$(target);container.hidden=false;container.replaceChildren();const card=el('div',undefined,'card');card.append(el('h3',title));notice(card,'Target: '+display(state.profile_name)+' · '+display(state.environment));container.append(card);return card;}
  function applyButton(card,kind,label,actionName,checkText){
    let checkId;
    if(checkText){checkId='review-'+kind;const labelNode=el('label',undefined,'review-check');const check=el('input');check.type='checkbox';check.id=checkId;check.addEventListener('change',controls);labelNode.append(check,document.createTextNode(checkText));card.append(labelNode);}
    const button=el('button',label);button.dataset.previewKind=kind;if(checkId)button.dataset.checkId=checkId;
    button.addEventListener('click',()=>{const id=previews[kind];if(id)action(actionName,{preview_id:id});});card.append(button);controls();
  }
  function renderPreview(result,job){
    const p=result.preview || {};previews[result.kind]=result.preview_id;
    if(result.kind==='create'){
      const card=previewCard('create-preview','Review request '+display(p.request_id));const group=el('div',undefined,'stats');const counts=p.counts || {};stats(group,[['Events',counts.new_events],['Calendars',counts.new_calendars],['Event links',counts.selected_links]]);card.append(group);
      const blockers=p.blockers || [];if(blockers.length){const warning=notice(card,'This workbook cannot be applied yet.','danger');const list=el('ul');blockers.forEach(text=>list.append(el('li',String(text))));warning.append(list);}
      const calendars=el('div',undefined,'table-wrap');tableInto(calendars,['Calendar','Start date','End date','Selected events'],(p.calendars || []).map(c=>[c.Name,c.StartDate,c.EndDate,(p.associations || []).filter(a=>a.calendar_key===c.key).length]));card.append(calendars);
      const events=el('div',undefined,'table-wrap');tableInto(events,['Event','Code','Start timestamp','End timestamp (exclusive)','All day','Half day'],(p.events || []).map(e=>[e.Name,e.ShortCode,e.StartDateTimeLocal,e.EndDateTimeLocal,e.expected_readback_AllDayFlag,e.HalfDayForElapsed]));card.append(events);
      card.append(el('p','The build uses the selected workbook and saves its progress. An unchanged rerun checks the same records.','caption'));
      applyButton(card,'create','Build calendars in Oracle','apply_create');const button=card.querySelector('[data-preview-kind]');button.dataset.blocked=String(blockers.length>0);controls();
    }else if(result.kind==='calendar_edit'){
      const card=previewCard('edit-preview','Review calendar correction');const before=p.before || {},after=p.after || {};const wrap=el('div',undefined,'table-wrap');tableInto(wrap,['Field','Current value','Proposed value'],['Name','Description','StartDate','EndDate'].map(key=>[key,display(before[key]),display(after[key])]));card.append(wrap);card.append(el('p',p.unchanged?'No values changed. Applying verifies the current record.':'Oracle will receive only the changed calendar fields.','caption'));applyButton(card,'calendar_edit','Apply reviewed calendar change','apply_calendar_edit');tab('edit');
    }else if(result.kind==='event_edit'){
      const target=job==='prepare_restore'?'recovery-preview':'edit-preview';const card=previewCard(target,job==='prepare_restore'?'Review event date restoration':'Review shared event correction');const old=p.old || p.before || {},next=p.new || p.after || {};
      card.append(el('p',(old.Name || old.ShortCode || 'Event')+' · Oracle event ID '+display(p.CalendarEventId)));const wrap=el('div',undefined,'table-wrap');tableInto(wrap,['Field','Current timestamp','Proposed timestamp'],['StartDateTime','EndDateTime'].map(key=>[key,old[key],next[key]]));card.append(wrap);
      const affected=p.affected_calendars || [];notice(card,'This event is used by '+affected.length+' visible calendar'+(affected.length===1?'':'s')+'. Its dates change in every calendar that uses it.');if(p.scope)card.append(el('p',p.scope,'caption'));
      const list=el('div',undefined,'table-wrap');tableInto(list,['Affected calendar','Oracle ID','Start date','End date'],affected.map(c=>[c.Name,c.CalendarId,c.StartDate,c.EndDate]));card.append(list);
      applyButton(card,'event_edit',job==='prepare_restore'?'Apply reviewed restoration':'Apply event dates to all affected calendars','apply_event_edit','I have reviewed the date boundaries and shared calendar impact.');tab(job==='prepare_restore'?'recovery':'edit');
    }else if(result.kind==='acceptance'){
      const card=previewCard('acceptance-preview','Review the live acceptance test');const counts=p.counts || {};const group=el('div',undefined,'stats');stats(group,[['New events',counts.events],['New calendars',counts.calendars],['New links',counts.links]]);card.append(group);
      const names=el('ul',undefined,'result-names');(p.calendar_names || []).forEach(name=>names.append(el('li',String(name))));card.append(names);
      const steps=el('ol');(p.steps || []).forEach(step=>steps.append(el('li',String(step))));card.append(steps);
      const data=p.event_date_test || {};if(data.enabled){const tableWrap=el('div',undefined,'table-wrap');tableInto(tableWrap,['Event date test','Original value','Temporary value'],[['Start',data.original_start,data.temporary_start],['End (exclusive)',data.original_end,data.temporary_end]]);card.append(tableWrap);}else notice(card,'Event date edits are skipped until the administrator has confirmed complete calendar visibility and no legacy event coverage.');
      notice(card,p.retention || 'The new test calendars and events remain available for review. No deletion is performed.');
      applyButton(card,'acceptance','Run reviewed live acceptance test','run_acceptance','I have reviewed the new records, temporary corrections, restoration, and retained test data.');tab('recovery');
    }else if(result.kind==='delete'){
      const card=previewCard('delete-preview','Deletion check: '+display(p.calendar?.Name || p.calendar_id));notice(card,p.reason || 'Deletion is blocked until complete assignment proof is available.','danger');card.append(el('p','User assignments: '+(p.assignment_count===null || p.assignment_count===undefined?'not proven':p.assignment_count)+' · Events: '+display(p.events)+' · Rule subscriptions: '+display(p.rule_subscriptions),'caption'));card.append(el('p','No calendar was deleted.'));tab('delete');
    }
  }
  function rememberReceipt(result,job){
    const receipt=result.receipt || result.report || result;const id=result.recovery_receipt_id || result.receipt_id || receipt.receipt_id;if(!id || typeof id!=='string')return;
    const type=(job.includes('event') || job==='prepare_restore' || receipt.operation==='event_date_edit')?'event':'calendar';
    receiptHistory=receiptHistory.filter(row=>row.id!==id);receiptHistory.unshift({id,type,status:String(receipt.status || receipt.state || result.status || 'recorded')});receiptHistory=receiptHistory.slice(0,30);
    try{if(receiptStorageKey)sessionStorage.setItem(receiptStorageKey,JSON.stringify(receiptHistory));}catch(_){}
    renderReceipts();$('receipt-id').value=id;$('recovery-type').value=type==='event'?'reconcile_event':'reconcile_calendar';
  }
  function renderReceipts(){const select=$('receipt-select'),previous=select.value;select.replaceChildren();const blank=el('option','Select a recent receipt');blank.value='';select.append(blank);receiptHistory.forEach(row=>{if(!row || typeof row.id!=='string')return;const option=el('option',row.type+' · '+row.status+' · '+row.id);option.value=row.id;select.append(option);});if(receiptHistory.some(row=>row.id===previous))select.value=previous;}
  $('receipt-select').addEventListener('change',()=>{const row=receiptHistory.find(r=>r.id===$('receipt-select').value);if(row){$('receipt-id').value=row.id;$('recovery-type').value=row.type==='event'?'reconcile_event':'reconcile_calendar';}});

  async function download(id,filename,environmentKey){
    const currentTarget=()=>!urlEdited && state.signed_in && environmentKey && environmentKey===currentEnvironmentKey;
    try{if(!currentTarget())throw new Error('Connect to the export\'s Oracle environment and export it again.');const response=await fetch('/api/download/'+encodeURIComponent(id),{headers:{'X-Launcher-Token':token},cache:'no-store',credentials:'same-origin',redirect:'error'});if(!response.ok)throw new Error('The export is no longer available. Export it again.');const blob=await response.blob();if(!currentTarget())throw new Error('The Oracle environment changed. Export again from the selected environment.');const url=URL.createObjectURL(blob);const a=el('a');a.href=url;a.download=filename || 'Calendar_Export.zip';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);}catch(error){localError(error.message);}
  }
  function renderResult(result,job){
    if(job==='connect' && result.status==='connected'){urlEdited=false;$('oracle-url').value=state.environment || '';previews={};for(const id of ['create-preview','edit-preview','delete-preview','recovery-preview'])$(id).hidden=true;}
    if(result.snapshot)acceptSnapshot(result.snapshot);
    if(result.preview_id)renderPreview(result,job);
    const container=$('operation-result');container.replaceChildren();
    const status=String(result.status || result.state || (result.preview_id?'ready_for_review':'completed'));
    const warning=warningStatus(status);
    const labels={environment_selected:'Environment selected. Enter credentials for this target.',connected:'Connected. Calendar inventory is ready.',signed_out:'Signed out. Oracle credentials cleared.',read_verified:'Calendar inventory refreshed from Oracle.',exported:'Export created from a fresh Oracle read.',ready_for_review:'Review ready. Nothing has been submitted.',applied_verified:'Build completed and verified in Oracle.',updated_verified:'Calendar correction verified in Oracle.',verified:'Operation verified in Oracle.',no_change:'No change needed. Current state verified.',unchanged_verified:'No change needed. Calendar state verified.',reconciled_applied:'The saved calendar correction is present in Oracle.',reconciled_original_state:'Oracle matches the original calendar values. No edit was retried.',completed_with_limitations:'Enabled tests completed. Limitations and skipped checks remain.',stopped:'The operation stopped before completion. Review the saved outcome before continuing.',skipped:'This operation was skipped and is not verified.'};
    const text=labels[status] || 'Operation status: '+status.replaceAll('_',' ');
    const failed=/fail|stop|conflict|reject|unknown/i.test(status);
    notice(container,text,failed?'danger':warning || result.preview_id?'':'success');
    const errors=[result.error,result.detail,result.report?.error,result.report?.detail,result.creation?.error].filter(value=>typeof value==='string' && value.trim());
    [...new Set(errors)].forEach(message=>notice(container,message,'danger'));
    if(result.environment)container.append(el('p','Oracle target: '+(result.profile_name || result.profile_id || '')+' · '+result.environment,'caption'));
    if(result.conclusion)container.append(el('p',String(result.conclusion)));
    if(result.calendar_names){const list=el('ul',undefined,'result-names');result.calendar_names.forEach(name=>list.append(el('li',String(name))));container.append(list);container.append(el('p',warning?'These are the requested calendar names. Check their recorded outcomes and current Oracle state.':'Search these exact names in Organization Calendars in Oracle.','caption'));}
    const counts=result.report?.result_counts || result.result_counts;if(counts){const group=el('div',undefined,'stats');stats(group,[['Events',counts.events],['Calendars',counts.calendars],['Links',counts.links]]);container.append(group);}
    const resultEnvironmentKey=currentEnvironmentKey;
    if(result.download_id){const button=el('button','Download '+(result.filename || 'export'),'secondary');button.addEventListener('click',()=>download(result.download_id,result.filename,resultEnvironmentKey));container.append(button);}
    if(result.receipt_download?.download_id){const saved=result.receipt_download;const button=el('button','Download operation receipt','secondary');button.addEventListener('click',()=>download(saved.download_id,saved.filename,resultEnvironmentKey));container.append(button);}
    const receiptId=result.recovery_receipt_id || result.receipt_id || result.report?.receipt_id;if(receiptId)container.append(el('p','Receipt ID: '+receiptId,'caption'));
    if(job==='run_acceptance'){
      if(result.acceptance_id)container.append(el('p','Acceptance request: '+result.acceptance_id,'caption'));
      const stages=result.steps || {};const stageRows=Object.entries(stages).map(([name,detail])=>[name.replaceAll('_',' '),detail.status || 'not recorded',detail.reason || (detail.write_requests===0?'Zero write requests':detail.result_status || '')]);
      const stageTable=el('div',undefined,'table-wrap');tableInto(stageTable,['Acceptance check','Outcome','Detail'],stageRows);container.append(stageTable);
      if(result.deletion?.reason)notice(container,'Deletion remains blocked. '+result.deletion.reason);
      (result.possible_temporary_changes || []).forEach(message=>notice(container,String(message),'danger'));
      if(result.record_retention)container.append(el('p',String(result.record_retention),'caption'));
      if(result.recovery_required)notice(container,'To resume this same saved test, open Recovery, select Prepare live acceptance test, review the saved request, then run it again. The launcher reconciles saved work before continuing.');
    }
    $('receipt-details').hidden=false;$('receipt-json').textContent=JSON.stringify(result,null,2);rememberReceipt(result,job);
    if(job==='disconnect')clearEnvironmentUI();
    if(job.startsWith('apply_') && !warning)container.append(el('p','Use Read & export → Refresh from Oracle to review the latest inventory here.','caption'));
  }
  function clearEnvironmentUI(){
    snapshot=null;previews={};selectedCalendar='';receiptHistory=[];lastRendered='';localErrorText='';
    $('username').value='';$('password').value='';$('workbook').value='';$('receipt-id').value='';$('receipt-select').value='';$('search-calendars').value='';
    for(const id of ['calendar-name','calendar-description','calendar-start','calendar-end','event-start','event-end'])$(id).value='';
    for(const id of ['calendar-edit-select','event-edit-select','delete-select'])$(id).replaceChildren();
    for(const id of ['create-preview','edit-preview','delete-preview','recovery-preview','acceptance-preview','selected-calendar','receipt-details','operation-error'])$(id).hidden=true;
    for(const id of ['inventory-counts','operation-result','receipt-json','progress-log'])$(id).replaceChildren();
    renderReceipts();renderInventory();
  }
  function acceptEnvironment(next){
    const key=next.environment_state_key || '';
    if(key!==currentEnvironmentKey){
      urlEdited=false;$('oracle-url').value=next.environment || '';
      clearEnvironmentUI();currentEnvironmentKey=key;receiptStorageKey=key?'k12-calendar-receipts:'+key:'';
      try{receiptHistory=JSON.parse(sessionStorage.getItem(receiptStorageKey) || '[]');if(!Array.isArray(receiptHistory))receiptHistory=[];}catch(_){receiptHistory=[];}
      renderReceipts();
    }
    if(!urlEdited)$('oracle-url').value=next.environment || '';
    const select=$('environment-select');
    const profileSignature=JSON.stringify(next.profiles || []);
    if(select.dataset.signature!==profileSignature){select.replaceChildren();const blank=el('option','Choose a saved environment');blank.value='';select.append(blank);for(const profile of next.profiles || []){const option=el('option',profile.name+' · '+profile.environment);option.value=profile.id;select.append(option);}select.dataset.signature=profileSignature;select.value=next.profile_id || '';}
    if(select.dataset.active!==next.profile_id){select.value=next.profile_id;select.dataset.active=next.profile_id;}
    $('environment-target').textContent=next.environment || 'Choose environment';
    $('environment-eyebrow').textContent='ORACLE HCM · '+(next.profile_name || 'Choose environment');
    $('environment-footer').textContent=(next.profile_name ? next.profile_name+' · ' : '')+'Keep the launcher console open while an operation runs.';
    document.title='Calendar Launcher · '+(next.profile_name || 'Choose environment');
    if(next.environment){$('open-oracle').href=next.environment;$('open-calendar-events').href=next.environment+'/fscmUI/redwood/org-calendar-event/calendar-event/home';}
  }
  async function poll(){
    if(polling || !token)return;polling=true;
    try{
      const next=await api('/api/state');acceptEnvironment(next);state=next;$('startup-error').hidden=true;$('version').textContent='Calendar Launcher '+display(state.version);
      let added=false;for(const row of state.receipts || []){if(!row || !row.receipt_id || row.operation==='create')continue;if(!receiptHistory.some(saved=>saved.id===row.receipt_id)){receiptHistory.unshift({id:row.receipt_id,type:row.operation==='event_edit'?'event':'calendar',status:'saved'});added=true;}}if(added)renderReceipts();
      const outcomeWarning=warningStatus(state.result?.status || state.result?.state);
      $('activity-label').textContent=state.busy?(friendlyJobs[state.job_name] || 'Operation running'):(state.error || localErrorText)?'Action needed':outcomeWarning?'Review operation outcome':state.result?'Operation complete':'Ready';
      $('activity-dot').className='dot'+(state.busy?' running':(state.error || localErrorText)?' failed':outcomeWarning?' warning':state.result?' connected':'');$('job-label').textContent=state.busy?'Please keep this launcher open.':'';
      $('progress-log').textContent=state.logs?.length?state.logs.join('\n'):'No operation has started.';if(state.busy)$('progress-log').scrollTop=$('progress-log').scrollHeight;
      const identity=String(state.job_id)+':'+String(state.busy)+':'+String(state.error || '')+':'+Boolean(state.result);
      if(!state.busy && identity!==lastRendered){lastRendered=identity;$('operation-error').hidden=!state.error;if(state.error){$('operation-error').textContent=state.error;const receipt=state.error_receipt || state.receipt;if(receipt)rememberReceipt(receipt,state.job_name || '');}if(state.result && (!urlEdited || (state.job_name==='connect' && state.result.status==='connected')))renderResult(state.result,state.job_name || '');}
      controls();
    }catch(error){$('startup-error').textContent=error.message;$('startup-error').hidden=false;}
    finally{polling=false;}
  }

  $('oracle-url').addEventListener('input',markURLChanged);
  ['username','password'].forEach(id=>$(id).addEventListener('input',controls));
  $('environment-select').addEventListener('change',()=>{const profile=(state.profiles || []).find(row=>row.id===$('environment-select').value);if(profile){$('oracle-url').value=profile.environment;markURLChanged();}});
  $('environment-form').addEventListener('submit',event=>{event.preventDefault();action('select_environment',{profile_id:$('environment-select').value});});
  $('connect-form').addEventListener('submit',event=>{event.preventDefault();if(state.signed_in && !urlEdited)return;if(!typedOrigin() || !$('username').value.trim() || !$('password').value){localError('Enter an HTTPS Oracle URL and credentials for that environment.');return;}const data={environment_url:$('oracle-url').value.trim(),username:$('username').value,password:$('password').value};$('password').value='';action('connect',data);});
  $('disconnect').addEventListener('click',()=>action('disconnect'));
  $('refresh').addEventListener('click',()=>action('read'));$('export').addEventListener('click',()=>action('export'));
  $('sample-preview').addEventListener('click',()=>action('preview_create',{example:true}));
  $('create-form').addEventListener('submit',async event=>{event.preventDefault();const file=$('workbook').files[0];if(!file)return;if(!/\.xlsx$/i.test(file.name) || file.size>12*1024*1024){localError('Choose an .xlsx workbook no larger than 12 MB.');return;}try{const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error('The selected workbook could not be read.'));reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.readAsDataURL(file);});await action('preview_create',{content_base64:data,filename:file.name});}catch(error){localError(error.message);}});
  $('workbook').addEventListener('change',()=>{previews.create=null;$('create-preview').hidden=true;controls();});
  $('calendar-edit-form').addEventListener('submit',event=>{event.preventDefault();action('preview_calendar_edit',{calendar_id:$('calendar-edit-select').value,changes:{Name:$('calendar-name').value,Description:$('calendar-description').value,StartDate:$('calendar-start').value,EndDate:$('calendar-end').value}});});
  $('event-edit-form').addEventListener('submit',event=>{event.preventDefault();action('preview_event_edit',{event_id:$('event-edit-select').value,start:$('event-start').value,end:$('event-end').value});});
  ['calendar-name','calendar-description','calendar-start','calendar-end'].forEach(id=>$(id).addEventListener('input',()=>{previews.calendar_edit=null;controls();}));
  ['event-start','event-end'].forEach(id=>$(id).addEventListener('input',()=>{previews.event_edit=null;controls();}));
  $('delete-form').addEventListener('submit',event=>{event.preventDefault();action('check_delete',{calendar_id:$('delete-select').value});});
  $('recovery-form').addEventListener('submit',event=>{event.preventDefault();action($('recovery-type').value,{receipt_id:$('receipt-id').value.trim()});});
  const acceptanceCard=el('div',undefined,'card form-card');acceptanceCard.append(el('h3','Run the live acceptance test'),el('p','Prepare an isolated 3-event, 2-calendar test. Review the exact new names before it builds, checks, corrects, restores, and reruns the request.'));
  const prepareAcceptance=el('button','Prepare live acceptance test','secondary');prepareAcceptance.setAttribute('data-auth','');prepareAcceptance.addEventListener('click',()=>action('prepare_acceptance'));acceptanceCard.append(prepareAcceptance);const acceptancePreview=el('div',undefined,'preview');acceptancePreview.id='acceptance-preview';acceptancePreview.hidden=true;$('panel-recovery').append(acceptanceCard,acceptancePreview);
  const eventScope=el('p','Event corrections are available only after an administrator confirms complete shared-calendar visibility and no legacy event coverage.','notice');$('event-edit-form').append(eventScope);
  $('close-app').addEventListener('click',async()=>{try{await api('/api/close',{});token='';try{sessionStorage.removeItem(tokenKey);}catch(_){}state={signed_in:false,busy:false};controls();$('startup-error').textContent='Launcher closed. You can close this browser tab. Use START-CALENDAR-LAUNCHER.cmd to open it again.';$('startup-error').hidden=false;document.querySelectorAll('button').forEach(button=>button.disabled=true);}catch(error){localError(error.message);}});
  if(!token){$('startup-error').textContent='Open START-CALENDAR-LAUNCHER.cmd to launch this page with its local session. Do not open the HTML file directly.';$('startup-error').hidden=false;}
  renderReceipts();renderInventory();controls();poll();setInterval(poll,1200);
})();
