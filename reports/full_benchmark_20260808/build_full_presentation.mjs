import fs from "node:fs/promises";
import path from "node:path";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const ROOT = process.env.TARGET_REPO || "";
if (!ROOT) {
  throw new Error("TARGET_REPO must point to the Target repository root when building this report");
}
const REPORT = path.join(ROOT, "reports/full_benchmark_20260808");
const OUT = path.join(REPORT, "Target_full_benchmark_presentation_20260808.pptx");
const metrics = JSON.parse(await fs.readFile(path.join(REPORT, "metrics_summary.json"), "utf8"));

const W = 1280, H = 720;
const C = {
  ink: "#132238", muted: "#657287", blue: "#2F75B5", bright: "#3F8EF7",
  cyan: "#67C2E8", green: "#2F8F5B", orange: "#DD7A1F", yellow: "#E0A925",
  pale: "#EEF4FA", paleBlue: "#DCEAF7", light: "#F7F9FC", line: "#D6DEE8", white: "#FFFFFF",
};
const FONT = "Microsoft YaHei";
const deck = Presentation.create({ slideSize: { width: W, height: H } });
const MAX = Number(process.env.MAX_SLIDES || 12);

function shape(slide, left, top, width, height, fill, geometry="rect", lineFill="none", radius="rounded-xl") {
  return slide.shapes.add({ geometry, position: { left, top, width, height }, fill,
    line: { style: "solid", fill: lineFill, width: lineFill === "none" ? 0 : 1 }, borderRadius: radius });
}
function txt(slide, text, left, top, width, height, size=22, color=C.ink, bold=false, align="left", valign="top") {
  const s = slide.shapes.add({ geometry: "textbox", position: { left, top, width, height }, fill: "none",
    line: { style: "solid", fill: "none", width: 0 } });
  s.text = String(text);
  s.text.style = { fontSize: size, typeface: FONT, color, bold, alignment: align,
    verticalAlignment: valign, autoFit: "shrinkText", insets: { top: 0, right: 0, bottom: 0, left: 0 } };
  return s;
}
function base(title, kicker, number) {
  const s = deck.slides.add(); s.background.fill = C.white;
  txt(s, kicker.toUpperCase(), 48, 28, 500, 20, 12, C.blue, true);
  txt(s, title, 48, 58, 1170, 58, 34, C.ink, true);
  shape(s, 48, 124, 1184, 2, C.line);
  txt(s, String(number).padStart(2,"0"), 1178, 682, 54, 18, 11, C.muted, false, "right");
  return s;
}
function note(slide, sources, extra="") {
  slide.speakerNotes.textFrame.setText(`${extra}${extra ? "\n\n" : ""}[Sources]\n${sources.map(x=>`- ${x}`).join("\n")}\n[/Sources]`);
}
function card(slide, x, y, w, h, value, label, detail, accent=C.blue) {
  shape(slide, x, y, w, h, C.light, "roundRect", C.line);
  shape(slide, x, y, 8, h, accent, "roundRect");
  txt(slide, value, x+28, y+18, w-48, 48, 34, accent, true);
  txt(slide, label, x+28, y+70, w-48, 27, 17, C.ink, true);
  txt(slide, detail, x+28, y+104, w-48, Math.max(18,h-112), 13, C.muted);
}
function chartFrame(slide, x, y, w, h) { return shape(slide, x, y, w, h, C.white, "roundRect", C.line); }
function addChart(slide, type, x, y, w, h, categories, series, options={}) {
  const leftPad=46, rightPad=18, topPad=(series.length>1?48:26), bottomPad=52;
  const px=x+leftPad, py=y+topPad, pw=w-leftPad-rightPad, ph=h-topPad-bottomPad;
  const maxValue=Math.max(...series.flatMap(s=>s.values));
  const yMax=maxValue>=90?110:Math.ceil(maxValue*1.18*10)/10;
  for(let i=0;i<=4;i++){
    const gy=py+ph-(ph*i/4); shape(slide,px,gy,pw,1,C.line);
    txt(slide,(yMax*i/4).toFixed(yMax<10?1:0),x,gy-8,leftPad-8,16,9,C.muted,false,"right");
  }
  const groupW=pw/categories.length, inner=groupW*0.72, barW=inner/series.length;
  categories.forEach((cat,ci)=>{
    series.forEach((s,si)=>{
      const val=s.values[ci], bh=Math.max(1,ph*val/yMax);
      const bx=px+ci*groupW+(groupW-inner)/2+si*barW;
      shape(slide,bx,py+ph-bh,Math.max(3,barW-3),bh,s.fill||C.blue);
      txt(slide,val.toFixed(val<10?2:1),bx-5,py+ph-bh-18,barW+10,16,8,C.ink,true,"center");
    });
    txt(slide,cat,px+ci*groupW,py+ph+10,groupW,28,categories.length>8?8:10,C.ink,false,"center");
  });
  if(series.length>1){
    let lx=px+pw-360;
    series.forEach(s=>{shape(slide,lx,py-28,14,14,s.fill||C.blue);txt(slide,s.name,lx+20,py-30,160,18,10,C.ink);lx+=180;});
  }
}
function grid(slide, x, y, widths, rows, rowH=42) {
  let yy=y;
  rows.forEach((row, ri)=>{
    let xx=x;
    row.forEach((v, ci)=>{
      shape(slide, xx, yy, widths[ci], rowH, ri===0 ? C.pale : C.white, "rect", C.line);
      txt(slide, v, xx+8, yy+7, widths[ci]-16, rowH-14, ri===0?14:13, C.ink, ri===0, ci===0?"left":"center", "middle");
      xx += widths[ci];
    }); yy += rowH;
  });
}

// 1 — cover
if (MAX >= 1) {
  const s=deck.slides.add(); s.background.fill=C.white;
  shape(s,0,0,18,H,C.blue); shape(s,920,0,360,H,C.pale);
  txt(s,"TARGET · FULL BENCHMARK",60,52,600,26,14,C.blue,true);
  txt(s,"完整评测运行\n与关系评测",60,155,760,150,52,C.ink,true);
  txt(s,"疾病—靶点—组织—细胞—时期\n从本地回归到上下文敏感度",60,340,700,80,23,C.muted);
  card(s,930,100,300,150,"81 / 81","pytest 通过","另有 4 项跳过",C.blue);
  card(s,930,270,300,150,"72 / 72","疾病任务","234 条断言全通过",C.cyan);
  card(s,930,440,300,150,"145","关系样本","基线 62.8% · 自检 100%",C.green);
  txt(s,"2026-08-08  ·  Local reproducible run",60,650,700,24,14,C.muted);
  note(s,["reports/full_benchmark_20260808/metrics_summary.json"],"主讲提示：先说明本次为完整本地 fake/unit 运行，不含 live API 与 GPU profile。");
}

// 2 — scope
if (MAX >= 2) {
  const s=base("一次完整运行，覆盖四层质量信号","Scope",2);
  const xs=[48,350,652,954], colors=[C.blue,C.bright,C.cyan,C.green];
  const vals=["85","11","72","145"], labels=["pytest 用例","主 Benchmark","疾病任务","关系样本"];
  const details=["代码与合同回归\n81 pass · 4 skip","27 条断言\n主链 / 迁移 / 鲁棒性","18 疾病 × 4 桶\n234 条断言","锚点 + 上下文\ntrain / val / test"];
  xs.forEach((x,i)=>card(s,x,170,278,260,vals[i],labels[i],details[i],colors[i]));
  txt(s,"验收边界",48,485,250,28,20,C.orange,true);
  txt(s,"未运行 live 外部 API 与 Reviewer LoRA GPU matrix；二者依赖网络/API/GPU 部署配置，也不是当前 CI 合并门槛。",48,525,1150,62,18,C.ink);
  shape(s,48,620,1184,42,C.paleBlue,"roundRect");
  txt(s,`本机 Python ${metrics.environment.python}；正式 acceptance runtime 为 Python 3.11，发布前需在远端 3.11 profile 复跑。`,66,631,1148,22,14,C.blue,true);
  note(s,["reports/full_benchmark_20260808/raw/pytest.xml","benchmark/goldset_v2.jsonl","benchmark/goldset_diseases.jsonl","benchmark/goldset_context_relations.jsonl"]);
}

// 3 — headline
if (MAX >= 3) {
  const s=base("三套执行结果全绿，关系集提供了真正的区分度","Executive result",3);
  card(s,48,165,360,220,"100%","主 Benchmark","11/11 任务 · 27/27 断言",C.blue);
  card(s,460,165,360,220,"100%","疾病库 Benchmark","72/72 任务 · 234/234 断言",C.cyan);
  card(s,872,165,360,220,"62.8%","上下文盲基线","标签准确率与必要动作召回",C.orange);
  shape(s,48,435,1184,150,C.light,"roundRect",C.line);
  txt(s,"关键判断",78,465,180,30,18,C.green,true);
  txt(s,"Gold / 评分器自检为 100%，它只证明样本与 scorer 对齐；不能被解读为 Agent 的关系理解性能。基线在覆盖率与禁用声明安全上均为 100%，但无法识别缺失上下文和错配上下文。",250,458,930,85,18,C.ink);
  note(s,["reports/full_benchmark_20260808/metrics_summary.json","reports/full_benchmark_20260808/raw/relation_context_blind_report.json","reports/full_benchmark_20260808/raw/relation_oracle_report.json"]);
}

// 4 — main categories
if (MAX >= 4) {
  const s=base("主 Benchmark：五类能力、27 条断言全部通过","Main benchmark",4);
  chartFrame(s,48,160,760,430);
  addChart(s,"bar",78,190,700,360,["主链","迁移","确定性/恢复","鲁棒性","合同"],[{name:"通过率",values:[100,100,100,100,100],fill:C.blue}],{
    yAxis:{visible:true,deleted:false,min:0,max:110,majorUnit:20,majorGridlines:{style:"solid",width:1,fill:C.line},line:{style:"solid",width:0,fill:C.white},textStyle:{typeface:FONT,fontSize:11,color:C.ink}},
    barOptions:{direction:"column",grouping:"clustered",gapWidth:75}
  });
  card(s,850,160,382,125,"14 / 14","主链","UC 主链输出与 evidence provenance",C.blue);
  card(s,850,305,382,125,"3 + 3","迁移与确定性","Legacy/LangGraph parity · 三连跑",C.green);
  card(s,850,450,382,140,"4 + 3","鲁棒性与合同","预算降级 / OOD / schema / export",C.orange);
  txt(s,"柱顶均为 100%；样本数分别为 14、3、3、4、3。",48,620,1000,24,14,C.muted);
  note(s,["reports/full_benchmark_20260808/raw/main/benchmark_report.json","benchmark/rubric.md"]);
}

// 5 — runtime
if (MAX >= 5) {
  const s=base("主 Benchmark 耗时：确定性三连跑是主要长尾","Runtime",5);
  chartFrame(s,48,155,820,455);
  const tasks=metrics.main_benchmark.tasks.filter(t=>Number.isFinite(t.elapsed_s));
  addChart(s,"bar",80,185,750,390,tasks.map(t=>t.id),[{name:"秒",values:tasks.map(t=>t.elapsed_s),fill:C.bright}],{
    barOptions:{direction:"bar",grouping:"clustered",gapWidth:55},
    xAxis:{visible:true,deleted:false,min:0,max:4.5,majorUnit:0.5,majorGridlines:{style:"solid",width:1,fill:C.line},line:{style:"solid",width:1,fill:C.line},textStyle:{typeface:FONT,fontSize:11,color:C.ink}},
    yAxis:{visible:true,deleted:false,line:{style:"solid",width:0,fill:C.white},textStyle:{typeface:FONT,fontSize:11,color:C.ink}}
  });
  card(s,910,175,322,150,"4.16 s","BM-03","三次确定性运行，因此耗时最高",C.orange);
  card(s,910,350,322,150,"0.00 s","BM-09 / 10","纯合同与 schema 检查",C.green);
  txt(s,"观察值来自 runner 报告；整条命令墙钟约 21.7 秒。",910,540,310,50,15,C.muted);
  note(s,["reports/full_benchmark_20260808/raw/main/benchmark_report.json","reports/full_benchmark_20260808/raw/main_console.txt"]);
}

// 6 — disease matrix
if (MAX >= 6) {
  const s=base("疾病矩阵：18 个疾病在四类任务桶中全部通过","Disease benchmark",6);
  grid(s,48,165,[300,180,220,220],[
    ["任务桶","任务数","断言数","通过率"],["normal","18","54","100%"],["missing_context","18","54","100%"],
    ["conflicting_evidence","18","54","100%"],["trap","18","72","100%"]],60);
  card(s,1010,165,222,145,"72 / 72","任务","18 疾病 × 4 桶",C.cyan);
  card(s,1010,330,222,145,"234 / 234","断言","trap 额外检查因果边界",C.green);
  shape(s,48,525,1184,95,C.pale,"roundRect",C.line);
  txt(s,"覆盖面",72,548,140,24,17,C.blue,true);
  txt(s,"正常路径 · 缺失上下文 · 冲突证据 · 因果陷阱",220,542,940,36,21,C.ink,true);
  txt(s,"疾病 Goldset 生成一致性检查也已通过。",220,582,940,24,14,C.muted);
  note(s,["reports/full_benchmark_20260808/raw/diseases/benchmark_report.json","benchmark/goldset_diseases.jsonl","configs/disease_library.yaml"]);
}

// 7 — disease runtime
if (MAX >= 7) {
  const s=base("疾病任务耗时稳定，首个 UC 上下文承担初始化成本","Disease runtime",7);
  const rt=metrics.disease_runtime;
  chartFrame(s,48,165,760,410);
  const cats=["平均","中位数","P95","最大值"];
  addChart(s,"bar",85,205,690,330,cats,[{name:"秒",values:[rt.mean_s,rt.median_s,rt.p95_s,rt.max_s],fill:C.cyan}],{
    barOptions:{direction:"column",grouping:"clustered",gapWidth:80},
    yAxis:{visible:true,deleted:false,min:0,max:2.7,majorUnit:0.5,majorGridlines:{style:"solid",width:1,fill:C.line},line:{style:"solid",width:0,fill:C.white},textStyle:{typeface:FONT,fontSize:11,color:C.ink}}
  });
  card(s,850,165,382,130,`${rt.mean_s.toFixed(3)} s`,`平均耗时`,`72 个疾病任务`,C.blue);
  card(s,850,315,382,130,`${rt.p95_s.toFixed(3)} s`,`P95`,`绝大多数任务集中在约 1.1–1.4 秒`,C.green);
  card(s,850,465,382,130,`${rt.max_s.toFixed(3)} s`,`最大值`,`UC 首轮初始化抬高长尾`,C.orange);
  note(s,["reports/full_benchmark_20260808/raw/diseases/benchmark_report.json"]);
}

// 8 — relation composition
if (MAX >= 8) {
  const s=base("关系评测：145 条样本按疾病隔离切分","Relation dataset",8);
  chartFrame(s,48,160,550,410); chartFrame(s,630,160,602,410);
  addChart(s,"bar",78,200,490,320,["train","validation","test"],[{name:"样本",values:[81,32,32],fill:C.blue}],{
    barOptions:{direction:"column",grouping:"clustered",gapWidth:80},
    yAxis:{visible:true,deleted:false,min:0,max:90,majorUnit:20,majorGridlines:{style:"solid",width:1,fill:C.line},line:{style:"solid",width:0,fill:C.white},textStyle:{typeface:FONT,fontSize:11,color:C.ink}}
  });
  addChart(s,"bar",660,200,542,320,["锚点","完整","缺失","错配"],[{name:"Gold 标签",values:[73,18,18,36],fill:C.green}],{
    barOptions:{direction:"column",grouping:"clustered",gapWidth:75},
    yAxis:{visible:true,deleted:false,min:0,max:80,majorUnit:20,majorGridlines:{style:"solid",width:1,fill:C.line},line:{style:"solid",width:0,fill:C.white},textStyle:{typeface:FONT,fontSize:11,color:C.ink}}
  });
  txt(s,"Split",72,174,120,24,15,C.muted,true); txt(s,"Gold 标签构成",654,174,180,24,15,C.muted,true);
  shape(s,48,600,1184,54,C.paleBlue,"roundRect");
  txt(s,"同一疾病的样本只进入一个 split，避免疾病级信息泄漏。",70,615,1140,24,16,C.blue,true);
  note(s,["benchmark/goldset_context_relations.jsonl","benchmark/benchmark_context_relations_summary.json","schemas/context_relation_case.schema.json"]);
}

// 9 — baseline vs oracle
if (MAX >= 9) {
  const s=base("上下文盲基线暴露了 37.2 个百分点的理解缺口","Relation metrics",9);
  chartFrame(s,48,160,820,430);
  addChart(s,"bar",78,200,760,350,["覆盖率","标签准确率","必要动作召回","禁用声明安全"],[
    {name:"上下文盲基线",values:[100,62.7586,62.7586,100],fill:C.orange},
    {name:"Gold / 评分器自检",values:[100,100,100,100],fill:C.green}],{
    hasLegend:true,legend:{position:"bottom",textStyle:{typeface:FONT,fontSize:12,color:C.ink}},
    barOptions:{direction:"column",grouping:"clustered",gapWidth:55},
    yAxis:{visible:true,deleted:false,min:0,max:110,majorUnit:20,majorGridlines:{style:"solid",width:1,fill:C.line},line:{style:"solid",width:0,fill:C.white},textStyle:{typeface:FONT,fontSize:11,color:C.ink}}
  });
  card(s,910,160,322,150,"62.8%","label / action","盲基线把所有上下文化样本都判为完整",C.orange);
  card(s,910,335,322,150,"100%","coverage / safety","无遗漏，也没有输出禁用声明",C.blue);
  txt(s,"Oracle = scorer 完整性检查，不是 Agent 性能。",910,525,310,64,16,C.green,true);
  note(s,["reports/full_benchmark_20260808/raw/relation_context_blind_report.json","reports/full_benchmark_20260808/raw/relation_oracle_report.json","benchmark/score_context_relations.py"]);
}

// 10 — scorecard
if (MAX >= 10) {
  const s=base("关键步骤记分卡：每个重要环节都有可追溯指标","Scorecard",10);
  grid(s,48,155,[250,430,210,250],[
    ["步骤","核心指标","结果","判定"],["Goldset 生成","字节一致 · Schema valid","145 / 145","通过"],
    ["任务规划与主链","BM-01 主链断言","11 / 11","通过"],["迁移兼容","Legacy / LangGraph parity","3 / 3","通过"],
    ["确定性与恢复","deterministic / resume","3 / 3","通过"],["鲁棒性","预算降级 + OOD","4 / 4","通过"],
    ["合同与 Schema","version / whitelist / export","3 / 3","通过"],["疾病泛化","18 疾病 × 4 桶","234 / 234","通过"],
    ["关系上下文","盲基线 label / action","62.8 / 62.8%","具有区分度"],["代码回归","pytest","81 pass / 4 skip","通过"]],46);
  txt(s,"通过 ≠ 已完成 live/GPU 验收；本页只汇总本地可复现质量信号。",48,640,1100,24,14,C.muted);
  note(s,["reports/full_benchmark_20260808/metrics_summary.json","reports/full_benchmark_20260808/raw/pytest.xml"]);
}

// 11 — next steps
if (MAX >= 11) {
  const s=base("下一步：把关系评测从 scorer 自检接到真实 Agent 输出","Next steps",11);
  const steps=[
    ["01","接入输出适配器","读取 Planner / Reviewer 的 relation label、required actions 与 forbidden claims。"],
    ["02","增强证据字段","为 disease-target anchor 增加 PMID/DOI、方向证据与 cell/tissue/stage source span。"],
    ["03","建设时间外推","加入 paper-level 与 2021–2024 / 2025 / 2026 time split，避免论文派生资产泄漏。"],
    ["04","补齐部署验收","在远端 Python 3.11 acceptance profile 与 Reviewer LoRA GPU profile 复跑。"]];
  steps.forEach((st,i)=>{ const y=155+i*115; shape(s,48,y,1184,92,C.light,"roundRect",C.line); txt(s,st[0],68,y+23,70,34,24,i===3?C.orange:C.blue,true); txt(s,st[1],155,y+16,280,28,18,C.ink,true); txt(s,st[2],155,y+48,1020,30,15,C.muted); });
  note(s,["reports/full_benchmark_20260808/Target_full_benchmark_report_20260808.docx","benchmark/README_context_relations.md"]);
}

// 12 — close
if (MAX >= 12) {
  const s=deck.slides.add(); s.background.fill=C.ink;
  txt(s,"TARGET · BENCHMARK HANDOFF",58,52,600,24,14,C.cyan,true);
  txt(s,"本地质量闭环已完成",58,170,900,70,46,C.white,true);
  txt(s,"主链与疾病矩阵保持全绿；关系评测已经能区分“识别锚点”和“理解上下文”。",58,275,900,72,22,"#D8E2F0");
  shape(s,58,410,1160,2,"#40516A");
  txt(s,"PR #12",58,455,240,44,30,C.cyan,true); txt(s,"报告与演示文稿均随分支更新",300,460,780,34,18,C.white);
  txt(s,"下一验收点：真实 Agent relation 输出 + Python 3.11 / live / GPU profiles",58,565,1120,36,18,"#AEBBD0");
  txt(s,"2026-08-08",1080,665,138,18,12,"#8998AE",false,"right");
  note(s,["https://github.com/ocean-debug/Target/pull/12","reports/full_benchmark_20260808/Target_full_benchmark_report_20260808.docx"]);
}

if (process.env.DEBUG_LAYOUT === "1") {
  for (const [index, slide] of deck.slides.items.entries()) {
    const layout = await slide.export({ format: "layout" });
    await fs.writeFile(path.join(REPORT, `debug-slide-${index+1}.layout.json`), await layout.text());
  }
}
const pptx = await PresentationFile.exportPptx(deck);
await pptx.save(OUT);
await fs.rm(`${OUT}.inspect.ndjson`, { force: true });
console.log(OUT);
