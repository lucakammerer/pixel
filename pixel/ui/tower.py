"""The start page's opening band: the world map from `map.py`, cut down to the headline and one live line.

The full control tower with bid race, event log and numbers stays at `/map`. This band reads the same CRM lanes
(`map.page_data`) and replays them as flights; nothing here sends, books or prices anything.
"""

from pixel.core.context import Context
from pixel.ui.map import AIRPORTS_JS, page_data


def render(ctx: Context) -> str:
    blob, land = page_data(ctx)
    return HTML.replace("__DATA__", blob).replace("__LAND__", land).replace("__AIRPORTS__", AIRPORTS_JS)


HTML = r"""<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
body{overflow-x:clip}
.tower{--t-bg:#282322;--t-ink:#f5eee9;--t-muted:#9d938c;--t-accent:#DE5531;position:relative;width:100vw;
margin:-116px calc(50% - 50vw) 0;height:100vh;min-height:520px;overflow:hidden;color:var(--t-ink);
background:radial-gradient(1100px 640px at 50% 44%,#302a28 0%,var(--t-bg) 50%,#1f1b1a 100%)}
.tower canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.tower::after{content:"";position:absolute;inset:0;pointer-events:none;background:
linear-gradient(180deg,rgba(31,27,26,.7),transparent 22%,transparent 62%,rgba(31,27,26,.92))}
.tower .say{position:absolute;z-index:2;left:max(24px,calc(50vw - 596px));right:max(24px,calc(50vw - 596px));bottom:44px;
display:flex;align-items:flex-end;justify-content:space-between;gap:24px;pointer-events:none}
.tower h1{margin:0;font-size:clamp(22px,5.8vw,64px);line-height:1;letter-spacing:-.04em;font-weight:500;color:var(--t-ink)}
.tower h1 em{font-style:normal;color:var(--t-accent)}
.tower .now{display:flex;align-items:center;gap:10px;font:500 12px/1 "JetBrains Mono",ui-monospace,monospace;
color:var(--t-muted);white-space:nowrap;padding-bottom:8px}
.tower .now b{font-weight:500;color:var(--t-ink);transition:opacity .4s}
.tower .now i{width:7px;height:7px;border-radius:50%;background:var(--t-accent);position:relative}
.tower .now i::after{content:"";position:absolute;inset:0;border-radius:50%;background:inherit;animation:tping 1.8s ease-out infinite}
@keyframes tping{from{transform:scale(1);opacity:.7}to{transform:scale(3.4);opacity:0}}
.tower .card{position:absolute;z-index:3;width:260px;padding:16px;border:1px solid rgba(255,236,224,.16);border-radius:12px;
background:rgba(36,31,30,.92);backdrop-filter:blur(14px);box-shadow:0 20px 50px rgba(0,0,0,.45);opacity:0;
transform:translateY(6px);transition:opacity .18s,transform .18s;pointer-events:none;font-size:13px}
.tower .card.on{opacity:1;transform:none;pointer-events:auto}
.tower .card .ln{font:500 20px/1.2 "JetBrains Mono",ui-monospace,monospace}.tower .card .ln span{color:var(--t-accent)}
.tower .card .sub{color:var(--t-muted);font-size:12px;margin-top:2px}
.tower .card .bar{height:3px;background:rgba(255,236,224,.14);border-radius:3px;margin:12px 0;overflow:hidden}
.tower .card .bar b{display:block;height:100%;background:var(--t-accent)}
.tower .card a{color:var(--t-ink);font-weight:500}.tower .card a span{color:var(--t-accent)}
@media (max-width:640px){.tower .say{flex-direction:column;align-items:flex-start;bottom:32px}.tower .now{padding:0}}
@media (prefers-reduced-motion:reduce){.tower .now i::after{animation:none}}
</style>
<section class="tower" id="tower" aria-label="Live map of PIXEL's shipments">
  <canvas id="t-land"></canvas><canvas id="t-fx"></canvas>
  <div class="say"><h1>Nobody likes doing logistics,<br><em>we automate it.</em></h1>
    <div class="now"><i></i><b id="t-now">&nbsp;</b></div></div>
  <div class="card" id="t-card"></div>
</section>
<script>
(()=>{
const DATA = __DATA__;
const LAND = __LAND__;
__AIRPORTS__
const HUBS=["FRA","MUC","DUS","HAM","STR","CGN"];
const DEMO_DEST=["JFK","ORD","ATL","MIA","LAX","YYZ","MEX","BOG","GRU","EZE","SCL","LIM","LOS","NBO","JNB","CAI","TLV",
"DXB","DOH","RUH","BOM","DEL","BKK","SIN","KUL","HKG","PVG","PEK","ICN","NRT","TPE","MNL","SGN","SYD","MEL","ULN","KTM","TNR"];
const rnd=(a,b)=>a+Math.random()*(b-a),pick=a=>a[Math.floor(Math.random()*a.length)];
const LON_C=14,SPAN=320,LAT_TOP=80,LAT_BOT=-58,LON_MIN=LON_C-SPAN/2;
const known=c=>AP[c]&&AP[c][2]>=-146&&AP[c][2]<=174;
const LANES=(DATA.lanes||[]).filter(l=>known(l.o)&&known(l.d)&&l.o!==l.d).map(l=>({...l,kg:+l.kg,eur:+l.eur}));
const nextLane=()=>LANES.length&&Math.random()<.85?{...pick(LANES)}:{o:pick(HUBS),d:pick(DEMO_DEST),kg:Math.round(rnd(60,2400)/10)*10,session:null};
const root=document.getElementById('tower'),landCv=document.getElementById('t-land'),fx=document.getElementById('t-fx');
const lctx=landCv.getContext('2d'),ctx=fx.getContext('2d'),nowEl=document.getElementById('t-now'),card=document.getElementById('t-card');
let W=0,H=0,DPR=1,S=1,OX=0,OY=0;const rad=Math.PI/180;
const proj=(lat,lon)=>[OX+(((lon-LON_MIN)%360+360)%360)*S,OY+(LAT_TOP-lat)*S];
function gc(a,b,n){const v=([la,lo])=>[Math.cos(la*rad)*Math.cos(lo*rad),Math.cos(la*rad)*Math.sin(lo*rad),Math.sin(la*rad)];
  const A=v(a),B=v(b),w=Math.acos(Math.min(1,A[0]*B[0]+A[1]*B[1]+A[2]*B[2]))||1e-6,out=[];
  for(let i=0;i<=n;i++){const t=i/n,k1=Math.sin((1-t)*w)/Math.sin(w),k2=Math.sin(t*w)/Math.sin(w);
    const x=k1*A[0]+k2*B[0],y=k1*A[1]+k2*B[1],z=k1*A[2]+k2*B[2];out.push([Math.atan2(z,Math.hypot(x,y))/rad,Math.atan2(y,x)/rad]);}
  return {pts:out,km:w*6371};}
function route(o,d){const g=gc([AP[o][1],AP[o][2]],[AP[d][1],AP[d][2]],90);let P=g.pts.map(([la,lo])=>proj(la,lo));
  const [x0,y0]=P[0],[x1,y1]=P[P.length-1],L=Math.hypot(x1-x0,y1-y0)||1;let nx=(y1-y0)/L,ny=-(x1-x0)/L;if(ny>0){nx=-nx;ny=-ny;}
  const lift=Math.min(80,L*.16);P=P.map(([x,y],i)=>{const s=Math.sin(Math.PI*i/(P.length-1))*lift;return [x+nx*s,y+ny*s];});
  const seg=[0];for(let i=1;i<P.length;i++)seg.push(seg[i-1]+Math.hypot(P[i][0]-P[i-1][0],P[i][1]-P[i-1][1]));
  return {P,seg,len:seg[seg.length-1],km:g.km};}
function at(r,t){const want=t*r.len;let i=1;while(i<r.seg.length-1&&r.seg[i]<want)i++;
  const a=r.P[i-1],b=r.P[i],f=(want-r.seg[i-1])/((r.seg[i]-r.seg[i-1])||1);return [a[0]+(b[0]-a[0])*f,a[1]+(b[1]-a[1])*f];}
const DOTS=[];LAND.rows.forEach((row,r)=>{const lat=LAND.lat0-r*LAND.step;let col=0,on=false;
  for(const n of row.split('.').map(Number)){if(on)for(let k=0;k<n;k++)DOTS.push([lat,-180+LAND.step/2+(col+k)*LAND.step]);col+=n;on=!on;}});
function sun(){const n=new Date(),d=(Date.UTC(n.getUTCFullYear(),n.getUTCMonth(),n.getUTCDate())-Date.UTC(n.getUTCFullYear(),0,0))/864e5;
  return {dec:-23.44*Math.cos(2*Math.PI/365*(d+10)),lon:(12-n.getUTCHours()-n.getUTCMinutes()/60)*15};}
function drawLand(){lctx.setTransform(DPR,0,0,DPR,0,0);lctx.clearRect(0,0,W,H);
  const s=sun(),sd=Math.sin(s.dec*rad),cd=Math.cos(s.dec*rad),r=Math.max(.9,S*.29);
  for(const [lat,lon] of DOTS){const [x,y]=proj(lat,lon);if(x<-4||x>W+4)continue;
    const cz=Math.sin(lat*rad)*sd+Math.cos(lat*rad)*cd*Math.cos((lon-s.lon)*rad),day=Math.min(1,Math.max(0,(cz+.08)/.22));
    const heat=Math.max(0,1-Math.hypot(lat-50.5,(lon-9.5)*.65)/11),north=Math.min(1,Math.max(0,(82-lat)/10)),a=(.13+.21*day+.4*heat)*north;
    lctx.fillStyle=`rgba(${Math.round(230+25*heat)},${Math.round(218-110*heat)},${Math.round(206-150*heat)},${a.toFixed(3)})`;
    lctx.beginPath();lctx.arc(x,y,r,0,7);lctx.fill();}}
const flights=[],rings=[];let quoting=null,selected=null;
const eur=v=>'€'+Math.round(v).toLocaleString('en-US');
function addFlight(l,t0){const r=route(l.o,l.d);flights.push({l,r,t:t0||0,dur:rnd(16,30)*(.6+r.km/14000)});}
function ring(c,color,max,dur){rings.push({c,color,max,dur,t0:performance.now()});}
function say(text){nowEl.style.opacity=0;setTimeout(()=>{nowEl.textContent=text;nowEl.style.opacity=1;},380);}
function request(){const l=nextLane();quoting={l,r:route(l.o,l.d),t0:performance.now()};ring(l.o,'245,238,233',30,1300);
  const secs=l.secs&&l.secs<240?l.secs:Math.round(rnd(14,48));
  setTimeout(()=>{say(`${l.o} → ${l.d}  quoted in ${secs} s`);quoting.done=true;ring(l.o,'222,85,49',40,1500);
    setTimeout(()=>{if(Math.random()<.65)addFlight(l);quoting=null;},2200);},2600);}
function label(c,alpha){const [x,y]=proj(AP[c][1],AP[c][2]);ctx.font='500 10px "JetBrains Mono",ui-monospace,monospace';
  ctx.textAlign='left';ctx.textBaseline='middle';ctx.fillStyle=`rgba(245,238,233,${alpha})`;ctx.fillText(c,x+7,y-7);}
let last=performance.now();
function frame(now){const dt=Math.min(.05,(now-last)/1000);last=now;ctx.setTransform(DPR,0,0,DPR,0,0);ctx.clearRect(0,0,W,H);
  ctx.lineWidth=1;ctx.strokeStyle='rgba(222,85,49,.16)';
  for(const f of flights){ctx.beginPath();f.r.P.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();}
  ctx.globalCompositeOperation='lighter';
  for(let i=flights.length-1;i>=0;i--){const f=flights[i];f.t+=dt/f.dur;
    if(f.t>=1){ring(f.l.d,'88,200,150',30,1800);flights.splice(i,1);if(selected===f)close();continue;}
    const tail=Math.max(0,f.t-.2),N=20;
    for(let k=0;k<N;k++){const a=at(f.r,tail+(f.t-tail)*k/N),b=at(f.r,tail+(f.t-tail)*(k+1)/N),q=(k+1)/N;
      ctx.strokeStyle=`rgba(232,110,72,${(q*q*.85).toFixed(3)})`;ctx.lineWidth=.6+q*1.6;ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.stroke();}
    const [x,y]=at(f.r,f.t),g=ctx.createRadialGradient(x,y,0,x,y,14);g.addColorStop(0,'rgba(240,138,98,.75)');g.addColorStop(1,'rgba(222,85,49,0)');
    ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,14,0,7);ctx.fill();ctx.fillStyle='#fff3ec';ctx.beginPath();ctx.arc(x,y,1.9,0,7);ctx.fill();f.x=x;f.y=y;}
  ctx.globalCompositeOperation='source-over';
  if(quoting){const q=quoting,p=Math.min(1,(now-q.t0)/2600),n=Math.max(2,Math.round(q.r.P.length*p));
    ctx.setLineDash([3,5]);ctx.lineDashOffset=-now/40;ctx.strokeStyle=q.done?'rgba(240,138,98,.9)':'rgba(245,238,233,.75)';ctx.lineWidth=1.4;
    ctx.beginPath();for(let i=0;i<n;i++){const [x,y]=q.r.P[i];i?ctx.lineTo(x,y):ctx.moveTo(x,y);}ctx.stroke();ctx.setLineDash([]);label(q.l.d,.95);}
  for(const c of new Set(flights.map(f=>f.l.d))){const [x,y]=proj(AP[c][1],AP[c][2]);ctx.fillStyle='rgba(245,238,233,.8)';ctx.beginPath();ctx.arc(x,y,2,0,7);ctx.fill();}
  HUBS.forEach((c,i)=>{const [x,y]=proj(AP[c][1],AP[c][2]),ph=((now/1000+i*.33)%2.2)/2.2;
    ctx.strokeStyle=`rgba(222,85,49,${(.7*(1-ph)).toFixed(3)})`;ctx.lineWidth=1;ctx.beginPath();ctx.arc(x,y,3+ph*13,0,7);ctx.stroke();
    ctx.fillStyle='#DE5531';ctx.beginPath();ctx.arc(x,y,2.6,0,7);ctx.fill();});
  for(let i=rings.length-1;i>=0;i--){const r=rings[i],k=(now-r.t0)/r.dur;if(k>=1){rings.splice(i,1);continue;}
    const [x,y]=proj(AP[r.c][1],AP[r.c][2]),e=1-Math.pow(1-k,3);ctx.strokeStyle=`rgba(${r.color},${(1-k).toFixed(3)})`;ctx.lineWidth=1.5;
    ctx.beginPath();ctx.arc(x,y,4+e*r.max,0,7);ctx.stroke();}
  if(selected){const f=selected;ctx.strokeStyle='rgba(245,238,233,.85)';ctx.lineWidth=1.2;ctx.beginPath();
    f.r.P.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();ctx.beginPath();ctx.arc(f.x,f.y,9,0,7);ctx.stroke();
    const b=card.querySelector('.bar b');if(b)b.style.width=(f.t*100).toFixed(1)+'%';}
  requestAnimationFrame(frame);}
function close(){selected=null;card.classList.remove('on');}
fx.addEventListener('click',e=>{const r=fx.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  let f=null,bd=22;for(const g of flights){const d=Math.hypot(g.x-mx,g.y-my);if(d<bd){bd=d;f=g;}}
  if(!f)return close();selected=f;const l=f.l;
  card.innerHTML=`<div class="ln">${l.o} <span>→</span> ${l.d}</div><div class="sub">${AP[l.o][0]} to ${AP[l.d][0]} · ${Math.round(l.kg).toLocaleString('en-US')} kg</div>`+
    `<div class="bar"><b></b></div><a href="${l.session?'/sessions/'+l.session:'/requests'}">Open session <span>→</span></a>`;
  let x=f.x+18,y=f.y-30;if(x+270>W)x=f.x-278;card.style.left=x+'px';card.style.top=Math.max(90,Math.min(y,H-200))+'px';card.classList.add('on');});
fx.addEventListener('mousemove',e=>{const r=fx.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;
  fx.style.cursor=flights.some(f=>Math.hypot(f.x-mx,f.y-my)<22)?'pointer':'default';});
document.addEventListener('keydown',e=>{if(e.key==='Escape')close();});
function resize(){const b=root.getBoundingClientRect();W=b.width;H=b.height;DPR=Math.min(2,window.devicePixelRatio||1);
  for(const c of [landCv,fx]){c.width=Math.round(W*DPR);c.height=Math.round(H*DPR);}
  const rows=LAT_TOP-LAT_BOT;S=Math.min(Math.max(W/SPAN,H*.62/rows),H*.96/rows);
  OX=W/2-(LON_C-LON_MIN)*S;OY=Math.max(130,(H-rows*S)/2+40);
  for(const f of flights)f.r=route(f.l.o,f.l.d);if(quoting)quoting.r=route(quoting.l.o,quoting.l.d);drawLand();}
addEventListener('resize',()=>{clearTimeout(resize.t);resize.t=setTimeout(resize,120);});
resize();setInterval(drawLand,60000);
for(let i=0;i<16;i++)addFlight(nextLane(),Math.random()*.9);
say(`${flights.length} shipments in the air`);
setTimeout(request,1500);setInterval(request,7000);
setInterval(()=>{if(flights.length<18)addFlight(nextLane());},4200);
requestAnimationFrame(frame);
})();
</script>"""
