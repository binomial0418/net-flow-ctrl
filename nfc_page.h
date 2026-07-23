// net-flow-ctrl — the config portal page, served from flash.
// Self-contained on purpose: a blocked client has no internet, so nothing here
// may reference an external CDN.
#pragma once

#include <Arduino.h>

static const char NFC_PAGE[] PROGMEM = R"HTML(<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>網路流量控制</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1c1e21;--mut:#6b7280;--line:#e3e5e8;--pri:#2563eb;--ok:#16a34a;--bad:#dc2626;--warn:#d97706}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--card:#1f2227;--fg:#e8eaed;--mut:#9aa0a6;--line:#33373d}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Noto Sans TC","Microsoft JhengHei",sans-serif}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:3px}
input,select{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font-size:14px}
button{padding:7px 14px;border:0;border-radius:6px;background:var(--pri);color:#fff;font-size:14px;cursor:pointer}
button:hover{opacity:.88}
button.sec{background:transparent;color:var(--pri);border:1px solid var(--pri)}
button.danger{background:var(--bad)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px}
.stat{display:flex;flex-wrap:wrap;gap:18px}
.stat div{font-size:13px}
.stat b{display:block;font-size:15px;font-weight:600}
.tbl{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:640px}
th,td{padding:8px 6px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:12px}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;color:#fff}
.p-ok{background:var(--ok)}.p-bad{background:var(--bad)}.p-off{background:var(--mut)}.p-warn{background:var(--warn)}
.bar{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:3px;min-width:70px}
.bar i{display:block;height:100%;background:var(--pri)}
dialog{border:0;border-radius:10px;padding:0;background:var(--card);color:var(--fg);max-width:420px;width:92%}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dlg{padding:16px}
.chk{display:flex;align-items:center;gap:7px;margin:10px 0 4px;font-size:13px;color:var(--fg)}
.chk input{width:auto}
.hint{font-size:11px;color:var(--mut);margin-top:3px}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#000;color:#fff;padding:9px 16px;border-radius:6px;font-size:13px;opacity:0;transition:.25s;pointer-events:none}
#toast.on{opacity:.92}
</style>
</head>
<body>
<div class="wrap">
<h1>網路流量控制</h1>
<div class="sub">ESP32 雙模路由 · 個別裝置連外時段與時數管理</div>

<div class="card">
  <h2>狀態</h2>
  <div class="stat" id="st"></div>
</div>

<div class="card">
  <h2>全域設定</h2>
  <div class="grid">
    <div>
      <label>對外 WiFi 名稱 (SSID)</label>
      <div style="display:flex;gap:6px">
        <input id="staSsid" list="ssids" placeholder="上游 AP">
        <button class="sec" id="scanBtn" style="flex:0 0 auto">掃描</button>
      </div>
      <datalist id="ssids"></datalist>
    </div>
    <div><label>對外 WiFi 密碼</label><input id="staPass" type="password" placeholder="留空表示不變更"></div>
    <div><label>每日重置時間</label><input id="resetAt" type="time"><div class="hint">此時刻歸零所有裝置的已用時數</div></div>
    <div><label>時區 (POSIX TZ)</label><input id="tz" placeholder="CST-8"></div>
    <div><label>NTP 伺服器</label><input id="ntp" placeholder="pool.ntp.org"></div>
    <div><label>未知新裝置</label><select id="defAllow"><option value="1">預設允許連外</option><option value="0">預設封鎖連外</option></select></div>
    <div><label>計時流量門檻（KB/分）</label><input id="actKB" type="number" min="1" max="60000"><div class="hint">每分鐘流量達此值才累計使用時數，低於則視為閒置</div></div>
  </div>
  <div class="row"><button id="saveG">儲存並套用</button><span class="hint">變更對外 WiFi 會重新連線，AP 熱點不中斷</span></div>
</div>

<div class="card">
  <h2>裝置清單</h2>
  <div class="tbl">
  <table>
    <thead><tr><th>裝置</th><th>IP</th><th>狀態</th><th>今日已用</th><th>時段</th><th>流量</th><th></th></tr></thead>
    <tbody id="devs"></tbody>
  </table>
  </div>
  <div class="row"><button class="sec" id="resetU">立即重置今日時數</button></div>
</div>
</div>

<dialog id="dlg"><form method="dialog" class="dlg">
  <h2 id="dTitle" style="margin:0 0 12px;font-size:16px"></h2>
  <div><label>裝置名稱</label><input id="dName" maxlength="23"></div>
  <div class="chk"><input type="checkbox" id="dAppr"><label for="dAppr" style="margin:0">核准此裝置連外</label></div>
  <div class="hint">未核准的裝置仍可連上熱點並開啟本頁，但無法連到外網。</div>
  <div class="chk"><input type="checkbox" id="dWinEn"><label for="dWinEn" style="margin:0">啟用可連線時段</label></div>
  <div class="grid">
    <div><label>開始</label><input id="dWinS" type="time"></div>
    <div><label>結束</label><input id="dWinE" type="time"></div>
  </div>
  <div class="hint">超出此時段即中斷連外。結束早於開始表示跨午夜。</div>
  <div class="chk"><input type="checkbox" id="dQuoEn"><label for="dQuoEn" style="margin:0">啟用每日累積連外時數上限</label></div>
  <div><label>上限（分鐘）</label><input id="dQuo" type="number" min="1" max="1440"><div class="hint">僅在裝置實際有連外流量時累計</div></div>
  <div class="chk"><input type="checkbox" id="dManual"><label for="dManual" style="margin:0">手動永久封鎖（不受每日重置影響）</label></div>
  <div class="row" style="justify-content:space-between">
    <button type="button" class="danger" id="dDel">刪除</button>
    <span><button type="button" class="sec" id="dCancel">取消</button>
    <button type="button" id="dSave">儲存</button></span>
  </div>
</form></dialog>

<dialog id="edlg"><form method="dialog" class="dlg">
  <h2 id="eTitle" style="margin:0 0 12px;font-size:16px"></h2>
  <div><label>本日延長至</label><input id="eUntil" type="time"></div>
  <div class="hint">在此時刻前，即使超過可用時段或每日時數上限，都暫時放行連外。<b>僅限今日有效</b>，跨過每日重置時間即失效。</div>
  <div id="eCur" class="hint" style="margin-top:8px"></div>
  <div class="row" style="justify-content:space-between">
    <button type="button" class="danger" id="eClear">取消延長</button>
    <span><button type="button" class="sec" id="eCancel">關閉</button>
    <button type="button" id="eSave">套用</button></span>
  </div>
</form></dialog>
<div id="toast"></div>

<script>
const $=i=>document.getElementById(i);
let devs=[],cur=null;
const hhmm=m=>String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0');
const toMin=s=>{const[h,m]=s.split(':').map(Number);return h*60+m};
const dur=s=>{const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+'時'+m+'分':m+'分'};
const size=b=>b<1024?b+' B':b<1048576?(b/1024).toFixed(1)+' KB':(b/1048576).toFixed(1)+' MB';
function toast(t){const e=$('toast');e.textContent=t;e.className='on';setTimeout(()=>e.className='',1800)}
async function jget(u){const r=await fetch(u);return r.json()}
async function jpost(u,d){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  if(!r.ok){toast('失敗：'+await r.text());return null}return r.json()}

async function loadStatus(){
  const s=await jget('/api/status');
  $('st').innerHTML=
    '<div>對外連線<b>'+(s.staUp?s.staSsid+' <span class="pill p-ok">已連線</span>':'<span class="pill p-bad">未連線</span>')+'</b></div>'+
    '<div>對外 IP<b>'+(s.staIp||'-')+(s.staUp?' ('+s.rssi+' dBm)':'')+'</b></div>'+
    (s.mdns?'<div>本機網址<b>http://'+s.mdns+'</b></div>':'')+
    '<div>熱點<b>'+s.apSsid+' · '+s.apIp+'</b></div>'+
    '<div>目前時間<b>'+(s.timeValid?s.time:'<span class="pill p-warn">未校時</span>')+'</b></div>'+
    '<div>下次重置<b>'+hhmm(s.resetMin)+'</b></div>'+
    '<div>連線裝置<b>'+s.online+' 台</b></div>';
  if(!$('staSsid').dataset.t){
    $('staSsid').value=s.staSsid;$('resetAt').value=hhmm(s.resetMin);
    $('tz').value=s.tz;$('ntp').value=s.ntp;$('defAllow').value=s.defaultAllow?'1':'0';
    $('actKB').value=s.activeKBmin;
    $('staSsid').dataset.t=1;
  }
}
function reasonPill(d){
  if(!d.online)return '<span class="pill p-off">離線</span>';
  switch(d.reason){
    case 0:return '<span class="pill p-ok">可連外</span>';
    case 1:return '<span class="pill p-bad">手動封鎖</span>';
    case 2:return '<span class="pill p-bad">非可用時段</span>';
    case 3:return '<span class="pill p-bad">時數用盡</span>';
    case 4:return '<span class="pill p-warn">無上游</span>';
    case 5:return '<span class="pill p-warn">未核准</span>';
    default:return '<span class="pill p-off">—</span>';
  }
}
async function loadDevs(){
  devs=(await jget('/api/devices')).devices;
  $('devs').innerHTML=devs.map((d,i)=>{
    const pct=d.quotaEnabled?Math.min(100,d.usedSec/(d.quotaMin*60)*100):0;
    const ext=d.extendActive?'<div class="hint" style="color:var(--ok)">延長至 '+hhmm(d.extendUntil)+'</div>':'';
    return '<tr><td><b>'+d.name+'</b><div class="hint">'+d.mac+'</div></td>'+
      '<td>'+(d.ip||'-')+'</td><td>'+reasonPill(d)+ext+'</td>'+
      '<td>'+dur(d.usedSec)+(d.quotaEnabled?' / '+dur(d.quotaMin*60)+'<div class="bar"><i style="width:'+pct+'%"></i></div>':'')+'</td>'+
      '<td>'+(d.winEnabled?hhmm(d.winStart)+'–'+hhmm(d.winEnd):'不限')+'</td>'+
      '<td>↑'+size(d.up)+'<div class="hint">↓'+size(d.down)+'</div></td>'+
      '<td style="white-space:nowrap"><button class="sec" onclick="edit('+i+')">設定</button> '+
      '<button class="sec" onclick="extendDlg('+i+')">本日延長</button></td></tr>';
  }).join('')||'<tr><td colspan="7" style="color:var(--mut)">尚無裝置連入</td></tr>';
}
window.edit=i=>{
  cur=devs[i];
  $('dTitle').textContent=cur.name+' · '+cur.mac;
  $('dName').value=cur.name;$('dAppr').checked=cur.approved;$('dWinEn').checked=cur.winEnabled;
  $('dWinS').value=hhmm(cur.winStart);$('dWinE').value=hhmm(cur.winEnd);
  $('dQuoEn').checked=cur.quotaEnabled;$('dQuo').value=cur.quotaMin;
  $('dManual').checked=cur.manualBlock;$('dlg').showModal();
};
$('dCancel').onclick=()=>$('dlg').close();
$('dSave').onclick=async()=>{
  const r=await jpost('/api/device',{mac:cur.mac,name:$('dName').value,approved:$('dAppr').checked,
    winEnabled:$('dWinEn').checked,winStart:toMin($('dWinS').value),winEnd:toMin($('dWinE').value),
    quotaEnabled:$('dQuoEn').checked,quotaMin:+$('dQuo').value,manualBlock:$('dManual').checked});
  if(r){$('dlg').close();toast('已儲存');loadDevs()}
};
$('dDel').onclick=async()=>{
  if(!confirm('刪除此裝置的所有設定與統計？'))return;
  const r=await jpost('/api/device',{mac:cur.mac,remove:true});
  if(r){$('dlg').close();toast('已刪除');loadDevs()}
};
window.extendDlg=i=>{
  cur=devs[i];
  $('eTitle').textContent=cur.name+' · 本日延長';
  $('eUntil').value=cur.extendUntil?hhmm(cur.extendUntil):'';
  $('eCur').innerHTML=cur.extendActive?'目前延長至 <b>'+hhmm(cur.extendUntil)+'</b>':'目前未設定延長';
  $('edlg').showModal();
};
$('eCancel').onclick=()=>$('edlg').close();
$('eSave').onclick=async()=>{
  if(!$('eUntil').value){toast('請先選擇時間');return}
  const r=await jpost('/api/extend',{mac:cur.mac,untilMin:toMin($('eUntil').value)});
  if(r){$('edlg').close();toast('已延長');loadDevs()}
};
$('eClear').onclick=async()=>{
  const r=await jpost('/api/extend',{mac:cur.mac,cancel:true});
  if(r){$('edlg').close();toast('已取消延長');loadDevs()}
};
$('saveG').onclick=async()=>{
  const r=await jpost('/api/global',{staSsid:$('staSsid').value,staPass:$('staPass').value,
    resetMin:toMin($('resetAt').value),tz:$('tz').value,ntp:$('ntp').value,defaultAllow:$('defAllow').value==='1',
    activeKBmin:+$('actKB').value});
  if(r){toast('已套用');$('staPass').value=''}
};
$('scanBtn').onclick=async()=>{
  $('scanBtn').textContent='掃描中';$('scanBtn').disabled=true;
  const r=await jget('/api/scan');
  $('ssids').innerHTML=r.nets.map(n=>'<option value="'+n.ssid+'">'+n.rssi+' dBm</option>').join('');
  $('scanBtn').textContent='掃描';$('scanBtn').disabled=false;toast('找到 '+r.nets.length+' 個網路');
};
$('resetU').onclick=async()=>{
  if(!confirm('立即將所有裝置的今日已用時數歸零？'))return;
  if(await jpost('/api/reset-usage',{})){toast('已重置');loadDevs()}
};
function tick(){loadStatus();loadDevs()}
tick();setInterval(tick,5000);
</script>
</body></html>)HTML";
