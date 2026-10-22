export const NAMES={sz159915:'创业板ETF',sh513100:'纳斯达克100ETF',CASH:'现金',MIXED:'创业板＋纳指'};
export const REASONS={asset_change:'切换／建仓',risk_excess:'风险超限调整',band_exceeded:'偏差达到缓冲阈值',hold_band:'缓冲区内维持',scheduled:'例行调整',four_phase:'四份独立控仓'};
const date=v=>/^\d{4}-\d{2}-\d{2}$/.test(v);
const weight=v=>Number.isFinite(v)&&v>=0&&v<=1.000001;
const pct=v=>Number.isFinite(v)?`${(v*100).toFixed(1)}%`:'—';
export function validWeights(w){return !!w&&['sz159915','sh513100','CASH'].every(k=>weight(w[k]))&&Math.abs(w.sz159915+w.sh513100+w.CASH-1)<1e-8;}
export function allocationText(w){return `创业板 ${pct(w.sz159915)}，纳指 ${pct(w.sh513100)}，现金 ${pct(w.CASH)}`;}
export function chinaTime(now=new Date()){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now).map(p=>[p.type,p.value]));
  return {date:`${parts.year}-${parts.month}-${parts.day}`,minutes:Number(parts.hour)*60+Number(parts.minute)};
}
export function validateSnapshot(p){
  const modern=p?.schemaVersion===2&&p.strategyId==='two-etf-thu-fri-v2'&&p.mode==='automated_thursday_friday';
  const blended=p?.schemaVersion===3&&p.strategyId==='two-etf-risk-blend-v3'&&p.mode==='automated_four_phase';
  const legacy=p?.schemaVersion===1&&p.strategyId==='two-etf-v1'&&p.mode==='automated_weekly';
  if(!modern&&!legacy&&!blended)throw Error('策略数据版本不兼容');
  if(!date(p.priceAsOf)||!date(p.signalAsOf)||p.signalAsOf>p.priceAsOf||!Number.isFinite(Date.parse(p.generatedAt)))throw Error('日期无效');
  if(!NAMES[p.holding]||!weight(p.exposure)||!Array.isArray(p.history)||p.history.length<2)throw Error('数据不完整');
  if(!Array.isArray(p.candidates)||p.candidates.length!==2||new Set(p.candidates.map(c=>c.symbol)).size!==2||p.candidates.some(c=>!['sz159915','sh513100'].includes(c.symbol)||!Number.isFinite(c.momentum)||!Number.isFinite(c.volatility)))throw Error('标的池不符');
  if(p.history.some((r,i)=>!date(r.date)||!Number.isFinite(r.nav)||r.nav<=0||!weight(r.exposure)||!Number.isFinite(r.sz159915)||r.sz159915<=0||!Number.isFinite(r.sh513100)||r.sh513100<=0||(i>0&&r.date<=p.history[i-1].date)))throw Error('走势数据无效');
  if(p.history.at(-1).date!==p.priceAsOf||!p.provenance?.acceptedBaselineIdentical||!p.provenance.selectionAudit?.passed||!p.provenance.portfolioAudit?.passed)throw Error('快照未经审计');
  if(!Array.isArray(p.decisions)||!p.decisions.length)throw Error('缺少执行记录');
  const s=modern||blended?p.latestSignal:p.pendingSignal;
  if(!s?.auditPassed||!date(s.executionDate))throw Error('调仓方案未通过核验');
  if(modern||blended){
    if(!date(p.positionAsOf)||p.positionAsOf>p.priceAsOf||!date(p.nextSignalDate)||!date(p.nextExecutionDate)||!p.provenance.portfolioAudit.causalThursdayInputs||!p.provenance.portfolioAudit.fridayCloseProxy)throw Error('模型仓位未确认');
    const confirmed=p.decisions.find(d=>d.executionDate===p.positionAsOf);
    if(!confirmed||confirmed.actualHolding!==p.holding||Math.abs(confirmed.executionWeight-p.exposure)>1e-9)throw Error('模型仓位与执行记录不一致');
    if(blended){
      if(!validWeights(p.confirmedWeights)||Math.abs(p.exposure-(1-p.confirmedWeights.CASH))>1e-9)throw Error('合计配置无效');
      const active=['sz159915','sh513100'].filter(k=>p.confirmedWeights[k]>1e-10);
      const holding=active.length>1?'MIXED':active[0]||'CASH';
      if(holding!==p.holding)throw Error('组合持仓标签不一致');
      if(p.rules?.sizeGateEnabled!==false||p.rules?.sleeveCount!==4||p.rules?.selectionRanking!=='momentum26_over_volatility63'||!p.provenance.portfolioAudit.fourSleeveLedger||!p.provenance.portfolioAudit.grossFees)throw Error('四相位规则或审计无效');
      if(!Array.isArray(p.sleeves)||p.sleeves.length!==4||new Set(p.sleeves.map(x=>x.phase)).size!==4||p.sleeves.some(x=>![0,1,2,3].includes(x.phase)||!weight(x.equityWeight))||Math.abs(p.sleeves.reduce((a,x)=>a+x.equityWeight,0)-1)>1e-8)throw Error('子账户配置无效');
      if(!Array.isArray(s.sleeves)||s.sleeves.length!==4||new Set(s.sleeves.map(x=>x.phase)).size!==4)throw Error('缺少四份方案');
      if(s.sleeves.some(x=>![0,1,2,3].includes(x.phase)||!x.auditPassed||x.executionDate!==s.executionDate||x.decisionDate!==s.decisionDate))throw Error('子方案时点或审计无效');
      if(s.status!=='skipped_holiday'){
        if(!validWeights(s.plannedWeights)||!validWeights(s.theoreticalWeights)||Math.abs(s.plannedWeight-(1-s.plannedWeights.CASH))>1e-9||Math.abs(s.target-(1-s.theoreticalWeights.CASH))>1e-9)throw Error('合并目标配置不一致');
        if(s.sleeves.some(x=>!NAMES[x.asset]||x.asset==='MIXED'||!weight(x.target)||!weight(x.plannedWeight)))throw Error('子方案目标无效');
        const total=s.sleeves.reduce((a,x)=>a+x.signalEquity,0);
        if(!Number.isFinite(total)||total<=0||s.sleeves.some(x=>!Number.isFinite(x.signalEquity)||x.signalEquity<=0))throw Error('方案权益无效');
        for(const [field,key] of [['plannedWeights','plannedWeight'],['theoreticalWeights','target']]){
          const expected={sz159915:0,sh513100:0,CASH:0};
          for(const child of s.sleeves){const fraction=child.signalEquity/total;if(child.asset!=='CASH')expected[child.asset]+=fraction*child[key];expected.CASH+=fraction*(1-child[key]);}
          if(['sz159915','sh513100','CASH'].some(k=>Math.abs(expected[k]-s[field][k])>1e-8))throw Error('合计与子方案不一致');
        }
        if(p.candidates.some(c=>c.eligible&&(!Number.isFinite(c.riskScore)||c.volatility<=0)))throw Error('风险分数无效');
      }
      if(confirmed.executionWeights&&(!validWeights(confirmed.executionWeights)||['sz159915','sh513100','CASH'].some(k=>Math.abs(confirmed.executionWeights[k]-p.confirmedWeights[k])>1e-9)))throw Error('配置与确认记录不一致');
      if(['model_executed','model_held'].includes(s.status)&&s.executionDate===p.positionAsOf&&(!validWeights(s.executionWeights)||['sz159915','sh513100','CASH'].some(k=>Math.abs(s.executionWeights[k]-p.confirmedWeights[k])>1e-9)))throw Error('最近执行与配置不一致');
    }
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
  const blended=p.schemaVersion===3,modern=p.schemaVersion===2||blended,s=modern?p.latestSignal:p.pendingSignal,t=chinaTime(now);
  const common={date:s.signalDate||s.decisionDate,target:s.target,plannedWeight:s.plannedWeight,sizing:REASONS[s.sizingReason]||'本周不执行',
    caption:'周四收盘后定方案，周五尾盘执行。模型用周五收盘价近似尾盘成交，盘后确认仓位；周五休市则本周跳过。'};
  if(s.status==='skipped_holiday')return {...common,title:'本周暂停调仓',state:'休市安排',description:s.skipReason==='friday_closed'?`${s.executionDate} 周五休市，本周跳过执行，等待下一周周四重新生成方案。`:'本周四之前没有可用交易日收盘数据，本周跳过执行。'};
  const reason=blended?`${(s.selectedPhases||[]).map(i=>`第${i+1}份`).join('、')||'四份资金'}按规则检查，分别执行资格退出和仓位控制`:s.selection?'四周选强':s.reason==='eligibility_exit'?'持仓资格失效，退出至现金':'周度资格与仓位检查';
  if(s.status==='model_executed'||s.status==='model_held'){
    const tail=s.executionTiming==='friday_close_proxy';
    return {...common,title:'最近一次调仓确认',state:s.status==='model_held'?'模型维持原份额':'模型已更新',
      description:blended?`${s.executionDate} 尾盘模型${s.traded?'调仓已计入':'维持原份额'}。确认配置：${allocationText(p.confirmedWeights)}。四份资金分别记账，不重置为等权。`:`${reason}。${s.executionDate} ${tail?'尾盘':'开盘（旧时点）'}${s.traded?'模型调仓已计入':'维持原有份额'}：${NAMES[s.actualHolding||s.asset]}，确认仓位 ${pct(s.executionWeight)}。${tail?'':`新时点从 ${p.timing.effectiveDecisionDate} 周四起启用。`}`};
  }
  const overdue=s.executionDate<t.date||(s.executionDate===t.date&&t.minutes>=15*60);
  const tail=s.executionTiming==='friday_close_proxy';
  const action=blended?(s.traded?`按下方四份方案分别调整，周四估计合计配置：${allocationText(s.plannedWeights)}`:'四份均维持现有份额'):s.traded?`调整至 ${NAMES[s.asset]}，目标仓位 ${pct(s.plannedWeight)}`:`维持现有份额，周四参考仓位 ${pct(s.plannedWeight)}`;
  return {...common,title:overdue?'等待盘后模型确认':'本周调仓方案',state:overdue?'执行窗口已过，待更新':s.executionDate===t.date?'今日尾盘待执行':'待执行',
    description:`${reason}。计划 ${s.executionDate} ${tail?'尾盘':'开盘（旧时点）'}${action}。${overdue?'当前快照尚未确认本次执行，请查看仓位确认日期。':'执行完成后，顶部模型仓位会随盘后快照更新。'}`};
}
export function snapshotStatus(p,cached,now=new Date()){
  const t=chinaTime(now),expected=p.nextRefreshExpected||p.priceAsOf;
  const late=t.date>expected||(t.date===expected&&t.minutes>=22*60);
  if(![2,3].includes(p.schemaVersion))return `${cached?'离线／本机缓存 · ':''}旧版周度快照：行情 ${p.priceAsOf}。周五执行后的模型仓位尚未确认。`;
  return `${cached?'离线／本机缓存 · ':''}${late?'发布数据待更新 · ':''}方案参考 ${p.signalAsOf}｜模型仓位确认 ${p.positionAsOf}｜周四定方案，周五尾盘执行后更新仓位。`;
}
