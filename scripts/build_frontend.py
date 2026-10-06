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
  - роадмап: «Solana support (pump.fun)» — в SHIPPED; в NEXT первым «Early buyers crawl», за ним
    «All-chain support» (больше EVM-сетей и не только); Launch radar — в конец LATER;
  - герой: обе сети — бейдж «memecoin holder scanner · Robinhood and Solana», заголовок «for Robinhood and
    Solana memecoins», подпись под полем «… · Robinhood and Solana»; Solana — цветом Solana (#9945FF);
  - токен проекта: PONS -> страница токена, секция «the token» (чарт, coming soon, CA с copy)
    между «crawlers at work» и «how it works», строка CA с copy в герое;
  - телефон (≤ 640 px): без горизонтальной прокрутки — компактное меню в шапке, таблицы в две строки,
    переносы в логах, отступы 16 px;
  - полоса TOO EARLY (TOO_EARLY_OR_LATE) и счёт «—» без скора;
  - цвета частей скора и критериев: больше баллов = чище = зелёный, мало = красный;
  - две сети: сеть по адресу (0x + 40 hex — Robinhood, base58 32–44 — Solana, регистр Solana
    не меняется), переключатель «Robinhood | Solana» над полем (плейсхолдер и sample сети),
    бейдж сети у тикера, ссылки solscan для Solana (у Robinhood ссылок нет, как в дизайне),
    слот вместо блока, unread-кошельки серым, «operator (dump impact)», сообщение
    «Solana support is coming soon» под полем (флаг из /api/config).
  - розыгрыш: секция «daily draw» после «the token», только если /api/draw/status -> enabled: true:
    победитель последнего розыгрыша (solscan, шанс, приз, транзакция выплаты или payout pending),
    таймер до next_draw, участники и снимки за сегодня, правила, Verify (пересчёт list_hash и
    победителя в браузере: SubtleCrypto sha256 + BigInt, формула draw.py), история за 7 дней;
    номера how/roadmap сдвигаются, только когда секция есть.
  - Token Burn & Holder Rewards: секция сразу после «the token», только если /api/rewards/status ->
    enabled: true. Две карточки: Burn (таймер до next_burn, последнее сжигание, всего сожжено и % сапплая,
    кошелёк разработчика «burns from» и адрес сжиганий с copy) и Holder Rewards (кошелёк «rewards paid from», таймер до next_draw, блок доверия с Verify по
    /api/rewards/<day>/*, последний победитель с copy, шанс, выплата или payout pending).
    Таймер в нуле — «Waiting for burn transaction…» / «Picking the winner…»; новое сжигание — вспышка
    «Tokens burned: N», новый розыгрыш — появление победителя. Большие таймеры — цифры в зелёных плитках
    (моноширинные, фиксированной ширины), двоеточия без плиток. Ссылки — Blockscout Robinhood Chain.

Если дизайн поменялся так, что якорь правки не найден, скрипт падает с понятной ошибкой
и index.html не перезаписывает.

Запуск из корня репозитория: python3 scripts/build_frontend.py
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC, OUT = os.path.join(ROOT, "design.html"), os.path.join(ROOT, "index.html")
SAMPLES = {"robinhood": "0xb4bb188e2d0e82ef9dba8b31ffe41855a2feac0f",   # «try a sample» по сети
           "solana": "fjKUqPWK9m331Y5TZZNFismtqoP2MGWAMHEkB62pump"}
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
const RH_RE=/^0x[0-9a-fA-F]{40}$/, SOL_RE=/^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const chainOf=a=>typeof a!=='string'?null:RH_RE.test(a)?'robinhood':SOL_RE.test(a)?'solana':null;   // как engine.chain_of
const CHAIN_NAME={robinhood:'Robinhood',solana:'Solana'};
const SAMPLES=__SAMPLES__;
const PLACEHOLDER={robinhood:'0x… token contract address',solana:'token mint address (base58)'};
const SOON='Solana support is coming soon';
const EXPLORER={solana:{account:a=>'https://solscan.io/account/'+a,token:a=>'https://solscan.io/token/'+a}};   // Robinhood: ссылок нет
const FULL={};   // короткий адрес -> полный (ссылки на эксплорер)
const shortAddr=a=>{const c=chainOf(a); if(!c) return a; const s=a.slice(0,c==='robinhood'?6:4)+'…'+a.slice(-4); FULL[s]=a; return s;};
const fmtImpact=i=>typeof i!=='number'?null:i<0.01?'<1%':'−'+Math.round(i*100)+'%';
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
      if(e.kind==null&&!fl.includes('unread')) fl.push('unread');   // вход не найден (Solana)
      o.detail=(fl.length?fl:['clean']).join(', ');
      o.share=e.share!=null?e.share*100:null; break;
    }
    case 'link': {   // где связь: блок (Robinhood) / слот (Solana), общая транзакция, раздатчик
      const v=e.via||'', mb=/^block (\d+)/.exec(v);
      o.where=mb?(e.chain==='solana'?'slot ':'block ')+mb[1]:e.kind==='same_tx'?'same tx':e.kind==='distributor'?'via '+shortAddr(v):e.kind==='direct'?'direct transfer':'';
      break;
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
  const ch=r.chain||chainOf(r.token)||'robinhood', dec=ch==='solana'?1e6:1e18;   // pump.fun — 6 знаков, Pons — 18
  const h=r.header||{}, sup=(r.supply||0)/dec, circ=(r.circulating||0)/dec;
  return {
    header:{name:h.name,ticker:h.ticker,mcap:h.mcap_usd,liquidity:h.liquidity_usd,vol24h:h.vol24h_usd,
            age:fmtAge(h.age_h),price:fmtPrice(h.price_usd),holders:r.holders_total,
            supply:sup?Math.round(sup).toLocaleString('en-US'):null,
            circulating:sup?`${fmtTok(circ)} (${(circ/sup*100).toFixed(1)}%)`:null,
            pool:ch==='solana'?(r.launch&&r.launch.complete?'PumpSwap':'pump.fun curve'):'Pons V2',
            deployer:shortAddr(r.launch&&r.launch.deployer)},
    chain:ch, impact:r.metrics&&typeof r.metrics.impact==='number'?r.metrics.impact:null,
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
    if(!chainOf(ca)){fail('not a token address'); step(); return;}   // same rule as engine.chain_of, no request
    (async()=>{
      try{
        const r=await fetch('/api/scan',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:ca})});
        const d=await r.json().catch(()=>({}));
        if(!alive()) return;
        if(!r.ok&&d.error===SOON){this.soon(ca); return;}   // Solana выключена: сообщение под полем, не поломка
        if(!r.ok||!d.job){fail(d.error||('http '+r.status)); step(); return;}
        feed.job=d.job; poll(); step();
      }catch(err){fail('server unreachable'); step();}
    })();
  }
  stopFeed(){const f=this.feed; if(f){f.stopped=true; clearTimeout(f.pollT); clearTimeout(f.feedT); f.queue=[];} this.feed=null;}''')

# счёт для TOO EARLY: «—», а не 0
rep("score:done?String(m.scoreShown):'—',", "score:done&&done.score!=null?String(m.scoreShown):'—',")
# sample — настоящий токен выбранной сети (SAMPLES)
rep("this.startScan('0x4d3d8a71c02f5be9e6b14d07a3c9f1e28b5a9023');", "this.startScan(SAMPLES[this.state.net]);")
# роадмап (v1.1.0): Solana support -> в конец SHIPPED; в NEXT первыми Early buyers crawl и All-chain support
# (больше EVM-сетей и не только, без бейджа); Launch radar -> в конец LATER. Карточки — по образцу карточек дизайна.
ITEM = '              <div data-grip="1" style="padding:18px 0;border-bottom:1px solid #141b20;display:flex;flex-direction:column;gap:6px">'
TITLE = '<span style="font-size:17px;font-weight:500;color:#eef1f3">'
DESC = '<span style="font-size:14.5px;line-height:1.5;color:#8a959c;text-wrap:pretty">'
mm = re.search(r'(<div style="display:flex;align-items:center;justify-content:space-between;gap:12px">)'
               + re.escape(TITLE) + r'Telegram bot</span>(<span style="display:flex;align-items:center;gap:6px;flex-shrink:0;[^"]*">.*?in progress</span>)</div>', t)
if not mm:
    sys.exit("design.html: не найдена карточка Telegram bot с бейджем in progress — сборка остановлена")
telegram = ITEM + mm.group(0)
multichain_old = ITEM + TITLE + 'Multichain</span>' + DESC + 'Solana, Base, BNB.</span></div>\n'
radar = ITEM + TITLE + 'Launch radar</span>' + DESC + 'Crawlers scan every new launch automatically and post alerts.</span></div>\n'
browser = ITEM + TITLE + 'Browser extension</span>' + DESC + 'Crawl any token straight from Dexscreener.</span></div>\n'
operator_card = (ITEM + TITLE + 'Operator clustering</span>' + DESC
                 + 'Linked wallets collapse into one operator, scored on real circulating float.</span></div>\n')
solana_card = ITEM + TITLE + 'Solana support (pump.fun)</span>' + DESC + 'Chain detected from the address, Solscan links, same verdict.</span></div>\n'
early_card = ITEM + TITLE + 'Early buyers crawl</span>' + DESC + 'See how much supply was bundled at launch, even after bundlers exit.</span></div>\n'
multichain_new = early_card + ITEM + TITLE + 'All-chain support</span>' + DESC + 'More EVM chains and beyond.</span></div>\n'
rep(multichain_old, '')
rep(radar, '')
rep(telegram, multichain_new + telegram)
rep(browser, browser + radar)
rep(operator_card, operator_card + solana_card)

# герой: «for Robinhood and Solana memecoins», «and» — цветом заголовка, Solana — фиолетовым Solana
rep('<span data-grip="1" style="color:#00c805">Robinhood</span><span data-grip="1" style="color:#00c805">memecoins</span>',
    '<span data-grip="1" style="color:#00c805">Robinhood</span><span data-grip="1">and</span>'
    '<span data-grip="1" style="color:#9945FF">Solana</span><span data-grip="1" style="color:#00c805">memecoins</span>')

rep('<span>memecoin holder scanner · Robinhood Chain</span>',
    '<span>memecoin holder scanner · <span style="white-space:nowrap">Robinhood and <span style="color:#9945FF">Solana</span></span></span>')

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
    '          <span>read-only · no wallet connect · <span style="white-space:nowrap">Robinhood and <span style="color:#9945FF">Solana</span></span></span>\n'
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

# ---------------------------------------------------------------------------
# телефон (≤ 640 px): без горизонтальной прокрутки и без обрезки. Классы cs-* навешиваются на
# элементы дизайна, правила — в одном @media (инлайн-стили дизайна перебиваются !important).
# ---------------------------------------------------------------------------
MOBILE_CSS = """
.cs-menu{display:none;position:relative}
.cs-menu>summary{list-style:none}
.cs-menu>summary::-webkit-details-marker{display:none}
@media (max-width:640px){
  .cs-wrap{padding-left:16px!important;padding-right:16px!important}
  .cs-head{gap:12px!important}
  .cs-nav{gap:14px!important}
  .cs-wide{display:none!important}
  .cs-menu{display:block}
  .cs-demo-table{padding:10px 10px 10px 22px!important}
  .cs-demo-head,.cs-demo-row{grid-template-columns:minmax(0,1fr) 52px!important;gap:6px 12px!important;padding-top:8px!important;padding-bottom:8px!important}
  .cs-demo-head>:nth-child(3),.cs-demo-row>:nth-child(3){grid-column:1/-1}
  .cs-scan-table{padding:0 92px 0 18px!important}
  .cs-row-head,.cs-row{grid-template-columns:22px minmax(0,1fr) auto!important;gap:6px 10px!important;padding:8px!important}
  .cs-row-head>:nth-child(4),.cs-row>:nth-child(4){grid-column:2/-1}
  .cs-bar{display:none!important}
  .cs-crit-head,.cs-crit-row{grid-template-columns:minmax(0,1fr) minmax(110px,140px)!important;gap:10px 14px!important;padding-left:14px!important;padding-right:14px!important}
  .cs-crit-head>:first-child,.cs-crit-row>:first-child{grid-column:1/-1}
  .cs-status{height:auto!important;min-height:52px;flex-wrap:wrap;font-size:13px!important;gap:6px 12px!important;padding-top:8px!important;padding-bottom:8px!important}
  .cs-status>div{flex-wrap:wrap;gap:6px 10px!important}
  .cs-status-ca{white-space:nowrap}
  .cs-stage{white-space:normal!important;overflow:visible!important;text-overflow:clip!important}
  .cs-log-line{white-space:normal!important;flex-wrap:wrap}
  .cs-log-line>span{overflow:visible!important;text-overflow:clip!important;overflow-wrap:anywhere}
}
"""
rep("a{color:#8a959c;text-decoration:none}", "a{color:#8a959c;text-decoration:none}" + MOBILE_CSS)

# контейнеры секций и шапки: боковые отступы 16 px на телефоне
n_wrap = t.count(' style="max-width:1280px;')
if n_wrap < 5:
    sys.exit(f"design.html: контейнеров max-width:1280px найдено {n_wrap} — сборка остановлена")
t = t.replace(' style="max-width:1280px;', ' class="cs-wrap" style="max-width:1280px;')
rep('class="cs-wrap" style="max-width:1280px;margin:0 auto;padding:0 32px;height:64px;',
    'class="cs-wrap cs-head" style="max-width:1280px;margin:0 auto;padding:0 32px;height:64px;')

# шапка: «how it works» и «roadmap» на телефоне уходят в компактное меню, X и PONS видны всегда
rep('<nav style="display:flex;align-items:center;gap:28px;', '<nav class="cs-nav" style="display:flex;align-items:center;gap:28px;')
rep('<a href="#how" sc-camel-on-click="{{navHow}}" data-grip="1"', '<a class="cs-wide" href="#how" sc-camel-on-click="{{navHow}}" data-grip="1"')
rep('<a href="#roadmap" sc-camel-on-click="{{navRoadmap}}" data-grip="1"', '<a class="cs-wide" href="#roadmap" sc-camel-on-click="{{navRoadmap}}" data-grip="1"')
menu_link = lambda handler, href, text: (f'<a href="{href}" sc-camel-on-click="{{{{{handler}}}}}" style="display:block;padding:10px 12px;'
                                         f'border-radius:6px;color:#c9d1d6" style-hover="background:#121a1f;color:#ffffff">{text}</a>')
MENU = ('<details class="cs-menu"><summary aria-label="menu" title="menu" style="display:flex;align-items:center;cursor:pointer;'
        'color:#8a959c;padding:4px 2px">'
        '<svg width="18" height="18" sc-camel-view-box="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" style="display:block"><path d="M3 5h12M3 9h12M3 13h12"></path></svg></summary>'
        '<div style="position:absolute;right:0;top:calc(100% + 14px);min-width:180px;display:flex;flex-direction:column;'
        'padding:6px;border:1px solid #1c252b;border-radius:10px;background:#0b1013;box-shadow:0 12px 32px rgba(0,0,0,0.5);z-index:30">'
        + menu_link("navToken", "#token", "the token") + menu_link("navHow", "#how", "how it works")
        + menu_link("navRoadmap", "#roadmap", "roadmap") + '</div></details>')
mr = re.search(r'(<a class="cs-wide" href="#roadmap"[^>]*>roadmap</a>)', t)
if not mr:
    sys.exit("design.html: не найден пункт roadmap в шапке — сборка остановлена")
t = t[:mr.end()] + "\n" + MENU + t[mr.end():]
# пункты меню закрывают его и прокручивают к секции
rep("navRoadmap:e=>{e.preventDefault(); this.scrollToId('roadmap');},",
    "navRoadmap:e=>{e.preventDefault(); this.closeMenu(); this.scrollToId('roadmap');},\n"
    "      navToken:e=>{e.preventDefault(); this.closeMenu(); this.scrollToId('token');},")
rep("navHow:e=>{e.preventDefault(); this.scrollToId('how');},",
    "navHow:e=>{e.preventDefault(); this.closeMenu(); this.scrollToId('how');},")
rep("  blank(ca){return {", "  closeMenu(){document.querySelectorAll('details.cs-menu').forEach(d=>d.removeAttribute('open'));}\n  blank(ca){return {")

# демо-таблица на лендинге
rep('padding:10px 14px 10px 44px;box-sizing:border-box">', 'padding:10px 14px 10px 44px;box-sizing:border-box" class="cs-demo-table">')
rep('<div style="display:grid;grid-template-columns:minmax(120px,150px) 64px minmax(0,1fr);gap:16px;padding:10px 12px;',
    '<div class="cs-demo-head" style="display:grid;grid-template-columns:minmax(120px,150px) 64px minmax(0,1fr);gap:16px;padding:10px 12px;')
rep('<div data-grip="1" data-demo-row="{{$index}}" style=', '<div class="cs-demo-row" data-grip="1" data-demo-row="{{$index}}" style=')

# таблица кошельков скана: на телефоне ранг · адрес · доля, флаги — второй строкой
rep('<div data-table="1" style="flex:1 1 640px;min-width:0;padding:0 150px 0 40px;',
    '<div class="cs-scan-table" data-table="1" style="flex:1 1 640px;min-width:0;padding:0 150px 0 40px;')
rep('<div style="display:grid;grid-template-columns:28px minmax(120px,150px) minmax(110px,1fr) minmax(0,1.5fr);gap:16px;padding:10px 12px;',
    '<div class="cs-row-head" style="display:grid;grid-template-columns:28px minmax(120px,150px) minmax(110px,1fr) minmax(0,1.5fr);gap:16px;padding:10px 12px;')
rep('<div data-grip="1" data-row="{{r.addr}}" style=', '<div class="cs-row" data-grip="1" data-row="{{r.addr}}" style=')
rep('<div style="flex:0 0 64px;height:3px;background:#141b20;', '<div class="cs-bar" style="flex:0 0 64px;height:3px;background:#141b20;')
# подпись оператора у скобки: на узком экране — тремя короткими строками
rep("ctx.fillStyle='#c9d1d6'; ctx.fillText(C.label2,x+12,mid+12);",
    "ctx.fillStyle='#c9d1d6'; if(innerWidth<640) C.label2.split(' · ').forEach((s,i)=>ctx.fillText(s,x+12,mid+12+i*14)); else ctx.fillText(C.label2,x+12,mid+12);")

# строка статуса скана: на телефоне переносится, стадия без многоточия
rep('class="cs-wrap" style="max-width:1280px;margin:0 auto;padding:0 32px;height:52px;',
    'class="cs-wrap cs-status" style="max-width:1280px;margin:0 auto;padding:0 32px;height:52px;')
rep('<span data-grip="1" style="color:#9fd9ff">{{caShort}}</span>', '<span class="cs-status-ca" data-grip="1" style="color:#9fd9ff">{{caShort}}</span>')
rep('<span style="color:#5f6b72;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">· {{stage}}</span>',
    '<span class="cs-stage" style="color:#5f6b72;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">· {{stage}}</span>')

# таблица критериев: на телефоне критерий во всю ширину, под ним finding и score
rep('<div style="display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1.3fr) minmax(120px,170px);gap:20px;padding:12px 20px;',
    '<div class="cs-crit-head" style="display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1.3fr) minmax(120px,170px);gap:20px;padding:12px 20px;')
rep('<div data-grip="1" style="display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1.3fr) minmax(120px,170px);gap:20px;align-items:center;',
    '<div class="cs-crit-row" data-grip="1" style="display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1.3fr) minmax(120px,170px);gap:20px;align-items:center;')

# строки логов: на телефоне переносятся вместо обрезки
rep('<div data-grip="1" style="display:flex;gap:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">',
    '<div class="cs-log-line" data-grip="1" style="display:flex;gap:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">')
rep('<div style="display:flex;gap:10px;white-space:nowrap;overflow:hidden">',
    '<div class="cs-log-line" style="display:flex;gap:10px;white-space:nowrap;overflow:hidden">')
rep('<div style="display:flex;gap:10px;white-space:nowrap">', '<div class="cs-log-line" style="display:flex;gap:10px;white-space:nowrap">')

# чипы адресов на сцене: на узком экране круг уже, чтобы крайние чипы не выходили за экран
rep("chips.push({left:(50+Math.cos(a)*38)+'%'", "chips.push({left:(50+Math.cos(a)*(innerWidth<640?31:38))+'%'")

# ---------------------------------------------------------------------------
# две сети: переключатель над полем, плейсхолдер/sample сети, бейдж сети, ссылки solscan,
# слот вместо блока, unread серым, «operator (dump impact)», «coming soon» под полем.
# ---------------------------------------------------------------------------
t = t.replace("const SAMPLES=__SAMPLES__;", "const SAMPLES=" + json.dumps(SAMPLES) + ";")
pill = lambda key, handler, label, extra="": (
    f'<button sc-camel-on-click="{{{{{handler}}}}}" aria-label="{label}" style="height:30px;padding:0 14px;border-radius:999px;'
    f'border:1px solid {{{{{key}Border}}}};background:{{{{{key}Bg}}}};color:{{{{{key}Color}}}};{MONO};font-size:12px;cursor:pointer;'
    f'display:inline-flex;align-items:center;gap:6px" style-hover="color:#ffffff">{label}{extra}</button>')
SOON_TAG = '<sc-if value="{{solSoon}}" hint-placeholder-val="{{false}}"><span style="color:#5f6b72;font-size:10.5px">soon</span></sc-if>'
TOGGLE = ('        <div role="group" aria-label="network" style="display:flex;align-items:center;gap:6px">'
          + pill("rh", "pickRh", "Robinhood") + pill("sol", "pickSol", "Solana", SOON_TAG) + '</div>\n')
rep('        <div style="display:flex;flex-wrap:wrap;gap:12px;">\n', TOGGLE + '        <div style="display:flex;flex-wrap:wrap;gap:12px;">\n')
rep('placeholder="0x… token contract address"', 'placeholder="{{placeholder}}"')
rep('<sc-if value="{{inputError}}" hint-placeholder-val="{{false}}"><span style="color:#ff4d4d">{{inputError}}</span></sc-if>',
    '<sc-if value="{{inputError}}" hint-placeholder-val="{{false}}"><span style="color:#ff4d4d">{{inputError}}</span></sc-if>'
    '<sc-if value="{{inputNotice}}" hint-placeholder-val="{{false}}"><span data-notice="1" style="display:inline-flex;align-items:center;gap:8px;color:#9fd9ff">'
    '<span style="width:6px;height:6px;border-radius:50%;background:#9fd9ff;box-shadow:0 0 6px #9fd9ff"></span>{{inputNotice}}</span></sc-if>')

# бейдж сети у тикера и ссылка на токен
TICKER = "<span data-grip=\"1\" style=\"font-family:'JetBrains Mono',monospace;font-size:14px;color:#9fd9ff\">${{hTicker}}</span>"
rep(TICKER, TICKER + f'<span data-chain="1" style="align-self:center;{MONO};font-size:10.5px;padding:2px 8px;border-radius:999px;color:#c9d1d6;'
    'border:1px solid #2c353b;background:#0b1013">{{chainLabel}}</span>')
CA_STYLE = f"{MONO};font-size:12px;color:#5f6b72;word-break:break-all"
rep("<span style=\"font-family:'JetBrains Mono',monospace;font-size:12px;color:#5f6b72\">{{ca}}</span>",
    f'<sc-if value="{{{{caLink}}}}" hint-placeholder-val="{{{{false}}}}"><a href="{{{{caUrl}}}}" target="_blank" rel="noopener" style="{CA_STYLE}" style-hover="color:#9fd9ff">{{{{ca}}}} ↗</a></sc-if>'
    f'<sc-if value="{{{{caPlain}}}}" hint-placeholder-val="{{{{true}}}}"><span style="{CA_STYLE}">{{{{ca}}}}</span></sc-if>')

# адрес кошелька в таблице: ссылка solscan для Solana, иначе текст
ADDR = "font-family:'JetBrains Mono',monospace;font-size:13px;color:{{r.addrColor}}"
rep(f'<span style="{ADDR}">{{{{r.addr}}}}</span>',
    f'<span style="display:flex;min-width:0">'
    f'<sc-if value="{{{{r.hasUrl}}}}" hint-placeholder-val="{{{{false}}}}"><a href="{{{{r.url}}}}" target="_blank" rel="noopener" style="{ADDR}" style-hover="color:#9fd9ff">{{{{r.addr}}}}</a></sc-if>'
    f'<sc-if value="{{{{r.noUrl}}}}" hint-placeholder-val="{{{{true}}}}"><span style="{ADDR}">{{{{r.addr}}}}</span></sc-if></span>')

# unread: свой флаг (серый, без веса); строка таблицы серым
rep("clean:{t:'clean',c:G,w:0,log:'clean'}};", "clean:{t:'clean',c:G,w:0,log:'clean'},unread:{t:'unread',c:'#5f6b72',w:0,log:'entry not read'}};")
rep("    const n=Math.max(20,m.wallets.length), rows=[], chips=[];",
    "    const n=Math.max(20,m.wallets.length), rows=[], chips=[], ex=EXPLORER[chainOf(m.ca)];")
rep("rows.push({rank,addr:'0x····…····',addrColor:'#262f35',bg:'transparent',shareText:'',shareW:'0%',shareColor:'#1c252b',badges:[]});",
    "rows.push({rank,addr:ex?'····…····':'0x····…····',addrColor:'#262f35',bg:'transparent',shareText:'',shareW:'0%',shareColor:'#1c252b',badges:[],url:'',hasUrl:false,noUrl:true});")
rep("label:w?w.addr:'0x····',", "label:w?w.addr:(ex?'····':'0x····'),")   # чипы на сцене — так же
rep("        rows.push({rank,addr:w.addr,addrColor:'#dfe5e8',bg:col?rgba(col,fresh?0.2:(wo.w?0.06:0.025)):'rgba(159,217,255,0.03)',shareText:w.share!=null?w.share.toFixed(1)+'%':'…',shareW:w.share!=null?Math.min(100,w.share/6*100)+'%':'0%',shareColor:col||'#3a454c',badges:fl.map(badge)});",
    "        const un=fl.includes('unread'), url=ex?ex.account(FULL[w.addr]||w.addr):'';\n"
    "        rows.push({rank,addr:w.addr,addrColor:un?'#5f6b72':'#dfe5e8',bg:un?'transparent':col?rgba(col,fresh?0.2:(wo.w?0.06:0.025)):'rgba(159,217,255,0.03)',shareText:w.share!=null?w.share.toFixed(1)+'%':'…',shareW:w.share!=null?Math.min(100,w.share/6*100)+'%':'0%',shareColor:un?'#2c353b':col||'#3a454c',badges:fl.map(badge),url,hasUrl:!!url,noUrl:!url});")

# лог связи: где (блок / слот / транзакция / раздатчик)
rep("this.log('link',e.level==='proven'?RD:A,`${e.wallet} ⇄ ${e.b} · ${e.level}`); break;",
    "this.log('link',e.level==='proven'?RD:A,`${e.wallet} ⇄ ${e.b} · ${e.level}`+(e.where?` · ${e.where}`:'')); break;")

# части скора и критерии: оператор — по dump impact; стая — слоты на Solana
rep("return {label:k,text:v!=null?`${v} / ${max}`:`— / ${max}`,",
    "return {label:k==='operator'?'operator (dump impact)':k,text:v!=null?`${v} / ${max}`:`— / ${max}`,")
rep("w:(r*100)+'%',color:r>0.66?RD:r>0.33?A:G};});", "w:(r*100)+'%',color:r>=0.8?G:r>=0.4?A:RD};});")   # баллы части: больше = чище = зелёный
rep("    const lvl=r=>r>=0.6?RD:r>0.2?A:G;",
    "    const lvl=r=>r>=0.8?G:r>=0.4?A:RD, slots=chainOf(m.ca)==='solana', imp=fmtImpact(res&&res.impact);")   # r — доля набранных баллов: больше = чище
rep("{name:'Operator clustering',desc:'Linked wallets collapse into one operator',find:n?`${n} wallets → ${ops} operators`+(big.length>1?` · biggest: ${big.length} wallets, ${pct(big)}`:''):null,",
    "{name:'Operator clustering',desc:'Linked wallets collapse into one operator, scored by dump impact',find:n?`${n} wallets → ${ops} operators`+(big.length>1?` · biggest: ${big.length} wallets, ${pct(big)}`:'')+(done&&imp?` · dump ${imp}`:''):null,")
rep("desc:'3+ fresh wallets buying in one block with matching sizes',find:n?(pk.length?`${pk.length} pack · ${pk[0].wallets.length} wallets in one block`",
    "desc:slots?'3+ fresh wallets buying within 2 slots with matching sizes':'3+ fresh wallets buying in one block with matching sizes',find:n?(pk.length?`${pk.length} pack · ${pk[0].wallets.length} wallets `+(slots?'within 2 slots':'in one block')")

# состояние: сеть, сообщение под полем, флаг Solana с сервера
rep("  state={view:'landing',input:'',inputError:'',copied:false,v:0};",
    "  state={view:'landing',input:'',inputError:'',inputNotice:'',net:'robinhood',solanaOn:null,copied:false,v:0};")
rep("    window.handleResult=r=>this.handleResult(r);\n",
    "    window.handleResult=r=>this.handleResult(r);\n"
    "    this.cfg=fetch('/api/config').then(r=>r.json()).then(d=>this.setState({solanaOn:!!d.solana})).catch(()=>{});\n")
# ?ca= с солановским адресом: сначала флаг Solana с сервера, иначе лишний POST и 400 в консоли
rep("    if(ca) this.startScan(ca,true); else this.toLanding(true);",
    "    if(ca&&chainOf(ca.trim())==='solana'&&this.state.solanaOn==null&&this.cfg){const c=this.cfg; this.cfg=null; c.then(()=>this.fromUrl()); return;}\n"
    "    if(ca) this.startScan(ca,true); else this.toLanding(true);")
rep("    const ca=(raw||'').trim();\n    if(!/^0x[0-9a-fA-F]{6,}$/.test(ca)){this.setState({inputError:'enter a 0x… contract address'});return;}",
    "    const ca=(raw||'').trim(), net=chainOf(ca);   // регистр не меняем: base58 чувствителен к регистру\n"
    "    if(!net){this.setState({inputError:'enter a token address: 0x… (Robinhood) or base58 (Solana)',inputNotice:''});return;}\n"
    "    if(net==='solana'&&this.state.solanaOn===false){this.soon(ca);return;}")
rep("    this.setState({view:'scan',input:'',inputError:'',copied:false});",
    "    this.setState({view:'scan',input:'',inputError:'',inputNotice:'',net,copied:false});")
rep("  blank(ca){return {", """  soon(ca){   // Solana выключена: лендинг, адрес в поле, сообщение под ним (не красная поломка)
    this.stopFeed(); clearInterval(this.timer);
    if(location.search) history.replaceState(null,'',location.pathname);
    this.links=[]; this.clusters=[];
    const was=this.state.view;
    this.setState({view:'landing',input:ca,inputError:'',inputNotice:SOON,net:'solana'});
    if(was!=='landing'){window.scrollTo(0,0); this.startLanding();}
  }
  netVals(){
    const net=this.state.net, ch=chainOf(this.m.ca)||'robinhood', ex=EXPLORER[ch];
    const st=on=>on?{c:'#eef1f3',b:'rgba(0,200,5,0.55)',g:'rgba(0,200,5,0.1)'}:{c:'#5f6b72',b:'#1c252b',g:'transparent'};
    const rh=st(net==='robinhood'), so=st(net==='solana');
    const pick=n=>()=>this.setState({net:n,inputError:'',inputNotice:''});
    return {placeholder:PLACEHOLDER[net], inputNotice:this.state.inputNotice,
      rhColor:rh.c, rhBorder:rh.b, rhBg:rh.g, solColor:so.c, solBorder:so.b, solBg:so.g,
      solSoon:this.state.solanaOn===false, pickRh:pick('robinhood'), pickSol:pick('solana'),
      chainLabel:CHAIN_NAME[ch], caUrl:ex?ex.token(this.m.ca):'', caLink:!!ex, caPlain:!ex};
  }
  blank(ca){return {""")
rep("      onInput:e=>this.setState({input:e.target.value,inputError:''}),",
    "      onInput:e=>{const v=e.target.value, c=chainOf(v.trim()); this.setState(c?{input:v,inputError:'',inputNotice:'',net:c}:{input:v,inputError:'',inputNotice:''});},\n"
    "      ...this.netVals(),")


# ---------------------------------------------------------------------------
# розыгрыш: секция «daily draw» после «the token». Есть только если GET /api/draw/status
# вернул enabled: true (иначе секции нет, номера следующих секций не сдвигаются).
# Победитель последнего розыгрыша, таймер до next_draw, участники и снимки за сегодня,
# правила, Verify (пересчёт list_hash и победителя в браузере по формуле draw.py), история за 7 дней.
# ---------------------------------------------------------------------------
DRAW_JS = r'''// ---- daily draw (/api/draw/*) ----
const SOL_ACC=a=>'https://solscan.io/account/'+a, SOL_TX=t=>'https://solscan.io/tx/'+t;
const drawShort=a=>a?a.slice(0,4)+'…'+a.slice(-4):'—';
const BIG_KEYS=['weight','total_weight','r','winner_weight','total'];   // веса — целые базовые единицы: BigInt, без потери точности
const parseBig=txt=>JSON.parse(txt,(k,v,ctx)=>BIG_KEYS.includes(k)&&typeof v==='number'?BigInt(ctx&&ctx.source!=null?ctx.source:v):v);
async function sha256hex(s){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s)); return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');}
const fmtChance=(w,t)=>{if(w==null||!Number(t)) return '—'; const p=Number(w)/Number(t)*100; return p>=10?p.toFixed(1)+'%':p>=0.01?p.toFixed(2)+'%':'<0.01%';};
const fmtLeft=ms=>{const s=Math.max(0,Math.floor(ms/1000)); return [Math.floor(s/3600),Math.floor(s/60)%60,s%60].map(x=>String(x).padStart(2,'0')).join(':');};
// пересчёт розыгрыша в браузере, та же формула, что draw.py:
// list_hash = sha256(канонический JSON [[адрес, вес], ...] по адресу, без пробелов);
// r = int(sha256(blockhash + list_hash), 16) mod сумма_весов; победитель — первый адрес, у которого накопленная сумма > r
async function verifyDraw(day,base='/api/draw'){   // base: /api/draw (Solana) или /api/rewards (Robinhood)
  const get=async u=>{const r=await fetch(u); if(!r.ok) throw new Error('http '+r.status); return parseBig(await r.text());};
  const [p,v]=await Promise.all([get(`${base}/${day}/participants`),get(`${base}/${day}/verify`)]);
  const rows=p.participants.map(x=>[String(x.address),BigInt(x.weight)]).sort((a,b)=>a[0]<b[0]?-1:a[0]>b[0]?1:0);
  const canon='['+rows.map(([a,w])=>'['+JSON.stringify(a)+','+w.toString()+']').join(',')+']';
  const lh=await sha256hex(canon), total=rows.reduce((s,[,w])=>s+w,0n);
  let r=null, winner=null;
  if(rows.length&&total>0n){
    r=BigInt('0x'+await sha256hex((v.inputs.blockhash||'')+lh))%total;
    let acc=0n; for(const [a,w] of rows){acc+=w; if(acc>r){winner=a; break;}}
  }
  const str=x=>x==null?null:x.toString(), bad=[];
  if(lh!==v.inputs.list_hash||lh!==p.list_hash) bad.push('list_hash');
  if(str(total)!==str(v.inputs.total_weight)) bad.push('total weight');
  if(str(r)!==str(v.stored.r)) bad.push('r');
  if(winner!==v.stored.winner) bad.push('winner');
  return {ok:!bad.length,bad,lh,r,winner,n:rows.length};
}

class Component extends DCLogic {'''
rep("class Component extends DCLogic {", DRAW_JS)

rep("  blank(ca){return {", r"""  loadDraw(){   // статус розыгрыша; выключен — секции нет и больше никаких запросов
    const j=u=>fetch(u).then(r=>r.ok?r.json():null).catch(()=>null);
    this._drawAt=Date.now();
    j('/api/draw/status').then(st=>{
      if(!st||!st.enabled){this.setState({draw:null}); clearInterval(this._drawT); this._drawT=null; return;}
      j('/api/draw/history?limit=7').then(h=>{
        this.setState({draw:{st,hist:(h&&h.draws)||[]},drawNow:Date.now()});
        if(!this._drawT) this._drawT=setInterval(()=>this.drawTick(),1000);
      });
    });
  }
  drawTick(){   // таймер раз в секунду; после розыгрыша и раз в 5 минут — свежие данные
    const d=this.state.draw, now=Date.now(); if(!d) return;
    const since=now-this._drawAt, past=Date.parse(d.st.next_draw)<now-60000;
    if((past&&since>60000)||since>300000) this.loadDraw();
    this.setState({drawNow:now});
  }
  runVerify(){
    const L=this.state.draw&&this.state.draw.hist[0]; if(!L) return;
    const day=L.day, set=v=>this.setState({drawVerify:{day,...v}});
    if(!(window.crypto&&crypto.subtle)){set({s:'err',text:'verification needs a secure (https) page'}); return;}
    set({s:'busy',text:'recomputing…'});
    verifyDraw(day).then(v=>set({s:v.ok?'ok':'bad',text:v.ok?'verified: matches':'mismatch: '+v.bad.join(', '),
        detail:`${v.n} participants · list_hash ${v.lh.slice(0,12)}… · winner ${v.winner?drawShort(v.winner):'none'}`}))
      .catch(e=>set({s:'err',text:'could not verify: '+e.message}));
  }
  drawVals(){
    const d=this.state.draw;
    if(!d) return {drawOn:false};
    const st=d.st, now=this.state.drawNow||Date.now(), next=Date.parse(st.next_draw), left=next-now;
    const L=d.hist[0]||null, won=!!(L&&L.winner), paid=!!(L&&L.payout_tx);
    const vf=L&&this.state.drawVerify&&this.state.drawVerify.day===L.day?this.state.drawVerify:null;
    const iso=new Date(next).toISOString(), prize=h=>`${h.prize_amount} ${h.prize_currency}`;
    const hist=d.hist.map(h=>({day:h.day, hasWinner:!!h.winner, noWinner:!h.winner,
      winner:h.winner?drawShort(h.winner):'no eligible holders', winnerUrl:h.winner?SOL_ACC(h.winner):'#',
      chance:h.winner?fmtChance(h.winner_weight,h.total_weight):'—',
      prize:h.payout_tx?prize(h):h.winner?'payout pending':'carries over', prizeColor:h.payout_tx?'#dfe5e8':'#5f6b72',
      hasTx:!!h.payout_tx, noTx:!h.payout_tx, txUrl:h.payout_tx?SOL_TX(h.payout_tx):'#', txShort:h.payout_tx?drawShort(h.payout_tx):'—'}));
    const vc={ok:G,bad:RD}[vf&&vf.s]||'#8a959c';
    return {drawOn:true,
      drawFirst:!L, drawWon:won, drawNone:!!L&&!won, dDayLabel:L?'draw of '+L.day:'no draws yet',
      dWinner:won?drawShort(L.winner):'', dWinnerFull:won?L.winner:'', dWinnerUrl:won?SOL_ACC(L.winner):'#',
      dChance:won?fmtChance(L.winner_weight,L.total_weight):'—',
      dPaid:won&&paid, dPending:won&&!paid, dPrize:paid?prize(L):'', dTxUrl:paid?SOL_TX(L.payout_tx):'#', dTxShort:paid?drawShort(L.payout_tx):'',
      dCountdown:left>0?fmtLeft(left):'drawing…', dNextUtc:`${iso.slice(11,16)} UTC · ${iso.slice(0,10)}`,
      dParticipants:Number(st.participants_so_far||0).toLocaleString('en-US'), dSnaps:`${st.snapshots_today||0} / 24`,
      dMinUsd:'$'+(st.min_usd!=null?st.min_usd:10),
      dHist:hist, dHistAny:hist.length>0, dHistEmpty:!hist.length,
      dCanVerify:!!L, onVerify:()=>this.runVerify(), dVerifyBusy:!!vf&&vf.s==='busy',
      dVerifyShow:!!vf, dVerifyText:vf?vf.text:'', dVerifyColor:vc, dVerifyDetail:vf&&vf.detail?vf.detail:'',
      dRawP:L?`/api/draw/${L.day}/participants`:'#', dRawV:L?`/api/draw/${L.day}/verify`:'#'};
  }
  blank(ca){return {""")
rep("  state={view:'landing',input:'',inputError:'',inputNotice:'',net:'robinhood',solanaOn:null,copied:false,v:0};",
    "  state={view:'landing',input:'',inputError:'',inputNotice:'',net:'robinhood',solanaOn:null,copied:false,v:0,draw:null,drawNow:0,drawVerify:null};")
rep("    this.cfg=fetch('/api/config').then(r=>r.json()).then(d=>this.setState({solanaOn:!!d.solana})).catch(()=>{});\n",
    "    this.cfg=fetch('/api/config').then(r=>r.json()).then(d=>this.setState({solanaOn:!!d.solana})).catch(()=>{});\n"
    "    this.loadDraw();\n")
rep("this.stopFeed(); this.stopLanding(); clearInterval(this.timer);",
    "this.stopFeed(); this.stopLanding(); clearInterval(this.timer); clearInterval(this._drawT);")
rep("      ...this.netVals(),\n", "      ...this.netVals(),\n      ...this.drawVals(),\n")

# номера секций: с розыгрышем how/roadmap сдвигаются на один
rep('''color:#eef1f3">how it works</h2>
          <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#5f6b72">03</span>''',
    '''color:#eef1f3">how it works</h2>
          <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#5f6b72">{{numHow}}</span>''')
mr = re.search(r'''(color:#eef1f3">roadmap</h2>\s*<span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:#5f6b72">)04(</span>)''', t)
if not mr:
    sys.exit("design.html: не найден номер секции roadmap (04) — сборка остановлена")
t = t[:mr.start()] + mr.group(1) + "{{numRoadmap}}" + mr.group(2) + t[mr.end():]

CARD = ('display:flex;flex-direction:column;gap:18px;padding:28px 26px;border:1px solid #141b20;border-radius:14px;'
        'background:linear-gradient(180deg,#0c1114,#090c0f);box-shadow:inset 0 1px 0 rgba(255,255,255,0.03);box-sizing:border-box;min-width:0')
LABEL = f'{MONO};font-size:11px;letter-spacing:0.08em;text-transform:uppercase;color:#5f6b72'
LINK = f'{MONO};color:#9fd9ff'
stat = lambda label, body: (f'<div style="display:flex;flex-direction:column;gap:6px;min-width:0"><span style="{LABEL}">{label}</span>{body}</div>')
STEPS = [
    "Hold the token. Your chance is your average balance over the day: 1 token = 1 ticket.",
    "Wallets holding under {{dMinUsd}} on average are not eligible. Pools and programs never are.",
    "The winner is picked from a Solana block hash that nobody knows in advance.",
    "Prizes are sent manually from a public wallet. Every payout links to its transaction.",
]
steps = "".join(
    f'<div style="display:flex;gap:14px;align-items:baseline"><span style="{MONO};font-size:12px;color:#00c805;flex-shrink:0">0{i + 1}</span>'
    f'<span style="font-size:15px;line-height:1.55;color:#aab4ba;text-wrap:pretty">{s}</span></div>' for i, s in enumerate(STEPS))
HIST_COLS = "grid-template-columns:110px minmax(0,1fr) 90px minmax(0,1fr) minmax(0,1fr);gap:16px"
DRAW_SECTION = f'''      <sc-if value="{{{{drawOn}}}}" hint-placeholder-val="{{{{false}}}}"><section id="draw" class="cs-wrap" style="max-width:1280px;margin:0 auto;padding:40px 32px 120px;box-sizing:border-box">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:24px;border-top:1px solid #12181c;padding-top:22px;margin-bottom:44px">
          <h2 data-grip="1" style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.03em;color:#eef1f3">daily draw</h2>
          <span style="{MONO};font-size:12px;color:#5f6b72">{{{{numDraw}}}}</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:24px;align-items:stretch">
          <div data-grip="1" style="flex:1 1 560px;{CARD}">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;{MONO};font-size:13px"><span style="color:#00c805">today's winner</span><span style="color:#5f6b72">{{{{dDayLabel}}}}</span></div>
            <sc-if value="{{{{drawFirst}}}}" hint-placeholder-val="{{{{false}}}}"><h3 style="margin:0;font-size:28px;font-weight:500;letter-spacing:-0.02em;color:#eef1f3">first draw in <span style="{MONO};color:#00c805">{{{{dCountdown}}}}</span></h3>
              <p style="margin:0;font-size:15px;line-height:1.6;color:#8a959c">The first winner is drawn from today's hourly snapshots at {{{{dNextUtc}}}}.</p></sc-if>
            <sc-if value="{{{{drawNone}}}}" hint-placeholder-val="{{{{false}}}}"><h3 style="margin:0;font-size:28px;font-weight:500;letter-spacing:-0.02em;color:#eef1f3">no eligible holders</h3>
              <p style="margin:0;font-size:15px;line-height:1.6;color:#8a959c">No wallet held enough on average that day. The prize carries over to the next draw.</p></sc-if>
            <sc-if value="{{{{drawWon}}}}" hint-placeholder-val="{{{{false}}}}"><a href="{{{{dWinnerUrl}}}}" title="{{{{dWinnerFull}}}}" target="_blank" rel="noopener" data-draw-winner="1" style="{MONO};font-size:clamp(28px,4vw,40px);letter-spacing:-0.02em;color:#eef1f3" style-hover="color:#9fd9ff">{{{{dWinner}}}} ↗</a>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:18px;border-top:1px solid #141b20;padding-top:18px">
                {stat("chance", f'<span data-draw-chance="1" style="{MONO};font-size:18px;color:#eef1f3">{{{{dChance}}}}</span>')}
                {stat("prize", f'<sc-if value="{{{{dPaid}}}}" hint-placeholder-val="{{{{false}}}}"><span style="{MONO};font-size:18px;color:#00c805">{{{{dPrize}}}}</span></sc-if><sc-if value="{{{{dPending}}}}" hint-placeholder-val="{{{{false}}}}"><span style="{MONO};font-size:18px;color:#8a959c">payout pending</span></sc-if>')}
                {stat("payout", f'<sc-if value="{{{{dPaid}}}}" hint-placeholder-val="{{{{false}}}}"><a href="{{{{dTxUrl}}}}" target="_blank" rel="noopener" style="{LINK};font-size:15px" style-hover="color:#ffffff">{{{{dTxShort}}}} ↗</a></sc-if><sc-if value="{{{{dPending}}}}" hint-placeholder-val="{{{{false}}}}"><span style="{MONO};font-size:15px;color:#5f6b72">—</span></sc-if>')}
              </div></sc-if>
          </div>
          <div data-grip="1" style="flex:1 1 320px;{CARD}">
            <span style="{MONO};font-size:13px;color:#00c805">next draw</span>
            <span data-draw-timer="1" style="{MONO};font-size:clamp(34px,4vw,44px);letter-spacing:-0.02em;color:#eef1f3">{{{{dCountdown}}}}</span>
            <span style="{MONO};font-size:12px;color:#5f6b72">{{{{dNextUtc}}}}</span>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;border-top:1px solid #141b20;padding-top:18px;margin-top:auto">
              {stat("participants today", f'<span style="{MONO};font-size:18px;color:#eef1f3">{{{{dParticipants}}}}</span>')}
              {stat("snapshots today", f'<span style="{MONO};font-size:18px;color:#eef1f3">{{{{dSnaps}}}}</span>')}
            </div>
          </div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:24px;align-items:stretch;margin-top:24px">
          <div data-grip="1" style="flex:1 1 560px;{CARD}">
            <span style="{MONO};font-size:13px;color:#00c805">how it's picked</span>
            {steps}
          </div>
          <div data-grip="1" style="flex:1 1 320px;{CARD}">
            <span style="{MONO};font-size:13px;color:#00c805">verify</span>
            <p style="margin:0;font-size:15px;line-height:1.6;color:#8a959c">Recompute the latest draw in your browser from the raw data: the list hash, the random number and the winner.</p>
            <sc-if value="{{{{dCanVerify}}}}" hint-placeholder-val="{{{{false}}}}"><div style="display:flex;flex-wrap:wrap;align-items:center;gap:12px 18px">
              <button sc-camel-on-click="{{{{onVerify}}}}" data-draw-verify="1" style="{MONO};font-size:13px;color:#04140a;background:#00c805;border:0;padding:10px 18px;border-radius:8px;cursor:pointer" style-hover="background:#19dd1f">Verify</button>
              <a href="{{{{dRawP}}}}" target="_blank" rel="noopener" style="{LINK};font-size:12px" style-hover="color:#ffffff">participants.json ↗</a>
              <a href="{{{{dRawV}}}}" target="_blank" rel="noopener" style="{LINK};font-size:12px" style-hover="color:#ffffff">verify.json ↗</a>
            </div></sc-if>
            <sc-if value="{{{{drawFirst}}}}" hint-placeholder-val="{{{{false}}}}"><span style="{MONO};font-size:12px;color:#5f6b72">available after the first draw</span></sc-if>
            <sc-if value="{{{{dVerifyShow}}}}" hint-placeholder-val="{{{{false}}}}"><div style="display:flex;flex-direction:column;gap:6px">
              <span data-draw-verdict="1" style="{MONO};font-size:14px;color:{{{{dVerifyColor}}}}">{{{{dVerifyText}}}}</span>
              <span style="{MONO};font-size:11.5px;line-height:1.6;color:#5f6b72;overflow-wrap:anywhere">{{{{dVerifyDetail}}}}</span>
            </div></sc-if>
          </div>
        </div>
        <div data-grip="1" style="margin-top:24px;border:1px solid #141b20;border-radius:14px;background:#090c0f;padding:8px 14px;box-sizing:border-box">
          <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 12px 8px;{MONO};font-size:13px"><span style="color:#00c805">history</span><span style="color:#5f6b72">last 7 draws</span></div>
          <div class="cs-draw-head" style="display:grid;{HIST_COLS};padding:10px 12px;border-bottom:1px solid #141b20;{LABEL}"><span>day</span><span>winner</span><span>chance</span><span>prize</span><span>payout tx</span></div>
          <sc-if value="{{{{dHistEmpty}}}}" hint-placeholder-val="{{{{false}}}}"><div style="padding:16px 12px;{MONO};font-size:13px;color:#5f6b72">no draws yet</div></sc-if>
          <sc-for list="{{{{dHist}}}}" as="h" hint-placeholder-count="3"><div class="cs-draw-row" style="display:grid;{HIST_COLS};align-items:center;padding:12px;border-bottom:1px solid #12181c;{MONO};font-size:13px">
            <span style="color:#8a959c">{{{{h.day}}}}</span>
            <span style="min-width:0"><sc-if value="{{{{h.hasWinner}}}}" hint-placeholder-val="{{{{true}}}}"><a href="{{{{h.winnerUrl}}}}" target="_blank" rel="noopener" style="color:#dfe5e8" style-hover="color:#9fd9ff">{{{{h.winner}}}} ↗</a></sc-if><sc-if value="{{{{h.noWinner}}}}" hint-placeholder-val="{{{{false}}}}"><span style="color:#5f6b72">{{{{h.winner}}}}</span></sc-if></span>
            <span style="color:#dfe5e8">{{{{h.chance}}}}</span>
            <span style="color:{{{{h.prizeColor}}}}">{{{{h.prize}}}}</span>
            <span style="min-width:0"><sc-if value="{{{{h.hasTx}}}}" hint-placeholder-val="{{{{false}}}}"><a href="{{{{h.txUrl}}}}" target="_blank" rel="noopener" style="color:#9fd9ff" style-hover="color:#ffffff">{{{{h.txShort}}}} ↗</a></sc-if><sc-if value="{{{{h.noTx}}}}" hint-placeholder-val="{{{{true}}}}"><span style="color:#5f6b72">—</span></sc-if></span>
          </div></sc-for>
        </div>
      </section></sc-if>

'''
rep('      <section id="how" ', DRAW_SECTION + '      <section id="how" ')
# история на телефоне: день во всю строку, под ним победитель · шанс, приз · транзакция
rep("@media (max-width:640px){\n", "@media (max-width:640px){\n"
    "  .cs-draw-head{display:none!important}\n"
    "  .cs-draw-row{grid-template-columns:minmax(0,1fr) auto!important;gap:6px 12px!important}\n"
    "  .cs-draw-row>:first-child{grid-column:1/-1}\n")

# ---------------------------------------------------------------------------
# Token Burn & Holder Rewards: секция сразу после «the token», только если GET /api/rewards/status
# вернул enabled: true. Две карточки одинакового размера (на телефоне друг под другом):
#   Burn — таймер до next_burn, последнее сжигание, всего сожжено (% сапплая), адрес сжиганий;
#   Holder Rewards — таймер до next_draw, блок доверия с Verify (пересчёт по /api/rewards/<day>/*),
#   последний победитель, шанс и статус выплаты.
# Таймер в нуле: «Waiting for burn transaction…» до новой транзакции сжигания (сжигание не раньше чем
# за 30 минут до планового времени засчитывается ему), «Picking the winner…» до розыгрыша этих суток.
# Новое сжигание за время на странице — вспышка «Tokens burned: N», новый розыгрыш — появление победителя.
# Ссылки — эксплорер Robinhood Chain (Blockscout). Суммы — BigInt из базовых единиц и decimals.
# ---------------------------------------------------------------------------
RH_EXPLORER = "https://robinhoodchain.blockscout.com"
RW_JS = r'''// ---- token burn & holder rewards (/api/rewards/*) ----
const RH_ADDR=a=>'__RH__/address/'+a, RH_TX=t=>'__RH__/tx/'+t;
const rwShort=a=>a?a.slice(0,6)+'…'+a.slice(-4):'—';
const RW_BIG=['amount','total_supply','minted','dead_balance','weight','total_weight','payout_amount'];   // базовые единицы: BigInt
const parseRw=txt=>JSON.parse(txt,(k,v,ctx)=>RW_BIG.includes(k)&&typeof v==='number'?BigInt(ctx&&ctx.source!=null?ctx.source:v):v);
// базовые единицы -> токены с разделителями; меньше 1000 — до двух знаков после точки
const fmtUnits=(raw,dec)=>{
  if(raw==null) return '—';
  const base=10n**BigInt(dec??18), b=BigInt(raw), i=b/base, f=(b%base)*100n/base;
  const s=i.toLocaleString('en-US'); if(i>=1000n||f===0n) return s;
  return s+'.'+f.toString().padStart(2,'0').replace(/0$/,'');
};
const fmtPctOf=(a,b)=>{if(a==null||!b) return null; const p=Number(BigInt(a)*1000000n/BigInt(b))/10000; return p>=0.01?p.toFixed(2)+'%':p>0?'<0.01%':'0%';};
const fmtUtcTs=ms=>{const s=new Date(ms).toISOString(); return s.slice(0,10)+' '+s.slice(11,16)+' UTC';};
const RW_GRACE=1800000;   // сжигание за 30 минут до планового времени засчитывается этому времени
const rwTiles=ms=>fmtLeft(ms).split('').map(c=>({c,digit:c!==':',colon:c===':'}));   // таймер плитками: цифры в плитках, двоеточия без
const burnKey=st=>st.last_burn?st.last_burn.tx+':'+st.last_burn.log_index:null;
const burnedFor=(st,target)=>!!st.last_burn&&st.last_burn.time*1000>=target-RW_GRACE;

class Component extends DCLogic {'''.replace("__RH__", RH_EXPLORER)
rep("class Component extends DCLogic {", RW_JS)

rep("  blank(ca){return {", r"""  loadRewards(){   // статус Rewards & Burns; выключено — секции нет и больше никаких запросов
    this._rwAt=Date.now();
    fetch('/api/rewards/status').then(r=>r.ok?r.text():null).then(txt=>{
      const st=txt?parseRw(txt):null;
      if(!st||!st.enabled){this.setState({rw:null}); clearInterval(this._rwT); this._rwT=null; return;}
      this.rwUpdate(st);
      if(!this._rwT) this._rwT=setInterval(()=>this.rwTick(),1000);
    }).catch(()=>{});
  }
  rwUpdate(st){
    const now=Date.now(), prev=this.state.rw, s={rw:st,rwNow:now};
    if(!prev){   // первая загрузка: цели таймеров; только что прошедшее время (< 1 ч) без результата — ждём его
      const nb=Date.parse(st.next_burn), slot=(Date.parse(st.next_burns[1])-nb)||43200000;
      this._rwBurn={target:now-(nb-slot)<3600000&&!burnedFor(st,nb-slot)?nb-slot:nb, slot, seen:burnKey(st)};
      const nd=Date.parse(st.next_draw), pd=nd-86400000, pday=new Date(pd).toISOString().slice(0,10);
      const late=now-pd<3600000&&st.last_draw&&st.last_draw.day<pday;
      this._rwDraw={target:late?pd:nd, day:late?pday:st.next_draw_day, seen:st.last_draw?st.last_draw.day:null};
    }else{
      const b=this._rwBurn, d=this._rwDraw, key=burnKey(st), day=st.last_draw?st.last_draw.day:null;
      if(key&&key!==b.seen){b.seen=key; s.rwBurnFlash={text:fmtUnits(st.last_burn.amount,st.decimals),until:now+6000};}
      if(day&&day!==d.seen){d.seen=day; s.rwReveal=now+4000;}
    }
    this.setState(s); this.rwTick(st);
  }
  rwTick(fresh){   // таймеры раз в секунду; свежий статус раз в минуту, в нуле таймера — каждые 10 секунд
    const st=fresh||this.state.rw, now=Date.now(); if(!st) return;
    const b=this._rwBurn, d=this._rwDraw;
    while(now>=b.target&&burnedFor(st,b.target)) b.target+=b.slot;
    if(now>=d.target&&st.last_draw&&st.last_draw.day>=d.day){d.target=Date.parse(st.next_draw); d.day=st.next_draw_day;}
    const busy=now>=b.target||now>=d.target, since=now-this._rwAt;
    if(since>(busy?10000:60000)) this.loadRewards();
    this.setState({rwNow:now});
  }
  copyText(key,text){   // copy с галочкой на 1.5 с
    const fallback=()=>{const t=document.createElement('textarea'); t.value=text; t.style.position='fixed'; t.style.opacity='0'; document.body.appendChild(t); t.select(); try{document.execCommand('copy');}catch(e){} t.remove();};
    try{ if(navigator.clipboard&&window.isSecureContext) navigator.clipboard.writeText(text).catch(fallback); else fallback(); }catch(e){fallback();}
    this.setState({rwCopied:key}); clearTimeout(this._rwCT); this._rwCT=setTimeout(()=>this.setState({rwCopied:''}),1500);
  }
  runRwVerify(){
    const L=this.state.rw&&this.state.rw.last_draw; if(!L) return;
    const day=L.day, set=v=>this.setState({rwVerify:{day,...v}});
    if(!(window.crypto&&crypto.subtle)){set({s:'err',text:'verification needs a secure (https) page'}); return;}
    set({s:'busy',text:'recomputing…'});
    verifyDraw(day,'/api/rewards').then(v=>set({s:v.ok?'ok':'bad',text:v.ok?'verified: matches':'mismatch: '+v.bad.join(', '),
        detail:`draw of ${day} · ${v.n} participants · list_hash ${v.lh.slice(0,12)}… · winner ${v.winner?rwShort(v.winner):'none'}`}))
      .catch(e=>set({s:'err',text:'could not verify: '+e.message}));
  }
  rwVals(){
    const st=this.state.rw;
    if(!st) return {rwOn:false};
    const now=this.state.rwNow||Date.now(), dec=st.decimals, b=this._rwBurn, d=this._rwDraw;
    const waiting=now>=b.target, picking=now>=d.target;
    const lb=st.last_burn, tb=st.total_burned, L=st.last_draw, won=!!(L&&L.winner), paid=!!(L&&L.payout_tx);
    const fl=this.state.rwBurnFlash, flash=!!fl&&now<fl.until, reveal=!!this.state.rwReveal&&now<this.state.rwReveal;
    const vf=L&&this.state.rwVerify&&this.state.rwVerify.day===L.day?this.state.rwVerify:null;
    const cp=this.state.rwCopied, devs=k=>(st.dev_wallets||[]).map((a,i)=>({full:a, short:rwShort(a), url:RH_ADDR(a),
      copy:()=>this.copyText(k+i,a), idle:cp!==k+i, done:cp===k+i})), drawLeft=picking?'Picking the winner…':fmtLeft(d.target-now);
    return {rwOn:true,
      rwBurnCount:!waiting, rwBurnWait:waiting, rwBurnLeft:fmtLeft(b.target-now), rwBurnAt:fmtUtcTs(b.target),
      rwBurnFlash:flash, rwBurnFlashText:flash?fl.text:'',
      rwHasBurn:!!lb, rwNoBurn:!lb, rwLastAmt:lb?fmtUnits(lb.amount,dec):'', rwLastTime:lb?fmtUtcTs(lb.time*1000):'',
      rwLastTxUrl:lb?RH_TX(lb.tx):'#', rwLastTx:lb?rwShort(lb.tx):'',
      rwTotal:tb?fmtUnits(tb.amount,dec):fmtUnits(st.burned_by_dev.amount,dec),
      rwTotalPct:tb&&tb.minted?fmtPctOf(tb.amount,tb.minted)+' of supply':'',
      rwBurnTiles:rwTiles(b.target-now), rwDrawTiles:rwTiles(d.target-now),
      rwDevs:devs('burn'), rwPayDevs:devs('pay'), rwHasDevs:!!(st.dev_wallets&&st.dev_wallets.length),
      rwBurnAddr:st.burn_address, rwBurnAddrUrl:RH_ADDR(st.burn_address),
      copyRwBurn:()=>this.copyText('burn',st.burn_address), rwBurnDone:cp==='burn', rwBurnIdle:cp!=='burn',
      rwDrawCount:!picking, rwDrawPick:picking, rwDrawLeft:fmtLeft(d.target-now), rwDrawAt:fmtUtcTs(d.target),
      rwFirst:!L, rwFirstText:'First draw in '+drawLeft,
      rwWon:won&&!reveal, rwWonNew:won&&reveal, rwNone:!!L&&!won,
      rwWinner:won?rwShort(L.winner):'', rwWinnerFull:won?L.winner:'', rwWinnerUrl:won?RH_ADDR(L.winner):'#',
      copyRwWinner:()=>this.copyText('winner',won?L.winner:''), rwWinDone:cp==='winner', rwWinIdle:cp!=='winner',
      rwChance:won?fmtChance(L.weight,L.total_weight)+' chance':'', rwDayLabel:L?'draw of '+L.day:'',
      rwPaid:won&&paid, rwPending:won&&!paid, rwPrize:paid?fmtUnits(L.payout_amount,dec):'',
      rwTxUrl:paid?RH_TX(L.payout_tx):'#', rwTxShort:paid?rwShort(L.payout_tx):'',
      rwCanVerify:!!L, onRwVerify:()=>this.runRwVerify(), rwVerifyShow:!!vf, rwVerifyText:vf?vf.text:'',
      rwVerifyColor:{ok:G,bad:RD}[vf&&vf.s]||'#8a959c', rwVerifyDetail:vf&&vf.detail?vf.detail:''};
  }
  secVals(){   // номера секций: the token 02, дальше — только показанные
    let n=2; const next=()=>String(++n).padStart(2,'0');
    const out={};
    if(this.state.rw) out.numRewards=next();
    if(this.state.draw) out.numDraw=next();
    out.numHow=next(); out.numRoadmap=next();
    return out;
  }
  blank(ca){return {""")
rep("  state={view:'landing',input:'',inputError:'',inputNotice:'',net:'robinhood',solanaOn:null,copied:false,v:0,draw:null,drawNow:0,drawVerify:null};",
    "  state={view:'landing',input:'',inputError:'',inputNotice:'',net:'robinhood',solanaOn:null,copied:false,v:0,draw:null,drawNow:0,drawVerify:null,"
    "rw:null,rwNow:0,rwVerify:null,rwBurnFlash:null,rwReveal:0,rwCopied:''};")
rep("    this.loadDraw();\n", "    this.loadDraw();\n    this.loadRewards();\n")
rep("this.stopLanding(); clearInterval(this.timer); clearInterval(this._drawT);",
    "this.stopLanding(); clearInterval(this.timer); clearInterval(this._drawT); clearInterval(this._rwT);")
rep("      ...this.drawVals(),\n", "      ...this.drawVals(),\n      ...this.rwVals(),\n      ...this.secVals(),\n")

def rw_copy(key, idle, done, label):
    return (f'<button sc-camel-on-click="{{{{copyRw{key}}}}}" title="{label}" aria-label="{label}" '
            f'style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;padding:0;'
            f'border:1px solid #1c252b;border-radius:7px;background:#0b1013;color:#8a959c;cursor:pointer;flex-shrink:0" '
            f'style-hover="color:#ffffff;border-color:#2c353b">'
            f'<sc-if value="{{{{{idle}}}}}" hint-placeholder-val="{{{{true}}}}">{ICON_COPY}</sc-if>'
            f'<sc-if value="{{{{{done}}}}}" hint-placeholder-val="{{{{false}}}}">{ICON_CHECK}</sc-if></button>')

def sif(cond, body, ph="false"):
    return f'<sc-if value="{{{{{cond}}}}}" hint-placeholder-val="{{{{{ph}}}}}">{body}</sc-if>'

TIMER = f'{MONO};font-size:clamp(32px,3.6vw,42px);line-height:1;display:flex;align-items:center;gap:0.1em;font-variant-numeric:tabular-nums'
TILE = ('display:inline-flex;align-items:center;justify-content:center;width:1em;height:1.32em;border-radius:0.2em;'
        'background:rgba(0,200,5,0.16);border:1px solid rgba(0,200,5,0.42);color:#d9ffd4;box-sizing:border-box;'
        'box-shadow:inset 0 1px 0 rgba(255,255,255,0.06);text-shadow:0 0 12px rgba(0,200,5,0.35)')
COLON = 'display:inline-block;width:0.5em;text-align:center;color:#00c805;margin-top:-0.08em'
tiles = lambda key: (f'<sc-for list="{{{{{key}}}}}" as="c" hint-placeholder-count="8">'
                     f'<sc-if value="{{{{c.digit}}}}" hint-placeholder-val="{{{{true}}}}"><span class="cs-rw-tile" style="{TILE}">{{{{c.c}}}}</span></sc-if>'
                     f'<sc-if value="{{{{c.colon}}}}" hint-placeholder-val="{{{{false}}}}"><span style="{COLON}">:</span></sc-if></sc-for>')
BUSY = f'{MONO};font-size:clamp(20px,2.4vw,26px);letter-spacing:-0.01em;color:#f5a623;line-height:1.3'
TEXT = 'margin:0;font-size:15px;line-height:1.6;color:#8a959c;text-wrap:pretty'
BURN_TEXT = ("Every 12 hours the developer burns tokens from his personal supply. Burning makes tokens more valuable "
             "because the supply gets smaller and smaller day by day.")
DRAW_TEXT = ("Every 24 hours one holder is picked at random and receives 10% of the token's fees, paid in tokens. "
             "Every holder takes part, even the smallest. Your chance = your average balance over the day "
             "(1 token = 1 ticket), so buying right before the draw doesn't help.")
TRUST_TEXT = ("The winner is picked from the hash of a Robinhood Chain block produced after the holder list is locked. "
              "Nobody, including the developer, can know or change it in advance.")
winner_line = lambda cls: (
    f'<div class="{cls}" style="display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;{MONO};font-size:15px">'
    f'<span style="color:#8a959c">Last winner:</span>'
    f'<a href="{{{{rwWinnerUrl}}}}" title="{{{{rwWinnerFull}}}}" target="_blank" rel="noopener" data-rw-winner="1" style="color:#eef1f3" style-hover="color:#9fd9ff">{{{{rwWinner}}}} ↗</a>'
    + rw_copy("Winner", "rwWinIdle", "rwWinDone", "copy winner address") + '</div>')
dev_rows = lambda key, label, attr: sif("rwHasDevs", f'<span style="{LABEL}">{label}</span><sc-for list="{{{{{key}}}}}" as="w" hint-placeholder-count="1"><div {attr}="1" style="display:flex;align-items:center;gap:10px;min-width:0"><a href="{{{{w.url}}}}" title="{{{{w.full}}}}" target="_blank" rel="noopener" style="{MONO};font-size:13px;color:#dfe5e8;overflow-wrap:anywhere;min-width:0" style-hover="color:#9fd9ff"><span class="cs-rw-full">{{{{w.full}}}}</span><span class="cs-rw-short">{{{{w.short}}}}</span> ↗</a><button sc-camel-on-click="{{{{w.copy}}}}" title="copy developer wallet" aria-label="copy developer wallet" style="display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;padding:0;border:1px solid #1c252b;border-radius:7px;background:#0b1013;color:#8a959c;cursor:pointer;flex-shrink:0" style-hover="color:#ffffff;border-color:#2c353b"><sc-if value="{{{{w.idle}}}}" hint-placeholder-val="{{{{true}}}}">{ICON_COPY}</sc-if><sc-if value="{{{{w.done}}}}" hint-placeholder-val="{{{{false}}}}">{ICON_CHECK}</sc-if></button></div></sc-for><span style="height:6px"></span>')
REWARDS_SECTION = f'''      <sc-if value="{{{{rwOn}}}}" hint-placeholder-val="{{{{false}}}}"><section id="rewards" class="cs-wrap" style="max-width:1280px;margin:0 auto;padding:40px 32px 120px;box-sizing:border-box">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:24px;border-top:1px solid #12181c;padding-top:22px;margin-bottom:44px">
          <h2 data-grip="1" style="margin:0;font-size:34px;font-weight:500;letter-spacing:-0.03em;color:#eef1f3">token burn &amp; holder rewards</h2>
          <span style="{MONO};font-size:12px;color:#5f6b72">{{{{numRewards}}}}</span>
        </div>
        <div class="cs-rw-grid" style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;align-items:stretch">
          <div data-grip="1" data-rw-burn="1" style="{CARD}">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;{MONO};font-size:13px"><span style="color:#00c805">burn</span><span style="color:#5f6b72">every 12 hours</span></div>
            <div style="display:flex;flex-direction:column;gap:6px;min-height:76px">
              <span style="{LABEL}">next burn</span>
              {sif("rwBurnCount", f'<div data-rw-burn-timer="1" class="cs-rw-timer" role="timer" aria-label="{{{{rwBurnLeft}}}}" style="{TIMER}">{tiles("rwBurnTiles")}</div>', "true")}
              {sif("rwBurnWait", f'<span data-rw-burn-wait="1" class="cs-rw-pulse" style="{BUSY}">Waiting for burn transaction…</span>')}
              <span style="{MONO};font-size:12px;color:#5f6b72">{{{{rwBurnAt}}}}</span>
            </div>
            {sif("rwBurnFlash", f'<div class="cs-rw-flash" data-rw-flash="1" style="{MONO};font-size:15px;color:#04140a;background:#00c805;padding:10px 14px;border-radius:8px;overflow-wrap:anywhere">Tokens burned: {{{{rwBurnFlashText}}}}</div>')}
            <p style="{TEXT}">{BURN_TEXT}</p>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:18px;border-top:1px solid #141b20;padding-top:18px">
              {stat("last burn", sif("rwHasBurn", f'<span data-rw-last-burn="1" style="{MONO};font-size:18px;color:#eef1f3">{{{{rwLastAmt}}}} tokens</span><span style="{MONO};font-size:12px;color:#5f6b72">{{{{rwLastTime}}}}</span><a href="{{{{rwLastTxUrl}}}}" target="_blank" rel="noopener" style="{LINK};font-size:13px" style-hover="color:#ffffff">{{{{rwLastTx}}}} ↗</a>') + sif("rwNoBurn", f'<span style="{MONO};font-size:15px;color:#5f6b72">no burns yet</span>'))}
              {stat("total burned", f'<span data-rw-total="1" style="{MONO};font-size:18px;color:#eef1f3">{{{{rwTotal}}}} tokens</span><span style="{MONO};font-size:12px;color:#00c805">{{{{rwTotalPct}}}}</span>')}
            </div>
            <div style="display:flex;flex-direction:column;gap:8px;margin-top:auto;border-top:1px solid #141b20;padding-top:18px">
              {dev_rows("rwDevs", "burns from", "data-rw-dev")}
              <span style="{LABEL}">burn address</span>
              <div style="display:flex;align-items:center;gap:10px;min-width:0">
                <a href="{{{{rwBurnAddrUrl}}}}" target="_blank" rel="noopener" style="{MONO};font-size:13px;color:#dfe5e8;overflow-wrap:anywhere;min-width:0" style-hover="color:#9fd9ff">{{{{rwBurnAddr}}}} ↗</a>
                {rw_copy("Burn", "rwBurnIdle", "rwBurnDone", "copy burn address")}
              </div>
            </div>
          </div>
          <div data-grip="1" data-rw-draw="1" style="{CARD}">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;{MONO};font-size:13px"><span style="color:#00c805">holder rewards</span><span style="color:#5f6b72">every 24 hours</span></div>
            <div style="display:flex;flex-direction:column;gap:6px;min-height:76px">
              <span style="{LABEL}">next draw</span>
              {sif("rwDrawCount", f'<div data-rw-draw-timer="1" class="cs-rw-timer" role="timer" aria-label="{{{{rwDrawLeft}}}}" style="{TIMER}">{tiles("rwDrawTiles")}</div>', "true")}
              {sif("rwDrawPick", f'<span data-rw-draw-pick="1" class="cs-rw-pulse" style="{BUSY}">Picking the winner…</span>')}
              <span style="{MONO};font-size:12px;color:#5f6b72">{{{{rwDrawAt}}}}</span>
            </div>
            <p style="{TEXT}">{DRAW_TEXT}</p>
            <div style="display:flex;flex-direction:column;gap:14px;padding:16px 18px;border:1px solid #1c252b;border-radius:10px;background:#0a0e11">
              <p style="margin:0;font-size:14px;line-height:1.6;color:#aab4ba;text-wrap:pretty">{TRUST_TEXT}</p>
              {sif("rwCanVerify", f'<div style="display:flex;flex-wrap:wrap;align-items:center;gap:12px 18px"><button sc-camel-on-click="{{{{onRwVerify}}}}" data-rw-verify="1" style="{MONO};font-size:13px;color:#04140a;background:#00c805;border:0;padding:10px 18px;border-radius:8px;cursor:pointer" style-hover="background:#19dd1f">Verify</button><span style="{MONO};font-size:12px;color:#5f6b72">{{{{rwDayLabel}}}}</span></div>')}
              {sif("rwFirst", f'<span style="{MONO};font-size:12px;color:#5f6b72">Verify is available after the first draw</span>')}
              {sif("rwVerifyShow", f'<div style="display:flex;flex-direction:column;gap:6px"><span data-rw-verdict="1" style="{MONO};font-size:14px;color:{{{{rwVerifyColor}}}}">{{{{rwVerifyText}}}}</span><span style="{MONO};font-size:11.5px;line-height:1.6;color:#5f6b72;overflow-wrap:anywhere">{{{{rwVerifyDetail}}}}</span></div>')}
            </div>
            <div data-rw-last="1" style="display:flex;flex-direction:column;gap:8px;margin-top:auto;border-top:1px solid #141b20;padding-top:18px">
              {dev_rows("rwPayDevs", "rewards paid from", "data-rw-paydev")}
              {sif("rwFirst", f'<span data-rw-first="1" style="{MONO};font-size:15px;color:#8a959c">{{{{rwFirstText}}}}</span>')}
              {sif("rwNone", f'<span style="{MONO};font-size:15px;color:#8a959c">Last draw: no eligible holders</span>')}
              {sif("rwWon", winner_line("cs-rw-win"))}
              {sif("rwWonNew", winner_line("cs-rw-win cs-rw-reveal"))}
              {sif("rwWon", f'<span style="{MONO};font-size:12px;color:#5f6b72">{{{{rwChance}}}} · {{{{rwDayLabel}}}}</span>')}
              {sif("rwWonNew", f'<span style="{MONO};font-size:12px;color:#5f6b72">{{{{rwChance}}}} · {{{{rwDayLabel}}}}</span>')}
              {sif("rwPaid", f'<span data-rw-payout="1" style="{MONO};font-size:13px;color:#00c805;overflow-wrap:anywhere">reward sent: {{{{rwPrize}}}} tokens · <a href="{{{{rwTxUrl}}}}" target="_blank" rel="noopener" style="color:#9fd9ff" style-hover="color:#ffffff">{{{{rwTxShort}}}} ↗</a></span>')}
              {sif("rwPending", f'<span data-rw-payout="1" style="{MONO};font-size:13px;color:#8a959c">payout pending</span>')}
            </div>
          </div>
        </div>
      </section></sc-if>

'''
rep('      <sc-if value="{{drawOn}}" hint-placeholder-val="{{false}}"><section id="draw"',
    REWARDS_SECTION + '      <sc-if value="{{drawOn}}" hint-placeholder-val="{{false}}"><section id="draw"')
# анимации и телефон: карточки друг под другом
rep("@media (max-width:640px){\n",
    "@keyframes cs-rw-pulse{0%,100%{opacity:1}50%{opacity:0.45}}\n"
    "@keyframes cs-rw-flash{0%{opacity:0;transform:translateY(6px) scale(0.96)}12%{opacity:1;transform:none}"
    "85%{opacity:1}100%{opacity:0;transform:translateY(-4px)}}\n"
    "@keyframes cs-rw-reveal{0%{opacity:0;filter:blur(6px);transform:translateY(8px)}35%{opacity:1;filter:none;transform:none}"
    "55%{text-shadow:0 0 18px rgba(0,200,5,0.9)}100%{text-shadow:none}}\n"
    ".cs-rw-pulse{animation:cs-rw-pulse 1.6s ease-in-out infinite}\n"
    ".cs-rw-flash{animation:cs-rw-flash 6s ease-out both}\n"
    ".cs-rw-reveal{animation:cs-rw-reveal 4s ease-out both}\n"
    "@media (max-width:900px){.cs-rw-grid{grid-template-columns:minmax(0,1fr)!important}}\n"
    ".cs-rw-short{display:none}\n"
    "@media (max-width:640px){.cs-rw-full{display:none}.cs-rw-short{display:inline}.cs-rw-timer{font-size:28px!important}}\n"
    "@media (prefers-reduced-motion:reduce){.cs-rw-pulse,.cs-rw-flash,.cs-rw-reveal{animation:none}}\n"
    "@media (max-width:640px){\n")

# суммы меньше $1K — без хвоста знаков (тонкая ликвидность на Solana)
rep("':'$'+v;", "':'$'+(v>=10?Math.round(v):v.toFixed(2));")

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
