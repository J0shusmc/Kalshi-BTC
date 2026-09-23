const $ = id => document.getElementById(id);
const money = cents => Number.isFinite(cents) ? (cents/100).toLocaleString('en-US',{style:'currency',currency:'USD'}) : '—';
const price = dollars => Number.isFinite(dollars) ? `${(dollars*100).toFixed(1)}¢` : '—';
let snapshot = null, failed = false;
function node(tag, text, className='') { const el=document.createElement(tag); el.textContent=text; el.className=className; return el; }
function freshness() {
  const age=snapshot ? (Date.now()-Date.parse(snapshot.updated_at))/1000 : Infinity;
  const stale=failed || !Number.isFinite(age) || age>30;
  $('connection').textContent=failed?'Disconnected · last known values':stale?'Stale · bot has not updated':'● Receiving bot updates';
  $('connection').className=stale?'stale':'online';
  $('age').textContent=Number.isFinite(age)?`${Math.max(0,Math.floor(age))}s ago`:'No data';
  const remaining=snapshot ? Math.max(0,Math.ceil((Date.parse(snapshot.market.close_time)-Date.now())/1000)) : NaN;
  $('countdown').textContent=Number.isFinite(remaining)?`${Math.floor(remaining/60)}:${String(remaining%60).padStart(2,'0')} left`:'—';
}
function render(data) {
  snapshot=data;
  const active=data.active||{}, bot=active.bot||{}, stats=bot.paper?data.paper_stats:data.stats;
  $('mode').textContent=bot.paper?'PAPER':bot.live?'LIVE':'MONITOR';
  $('equity').textContent=money(data.portfolio_value_cents);
  $('cash').textContent=money(data.balance_cents); $('positionValue').textContent=money(data.position_value_cents);
  $('pnl').textContent=`${data.account_pnl_cents>=0?'+':''}${money(data.account_pnl_cents)} (${Number(data.account_pnl_pct).toFixed(2)}%)`;
  $('pnl').className=`pnl ${data.account_pnl_cents<0?'negative':'positive'}`;
  $('market').textContent=data.market.ticker;
  $('risk').textContent=`${bot.risk_pct}% sizing per signal · ${new Date(data.updated_at).toLocaleString()}`;
  $('stats').replaceChildren();
  for(const [label,value] of [['Wins / losses',`${stats.wins} / ${stats.losses}`],['Win rate',`${Number(stats.win_pct).toFixed(1)}%`],['Average win',money(stats.avg_win_cents)],['Average loss',money(stats.avg_loss_cents)],['Profit factor',Number.isFinite(stats.profit_factor)?stats.profit_factor.toFixed(2):'—'],['Closed trades',stats.closed]]) {
    const cell=node('div','','stat'); cell.append(node('small',label),node('strong',value)); $('stats').append(cell);
  }
  $('signals').replaceChildren();
  for(const signal of data.signals||[]) {
    const el=node('div','','signal'), row=node('div','','row');
    row.append(node('strong',`${signal.side} · ${signal.display_strategy}`,signal.side==='UP'?'up':'down'),node('span',signal.status));
    const progress=document.createElement('progress'); progress.className='progress';progress.max=signal.total;progress.value=signal.passed;
    el.append(row,node('small',`${signal.passed}/${signal.total} conditions met · Target ${signal.target_cents}¢`),progress);
    $('signals').append(el);
  }
  $('quotes').replaceChildren();
  for(const side of data.sides||[]) { const row=document.createElement('tr'); row.append(node('td',side.side,side.side==='UP'?'up':'down'),node('td',price(side.bid)),node('td',price(side.ask)),node('td',price(side.spread)));$('quotes').append(row); }
  const positions=bot.paper?Object.values(active.paper_positions||{}):active.positions||[];
  $('positions').replaceChildren(...(positions.length?positions.map(p=>node('p',`${p.side} · ${p.qty??p.remaining_count??p.entry_count??'—'} contracts · ${p.ticker}`)):[node('p','No positions for this market')]));
  const orders=bot.paper?[]:active.orders||[];
  $('orders').replaceChildren(...(orders.length?orders.map(o=>node('p',`${o.action} ${o.side} · ${o.remaining_count} @ ${o.price_cents}¢ · ${o.status}`)):[node('p','No orders for this market')]));
  $('error').textContent=active.error||'';
}
async function poll(){
  try {const response=await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(6000)});if(!response.ok)throw Error(response.status);render(await response.json());failed=false;}catch{failed=true;}
  freshness();setTimeout(poll,2000);
}
setInterval(freshness,1000);poll();
