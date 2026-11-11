import {createOffers} from '/offers.js';
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const euro = n => n == null ? '—' : new Intl.NumberFormat('fr-FR',{style:'currency',currency:'EUR'}).format(n);
const date = value => value ? new Date(value).toLocaleString('fr-FR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : 'Pas encore lancé';
const clone = value => JSON.parse(JSON.stringify(value));
const uid = () => globalThis.crypto?.randomUUID?.() || 'profile-'+Date.now()+'-'+Math.random().toString(36).slice(2);
let state, draft, selected, dirty=false, busy=false, view='feed', filter='matches', cursor=null, toastTimer, feedSignature='', itemsVersion=null, feedLimit=60, feedTimer;
const systemTheme = matchMedia('(prefers-color-scheme: dark)');

function toast(text,error=false){$('#toast').textContent=text;$('#toast').className=error?'error':'';$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6500);}
function markDirty(){dirty=true;$('#dirty').textContent='Modifications non enregistrées';$('#settings-dirty').textContent='Modifications non enregistrées';}
async function post(path,body){
  if(!state)throw new Error('Le serveur local n’est pas encore connecté.');
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Radar-Token':state.token},body:JSON.stringify(body)});
  const data=await r.json();if(!r.ok)throw new Error(data.error||'Erreur serveur');return data;
}
function appearance(){
  if(!draft)return;const a=draft.appearance;
  document.documentElement.dataset.theme=a.theme==='system'?(systemTheme.matches?'dark':'light'):a.theme;
  document.documentElement.dataset.accent=a.accent;document.documentElement.dataset.density=a.density;
  $$('#view-settings [data-appearance]').forEach(el=>{el.type==='checkbox'?el.checked=a[el.dataset.appearance]:el.value=a[el.dataset.appearance];});
  $$('[data-layout]').forEach(b=>b.classList.toggle('selected',b.dataset.layout===a.layout));
}
systemTheme.addEventListener('change',appearance);
const offers=createOffers({$,$$,esc,euro,date,post,toast});
const paths={contemporary:'/contemporary',ledger:'/bilan'};
const fromPath=()=>Object.keys(paths).find(k=>paths[k]===location.pathname)||'feed';
function navigate(next,push=true){
  view=next;const section=next==='favorites'?'feed':next;
  $$('.view').forEach(el=>el.hidden=el.id!=='view-'+section);
  $$('nav [data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===next));
  $('#breadcrumb').textContent={feed:'Annonces',favorites:'Ma sélection',profiles:'Recherches',events:'Activité',settings:'Préférences',data:'Données & connexion',contemporary:'Contemporain',ledger:'Bilan'}[next];
  if(push&&history.state?.view!==next)history.pushState({view:next},'',paths[next]||'/');
  $('nav [data-view="'+next+'"]')?.scrollIntoView({inline:'center',block:'nearest'});
  paths[next]?offers.show(next):offers.hide();
  $('#feed-title').textContent=next==='favorites'?'Ma sélection':'Les annonces repérées';
  $('#feed-subtitle').textContent=next==='favorites'?'Les annonces que tu as mises de côté.':'Des recherches à toi. Les annonces qui vont avec.';
  $('.segments').hidden=next==='favorites';
  if(state){if(section==='feed')renderFeed(true);if(section==='profiles')renderEditor();}
}
function match(item){return item.matches.find(m=>!$('#profile-filter').value||m.profile_id===$('#profile-filter').value)||item.matches[0];}
function renderFeed(force=false){
  if(!state)return;
  const a=draft.appearance, text=$('#search').value.toLowerCase(), profile=$('#profile-filter').value, sort=$('#sort').value;
  let items=state.items.filter(i=>(i.title+' '+i.brand+' '+i.seller).toLowerCase().includes(text));
  items=items.filter(i=>view==='favorites'?i.favorite:filter==='hidden'?i.hidden:!i.hidden);
  if(view!=='favorites'&&filter==='matches')items=items.filter(i=>i.matches.length);
  if(profile)items=items.filter(i=>i.matches.some(m=>m.profile_id===profile));
  items.sort((x,y)=>sort==='price'?x.price-y.price:sort==='margin'?(match(y)?.margin??-1e9)-(match(x)?.margin??-1e9):sort==='score'?(match(y)?.score??-1)-(match(x)?.score??-1):y.first_seen.localeCompare(x.first_seen));
  const signature=JSON.stringify([itemsVersion,items.length,a,view,filter,profile,sort,text,feedLimit]);
  if(!force&&signature===feedSignature)return;feedSignature=signature;
  $('#listings').className='listings'+(a.layout==='grid'?' grid':'');
  $('#result-count').textContent=items.length+' annonce'+(items.length!==1?'s':'')+' affichée'+(items.length!==1?'s':'');
  $$('[data-filter]').forEach(b=>b.classList.toggle('selected',b.dataset.filter===filter));
  if(!items.length){
    const empty=!state.items.length;
    $('#listings').innerHTML=`<div class="empty"><span class="empty-mark">↗</span><h2>${empty?'Une première recherche, et c’est parti.':'Rien ici pour le moment.'}</h2><p>${empty?'Les profils SO-DIMM sont prêts. Tu peux les ajuster, créer une recherche libre, puis lancer la veille.':'Change les filtres ou consulte tout le flux. Les annonces écartées restent consultables.'}</p><div class="empty-actions"><button data-action="new-profile">Créer une recherche</button><button data-view="profiles">Voir mes recherches</button></div></div>`;return;
  }
  const shown=items.slice(0,feedLimit);
  $('#listings').innerHTML=shown.map(i=>{
    const m=match(i),s=m?.specs||{}, tags=[i.brand,s.generation?'DDR'+s.generation:'',s.capacity?s.capacity+' Go':'',s.count?s.count+' barrettes':''];
    const image=i.image?`<img class="listing-image" src="${esc(i.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:'<div class="listing-image image-placeholder">PHOTO —</div>';
    return `<article class="listing">${image}<div class="listing-info"><a class="listing-title" href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">${esc(i.title)}</a><div class="listing-meta">${tags.filter(Boolean).map(t=>`<span>${esc(t)}</span>`).join('<span>·</span>')}<span>${date(i.first_seen)}</span>${i.source!=='vinted'?'<span class="label">Import</span>':''}</div><div class="listing-profile">${m?esc(m.profile_name):'Hors critères'}${a.show_score&&m?' · Score '+m.score+'/100':''}</div></div><div class="listing-price">${euro(i.price)}<small>${m?.per_gb!=null?euro(m.per_gb)+'/Go':'Prix de l’annonce'}</small></div><div class="listing-margin">${a.show_margin?euro(m?.margin):''}<small>${a.show_margin?'Marge hypothétique':''}</small></div><div class="listing-actions"><button class="star ${i.favorite?'saved':''}" data-favorite="${esc(i.id)}" aria-label="${i.favorite?'Retirer de':'Ajouter à'} ma sélection">${i.favorite?'★':'☆'}</button><button data-detail="${esc(i.id)}">Détails</button></div></article>`;
  }).join('')+(items.length>shown.length?`<button class="more" data-more="feed">Afficher plus (${items.length-shown.length})</button>`:'');
}
function field(p,key,label,type='text',wide=false,help=''){
  const value=Array.isArray(p[key])?p[key].join(type==='lines'?'\n':', '):p[key];
  const attrs=`data-key="${key}" data-format="${type}"`;
  const input=type==='lines'?`<textarea ${attrs} rows="3">${esc(value)}</textarea>`:`<input ${attrs} type="${type==='number'?'number':'text'}" ${type==='number'?'min="0" step="any"':''} value="${esc(value)}">`;
  return `<label class="${wide?'wide':''}">${label}${input}${help?'<small>'+help+'</small>':''}</label>`;
}
function check(p,key,label){return `<label class="check"><input type="checkbox" data-key="${key}" ${p[key]?'checked':''}>${label}</label>`;}
function current(){return draft?.profiles.find(p=>p.id===selected);}
function renderProfileList(){
  $('#profile-list').innerHTML=draft.profiles.map(p=>`<button class="profile-entry ${p.id===selected?'selected':''}" data-select-profile="${esc(p.id)}"><span><strong>${esc(p.name)}</strong><small>${{ram:'Mémoire RAM',clothing:'Vêtements'}[p.kind]||'Recherche libre'} · ${euro(p.max_price)} max.</small></span><i class="${p.enabled?'on':''}"></i></button>`).join('');
}
function renderEditor(){
  if(!draft)return;if(!current())selected=draft.profiles[0].id;const p=current();renderProfileList();
  $('#profile-editor').innerHTML=`<article class="surface"><div class="editor-header"><h2>Réglages de la recherche</h2>${check(p,'enabled','Recherche active')}</div><div class="form-grid">${field(p,'name','Nom de la recherche')}<label>Type de recherche<select data-key="kind"><option value="general" ${p.kind==='general'?'selected':''}>Tous les produits</option><option value="clothing" ${p.kind==='clothing'?'selected':''}>Vêtements · marques et créateurs</option><option value="ram" ${p.kind==='ram'?'selected':''}>Mémoire RAM · filtres techniques</option></select></label>${field(p,'queries','Recherches sur Vinted','lines',true,'Une recherche par ligne. Exemple : Nintendo Switch OLED.')}${field(p,'min_price','Prix minimum (€)','number')}${field(p,'max_price','Prix maximum (€)','number')}</div>
    <details class="profile-section"><summary>Partir d’un lien de recherche Vinted</summary><p class="help">Colle une recherche Vinted France pour reprendre ses mots-clés, prix et identifiants de filtres.</p><div class="url-row"><input id="search-url" aria-label="URL de recherche Vinted" placeholder="https://www.vinted.fr/catalog?…"><button data-action="parse-url">Appliquer</button></div></details>
    ${p.kind==='clothing'?`<details class="profile-section" open><summary>Marques et créateurs</summary><div class="form-grid">${field(p,'brands','Marques et créateurs surveillés','lines',true,'Une marque par ligne. Seules les annonces dont la marque ou le titre contient l’une de ces entrées sont évaluées par Claude. Ajoute aussi la marque dans les recherches Vinted ci-dessus.')}</div></details>`:''}
    <details class="profile-section"><summary>Mots-clés et exclusions</summary><div class="form-grid">${field(p,'required_all','Toutes ces expressions sont requises','list',true,'Sépare les expressions par des virgules.')}${field(p,'required_any','Au moins une de ces expressions','list',true)}${field(p,'exclude','Expressions à exclure','list',true)}${p.kind==='clothing'?'':field(p,'brands','Marques acceptées · vide = toutes','list',true)}</div></details>
    ${p.kind==='ram'?`<details class="profile-section" open><summary>Caractéristiques RAM</summary><div class="form-grid">${field(p,'generations','Générations DDR (ex. 4, 5)','numbers')}${field(p,'capacities','Capacités totales en Go · vide = toutes','numbers')}${field(p,'min_speed','Fréquence minimum · 0 = libre','number')}${field(p,'max_per_gb','Prix maximum par Go · 0 = libre','number')}</div>${check(p,'require_sodimm','Exiger le format SO-DIMM')}${check(p,'allow_unknown_capacity','Accepter les capacités non précisées')}</details>`:''}
    <details class="profile-section"><summary>Vendeur et filtres Vinted avancés</summary><div class="form-grid">${field(p,'min_rating','Note vendeur minimum /5','number')}${field(p,'min_reviews','Nombre minimum d’avis','number')}${field(p,'catalog_ids','Identifiants des catégories','numbers')}${field(p,'brand_ids','Identifiants des marques','numbers')}${field(p,'size_ids','Identifiants des tailles','numbers')}${field(p,'status_ids','Identifiants des états','numbers')}</div><p class="help">Utilise l’import de lien pour récupérer les identifiants. Une note ou un nombre d’avis absent ne satisfait pas un filtre minimum.</p></details>
    <details class="profile-section"><summary>Coûts et marge estimée</summary><p class="help">Entre tes hypothèses. Un prix de revente à 0 désactive la simulation.</p><div class="form-grid">${field(p,'shipping','Livraison estimée (€)','number')}${field(p,'buyer_fee_fixed','Frais d’achat fixes estimés (€)','number')}${field(p,'buyer_fee_percent','Frais d’achat estimés (%)','number')}${field(p,'conversion_cost','Préparation / remise en état (€)','number')}${field(p,'resale_target','Prix de revente envisagé (€)','number')}${field(p,'selling_cost','Frais de revente (€)','number')}${p.kind==='ram'?field(p,'resale_capacity','Capacité concernée par la revente (Go)','number'):''}${field(p,'min_margin','Marge minimum visée (€)','number')}</div>${check(p,'margin_only','Ne retenir que les annonces atteignant la marge minimum')}</details>
    <details class="profile-section"><summary>Essayer mes filtres sur un titre</summary><div class="form-grid"><label class="wide">Titre d’une annonce<input id="preview-title" placeholder="Ex. SO-DIMM DDR5 2 × 8 Go"></label><label>Prix (€)<input id="preview-price" type="number" min="0" value="60"></label></div><p><button data-action="preview">Tester sans enregistrer d’annonce</button></p><div id="preview-result" hidden></div></details>
    <div class="editor-actions"><button data-action="duplicate-profile">Dupliquer</button><button class="danger" data-action="delete-profile">Supprimer cette recherche</button></div></article>`;
}
function renderSettings(){
  $('#interval').value=draft.interval_seconds;$('#per-page').value=draft.per_page;$('#pages').value=draft.pages;$('#notify-initial').checked=draft.notify_initial;$('#threshold-panel').value=draft.thresholds.panel;$('#threshold-alert').value=draft.thresholds.alert;$$('[data-setting]').forEach(el=>{const [group,key]=el.dataset.setting.split('.'),value=draft[group][key];if(el.type==='checkbox')el.checked=value;else el.value=el.dataset.scale?Math.round(value*el.dataset.scale):value;});appearance();
}
function renderStatus(){
  const s=state.status;$('#toggle').disabled=false;$('#toggle').textContent=s.running?'Arrêter la veille':'Démarrer la veille';
  $('#status').textContent=s.busy?'Recherche en cours':s.error?'Connexion à vérifier':s.running?'Veille active':'À l’arrêt';
  $('#status').className='status'+(s.error?' failed':s.running?' active':'');
  $('#connection-error').hidden=!s.error;$('#connection-error').textContent=s.error;
  $('#stat-matches').textContent=state.items.filter(i=>i.matches.length&&!i.hidden).length;$('#nav-count').textContent=$('#stat-matches').textContent;
  $('#stat-total').textContent=state.items.length;$('#stat-active').textContent=state.config.profiles.filter(p=>p.enabled).length;
  $('#nav-favorites').textContent=state.items.filter(i=>i.favorite).length;$('#nav-profiles').textContent=state.config.profiles.length;$('#nav-offers').textContent=state.appraisal.done;$('#nav-events').textContent=state.events.length;
  $('#stat-scan').textContent=date(s.last_run);const ap=state.appraisal;$('#appraisal-status').textContent='Clé API du modèle : '+(ap.configured?'définie':'absente')+' · modèle '+(ap.model||'n/a')+' · appels aujourd’hui : '+ap.calls_today+(ap.daily_cap?' sur '+ap.daily_cap:' (sans plafond)')+' · en attente : '+ap.pending+' · expirées : '+ap.expired;$('#version').textContent=state.version;
  $('#next-scan').textContent=s.busy?s.current_query:s.running?'Prochain cycle : '+new Date(s.next_run*1000).toLocaleTimeString('fr-FR'):'Surveillance à l’arrêt';
  const selectedFilter=$('#profile-filter').value;
  const options='<option value="">Toutes les recherches</option>'+state.config.profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  if($('#profile-filter').innerHTML!==options){$('#profile-filter').innerHTML=options;$('#profile-filter').value=state.config.profiles.some(p=>p.id===selectedFilter)?selectedFilter:'';}
  $('#diagnostics').innerHTML=[['Connecteur',s.connector],['Dernière réponse HTTP',s.last_http??'Non reçue'],['Cycles réussis',s.cycles],['Résultats du dernier cycle',s.last_count],['Dernière réussite',date(s.last_run)],['Erreurs consécutives',s.failures]].map(([a,b])=>`<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join('');
}
function renderEvents(){
  $('#events').innerHTML=state.events.length?state.events.map(e=>{const item=state.items.find(i=>i.id===e.item_id);return `<div class="event"><span class="event-symbol">${e.kind==='price_drop'?'↓':'↗'}</span><a href="${esc(item?.url||'https://www.vinted.fr')}" target="_blank" rel="noopener noreferrer">${esc(e.message)}</a><time>${date(e.at)}</time></div>`;}).join(''):'<div class="empty"><h2>Le journal attend ses premières annonces.</h2><p>Les alertes apparaissent après le premier relevé, sauf si tu actives les notifications initiales dans les préférences.</p></div>';
  const latest=state.events[0]?.id||0;
  if(cursor!==null&&latest>cursor&&'Notification'in window&&Notification.permission==='granted'){
    const events=state.events.filter(e=>e.id>cursor);
    try{new Notification('Repère · '+events.length+' alerte(s)',{body:events[0].message,tag:'repere'});}catch{}
  }cursor=latest;
}
async function detail(id){
  if(!state.items.some(x=>x.id===id))return;
  let i;
  try{const response=await fetch('/api/item?id='+encodeURIComponent(id));i=await response.json();if(!response.ok)throw new Error(i.error||'Détails indisponibles.');}catch(error){toast(error.message,true);return;}
  $('#item-detail').innerHTML=`<p class="overline">${i.source==='vinted'?'VINTED':'ANNONCE IMPORTÉE'}</p><h2>${esc(i.title)}</h2>${i.image?`<img class="detail-photo" src="${esc(i.image)}" alt="Photo de l’annonce" referrerpolicy="no-referrer">`:''}<p class="listing-price">${euro(i.price)}</p><p class="detail-description">${esc(i.description||'Aucune description fournie par le flux.')}</p><p class="help">Vendeur : ${esc(i.seller||'non renseigné')} · Note : ${i.rating??'inconnue'} · Première détection : ${date(i.first_seen)}</p>${i.evaluations.map(m=>`<div class="detail-matches"><strong>${esc(m.profile_name)}</strong><p>${m.matched?'Retenue par tes critères':m.reasons.map(esc).join(' · ')}</p><p class="help">Coût estimé ${euro(m.landed)} · Marge hypothétique ${euro(m.margin)}</p></div>`).join('')}<h3>Historique des prix</h3><ul class="detail-history">${i.history.slice(-20).map(p=>`<li>${date(p.at)} — ${euro(p.price)}</li>`).join('')}</ul><div class="button-stack"><a class="button primary" href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">Voir sur Vinted ↗</a><button data-hide="${esc(i.id)}">${i.hidden?'Rétablir dans le flux':'Masquer du flux'}</button></div>`;
  $('#item-dialog').showModal();
}
const SECRET_LABELS={ANTHROPIC_API_KEY:'Clé API Anthropic',NVIDIA_API_KEY:'Clé API NVIDIA (modèles Nemotron et GLM)',GROQ_API_KEY:'Clé API Groq (très rapide, gratuit)',DISCORD_TOKEN:'Token du bot Discord',DISCORD_GUILD_ID:'ID du serveur',DISCORD_FORUM_CHANNEL_ID:'ID du salon forum',DISCORD_ALERTS_CHANNEL_ID:'ID du salon annonces',DISCORD_BILAN_CHANNEL_ID:'ID du salon bilan',DISCORD_ITEMS_CHANNEL_ID:'ID du salon articles (facultatif)',DISCORD_ALERT_ROLE_ID:'ID du rôle à pinger (facultatif)'};
function renderSecrets(){
  const form=$('#secrets-form');
  if(!form.children.length)form.innerHTML=Object.entries(SECRET_LABELS).map(([name,label])=>`<label>${label} <small id="sec-${name}"></small><input type="password" autocomplete="off" data-secret="${name}" placeholder="Saisir une valeur"></label>`).join('');
  for(const name of Object.keys(SECRET_LABELS))$('#sec-'+name).textContent=state.secrets?.[name]?'définie':'absente';
}
async function refresh(){
  if(busy)return;busy=true;
  try{
    const response=await fetch('/api/state'+(itemsVersion===null?'':'?v='+itemsVersion));if(!response.ok)throw new Error('Serveur indisponible');const fresh=await response.json();if(fresh.items===null)fresh.items=state.items;itemsVersion=fresh.items_version;state=fresh;
    if(!draft){draft=clone(state.config);selected=draft.profiles[0]?.id;$('#service-url').value=state.connection?.url||'';renderSettings();renderEditor();}
    if(draft&&!dirty&&JSON.stringify(draft)!==JSON.stringify(state.config)){draft=clone(state.config);if(!draft.profiles.some(p=>p.id===selected))selected=draft.profiles[0]?.id;renderSettings();renderEditor();}
    renderStatus();renderFeed();renderEvents();renderSecrets();
  }catch(e){$('#connection-error').hidden=false;$('#connection-error').textContent='Connexion au serveur local perdue. Relance le programme si nécessaire.';$('#toggle').disabled=true;}
  finally{busy=false;}
}
async function save(){
  const snapshot=clone(draft);await post('/api/config',snapshot);dirty=false;$('#dirty').textContent='Tous les réglages sont enregistrés';$('#settings-dirty').textContent='Préférences enregistrées';await refresh();toast('Réglages enregistrés.');
}
async function action(name){
  if(!draft)throw new Error('Attends la connexion au serveur local.');
  if(name==='new-profile'){$('#new-dialog').showModal();return;}
  if(name==='duplicate-profile'){
    if(draft.profiles.length>=20)throw new Error('Maximum 20 recherches.');
    const p=clone(current());p.id=uid();p.name+=' · copie';p.enabled=false;draft.profiles.push(p);selected=p.id;markDirty();renderEditor();return;
  }
  if(name==='delete-profile'){
    if(draft.profiles.length===1)throw new Error('Garde au moins une recherche.');
    if(!confirm('Supprimer cette recherche ? Les annonces déjà enregistrées restent conservées.'))return;
    draft.profiles=draft.profiles.filter(p=>p.id!==selected);selected=draft.profiles[0].id;markDirty();renderEditor();return;
  }
  if(name==='parse-url'){
    const values=await post('/api/parse-search',{url:$('#search-url').value});Object.assign(current(),values);markDirty();renderEditor();toast('Critères repris depuis le lien. Vérifie puis enregistre.');return;
  }
  if(name==='preview'){
    const result=await post('/api/preview',{profile:current(),item:{title:$('#preview-title').value,price:Number($('#preview-price').value)}});
    const el=$('#preview-result');el.hidden=false;el.className='preview-result '+(result.matched?'pass':'fail');el.textContent=result.matched?'Annonce retenue · coût estimé '+euro(result.landed)+' · marge hypothétique '+euro(result.margin):'Écartée : '+result.reasons.join(' · ');return;
  }
}
document.addEventListener('click',async event=>{
  const b=event.target.closest('button');if(!b)return;
  try{
    if(b.dataset.view){navigate(b.dataset.view);return;}
    if(b.dataset.action){await action(b.dataset.action);return;}
    if(b.dataset.filter){filter=b.dataset.filter;feedLimit=60;renderFeed(true);return;}
    if(b.dataset.more==='feed'){feedLimit+=60;renderFeed(true);return;}
    if(b.dataset.layout){draft.appearance.layout=b.dataset.layout;markDirty();appearance();renderFeed(true);toast('Présentation modifiée. Enregistre dans Préférences pour la conserver.');return;}
    if(b.dataset.selectProfile){selected=b.dataset.selectProfile;renderEditor();return;}
    if(b.dataset.template){
      if(draft.profiles.length>=20)throw new Error('Maximum 20 recherches.');
      const p=clone(state.templates[b.dataset.template]);p.id=uid();p.enabled=true;draft.profiles.push(p);selected=p.id;$('#new-dialog').close();markDirty();navigate('profiles');return;
    }
    if(b.dataset.detail){await detail(b.dataset.detail);return;}
    if(b.dataset.favorite||b.dataset.hide){
      const id=b.dataset.favorite||b.dataset.hide,i=state.items.find(x=>x.id===id),field=b.dataset.favorite?'favorite':'hidden';
      const previous=i[field];i[field]=!previous;renderFeed(true);
      try{await post('/api/item',{id,field,value:!previous});}catch(error){i[field]=previous;renderFeed(true);throw error;}
      if(field==='hidden')$('#item-dialog').close();await refresh();return;
    }
  }catch(error){toast(error.message,true);}
});
$('#profile-editor').addEventListener('input',event=>{
  const el=event.target,key=el.dataset.key;if(!key)return;
  let value=el.value;
  if(el.type==='checkbox')value=el.checked;
  else if(el.dataset.format==='number')value=value===''?null:Number(value);
  else if(['list','numbers','lines'].includes(el.dataset.format))value=value.split(el.dataset.format==='lines'?'\n':',').map(x=>x.trim()).filter(Boolean).map(x=>el.dataset.format==='numbers'?Number(x):x);
  current()[key]=value;markDirty();renderProfileList();if(key==='kind')renderEditor();
});
$$('[data-appearance]').forEach(el=>el.addEventListener('change',()=>{draft.appearance[el.dataset.appearance]=el.type==='checkbox'?el.checked:el.value;markDirty();appearance();renderFeed(true);}));
for(const [selector,key] of [['#interval','interval_seconds'],['#per-page','per_page'],['#pages','pages'],['#notify-initial','notify_initial']])$(selector).addEventListener('input',e=>{draft[key]=e.target.type==='checkbox'?e.target.checked:Number(e.target.value);markDirty();});
for(const [selector,key] of [['#threshold-panel','panel'],['#threshold-alert','alert']])$(selector).addEventListener('input',e=>{draft.thresholds[key]=Number(e.target.value);markDirty();});
$$('[data-setting]').forEach(el=>el.addEventListener('input',()=>{const [group,key]=el.dataset.setting.split('.');draft[group][key]=el.type==='checkbox'?el.checked:el.dataset.text?el.value:Number(el.value)/(Number(el.dataset.scale)||1);markDirty();}));
['#save','#save-settings','#save-settings-eval'].forEach(selector=>$(selector).addEventListener('click',async e=>{e.target.disabled=true;try{await save();}catch(error){toast(error.message,true);}finally{e.target.disabled=false;}}));
['#search','#profile-filter','#sort'].forEach(selector=>$(selector).addEventListener('input',()=>{clearTimeout(feedTimer);feedTimer=setTimeout(()=>{feedLimit=60;renderFeed(true);},selector==='#search'?120:0);}));
document.addEventListener('keydown',event=>{
  if(event.key!=='/'||/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)||document.querySelector('dialog[open]'))return;
  const field=$(view==='contemporary'?'#offers-search':'#search');if(field&&field.offsetParent){event.preventDefault();field.focus();}
});
$('#toggle').addEventListener('click',async()=>{try{if(dirty)toast('La veille utilise les derniers réglages enregistrés.');await post('/api/control',{action:state.status.running?'stop':'start'});await refresh();}catch(error){toast(error.message,true);}});
$('#notify').addEventListener('click',async()=>{if(!('Notification'in window))return toast('Ce navigateur ne propose pas de notifications.',true);try{const permission=await Notification.requestPermission();toast(permission==='granted'?'Notifications activées. Garde cet onglet ouvert.':'Notifications non autorisées.');}catch{toast('Notifications indisponibles.',true);}});
for(const kind of ['items','config'])$('#import-'+kind).addEventListener('change',async e=>{
  try{
    const file=e.target.files[0];if(!file)return;if(file.size>2_000_000)throw new Error('Fichier limité à 2 Mo.');const value=JSON.parse(await file.text());
    if(kind==='config'){
      if(!confirm('Remplacer les réglages actuels par ce fichier ?'))return;
      await post('/api/config',value);draft=null;dirty=false;await refresh();$('#dirty').textContent='Configuration restaurée';toast('Configuration restaurée.');
    }else{const result=await post('/api/import',{items:Array.isArray(value)?value:value.items});await refresh();toast(result.processed+' annonces importées · '+result.alerts+' alertes.');}
  }catch(error){toast(error.message,true);}finally{e.target.value='';}
});
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
window.addEventListener('popstate',()=>navigate(history.state?.view||fromPath(),false));
history.replaceState({view:fromPath()},'');
if(fromPath()!=='feed')navigate(fromPath(),false);
refresh();setInterval(refresh,5000);

$('#save-secrets').addEventListener('click',async()=>{try{const fields=$$('[data-secret]').filter(el=>el.value.trim());for(const el of fields)await post('/api/secret',{name:el.dataset.secret,value:el.value.trim()});fields.forEach(el=>el.value='');toast(fields.length?'Clés enregistrées. Redémarre Repère pour les réglages Discord.':'Aucune nouvelle valeur saisie.');await refresh();}catch(e){toast(e.message,true);}});
$('#save-connection').addEventListener('click',async()=>{try{await post('/api/connection',{url:$('#service-url').value,activation_key:$('#service-key').value});$('#service-key').value='';toast('Connexion enregistrée.');await refresh();}catch(e){toast(e.message,true);}});
$('#test-connection').addEventListener('click',async()=>{try{await post('/api/connection/test',{});toast('Accès au service confirmé.');}catch(e){toast(e.message,true);}});
