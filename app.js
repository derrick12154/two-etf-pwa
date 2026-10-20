import {validateEnvelope,unlockKey,decryptSnapshot} from './auth.js';
import {NAMES,REASONS,validateSnapshot,signalPresentation,snapshotStatus} from './signal-state.mjs';
const $=id=>document.getElementById(id);
const percent=(v,d=1)=>Number.isFinite(v)?`${(v*100).toFixed(d)}%`:'—';
const signed=v=>`${v>=0?'+':''}${percent(v)}`;
let data, range='all', installPrompt, cached=false, toastTimer, chartRows=[];
let session;
const CACHE_KEY='two-etf-encrypted-v1';
try{localStorage.removeItem('two-etf-snapshot-v1');}catch{}

function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4000);}
function validate(p){
  return validateSnapshot(p);
}
function updateStatus(){
  if(!data)return;
  $('status').classList.remove('error');
  $('status').textContent=snapshotStatus(data,cached);
  $('connection').textContent=cached?'读取加密缓存':'已发布周度数据';
}
function render(){
  updateStatus();
  $('holding').textContent=NAMES[data.holding];$('holding-code').textContent=data.holding==='CASH'?'等待原定选强日':`${data.holding.slice(2)} · 场内ETF`;
  $('weight').textContent=percent(data.exposure,1);$('cash').textContent=percent(1-data.exposure,1);
  $('allocation-fill').style.width=percent(data.exposure,3);
  $('execution').textContent=data.schemaVersion===2?`仓位确认 ${data.positionAsOf}${data.positionTiming==='legacy_next_open'?' 开盘':' 盘后'}`:`记录截至 ${data.priceAsOf}`;
  const shown=signalPresentation(data);
  $('signal-title').textContent=shown.title;$('signal-date').textContent=shown.date;
  $('decision').textContent=shown.description;$('signal-caption').textContent=shown.caption;
  $('target').textContent=percent(shown.target,1);$('plan-weight').textContent=percent(shown.plannedWeight,1);
  $('sizing').textContent=shown.sizing;$('execution-state').textContent=shown.state;
  $('candidates-date').textContent=`参考 ${data.signalAsOf}`;
  $('candidates').replaceChildren(...data.candidates.map(c=>{
    const card=document.createElement('article');card.className='card candidate';
    card.innerHTML='<div class="candidate-top"><div class="asset-name"></div><strong class="gain"></strong></div><div class="eligibility"></div><div class="candidate-bottom"></div>';
    card.querySelector('.asset-name').textContent=c.name;
    const code=document.createElement('small');code.textContent=c.code;card.querySelector('.asset-name').append(code);
    const gain=card.querySelector('.gain');gain.textContent=signed(c.momentum);gain.classList.toggle('negative',c.momentum<0);
    for(const [label,pass] of [['26周正收益',c.momentum>0],['高于40周均线',c.maDistance>0],['规模>10亿元',c.assetsYi>10]]){
      const badge=document.createElement('span');badge.className=`badge ${pass?'':'fail'}`;badge.textContent=`${pass?'✓':'×'} ${label}`;card.querySelector('.eligibility').append(badge);
    }
    card.querySelector('.candidate-bottom').textContent=`均线距离 ${signed(c.maDistance)} · 年化波动 ${percent(c.volatility)}`;
    return card;
  }));
  $('decision-history').replaceChildren(...data.decisions.map(d=>{
    const item=document.createElement('div');item.className='record';
    const top=document.createElement('div');top.className='record-top';
    const title=document.createElement('b');title.textContent=`${d.executionDate} · ${NAMES[d.asset]}`;
    const tag=document.createElement('small');tag.textContent=d.selection?'四周选强':'周度检查';top.append(title,tag);
    const p=document.createElement('p');p.textContent=`信号 ${d.signalDate}｜${d.reason==='eligibility_exit'?'资格退出':REASONS[d.sizingReason]||d.sizingReason}｜${d.executionTiming==='friday_close_proxy'?'尾盘':'开盘'}模型仓位 ${percent(d.executionWeight??d.openWeight,1)}`;item.append(top,p);return item;
  }));
  const stats=[['年化收益',data.summary.annual_return,'good'],['最大回撤',data.summary.max_drawdown,'bad'],['年化波动',data.summary.annual_vol,''],['平均ETF仓位',data.summary.average_exposure,'']];
  $('metrics').replaceChildren(...stats.map(([label,value,cls])=>{const item=document.createElement('div');item.className='metric';const s=document.createElement('span');s.textContent=label;const b=document.createElement('strong');b.className=cls;b.textContent=percent(value);item.append(s,b);return item;}));
  $('range-dates').textContent=`${data.summary.start} — ${data.priceAsOf}`;$('nav-value').textContent=`期末 ${data.nav.toFixed(3)}`;
  $('annual').innerHTML='<div class="annual-row heading"><span>年份</span><span>收益</span><span>最大回撤</span></div>';
  for(const y of [...data.annual].reverse()){
    const item=document.createElement('div');item.className='annual-row';
    for(const [value,cls] of [[`${y.year}${String(y.year)===data.priceAsOf.slice(0,4)?'（截至期末）':''}`, ''],[signed(y.return),y.return>=0?'positive':'negative'],[percent(y.max_drawdown),'negative']]){const cell=document.createElement('span');cell.className=cls;cell.textContent=value;item.append(cell);}
    $('annual').append(item);
  }
  $('audit-state').textContent=`${data.history.length} 个交易日 · 因果选强与持仓重建审计通过 · ${data.provenance.acceptedBaselineThrough}以前与已确认基准一致。`;
  $('data-stamp').textContent=`计算时间 ${new Date(data.generatedAt).toLocaleString('zh-CN')}。行情时点以页面显示的交易日期为准。`;
  drawCharts();
}
async function encryptedData(){
  try{
    const res=await fetch(`./data.enc.json?t=${Date.now()}`,{cache:'no-store'});
    if(!res.ok)throw Error('网站数据不可用');
    return {envelope:validateEnvelope(await res.json()),offline:res.headers.get('X-PWA-Offline')==='1'};
  }catch(error){
    try{return {envelope:validateEnvelope(JSON.parse(localStorage.getItem(CACHE_KEY))),offline:true};}catch{throw error;}
  }
}
function cacheEnvelope(envelope){try{localStorage.setItem(CACHE_KEY,JSON.stringify(envelope));}catch{}}
async function load(manual=false){
  if(!session)return;
  $('refresh').disabled=true;
  try{
    const {envelope,offline}=await encryptedData();
    const next=validate(await decryptSnapshot(envelope,session));cached=offline;
    if(data&&(next.priceAsOf<data.priceAsOf||next.schemaVersion<data.schemaVersion||(next.priceAsOf===data.priceAsOf&&next.generatedAt<data.generatedAt)))throw Error('拒绝过期数据覆盖');
    const changed=data&&next.generatedAt!==data.generatedAt;data=next;
    cacheEnvelope(envelope);
    render();if(manual)toast(cached?'无法联网，继续使用上次快照':changed?'已载入新发布快照':'已检查，网站暂无更新');
  }catch(error){
    if(data){cached=true;render();if(manual)toast('未能取得新数据，保留上次有效快照');}
    else{$('status').classList.add('error');$('status').textContent='数据加载失败。请联网后点击右上角刷新重试。';}
  }finally{$('refresh').disabled=false;}
}

$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();$('unlock').disabled=true;$('login-message').textContent='正在验证密码…';
  let password=$('password').value;$('password').value='';
  try{
    const {envelope,offline}=await encryptedData();
    const proposed=await unlockKey(password,envelope);password='';
    const next=validate(await decryptSnapshot(envelope,proposed));
    session=proposed;data=next;cached=offline;cacheEnvelope(envelope);
    $('login').hidden=true;$('app-shell').hidden=false;$('login-message').textContent='';
    tab(location.hash.slice(1));render();
  }catch(error){
    password='';session=undefined;
    $('login-message').textContent=error.name==='OperationError'?'密码不正确，请重新输入。':`无法解锁：${error.message}`;
  }finally{$('unlock').disabled=false;}
});
function lock(){
  session=undefined;data=undefined;chartRows=[];
  $('app-shell').hidden=true;$('login').hidden=false;$('password').value='';
  for(const id of ['candidates','decision-history','metrics','annual'])$(id).replaceChildren();
  for(const id of ['holding','holding-code','weight','cash','target','plan-weight','execution-state','sizing','decision','execution','signal-date','candidates-date','status','nav-value','chart-caption','range-dates','audit-state','data-stamp'])$(id).textContent='—';
  for(const id of ['nav-chart','dd-chart']){const c=$(id);c.getContext('2d').clearRect(0,0,c.width,c.height);}
  $('password').focus();
}
$('logout').addEventListener('click',lock);
document.addEventListener('visibilitychange',()=>{if(document.hidden&&session)lock();});
window.addEventListener('pagehide',()=>{if(session)lock();});
function selectRows(){
  const end=new Date(data.priceAsOf+'T00:00:00Z'), start=new Date(end);
  if(range==='3y')start.setUTCFullYear(start.getUTCFullYear()-3);
  if(range==='1y')start.setUTCFullYear(start.getUTCFullYear()-1);
  if(range==='ytd'){start.setUTCMonth(0,1);}
  if(range==='all')return data.history;
  const cutoff=start.toISOString().slice(0,10),idx=data.history.findIndex(r=>r.date>=cutoff);
  // Include the prior trading close as the normalization base for period returns.
  return data.history.slice(Math.max(0,idx-1));
}
function plot(canvas,rows,series,asPercent=false){
  const width=canvas.clientWidth;if(width<10)return;
  const height=Number(canvas.getAttribute('height')),dpr=window.devicePixelRatio||1;
  if(!canvas.dataset.height)canvas.dataset.height=height;
  const h=Number(canvas.dataset.height);canvas.style.height=h+'px';canvas.width=Math.round(width*dpr);canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);
  const left=40,right=8,top=10,bottom=26,w=width-left-right,ph=h-top-bottom;
  let min=Infinity,max=-Infinity;for(const s of series){for(const v of s.values){min=Math.min(min,v);max=Math.max(max,v);}}
  if(max===min){max+=.01;min-=.01;}const pad=(max-min)*.06;min-=pad;max+=pad;
  const x=i=>left+i/(rows.length-1)*w,y=v=>top+(max-v)/(max-min)*ph;
  ctx.font='10px -apple-system, sans-serif';
  for(let t=0;t<4;t++){const value=max-t*(max-min)/3,yy=y(value);ctx.strokeStyle='#e6ede6';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(width-right,yy);ctx.stroke();ctx.fillStyle='#84938c';ctx.textAlign='right';ctx.fillText(asPercent?percent(value,0):value.toFixed(1),left-8,yy+3);}
  for(const s of series){ctx.beginPath();s.values.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.strokeStyle=s.color;ctx.lineWidth=s.width||1.4;ctx.lineJoin='round';ctx.stroke();}
  ctx.fillStyle='#84938c';ctx.textAlign='left';ctx.fillText(rows[0].date.slice(0,7),left,h-5);ctx.textAlign='right';ctx.fillText(rows.at(-1).date.slice(0,7),width-right,h-5);
}
function drawCharts(){
  if(!data||$('performance').hidden)return;
  chartRows=selectRows();const first=chartRows[0];
  plot($('nav-chart'),chartRows,[{values:chartRows.map(r=>r.nav/first.nav),color:'#137d65',width:2.5},{values:chartRows.map(r=>r.sz159915/first.sz159915),color:'#c1a064'},{values:chartRows.map(r=>r.sh513100/first.sh513100),color:'#869ab0'}]);
  let peak=first.nav;const dd=chartRows.map(r=>{peak=Math.max(peak,r.nav);return r.nav/peak-1;});
  plot($('dd-chart'),chartRows,[{values:dd,color:'#b4564f',width:2}],true);
  $('chart-caption').textContent=`${first.date} — ${chartRows.at(-1).date} · 区间起点归一化为1。触摸曲线查看单日数据。`;
}
function tab(name){
  if(!['signal','performance','rules','install'].includes(name))name='signal';
  document.querySelectorAll('.view').forEach(v=>v.hidden=v.id!==name);
  document.querySelectorAll('[data-tab]').forEach(b=>b.dataset.tab===name?b.setAttribute('aria-current','page'):b.removeAttribute('aria-current'));
  history.replaceState(null,'','#'+name);window.scrollTo(0,0);drawCharts();
}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>tab(b.dataset.tab)));
$('refresh').addEventListener('click',()=>load(true));
// Check published snapshots while the app stays open, including Friday's confirmation.
setInterval(()=>{if(session&&!document.hidden)load();},5*60*1000);
$('show-history').addEventListener('click',()=>{const hidden=!$('decision-history').hidden;$('decision-history').hidden=hidden;$('show-history').textContent=hidden?'展开':'收起';});
document.querySelectorAll('[data-range]').forEach(b=>b.addEventListener('click',()=>{range=b.dataset.range;document.querySelectorAll('[data-range]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));drawCharts();}));
window.addEventListener('resize',drawCharts);
$('nav-chart').addEventListener('pointerdown',e=>{
  if(!chartRows.length)return;const rect=e.currentTarget.getBoundingClientRect();const f=Math.max(0,Math.min(1,(e.clientX-rect.left-40)/(rect.width-48)));const r=chartRows[Math.round(f*(chartRows.length-1))],b=chartRows[0];
  $('chart-caption').textContent=`${r.date}｜策略 ${(r.nav/b.nav).toFixed(3)}｜创业板 ${(r.sz159915/b.sz159915).toFixed(3)}｜纳指 ${(r.sh513100/b.sh513100).toFixed(3)}｜仓位 ${percent(r.exposure,0)}`;
});
$('export-data').addEventListener('click',()=>{if(!data)return;const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`two-etf-${data.priceAsOf}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);});
window.addEventListener('offline',()=>{if(data){cached=true;updateStatus();}toast('网络已断开，可继续查看缓存快照');});
window.addEventListener('online',()=>{load();});
const standalone=window.matchMedia('(display-mode: standalone)').matches||window.navigator.standalone;
$('install-state').textContent=standalone?'已在独立应用模式下打开。':location.protocol==='https:'?'此网站使用 HTTPS，可添加到 iPhone 主屏幕。':'当前为电脑本地预览。iPhone 安装请使用部署后的 HTTPS 地址。';
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installPrompt=e;$('native-install').hidden=false;});
$('native-install').addEventListener('click',async()=>{if(!installPrompt)return;await installPrompt.prompt();installPrompt=null;$('native-install').hidden=true;});
if('serviceWorker' in navigator&&(location.protocol==='https:'||['localhost','127.0.0.1'].includes(location.hostname))){
  navigator.serviceWorker.register('./sw.js',{scope:'./'}).then(reg=>{
    const ready=()=>$('offline-state').textContent='离线资源已就绪；成功载入的数据快照也会保留在本机。';
    if(reg.active)ready();navigator.serviceWorker.ready.then(ready);
    const offer=()=>{if(reg.waiting&&navigator.serviceWorker.controller){toast('发现应用新版，点刷新后重新打开即可更新');reg.waiting.postMessage({type:'ACTIVATE_UPDATE'});}};
    offer();reg.addEventListener('updatefound',()=>reg.installing?.addEventListener('statechange',offer));
  }).catch(()=>$('offline-state').textContent='离线资源注册失败，可继续联网使用。');
}else{$('offline-state').textContent='当前地址未启用离线资源缓存，部署到 HTTPS 后可用。';}
tab(location.hash.slice(1));
