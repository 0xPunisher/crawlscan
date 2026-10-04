"""Сборка фронта: design.html (экспорт Claude Design, самораспаковывающийся бандл) -> index.html.

Страница лежит JSON-строкой в <script type="__bundler/template">. Скрипт её раскодирует,
применяет правки и кодирует обратно тем же способом (json.dumps(ensure_ascii=False),
"</" -> "<\\u002F"); остальной файл не трогается, кроме <title> и иконок в обёртке.

Правки:
  - startFeed/stopFeed: живой скан через API (POST /api/scan, опрос /api/events каждые 300 мс,
    очередь с интервалом props.eventMs, после done — /api/result);
  - normalizeEvent/normalizeResult: формат engine/server -> формат дизайна
    (короткие адреса, флаги, доли в %, шапка токена);
  - <title>CRAWLSCAN</title> и ссылки на иконки (/favicon.svg, /favicon.png, /apple-touch-icon.png)
    снаружи и внутри страницы;
  - кнопка «try a sample» -> настоящий токен Pons V2;
  - полоса TOO EARLY (TOO_EARLY_OR_LATE) и счёт «—» без скора.

Если дизайн поменялся так, что якорь правки не найден, скрипт падает с понятной ошибкой
и index.html не перезаписывает.

Запуск из корня репозитория: python3 scripts/build_frontend.py
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC, OUT = os.path.join(ROOT, "design.html"), os.path.join(ROOT, "index.html")
SAMPLE = "0xb4bb188e2d0e82ef9dba8b31ffe41855a2feac0f"   # токен для «try a sample»
ICON_LINKS = "\n".join([                               # иконки отдаёт server.py из static/
    '<link rel="icon" type="image/svg+xml" href="/favicon.svg">',
    '<link rel="icon" type="image/png" href="/favicon.png">',
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">',
])

src = open(SRC, encoding="utf-8").read()
m = re.search(r'<script type="__bundler/template">', src)
a = m.end(); b = src.find('</script>', a)
raw = src[a:b]
t = json.loads(raw)
def encode(text):
    return "\n" + json.dumps(text, ensure_ascii=False).replace('</', '<\\u002F') + "\n  "

if encode(t) != raw:
    sys.exit("design.html: формат template изменился, кодировщик не совпадает — сборка остановлена")

def rep(old, new, count=1):
    global t
    if t.count(old) != count:
        sys.exit(f"design.html: якорь правки найден {t.count(old)} раз вместо {count}: {old[:70]!r}")
    t = t.replace(old, new)

# <title> внутри страницы
rep('<meta name="viewport" content="width=device-width, initial-scale=1">\n<script src="8e5b16f0',
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>CRAWLSCAN</title>\n' + ICON_LINKS + '\n<script src="8e5b16f0')

# полоса TOO EARLY
rep("const BANDS={CLEAN:G,OK:LG,RISKY:A,DANGER:RD,ERROR:RD};",
    "const BANDS={CLEAN:G,OK:LG,RISKY:A,DANGER:RD,ERROR:RD,'TOO EARLY':'#8a959c'};")

# нормализация API -> формат дизайна (перед классом)
rep("class Component extends DCLogic {", r'''// ---- live API (rh-crawler server.py) -> design format ----
const STAGE_TEXT={launch:'resolving token',transfers:'reading top holders',entries:'classifying entries',wallets:'tracing wallet history',links:'linking wallets'};
const FLAG_KEEP=['deployer','virgin','short_history','sniper','sold','unread'];
const shortAddr=a=>typeof a==='string'&&/^0x[0-9a-fA-F]{40}$/.test(a)?a.slice(0,6)+'…'+a.slice(-4):a;
function normalizeEvent(e){
  if(!e||!e.type) return e;
  const o={...e};
  if(o.wallet) o.wallet=shortAddr(o.wallet);
  if(o.b) o.b=shortAddr(o.b);
  if(Array.isArray(o.wallets)) o.wallets=o.wallets.map(shortAddr);
  switch(o.type){
    case 'stage': o.detail=STAGE_TEXT[o.detail]||o.detail; break;
    case 'wallet_flag': {
      const fl=(e.flags||[]).filter(f=>FLAG_KEEP.includes(f));
      if(e.kind==='transfer') fl.push('transfer');
      o.detail=(fl.length?fl:['clean']).join(', ');
      o.share=e.share!=null?e.share*100:null; break;
    }
    case 'done':
      o.band=e.band==='TOO_EARLY_OR_LATE'?'TOO EARLY':e.band; break;
    case 'error': o.detail=e.detail||'error'; break;
  }
  return o;
}
const fmtAge=h=>{if(typeof h!=='number')return null; const mins=Math.round(h*60); if(mins<60)return mins+'m'; if(h<48)return Math.floor(h)+'h '+(mins%60)+'m'; return Math.floor(h/24)+'d '+Math.floor(h%24)+'h';};
const fmtPrice=p=>typeof p!=='number'?null:'$'+(p>=1?p.toFixed(2):p.toPrecision(3));
const fmtTok=v=>v>=1e9?(v/1e9).toFixed(2)+'B':v>=1e6?(v/1e6).toFixed(0)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':String(Math.round(v));
function normalizeResult(r){
  if(!r) return r;
  const h=r.header||{}, sup=(r.supply||0)/1e18, circ=(r.circulating||0)/1e18;
  return {
    header:{name:h.name,ticker:h.ticker,mcap:h.mcap_usd,liquidity:h.liquidity_usd,vol24h:h.vol24h_usd,
            age:fmtAge(h.age_h),price:fmtPrice(h.price_usd),holders:r.holders_total,
            supply:sup?Math.round(sup).toLocaleString('en-US'):null,
            circulating:sup?`${fmtTok(circ)} (${(circ/sup*100).toFixed(1)}%)`:null,
            pool:'Pons V2',deployer:shortAddr(r.launch&&r.launch.deployer)},
    parts:r.parts||{},
    operators:(r.operators||[]).filter(o=>o.wallets.length>1).map(o=>({wallets:o.wallets.map(shortAddr),share:o.share*100,level:o.level})),
    raw:r
  };
}

class Component extends DCLogic {''')

# настоящий фид вместо фейкового
rep('''  // Fake player. To go live: replace with e.g. new EventSource('/crawl?ca='+ca) → onmessage: handleEvent(JSON.parse(e.data)); and call handleResult(result) when it arrives.
  startFeed(ca){
    const evs=fakeEvents(); let i=0;
    const step=()=>{
      if(this.m.ca!==ca||i>=evs.length) return;
      this.handleEvent(evs[i]);
      if(i===1) this.handleResult(FAKE_RESULT);
      i++; this.feedT=setTimeout(step,(this.props.eventMs??150)*(0.7+Math.random()*0.6));
    };
    this.feedT=setTimeout(step,500);
  }
  stopFeed(){clearTimeout(this.feedT);}''',
'''  // Live feed: POST /api/scan -> poll /api/events every 300 ms -> queue -> handleEvent every eventMs -> /api/result.
  startFeed(ca){
    const feed={ca,job:null,after:0,queue:[],apiDone:false,stopped:false};
    this.feed=feed;
    const current=()=>this.feed===feed&&this.m.ca===ca;   // same scan, not stopped by stopFeed
    const alive=()=>current()&&!feed.stopped;              // still playing events
    const fail=msg=>{if(alive()){feed.queue.push({type:'error',detail:msg}); feed.apiDone=true;}};
    const poll=async()=>{
      if(!alive()||feed.apiDone) return;
      try{
        const r=await fetch(`/api/events?job=${feed.job}&after=${feed.after}`);
        const d=await r.json().catch(()=>({}));
        if(!alive()) return;
        if(!r.ok){fail(d.error||('http '+r.status)); return;}
        (d.events||[]).forEach(e=>feed.queue.push(e)); feed.after+=(d.events||[]).length;
        if(d.done){feed.apiDone=true; return;}
      }catch(err){/* network hiccup: retry */}
      if(alive()) feed.pollT=setTimeout(poll,300);
    };
    const loadResult=async()=>{
      for(let k=0;k<20&&current();k++){
        try{
          const r=await fetch(`/api/result?job=${feed.job}`); const d=await r.json();
          if(!current()) return;
          if(d.done){ if(d.error) this.handleEvent({type:'error',detail:d.error}); else this.handleResult(normalizeResult(d.result)); return; }
        }catch(err){}
        await new Promise(res=>setTimeout(res,300));
      }
    };
    const step=()=>{
      if(!alive()) return;
      const e=feed.queue.shift();
      if(e){
        const n=normalizeEvent(e);
        if(n.type==='done') loadResult();
        this.handleEvent(n);
        if(n.type==='done'||n.type==='error'){feed.stopped=true; return;}
      }else if(feed.apiDone){return;}
      const base=this.props.eventMs??150, k=e&&e.type==='stage'?0.4:(0.7+Math.random()*0.6);
      feed.feedT=setTimeout(step,e?base*k:60);
    };
    if(!/^0x[0-9a-fA-F]{40}$/.test(ca)){fail('not a token address'); step(); return;}   // same rule as engine.validate, no request
    (async()=>{
      try{
        const r=await fetch('/api/scan',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:ca})});
        const d=await r.json().catch(()=>({}));
        if(!alive()) return;
        if(!r.ok||!d.job){fail(d.error||('http '+r.status)); step(); return;}
        feed.job=d.job; poll(); step();
      }catch(err){fail('server unreachable'); step();}
    })();
  }
  stopFeed(){const f=this.feed; if(f){f.stopped=true; clearTimeout(f.pollT); clearTimeout(f.feedT); f.queue=[];} this.feed=null;}''')

# счёт для TOO EARLY: «—», а не 0
rep("score:done?String(m.scoreShown):'—',", "score:done&&done.score!=null?String(m.scoreShown):'—',")
# sample — настоящий токен Pons V2
rep("this.startScan('0x4d3d8a71c02f5be9e6b14d07a3c9f1e28b5a9023');", "this.startScan('" + SAMPLE + "');")

enc = encode(t)
TITLE_OLD = '<title>Bundled Page</title>'
TITLE_NEW = '<title>CRAWLSCAN</title>\n  ' + ICON_LINKS.replace('\n', '\n  ')
prefix = src[:a]
if prefix.count(TITLE_OLD) != 1:
    sys.exit("design.html: в обёртке нет <title>Bundled Page</title> — сборка остановлена")
prefix = prefix.replace(TITLE_OLD, TITLE_NEW)
out = prefix + enc + src[b:]
# проверка: всё вне template и title — байт-в-байт
assert prefix.replace(TITLE_NEW, TITLE_OLD) == src[:a]
assert out.endswith(src[b:]) and out[len(prefix):len(prefix) + len(enc)] == enc
assert json.loads(enc) == t
open(OUT, "w", encoding="utf-8").write(out)
print(f"index.html собран из design.html: {len(src):,} -> {len(out):,} байт")
