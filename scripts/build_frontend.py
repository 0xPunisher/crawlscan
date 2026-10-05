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
  - ссылки X и GitHub -> реальные адреса, в новой вкладке (target="_blank" rel="noopener");
  - роадмап: Multichain первым в NEXT с бейджем «in progress», Launch radar — в конец LATER;
  - токен проекта: PONS -> страница токена, секция «the token» (чарт, coming soon, CA с copy)
    между «crawlers at work» и «how it works», строка CA с copy в герое;
  - полоса TOO EARLY (TOO_EARLY_OR_LATE) и счёт «—» без скора.

Если дизайн поменялся так, что якорь правки не найден, скрипт падает с понятной ошибкой
и index.html не перезаписывает.

Запуск из корня репозитория: python3 scripts/build_frontend.py
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC, OUT = os.path.join(ROOT, "design.html"), os.path.join(ROOT, "index.html")
SAMPLE = "0xb4bb188e2d0e82ef9dba8b31ffe41855a2feac0f"   # токен для «try a sample»
X_URL = "https://x.com/0x_Punisher"
PONS_URL = "https://www.ponsfamily.com/launchpad/0x19dCb63C4d2F29A6f077F094a4f858fC790145e1"
GITHUB_URL = "https://github.com/0xPunisher/crawlscan"
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
# роадмап: Multichain -> первым в NEXT с бейджем «in progress», Launch radar -> в конец LATER.
# Карточка с бейджем собирается по образцу карточки Telegram bot, чтобы стиль совпадал с дизайном.
ITEM = '              <div data-grip="1" style="padding:18px 0;border-bottom:1px solid #141b20;display:flex;flex-direction:column;gap:6px">'
TITLE = '<span style="font-size:17px;font-weight:500;color:#eef1f3">'
DESC = '<span style="font-size:14.5px;line-height:1.5;color:#8a959c;text-wrap:pretty">'
mm = re.search(r'(<div style="display:flex;align-items:center;justify-content:space-between;gap:12px">)'
               + re.escape(TITLE) + r'Telegram bot</span>(<span style="display:flex;align-items:center;gap:6px;flex-shrink:0;[^"]*">.*?in progress</span>)</div>', t)
if not mm:
    sys.exit("design.html: не найдена карточка Telegram bot с бейджем in progress — сборка остановлена")
head_row, badge = mm.group(1), mm.group(2)
telegram = ITEM + mm.group(0)
multichain_old = ITEM + TITLE + 'Multichain</span>' + DESC + 'Solana, Base, BNB.</span></div>\n'
radar = ITEM + TITLE + 'Launch radar</span>' + DESC + 'Crawlers scan every new launch automatically and post alerts.</span></div>\n'
browser = ITEM + TITLE + 'Browser extension</span>' + DESC + 'Crawl any token straight from Dexscreener.</span></div>\n'
multichain_new = (ITEM + head_row + TITLE + 'Multichain</span>' + badge + '</div>'
                  + DESC + 'Solana and other EVM chains.</span></div>\n')
rep(multichain_old, '')
rep(radar, '')
rep(telegram, multichain_new + telegram)
rep(browser, browser + radar)

# соцсети: заглушки href="#" -> реальные ссылки в новой вкладке (X — в шапке и футере, GitHub — в футере)
ext = lambda url: f'href="{url}" target="_blank" rel="noopener"'
rep('<a href="#" data-grip="1" style="color:#8a959c" style-hover="color:#ffffff">X</a>',
    f'<a {ext(X_URL)} data-grip="1" style="color:#8a959c" style-hover="color:#ffffff">X</a>', count=2)
rep('<a href="#" data-grip="1" style="color:#8a959c" style-hover="color:#ffffff">GitHub</a>',
    f'<a {ext(GITHUB_URL)} data-grip="1" style="color:#8a959c" style-hover="color:#ffffff">GitHub</a>')

# ---------------------------------------------------------------------------
# токен проекта: ссылка PONS, секция с чартом между «crawlers at work» и «how it works»,
# строка CA в герое. CA копируется в исходном регистре (чексумма).
# ---------------------------------------------------------------------------
TOKEN_CA = "0x19dCb63C4d2F29A6f077F094a4f858fC790145e1"
TOKEN_TICKER = "CrawlScan"                      # symbol() из контракта
TOKEN_CA_SHORT = TOKEN_CA[:6] + "…" + TOKEN_CA[-4:]
# Чарт: CHART_KIND = "gecko" | "dexscreener" | None (None -> карточка «chart available after migration»).
# GeckoTerminal — пул кривой Pons (dex pons-v2, реальная ликвидность и объём). Пары Dexscreener
# для этого токена — сторонние V4-пулы с ликвидностью около $1: их график показывал бы ложную цену.
CHART_KIND = "gecko"
CHART_POOL = "0x75777d4b075af933df9264d460a3ce2ba7b2e9dd"   # кривая токена (get_launch)
DEX_PAIR = None                                              # пара Dexscreener, если появится настоящая
CHART_URL = {
    "gecko": f"https://www.geckoterminal.com/robinhood/pools/{CHART_POOL}?embed=1&info=0&swaps=0&grayscale=0&light_chart=0",
    "dexscreener": f"https://dexscreener.com/robinhood/{DEX_PAIR}?embed=1&theme=dark&info=0&trades=0",
}.get(CHART_KIND)
DEX_URL = f"https://dexscreener.com/robinhood/{DEX_PAIR}" if DEX_PAIR else None

MONO = "font-family:'JetBrains Mono',monospace"
ICON_COPY = ('<svg width="14" height="14" sc-camel-view-box="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
             'style="display:block"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"></rect>'
             '<path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5h-5A1.5 1.5 0 0 0 2.5 3v5a1.5 1.5 0 0 0 1.5 1.5h.5"></path></svg>')
ICON_CHECK = ('<svg width="14" height="14" sc-camel-view-box="0 0 16 16" fill="none" stroke="#00c805" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round" style="display:block"><path d="M3 8.5l3.2 3L13 4.5"></path></svg>')

def copy_button(key, label):
    """Кнопка copy: иконка копирования, после клика 1.5 с — зелёная галочка."""
    return (f'<button sc-camel-on-click="{{{{copyCa{key}}}}}" title="copy contract address" aria-label="{label}" '
            f'style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;padding:0;'
            f'border:1px solid #1c252b;border-radius:7px;background:#0b1013;color:#8a959c;cursor:pointer;flex-shrink:0" '
            f'style-hover="color:#ffffff;border-color:#2c353b">'
            f'<sc-if value="{{{{ca{key}Idle}}}}" hint-placeholder-val="{{{{true}}}}">{ICON_COPY}</sc-if>'
            f'<sc-if value="{{{{ca{key}Done}}}}" hint-placeholder-val="{{{{false}}}}">{ICON_CHECK}</sc-if></button>')

def ext_link(url, text):
    return (f'<a href="{url}" target="_blank" rel="noopener" data-grip="1" style="color:#9fd9ff;{MONO};font-size:13px" '
            f'style-hover="color:#ffffff">{text} ↗</a>')

if CHART_URL:
    chart = (f'<div data-grip="1" style="flex:1 1 620px;min-width:0;height:440px;border:1px solid #141b20;border-radius:12px;'
             f'background:#090c0f;overflow:hidden">'
             f'<iframe src="{CHART_URL}" title="${TOKEN_TICKER} chart" loading="lazy" frameborder="0" allow="clipboard-write" '
             f'style="display:block;width:100%;height:100%;border:0"></iframe></div>')
else:
    chart = (f'<div data-grip="1" style="flex:1 1 620px;min-width:0;min-height:300px;border:1px dashed #1c252b;border-radius:12px;'
             f'background:#090c0f;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:32px;box-sizing:border-box">'
             f'<span style="{MONO};font-size:13px;color:#8a959c">chart available after migration</span>'
             f'<a href="{PONS_URL}" target="_blank" rel="noopener" style="{MONO};font-size:13px;color:#04140a;background:#00c805;'
             f'padding:10px 18px;border-radius:8px" style-hover="background:#19dd1f">open on Pons ↗</a></div>')

badge_soon = ('<span style="display:inline-flex;align-items:center;gap:6px;' + MONO + ';font-size:10.5px;padding:3px 8px;'
              'border-radius:999px;color:#00c805;border:1px solid rgba(0,200,5,0.45);background:rgba(0,200,5,0.08)">'
              '<span style="width:5px;height:5px;border-radius:50%;background:#00c805;box-shadow:0 0 6px #00c805"></span>coming soon</span>')

links = ext_link(PONS_URL, "Pons") + (ext_link(DEX_URL, "Dexscreener") if DEX_URL else "")
TOKEN_SECTION = f'''      <section id="token" style="max-width:1280px;margin:0 auto;padding:40px 32px 120px;box-sizing:border-box">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:24px;border-top:1px solid #12181c;padding-top:22px;margin-bottom:44px">
          <h2 data-grip="1" style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.03em;color:#eef1f3">the token</h2>
          <span style="{MONO};font-size:12px;color:#5f6b72">02</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:24px;align-items:stretch">
          {chart}
          <div data-grip="1" style="flex:1 1 320px;min-width:0;display:flex;flex-direction:column;gap:18px;padding:28px 26px;border:1px solid #141b20;border-radius:14px;background:linear-gradient(180deg,#0c1114,#090c0f);box-shadow:inset 0 1px 0 rgba(255,255,255,0.03);box-sizing:border-box">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;{MONO};font-size:13px"><span style="color:#00c805">${TOKEN_TICKER}</span>{badge_soon}</div>
            <h3 style="margin:0;font-size:28px;font-weight:500;letter-spacing:-0.02em;color:#eef1f3">CRAWLSCAN has a token</h3>
            <p style="margin:0;font-size:15px;line-height:1.6;color:#8a959c">Holders of ${TOKEN_TICKER} will unlock premium features and priority crawling. Both are coming soon; the scanner stays free and read-only for everyone.</p>
            <p style="margin:0;{MONO};font-size:11.5px;line-height:1.6;color:#5f6b72">Utility only. Not financial advice.</p>
          </div>
        </div>
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;margin-top:20px;padding:16px 18px;border:1px solid #141b20;border-radius:12px;background:#090c0f">
          <span style="{MONO};font-size:12px;color:#5f6b72">Contract:</span>
          <span data-grip="1" style="{MONO};font-size:13px;color:#dfe5e8;word-break:break-all;min-width:0">{TOKEN_CA}</span>
          {copy_button("Section", "copy contract address")}
          <span style="flex:1 1 0"></span>
          <div style="display:flex;gap:18px">{links}</div>
        </div>
      </section>

'''

# PONS в шапке и футере -> страница токена на Pons, в новой вкладке
rep('<a href="#" title="CRAWLSCAN on PONS" data-grip="1"', f'<a {ext(PONS_URL)} title="CRAWLSCAN on PONS" data-grip="1"')
rep('<a href="#" data-grip="1" style="display:flex;align-items:center;gap:7px;color:#8a959c"',
    f'<a {ext(PONS_URL)} data-grip="1" style="display:flex;align-items:center;gap:7px;color:#8a959c"')

# секция токена — между #work и #how; номера следующих секций сдвигаются
rep('      <section id="how" ', TOKEN_SECTION + '      <section id="how" ')
rep('''color:#eef1f3">how it works</h2>
          <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#5f6b72">02</span>''',
    '''color:#eef1f3">how it works</h2>
          <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#5f6b72">03</span>''')
mr = re.search(r'''(color:#eef1f3">roadmap</h2>\s*<span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#5f6b72">)03(</span>)''', t)
if not mr:
    sys.exit("design.html: не найден номер секции roadmap — сборка остановлена")
t = t[:mr.start()] + mr.group(1) + "04" + mr.group(2) + t[mr.end():]

# строка CA в герое, под полем ввода
rep('          <span>read-only · no wallet connect · Robinhood Chain</span>\n',
    '          <span>read-only · no wallet connect · Robinhood Chain</span>\n'
    f'          <span style="display:inline-flex;align-items:center;gap:8px"><span style="color:#00c805">${TOKEN_TICKER}</span>'
    f'<span>CA:</span><span title="{TOKEN_CA}" style="color:#aab4ba">{TOKEN_CA_SHORT}</span>{copy_button("Hero", "copy token contract address")}</span>\n')

# логика copy в компоненте: CA в исходном регистре, галочка на 1.5 с
rep("  blank(ca){return {", f"""  copyCa(key){{
    const ca={json.dumps(TOKEN_CA)};
    const fallback=()=>{{const t=document.createElement('textarea'); t.value=ca; t.style.position='fixed'; t.style.opacity='0'; document.body.appendChild(t); t.select(); try{{document.execCommand('copy');}}catch(e){{}} t.remove();}};
    try{{ if(navigator.clipboard&&window.isSecureContext) navigator.clipboard.writeText(ca).catch(fallback); else fallback(); }}catch(e){{fallback();}}
    this.setState({{caCopied:key}}); clearTimeout(this._caT); this._caT=setTimeout(()=>this.setState({{caCopied:''}}),1500);
  }}
  blank(ca){{return {{""")
rep("      canvasRef:this.canvasRef, logRef:this.logRef,",
    "      canvasRef:this.canvasRef, logRef:this.logRef,\n"
    "      copyCaHero:()=>this.copyCa('hero'), copyCaSection:()=>this.copyCa('section'),\n"
    "      caHeroDone:this.state.caCopied==='hero', caHeroIdle:this.state.caCopied!=='hero',\n"
    "      caSectionDone:this.state.caCopied==='section', caSectionIdle:this.state.caCopied!=='section',")

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
