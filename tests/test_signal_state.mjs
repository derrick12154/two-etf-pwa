import assert from 'node:assert/strict';
import {existsSync,readFileSync} from 'node:fs';
const remote=new URL('../signal-state.mjs',import.meta.url);
const {validateSnapshot,signalPresentation,chinaTime}=await import(existsSync(remote)?remote:new URL('../pwa/docs/signal-state.mjs',import.meta.url));
if(process.env.PWA_SNAPSHOT_TEST_OUTPUT){
  const snapshots=JSON.parse(readFileSync(process.env.PWA_SNAPSHOT_TEST_OUTPUT,'utf8'));
  for(const snapshot of snapshots){validateSnapshot(snapshot);signalPresentation(snapshot,new Date(snapshot.generatedAt));}
  assert.equal(snapshots[0].latestSignal.status,'pending_friday_close');
  assert.equal(snapshots[1].positionAsOf,'2026-10-23');
}
const legacy={schemaVersion:1,strategyId:'two-etf-v1',mode:'automated_weekly',generatedAt:'2026-10-18T12:27:00Z',priceAsOf:'2026-10-16',signalAsOf:'2026-10-16',holding:'sh513100',exposure:1,
  candidates:[{symbol:'sh513100',momentum:.2,volatility:.2},{symbol:'sz159915',momentum:.1,volatility:.3}],
  history:[{date:'2026-10-15',nav:1,exposure:1,sz159915:1,sh513100:1},{date:'2026-10-16',nav:1.01,exposure:1,sz159915:1,sh513100:1}],
  decisions:[{executionDate:'2026-10-12',asset:'sh513100',openWeight:1}],provenance:{acceptedBaselineIdentical:true,selectionAudit:{passed:true},portfolioAudit:{passed:true}},
  pendingSignal:{signalDate:'2026-10-16',executionDate:'2026-10-19',asset:'sh513100',selection:false,traded:true,target:.94,plannedWeight:.94,sizingReason:'band_exceeded',status:'pending_next_open',auditPassed:true}};
assert.equal(validateSnapshot(legacy),legacy);
const stale=signalPresentation(legacy,new Date('2026-10-20T02:00:00Z'));
assert.match(stale.state,/待更新/);
assert.doesNotMatch(stale.description,/尚未执行/);
assert.doesNotMatch(stale.state,/模型已更新/);
const signal={...legacy.pendingSignal,decisionDate:'2026-10-22',signalDate:'2026-10-22',executionDate:'2026-10-23',executionTiming:'friday_close_proxy',status:'pending_friday_close'};
const modern={...legacy,schemaVersion:2,strategyId:'two-etf-thu-fri-v2',mode:'automated_thursday_friday',priceAsOf:'2026-10-22',signalAsOf:'2026-10-22',positionAsOf:'2026-10-19',positionTiming:'legacy_next_open',exposure:.94,
  history:[...legacy.history,{date:'2026-10-22',nav:1.02,exposure:.94,sz159915:1,sh513100:1}],
  decisions:[{executionDate:'2026-10-19',actualHolding:'sh513100',executionWeight:.94}],latestSignal:signal,pendingSignal:signal,nextSignalDate:'2026-10-29',nextExecutionDate:'2026-10-30',
  timing:{effectiveDecisionDate:'2026-10-22'},provenance:{...legacy.provenance,portfolioAudit:{passed:true,causalThursdayInputs:true,fridayCloseProxy:true}}};
assert.equal(validateSnapshot(modern),modern);
assert.equal(signalPresentation(modern,new Date('2026-10-23T06:45:00Z')).state,'今日尾盘待执行');
assert.match(signalPresentation(modern,new Date('2026-10-23T07:01:00Z')).state,/待更新/);
const confirmed={...modern,priceAsOf:'2026-10-23',positionAsOf:'2026-10-23',positionTiming:'friday_close_proxy',exposure:.7,
  history:[...modern.history,{date:'2026-10-23',nav:1.03,exposure:.7,sz159915:1,sh513100:1}],
  latestSignal:{...signal,target:.7,plannedWeight:.7,status:'model_executed',actualHolding:'sh513100',executionWeight:.7},pendingSignal:null,
  decisions:[{executionDate:'2026-10-23',actualHolding:'sh513100',executionWeight:.7}]};
assert.equal(validateSnapshot(confirmed),confirmed);
assert.equal(signalPresentation(confirmed,new Date('2026-10-23T11:00:00Z')).state,'模型已更新');
assert.throws(()=>validateSnapshot({...confirmed,exposure:1}),/不一致/);
assert.throws(()=>validateSnapshot({...confirmed,latestSignal:signal}),/待执行/);
assert.throws(()=>validateSnapshot({...modern,latestSignal:{...signal,decisionDate:'2026-10-23'}}),/时点不符/);
const skipped={...modern,latestSignal:{decisionDate:'2026-10-22',executionDate:'2026-10-23',signalDate:'2026-10-22',status:'skipped_holiday',skipReason:'friday_closed',target:null,plannedWeight:null,traded:false,auditPassed:true}};
assert.equal(validateSnapshot(skipped),skipped);
assert.match(signalPresentation(skipped).description,/跳过执行/);
assert.deepEqual(chinaTime(new Date('2026-10-22T16:05:00Z')),{date:'2026-10-23',minutes:5});
console.log('PASS: legacy overdue state, Thursday pending, Friday confirmation, holiday skip, allocation consistency and China time.');
