export const NAMES={sz159915:'创业板ETF',sh513100:'纳斯达克100ETF',CASH:'现金'};
export const REASONS={asset_change:'切换／建仓',risk_excess:'风险超限调整',band_exceeded:'偏差达到缓冲阈值',hold_band:'缓冲区内维持',scheduled:'例行调整'};
const date=v=>/^\d{4}-\d{2}-\d{2}$/.test(v);
const weight=v=>Number.isFinite(v)&&v>=0&&v<=1.000001;
const pct=v=>Number.isFinite(v)?`${(v*100).toFixed(1)}%`:'—';
export function chinaTime(now=new Date()){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now).map(p=>[p.type,p.value]));
  return {date:`${parts.year}-${parts.month}-${parts.day}`,minutes:Number(parts.hour)*60+Number(parts.minute)};
}
export function validateSnapshot(p){
  const modern=p?.schemaVersion===2&&p.strategyId==='two-etf-thu-fri-v2'&&p.mode==='automated_thursday_friday';
  const legacy=p?.schemaVersion===1&&p.strategyId==='two-etf-v1'&&p.mode==='automated_weekly';
  if(!modern&&!legacy)throw Error('策略数据版本不兼容');
  if(!date(p.priceAsOf)||!date(p.signalAsOf)||p.signalAsOf>p.priceAsOf||!Number.isFinite(Date.parse(p.generatedAt)))throw Error('日期无效');
  if(!NAMES[p.holding]||!weight(p.exposure)||!Array.isArray(p.history)||p.history.length<2)throw Error('数据不完整');
  if(!Array.isArray(p.candidates)||p.candidates.length!==2||new Set(p.candidates.map(c=>c.symbol)).size!==2||p.candidates.some(c=>!['sz159915','sh513100'].includes(c.symbol)||!Number.isFinite(c.momentum)||!Number.isFinite(c.volatility)))throw Error('标的池不符');
  if(p.history.some((r,i)=>!date(r.date)||!Number.isFinite(r.nav)||r.nav<=0||!weight(r.exposure)||!Number.isFinite(r.sz159915)||r.sz159915<=0||!Number.isFinite(r.sh513100)||r.sh513100<=0||(i>0&&r.date<=p.history[i-1].date)))throw Error('走势数据无效');
  if(p.history.at(-1).date!==p.priceAsOf||!p.provenance?.acceptedBaselineIdentical||!p.provenance.selectionAudit?.passed||!p.provenance.portfolioAudit?.passed)throw Error('快照未经审计');
  if(!Array.isArray(p.decisions)||!p.decisions.length)throw Error('缺少执行记录');
  const s=modern?p.latestSignal:p.pendingSignal;
  if(!s?.auditPassed||!date(s.executionDate))throw Error('调仓方案未通过核验');
  if(modern){
    if(!date(p.positionAsOf)||p.positionAsOf>p.priceAsOf||!date(p.nextSignalDate)||!date(p.nextExecutionDate)||!p.provenance.portfolioAudit.causalThursdayInputs||!p.provenance.portfolioAudit.fridayCloseProxy)throw Error('模型仓位未确认');
    const confirmed=p.decisions.find(d=>d.executionDate===p.positionAsOf);
    if(!confirmed||confirmed.actualHolding!==p.holding||Math.abs(confirmed.executionWeight-p.exposure)>1e-9)throw Error('模型仓位与执行记录不一致');
    if(!['pending_friday_close','pending_next_open','model_executed','model_held','skipped_holiday'].includes(s.status))throw Error('未知执行状态');
    if(s.status==='skipped_holiday'){
      if(s.target!==null||s.plannedWeight!==null||s.traded)throw Error('休市周不能记入成交');
      return p;
    }
    if(s.executionTiming==='friday_close_proxy'){
      const decision=new Date(s.decisionDate+'T00:00:00Z'),execution=new Date(s.executionDate+'T00:00:00Z');
      if(!date(s.decisionDate)||decision.getUTCDay()!==4||execution.getUTCDay()!==5||execution-decision!==86400000)throw Error('周四／周五时点不符');
    }
    if(s.status.startsWith('pending_')){
      if(s.executionDate<=p.priceAsOf)throw Error('已发生行情不能保留为待执行');
    }else if(s.executionDate>p.priceAsOf||!weight(s.executionWeight))throw Error('未发生的成交不能计入模型');
  }else if(s.status!=='pending_next_open'||s.signalDate!==p.signalAsOf||s.executionDate<=p.signalAsOf)throw Error('旧版信号无效');
  if(!date(s.signalDate)||s.signalDate>p.priceAsOf||s.executionDate<=s.signalDate||!NAMES[s.asset]||!weight(s.target)||!weight(s.plannedWeight))throw Error('调仓方案数据无效');
  return p;
}
export function signalPresentation(p,now=new Date()){
  const modern=p.schemaVersion===2,s=modern?p.latestSignal:p.pendingSignal,t=chinaTime(now);
  const common={date:s.signalDate||s.decisionDate,target:s.target,plannedWeight:s.plannedWeight,sizing:REASONS[s.sizingReason]||'本周不执行',
    caption:'周四收盘后定方案，周五尾盘执行。模型用周五收盘价近似尾盘成交，盘后确认仓位；周五休市则本周跳过。'};
  if(s.status==='skipped_holiday')return {...common,title:'本周暂停调仓',state:'休市安排',description:s.skipReason==='friday_closed'?`${s.executionDate} 周五休市，本周跳过执行，等待下一周周四重新生成方案。`:'本周四之前没有可用交易日收盘数据，本周跳过执行。'};
  const reason=s.selection?'四周选强':s.reason==='eligibility_exit'?'持仓资格失效，退出至现金':'周度资格与仓位检查';
  if(s.status==='model_executed'||s.status==='model_held'){
    const tail=s.executionTiming==='friday_close_proxy';
    return {...common,title:'最近一次调仓确认',state:s.status==='model_held'?'模型维持原份额':'模型已更新',
      description:`${reason}。${s.executionDate} ${tail?'尾盘':'开盘（旧时点）'}${s.traded?'模型调仓已计入':'维持原有份额'}：${NAMES[s.actualHolding||s.asset]}，确认仓位 ${pct(s.executionWeight)}。${tail?'':`新时点从 ${p.timing.effectiveDecisionDate} 周四起启用。`}`};
  }
  const overdue=s.executionDate<t.date||(s.executionDate===t.date&&t.minutes>=15*60);
  const tail=s.executionTiming==='friday_close_proxy';
  const action=s.traded?`调整至 ${NAMES[s.asset]}，目标仓位 ${pct(s.plannedWeight)}`:`维持现有份额，周四参考仓位 ${pct(s.plannedWeight)}`;
  return {...common,title:overdue?'等待盘后模型确认':'本周调仓方案',state:overdue?'执行窗口已过，待更新':s.executionDate===t.date?'今日尾盘待执行':'待执行',
    description:`${reason}。计划 ${s.executionDate} ${tail?'尾盘':'开盘（旧时点）'}${action}。${overdue?'当前快照尚未确认本次执行，请查看仓位确认日期。':'执行完成后，顶部模型仓位会随盘后快照更新。'}`};
}
export function snapshotStatus(p,cached,now=new Date()){
  const t=chinaTime(now),expected=p.nextRefreshExpected||p.priceAsOf;
  const late=t.date>expected||(t.date===expected&&t.minutes>=22*60);
  if(p.schemaVersion!==2)return `${cached?'离线／本机缓存 · ':''}旧版周度快照：行情 ${p.priceAsOf}。周五执行后的模型仓位尚未确认。`;
  return `${cached?'离线／本机缓存 · ':''}${late?'发布数据待更新 · ':''}方案参考 ${p.signalAsOf}｜模型仓位确认 ${p.positionAsOf}｜周四定方案，周五尾盘执行后更新仓位。`;
}
