"""FastAPI dashboard — serves the single-page UI and an SSE event stream."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from wsb_trader.state import DashboardState

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WSB Trader</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono','Fira Code',monospace;background:#0d0d0d;color:#e0e0e0;font-size:13px;line-height:1.5;overflow:hidden;height:100vh}
header{display:flex;align-items:center;justify-content:space-between;padding:10px 18px;background:#141414;border-bottom:1px solid #222}
header h1{font-size:15px;color:#fff;letter-spacing:.06em}
.badge{font-size:11px;color:#666;display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:#4caf50;flex-shrink:0}
.dot.offline{background:#f44336}
.layout{display:grid;grid-template-rows:auto minmax(0,1fr) minmax(0,1fr);gap:10px;padding:10px;height:calc(100vh - 42px)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat{background:#141414;border:1px solid #222;border-radius:5px;padding:12px}
.stat .lbl{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.stat .val{font-size:20px;font-weight:700;color:#fff}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:10px;overflow:hidden}
.card{background:#141414;border:1px solid #222;border-radius:5px;padding:12px;display:flex;flex-direction:column;overflow:hidden}
.card h2{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px;flex-shrink:0}
.scroll{flex:1;overflow-y:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.08em;padding:0 6px 7px 0;border-bottom:1px solid #1e1e1e}
td{padding:6px 6px 6px 0;border-bottom:1px solid #181818;color:#bbb;white-space:nowrap}
tr:last-child td{border-bottom:none}
.pos{color:#4caf50}.neg{color:#f44336}
.bar-row{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.bar-ticker{width:56px;color:#fff;font-weight:700;font-size:12px;flex-shrink:0}
.bar-track{flex:1;background:#1e1e1e;border-radius:2px;height:17px;position:relative}
.bar-fill{height:100%;border-radius:2px;background:#1d4ed8;transition:width .3s}
.bar-fill.has-cashtag{background:#166534}
.bar-n{position:absolute;right:5px;top:50%;transform:translateY(-50%);font-size:10px;color:#777}
.feed{background:#141414;border:1px solid #222;border-radius:5px;padding:12px;display:flex;flex-direction:column;overflow:hidden}
.feed h2{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px;flex-shrink:0}
.feed-scroll{flex:1;overflow-y:auto}
.ev{display:flex;gap:8px;padding:4px 0;border-bottom:1px solid #181818;font-size:12px}
.ev:last-child{border-bottom:none}
.ev .ts{color:#383838;white-space:nowrap;flex-shrink:0}
.ev .kd{width:72px;flex-shrink:0;font-weight:700}
.ev.scrape .kd{color:#2196f3}
.ev.mentions .kd{color:#9c27b0}
.ev.signal .kd{color:#ff9800}
.ev.signal.BUY .kd{color:#4caf50}
.ev.signal.SELL .kd{color:#f44336}
.ev.trade .kd{color:#e91e63}
.ev.trade.short .kd{color:#9c27b0}
.ev.trade.exit .kd{color:#ff9800}
.ev.positions .kd{color:#607d8b}
.ev.error .kd{color:#f44336}
.ev .bd{color:#777;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{color:#333;font-style:italic;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>WSB Trader</h1>
  <div class="badge"><span class="dot offline" id="dot"></span><span id="badge-txt">connecting…</span></div>
</header>
<div class="layout">
  <div class="stats">
    <div class="stat"><div class="lbl">Cash</div><div class="val" id="s-cash">—</div></div>
    <div class="stat"><div class="lbl">Buying Power</div><div class="val" id="s-bp">—</div></div>
    <div class="stat"><div class="lbl">Portfolio Value</div><div class="val" id="s-pv">—</div></div>
    <div class="stat"><div class="lbl">Equity</div><div class="val" id="s-eq">—</div></div>
  </div>
  <div class="panels">
    <div class="card">
      <h2>Positions</h2>
      <div class="scroll">
        <table>
          <thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Change</th><th>Unreal P&L</th></tr></thead>
          <tbody id="pos-body"><tr><td colspan="6" class="empty">No positions</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h2>Last Tick — Mentions</h2>
      <div class="scroll" id="mentions-wrap"><p class="empty">Waiting for first tick…</p></div>
    </div>
  </div>
  <div class="feed">
    <h2>Live Events</h2>
    <div class="feed-scroll" id="feed-scroll">
      <div id="feed"></div>
    </div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const money=v=>v==null?'—':'$'+parseFloat(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const num=v=>v==null?'—':parseFloat(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const hhmm=ts=>new Date(ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});

function setAccount(a){
  if(!a)return;
  $('s-cash').textContent=money(a.cash);
  $('s-bp').textContent=money(a.buying_power);
  $('s-pv').textContent=money(a.portfolio_value);
  $('s-eq').textContent=money(a.equity);
}

function setPositions(ps){
  const tb=$('pos-body');
  if(!ps||!ps.length){tb.innerHTML='<tr><td colspan="6" class="empty">No positions</td></tr>';return;}
  tb.innerHTML=ps.map(p=>{
    const pl=parseFloat(p.unrealized_pl),cls=pl>=0?'pos':'neg';
    const curr=p.current_price?parseFloat(p.current_price):null;
    const change=curr?curr-parseFloat(p.avg_entry_price):null;
    const changePct=change&&parseFloat(p.avg_entry_price)>0?(change/parseFloat(p.avg_entry_price)*100):0;
    const changeCls=change>=0?'pos':'neg';
    return`<tr><td><strong>${p.symbol}</strong></td><td>${num(p.qty)}</td><td>${money(p.avg_entry_price)}</td><td>${money(curr)}</td><td class="${changeCls}">${money(change)} (${changePct.toFixed(1)}%)</td><td class="${cls}">${money(p.unrealized_pl)}</td></tr>`;
  }).join('');
}

function setMentions(ms){
  const wrap=$('mentions-wrap');
  if(!ms||!ms.length){wrap.innerHTML='<p class="empty">No tickers extracted</p>';return;}
  const max=ms[0].count;
  wrap.innerHTML=ms.slice(0,25).map(m=>{
    const pct=Math.max(3,(m.count/max)*100).toFixed(1);
    const cc=m.cashtag_count>0?' has-cashtag':'';
    return`<div class="bar-row"><div class="bar-ticker">${m.ticker}</div><div class="bar-track"><div class="bar-fill${cc}" style="width:${pct}%"></div><span class="bar-n">${m.count}</span></div></div>`;
  }).join('');
}

function evBody(ev){
  const d=ev.data;
  switch(ev.kind){
    case'scrape':return`${d.n_posts} new posts (${d.n_dupes||0} dupes) — ${(d.sources||[]).join(', ')}`;
    case'mentions':return`${(d.tickers||[]).length} tickers — top: ${(d.tickers||[]).slice(0,5).map(t=>`${t.ticker}×${t.count}`).join(', ')||'none'}`;
    case'signal':return`${d.ticker} → ${d.signal} @ ${((d.confidence||0)*100).toFixed(0)}%  "${d.reasoning}"`;
    case'trade':
      if(d.action==='buy')return`BUY ${d.ticker}  notional $${d.notional}`;
      if(d.action==='short')return`SHORT ${d.ticker}  notional $${d.notional}`;
      if(d.action==='exit')return`EXIT ${d.ticker}  ${d.reason||'manual'}`;
      return`CLOSE ${d.ticker}`;
    case'positions':return`${(d.positions||[]).length} open  portfolio ${money(d.account&&d.account.portfolio_value)}`;
    case'prices':return`${(d.positions||[]).length} positions  ${(d.positions||[]).slice(0,3).map(p=>`${p.ticker} ${p.change_pct>=0?'+':''}${p.change_pct}%`).join(', ')}`;
    case'error':return`[${d.source}] ${d.message}`;
    default:return JSON.stringify(d);
  }
}

function pushEvent(ev){
  const feed=$('feed');
  const row=document.createElement('div');
  let suffix='';
  if(ev.kind==='signal')suffix=' '+(ev.data.signal||'');
  if(ev.kind==='trade')suffix=' '+(ev.data.action||'');
  row.className=`ev ${ev.kind}${suffix}`;
  row.innerHTML=`<span class="ts">${hhmm(ev.ts)}</span><span class="kd">${ev.kind}</span><span class="bd">${evBody(ev)}</span>`;
  feed.prepend(row);
  while(feed.children.length>500)feed.removeChild(feed.lastChild);
}

function applyEvent(ev){
  pushEvent(ev);
  if(ev.kind==='positions'){setAccount(ev.data.account);setPositions(ev.data.positions);}
  if(ev.kind==='mentions'){setMentions(ev.data.tickers);}
  if(ev.kind==='prices'){updatePrices(ev.data.positions);}
  if(ev.kind==='scrape'){$('badge-txt').textContent='live — last tick '+hhmm(ev.ts);}
}

function updatePrices(priceData){
  if(!priceData)return;
  const priceMap={};
  priceData.forEach(p=>{priceMap[p.ticker]=p;});
  const rows=$('pos-body').querySelectorAll('tr');
  rows.forEach(row=>{
    const symCell=row.querySelector('td');
    if(!symCell)return;
    const ticker=symCell.textContent.trim();
    const priceInfo=priceMap[ticker];
    if(!priceInfo)return;
    const cells=row.querySelectorAll('td');
    if(cells.length>=5){
      cells[3].textContent=parseFloat(priceInfo.current_price).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
      const changeCls=parseFloat(priceInfo.change)>=0?'pos':'neg';
      cells[4].className=changeCls;
      cells[4].textContent=money(parseFloat(priceInfo.change))+' ('+priceInfo.change_pct+'%)';
    }
  });
}

fetch('/api/snapshot').then(r=>r.json()).then(s=>{
  setAccount(s.account);
  setPositions(s.positions);
  setMentions(s.mentions);
  if(s.last_tick_at)$('badge-txt').textContent='last tick '+hhmm(s.last_tick_at);
  (s.recent_events||[]).slice().reverse().forEach(pushEvent);
}).catch(()=>{});

function connect(){
  const es=new EventSource('/stream');
  es.onopen=()=>{$('dot').classList.remove('offline');$('badge-txt').textContent='live';};
  es.onmessage=e=>applyEvent(JSON.parse(e.data));
  es.onerror=()=>{
    $('dot').classList.add('offline');
    $('badge-txt').textContent='reconnecting…';
    es.close();setTimeout(connect,3000);
  };
}
connect();
</script>
</body>
</html>"""


def _pos_dict(p) -> dict:
    return {
        "symbol": p.symbol,
        "qty": str(p.qty),
        "market_value": str(p.market_value),
        "unrealized_pl": str(p.unrealized_pl),
        "avg_entry_price": str(p.avg_entry_price),
    }


def _acct_dict(a) -> dict | None:
    if a is None:
        return None
    return {
        "cash": str(a.cash),
        "buying_power": str(a.buying_power),
        "portfolio_value": str(a.portfolio_value),
        "equity": str(a.equity),
    }


def create_app(state: DashboardState) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> str:
        return _HTML

    @app.get("/api/snapshot")
    async def snapshot() -> dict:
        return {
            "account": _acct_dict(state.account),
            "positions": [_pos_dict(p) for p in state.positions],
            "mentions": [
                {"ticker": m.ticker, "count": m.count, "cashtag_count": m.cashtag_count}
                for m in state.mentions
            ],
            "last_tick_at": state.last_tick_at,
            "recent_events": [
                {"ts": e.ts, "kind": e.kind, "data": e.data}
                for e in list(state.events)
            ],
        }

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def _generate() -> AsyncIterator[str]:
            q = state.subscribe()
            yield ": connected\n\n"  # flush headers immediately so browser opens the connection
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=20.0)
                        payload = json.dumps({"ts": event.ts, "kind": event.kind, "data": event.data})
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"  # keep the connection alive through proxies/tunnels
            except asyncio.CancelledError:
                pass
            finally:
                state.unsubscribe(q)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
