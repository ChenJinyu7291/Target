import fs from "node:fs/promises";
import path from "node:path";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const ROOT = process.env.TARGET_REPO || "";
if (!ROOT) {
  throw new Error("TARGET_REPO must point to the Target repository root when building this report");
}
const REPORT = path.join(ROOT, "reports/full_benchmark_20260808");
const FIG = path.join(REPORT, "figures");
const OUT = path.join(REPORT, "Target_project_full_visualization_20260808.pptx");
const m = JSON.parse(await fs.readFile(path.join(REPORT, "project_metrics.json"), "utf8"));
const bm = JSON.parse(await fs.readFile(path.join(REPORT, "metrics_summary.json"), "utf8"));

const W=1280,H=720,FONT="Microsoft YaHei";
const C={ink:"#142238",muted:"#657287",blue:"#2F75B5",bright:"#3F8EF7",cyan:"#67C2E8",green:"#2F8F5B",orange:"#DD7A1F",yellow:"#DDAA25",pale:"#EEF4FA",light:"#F7F9FC",line:"#D7DFE9",white:"#FFFFFF",red:"#C84C4C"};
const deck=Presentation.create({slideSize:{width:W,height:H}});

function shape(s,x,y,w,h,fill,geometry="rect",line="none",radius="rounded-xl"){
  return s.shapes.add({geometry,position:{left:x,top:y,width:w,height:h},fill,line:{style:"solid",fill:line,width:line==="none"?0:1},borderRadius:radius});
}
function txt(s,t,x,y,w,h,size=20,color=C.ink,bold=false,align="left",valign="top"){
  const z=s.shapes.add({geometry:"textbox",position:{left:x,top:y,width:w,height:h},fill:"none",line:{style:"solid",fill:"none",width:0}});
  z.text=String(t); z.text.style={fontSize:size,typeface:FONT,color,bold,alignment:align,verticalAlignment:valign,autoFit:"shrinkText",insets:{top:0,right:0,bottom:0,left:0}}; return z;
}
function base(title,kicker,n){const s=deck.slides.add();s.background.fill=C.white;txt(s,kicker.toUpperCase(),48,26,600,20,12,C.blue,true);txt(s,title,48,55,1160,58,34,C.ink,true);shape(s,48,123,1184,2,C.line);txt(s,String(n).padStart(2,"0"),1178,682,54,18,11,C.muted,false,"right");return s;}
function note(s,sources,extra=""){s.speakerNotes.textFrame.setText(`${extra}${extra?"\n\n":""}[Sources]\n${sources.map(x=>`- ${x}`).join("\n")}\n[/Sources]`);}
function card(s,x,y,w,h,value,label,detail,accent=C.blue){shape(s,x,y,w,h,C.light,"roundRect",C.line);shape(s,x,y,8,h,accent,"roundRect");txt(s,value,x+26,y+16,w-44,44,32,accent,true);txt(s,label,x+26,y+62,w-44,28,17,C.ink,true);txt(s,detail,x+26,y+98,w-44,h-108,13,C.muted);}
async function img(s,file,x,y,w,h,alt){const b=await fs.readFile(path.join(FIG,file));s.images.add({blob:b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength),contentType:"image/png",alt,fit:"contain",position:{left:x,top:y,width:w,height:h}});}
function callout(s,text,x,y,w,h,accent=C.orange){shape(s,x,y,w,h,C.light,"roundRect",C.line);shape(s,x,y,7,h,accent,"roundRect");txt(s,text,x+24,y+16,w-40,h-28,16,C.ink,true,"left","middle");}

// 1 cover
{
  const s=deck.slides.add();s.background.fill=C.white;shape(s,0,0,18,H,C.blue);shape(s,885,0,395,H,C.pale);
  txt(s,"TARGET · PROJECT PANORAMA",62,48,690,24,14,C.blue,true);
  txt(s,"Target 项目全景\n数据可视化",62,145,720,130,54,C.ink,true);
  txt(s,"架构、疾病上下文、数据资产、Agent 运行、Reviewer、排序、训练对齐与完整评测",62,320,730,74,23,C.muted);
  card(s,915,88,315,132,String(m.inventory.diseases),"疾病","13 个组织 · 14 类细胞",C.blue);
  card(s,915,238,315,132,String(m.inventory.tools),"工具","13 启用 · 3 个兼容插件关闭",C.cyan);
  card(s,915,388,315,132,String(m.inventory.archived_runs),"归档运行","全部 completed_with_gaps",C.orange);
  card(s,915,538,315,110,String(m.inventory.alignment_rows),"训练/验收样本","均为高风险 Reviewer 场景",C.green);
  txt(s,"2026-08-08 · Local project audit",62,650,650,22,14,C.muted);
  note(s,["reports/full_benchmark_20260808/project_metrics.json","reports/full_benchmark_20260808/metrics_summary.json"],"项目全景汇报；归档矩阵是 fake/unit 回放，不是实时生物学验证。 ");
}

// 2 product
{
  const s=base("Target 是受契约约束的疾病驱动靶点发现 Agent","Product",2);
  const nodes=[["输入","疾病、组织、细胞、时期"],["规划","TaskSpec 与分析计划"],["证据","组学、遗传、文献、试验"],["审查","Reviewer 与科学边界"],["输出","六维排序、TargetCards、报告"]];
  nodes.forEach((d,i)=>{const x=48+i*238;shape(s,x,190,202,150,i===4?C.paleBlue:C.light,"roundRect",C.line);txt(s,String(i+1).padStart(2,"0"),x+18,208,36,22,13,C.blue,true);txt(s,d[0],x+18,245,165,28,20,C.ink,true);txt(s,d[1],x+18,286,165,50,14,C.muted);if(i<4){txt(s,"→",x+207,244,28,30,24,C.blue,true,"center");}});
  callout(s,"产品价值不只是给出一张靶点榜单，而是保留上下文、证据 provenance、Reviewer finding 和可追溯报告。",48,410,1184,100,C.green);
  txt(s,"当前工程边界",48,555,220,26,18,C.orange,true);txt(s,"30 次工具调用 · 20 个初始候选 · 2 轮 Review · 上下文匹配分 < 0.5 不进入正式排名",265,552,950,34,18,C.ink,true);
  note(s,["README.md","PROJECT_CHARTER.md","configs/workflow.yaml"]);
}

// 3 architecture
{
  const s=base("九状态工作流把输入、工具、证据和报告串成可追溯闭环","Architecture",3);
  const states=m.workflow.states;
  states.forEach((v,i)=>{const row=i<5?0:1, col=row===0?i:i-5;const count=row===0?5:4;const gap=row===0?18:24;const ww=row===0?220:273;const x=48+col*(ww+gap), y=row===0?170:370;shape(s,x,y,ww,100,i===5?C.paleBlue:C.light,"roundRect",C.line);txt(s,String(i+1),x+16,y+15,28,20,12,C.blue,true);txt(s,v,x+16,y+45,ww-32,28,17,C.ink,true,"center");if(col<count-1)txt(s,"→",x+ww+2,y+40,gap+14,26,20,C.blue,true,"center");});
  callout(s,"终态：completed / completed_with_gaps / needs_input / refused / failed。归档矩阵只出现 completed_with_gaps。",48,545,1184,82,C.orange);
  note(s,["configs/workflow.yaml","src/target_agent/runtime.py","src/target_agent/runtime_langgraph.py"]);
}

// 4 inventory
{
  const s=base("项目资产不止 benchmark：数据、契约、训练和运行产物共同构成主体","Inventory",4);await img(s,"07_project_inventory.png",48,150,820,475,"项目资产规模柱状图");
  card(s,900,160,332,130,"17","Schema","任务、工具、证据、审查、排序、报告",C.cyan);
  card(s,900,310,332,130,"73","参考靶点","用于 sanity check，不代表新发现",C.green);
  card(s,900,460,332,130,"72","归档回放","每次都有状态、排序、findings 和报告",C.orange);
  note(s,["reports/full_benchmark_20260808/project_metrics.json","schemas/","configs/disease_library.yaml","runs_archive/matrix_full/entries/"]);
}

// 5 context
{
  const s=base("疾病库已覆盖五类疾病，但组织和细胞上下文仍呈长尾分布","Disease context",5);await img(s,"08_disease_context_landscape.png",35,145,1210,480,"疾病、组织和细胞类型覆盖");
  callout(s,"18 个疾病 · 13 个组织 · 14 类细胞；自身免疫疾病最多，肺、上皮细胞覆盖相对集中。",75,625,1130,45,C.blue);
  note(s,["configs/disease_library.yaml"]);
}

// 6 reference
{
  const s=base("参考靶点以已批准药物和遗传证据为主，适合回归但带有锚点偏向","Reference targets",6);await img(s,"09_reference_evidence_and_genes.png",48,150,880,465,"参考证据等级与重复基因");
  card(s,960,170,272,125,"42 / 73","approved_drug","最大证据来源",C.green);
  card(s,960,315,272,125,"14 + 10","GWAS + Mendelian","遗传证据合计 24",C.blue);
  card(s,960,460,272,125,"TNF × 4","重复最多","频次不是跨疾病验证",C.orange);
  note(s,["configs/disease_library.yaml"]);
}

// 7 tools
{
  const s=base("16 个工具中 13 个启用；真实证据链依赖严格 registry 与输入审计","Tools & gates",7);
  const groups=[
    ["发现与审计","disease_resolver\ngeo_search\ngeo_metadata_audit",C.blue],
    ["组学分析","omics_recipe_builder\nbulk_expression_analysis\nsingle_cell_analysis",C.cyan],
    ["候选与通路","pathway_enrichment\nomics_candidate_extraction\ncellxgene_discovery",C.green],
    ["外部证据","open_targets\neurope_pmc_rag\nclinical_trials_gov",C.orange],
  ];
  groups.forEach((g,i)=>{const x=48+i*296;shape(s,x,170,270,270,C.light,"roundRect",C.line);shape(s,x,170,270,12,g[2],"roundRect");txt(s,g[0],x+20,205,230,28,20,C.ink,true);txt(s,g[1],x+20,255,230,140,15,C.muted);});
  callout(s,"关闭的 3 个兼容插件：uc_omics_snapshot、observed_tcell_perturbation、deltafactor。关闭不等于能力已被真实工具替代。",48,485,1184,88,C.orange);
  txt(s,"必需工具 11 · 工作流上限 30 次调用 · registry 外工具禁止运行",48,610,1184,28,18,C.blue,true,"center");
  note(s,["configs/tools.yaml","configs/workflow.yaml","src/target_agent/tools/"]);
}

// 8 code contracts
{
  const s=base("代码主体集中在 src；ToolResult 和 DatasetCandidate 是最复杂的契约","Code & contracts",8);await img(s,"10_code_and_contract_footprint.png",40,145,930,475,"代码与 schema 复杂度");
  card(s,995,165,237,120,"8,320","Python 非空行","最大文件 omics.py：1,695 行",C.blue);
  card(s,995,305,237,120,"82","测试函数","执行用例因参数化为 85",C.green);
  card(s,995,445,237,120,"22","ToolResult 属性","契约复杂度最高",C.cyan);
  note(s,["reports/full_benchmark_20260808/project_metrics.json","src/","schemas/","tests/"]);
}

// 9 UC omics
{
  const s=base("UC 候选组学同时显示效应方向、显著性与细胞类型","Derived biology · UC",9);await img(s,"12_uc_candidate_omics.png",48,145,860,485,"UC 候选基因散点图");
  card(s,940,165,292,125,"20","候选基因","GSE125527 donor pseudobulk",C.blue);
  card(s,940,310,292,125,"5","与扰动屏重叠","IL2 / TAGAP / CD27 / FOSB / GATA3",C.orange);
  callout(s,"差异表达是观察性证据，不能直接解释为因果靶点。",940,470,292,110,C.orange);
  note(s,["data/derived/uc_candidates.json","https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE125527"]);
}

// 10 perturbation
{
  const s=base("CRISPRa 扰动屏提供方向性信号，但疾病一致性仍是相关指标","Derived biology · perturbation",10);await img(s,"13_uc_perturbation_landscape.png",48,145,870,485,"扰动激活与疾病一致性");
  card(s,950,165,282,125,"71","扰动靶点","primary human T-cell CRISPRa",C.green);
  card(s,950,310,282,125,"CD27","最高正一致性","alignment ≈ 0.092",C.blue);
  callout(s,"CRISPRa 测量激活而非抑制；供体在迁移摘要中被合并。",950,470,282,110,C.orange);
  note(s,["data/derived/uc_perturbation.json","https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE190604"]);
}

// 11 MCH
{
  const s=base("MCH/K562 是当前唯一明确的 causal gold 配置","Causal gold",11);await img(s,"18_mch_replication.png",48,155,720,455,"MCH 论文与项目复现准确率");
  card(s,810,165,422,130,"43 / 59","Nature 论文","方向预测准确率 72.9%",C.green);
  card(s,810,315,422,130,"94 / 147","项目扩展复现","方向预测准确率 63.9%",C.blue);
  callout(s,"置换 p = 0.00019998；两者 hit set 不同，94/147 不能写成论文准确率复现。K562 也不是 UC 组织模型。",810,480,422,115,C.orange);
  note(s,["nature_framework/results/fig3a_summary.json","nature_framework/results/mch_sign_prediction.csv","https://www.nature.com/articles/s41586-025-09866-3"]);
}

// 12 archive completeness
{
  const s=base("72 个归档运行的产物合同完整，但终态全部带缺口","Archived runs",12);await img(s,"14_archive_pipeline_completeness.png",48,145,810,460,"归档产物完整性");
  card(s,890,160,342,125,"72 / 72","产物完整","status · ranking · findings · report",C.green);
  card(s,890,305,342,125,"12","每运行工具调用","每次 10 个排名、5 张卡片",C.blue);
  card(s,890,450,342,125,"72 / 72","completed_with_gaps","流程跑完不等于证据闭环",C.orange);
  note(s,["runs_archive/matrix_full/entries/","reports/full_benchmark_20260808/project_metrics.json"],"归档矩阵是 stored fake/unit replay。 ");
}

// 13 reviewer
{
  const s=base("Reviewer 的主要问题不是因果越界，而是数据资格、上下文和覆盖缺口","Reviewer findings",13);await img(s,"16_reviewer_findings.png",45,145,890,470,"Reviewer findings 类别与严重程度");
  card(s,965,160,267,115,"1,403","finding 总量","全部 resolved=false",C.orange);
  card(s,965,295,267,115,"797","major","占 56.8%",C.red);
  card(s,965,430,267,115,"502","dataset ineligibility","最大类别",C.blue);
  callout(s,"平均每运行 19.5 条；归档无闭环状态迁移",965,565,267,75,C.orange);
  note(s,["runs_archive/matrix_full/entries/*/reviewer_findings.jsonl"]);
}

// 14 ranking
{
  const s=base("六维排序在归档矩阵中明显失衡：三维平均分为零","Ranking",14);await img(s,"15_ranking_dimensions_and_decisions.png",40,145,910,470,"排序维度均分与决策分布");
  card(s,980,160,252,115,"18.37","human genetics","主导当前 fake/unit 排名",C.blue);
  card(s,980,295,252,115,"0 / 0 / 0","三维缺失","omics · perturbation · safety",C.orange);
  card(s,980,430,252,115,"350","GO","另有 234 insufficient evidence",C.green);
  callout(s,"有 GO 决策不等于六维证据已完整。",980,565,252,70,C.orange);
  note(s,["runs_archive/matrix_full/entries/*/ranked_targets.json"]);
}

// 15 genes
{
  const s=base("高频进入 Top-10 的基因反映固定回放资产，而非生物学验证","Archived ranking",15);await img(s,"17_archive_top_ranked_genes.png",65,145,850,485,"归档 Top-10 高频基因");
  card(s,950,170,282,125,"16 次","IL12B / IL23R / TYK2","归档出现频次最高",C.blue);
  card(s,950,320,282,125,"12 次","IL10 / CTLA4 / SH2B3","第二梯队",C.cyan);
  callout(s,"不要把归档频次解释成跨疾病生物学有效性。",950,485,282,105,C.orange);
  note(s,["runs_archive/matrix_full/entries/*/ranked_targets.json"]);
}

// 16 alignment
{
  const s=base("Reviewer 对齐数据覆盖六类高风险场景，并保持严格均衡","Alignment data",16);await img(s,"11_alignment_data_composition.png",48,145,850,480,"Reviewer 对齐数据构成");
  card(s,930,160,302,120,"120","SFT","每类 20",C.blue);
  card(s,930,300,302,120,"60","Preference","每类 10",C.cyan);
  card(s,930,440,302,120,"30","Heldout","每类 5",C.yellow);
  callout(s,"全部为 high-risk；manifest 明确禁止自动训练。",930,585,302,62,C.orange);
  note(s,["alignment_data/manifest.json","alignment_data/sft.jsonl","alignment_data/preferences.jsonl","alignment_data/heldout.jsonl"]);
}

// 17 benchmark
{
  const s=base("完整本地回归全绿，但它验证的是 fake/unit 链路与契约","Benchmark",17);
  card(s,48,165,270,160,"81 / 81","pytest","另有 4 skip · 0 fail",C.blue);
  card(s,344,165,270,160,"11 / 11","主 Benchmark","27 / 27 断言",C.green);
  card(s,640,165,270,160,"72 / 72","疾病矩阵","234 / 234 断言",C.cyan);
  card(s,936,165,296,160,"145","关系样本","跨疾病 split",C.orange);
  callout(s,"本机 Python 3.12.13；正式 acceptance runtime 为 Python 3.11。live API 与 Reviewer LoRA GPU profile 未在本地执行。",48,385,1184,90,C.orange);
  txt(s,"绿色 benchmark ≠ 项目级证据已闭环",48,535,1184,46,30,C.ink,true,"center");
  txt(s,"归档运行的 completed_with_gaps、零分维度和未解决 findings 是同等重要的质量信号。",100,595,1080,34,18,C.muted,false,"center");
  note(s,["reports/full_benchmark_20260808/raw/pytest.xml","reports/full_benchmark_20260808/raw/main/benchmark_report.json","reports/full_benchmark_20260808/raw/diseases/benchmark_report.json"]);
}

// 18 relation
{
  const s=base("关系评测显示：识别靶点不等于理解组织、细胞与时期上下文","Context relations",18);
  card(s,48,165,350,170,"62.8%","上下文盲基线","标签准确率与必要动作召回",C.orange);
  card(s,430,165,350,170,"100%","覆盖 / 安全","无遗漏、无禁用声明",C.blue);
  card(s,812,165,420,170,"100%","Gold / scorer 自检","只验证样本和评分器对齐",C.green);
  shape(s,48,385,1184,150,C.light,"roundRect",C.line);
  txt(s,"中央结论",76,415,170,26,18,C.blue,true);txt(s,"37.2 个百分点的差距来自缺失/错配上下文：盲基线能复述疾病靶点，却不能可靠决定应追问、降级还是拒绝因果表述。",245,405,930,72,20,C.ink,true);
  txt(s,"Oracle 不是 Agent 性能。",245,495,930,26,16,C.orange,true);
  note(s,["benchmark/goldset_context_relations.jsonl","reports/full_benchmark_20260808/raw/relation_context_blind_report.json","reports/full_benchmark_20260808/raw/relation_oracle_report.json"]);
}

// 19 synthesis
{
  const s=base("项目级结论：工程闭环存在，科学证据闭环仍需补齐","Synthesis",19);
  const rows=[
    ["已经成立","9 状态工作流、16 工具、17 Schema、TargetCards 与报告产物","#2F8F5B"],
    ["数据基础","18 疾病、13 组织、14 类细胞；UC 组学/扰动与 MCH causal gold","#2F75B5"],
    ["主要缺口","72/72 completed_with_gaps；1,403 findings 未解决","#DD7A1F"],
    ["排序风险","omics、perturbation、safety 三维在归档矩阵中均为 0","#C84C4C"],
  ];
  rows.forEach((r,i)=>{const y=155+i*112;shape(s,48,y,1184,88,C.light,"roundRect",C.line);shape(s,48,y,12,88,r[2],"roundRect");txt(s,r[0],82,y+18,175,28,20,r[2],true);txt(s,r[1],265,y+17,925,48,18,C.ink,true,"left","middle");});
  callout(s,"benchmark 全绿与 archive 全部 completed_with_gaps 必须同时解读：前者证明链路可回归，后者暴露真实项目缺口。",48,620,1184,52,C.orange);
  note(s,["reports/full_benchmark_20260808/project_metrics.json","reports/full_benchmark_20260808/metrics_summary.json"]);
}

// 20 priorities
{
  const s=base("下一步优先级：先让缺口可关闭，再扩大真实数据证据","Priorities",20);
  const items=[
    ["P0","Reviewer 闭环","为 finding 增加 owner、resolution、复核与终态迁移",C.red],
    ["P0","真实六维排序","让 omics、perturbation、safety 的 live 证据进入排名",C.orange],
    ["P1","上下文评测接 Agent","报告真实 Planner relation label、actions 与 forbidden claims",C.blue],
    ["P1","发布验收","Python 3.11 + live API + Reviewer LoRA GPU profile",C.green],
  ];
  items.forEach((it,i)=>{const y=155+i*112;shape(s,48,y,1184,88,C.light,"roundRect",C.line);shape(s,70,y+20,62,48,it[3],"roundRect");txt(s,it[0],70,y+31,62,24,16,C.white,true,"center");txt(s,it[1],165,y+18,250,28,20,C.ink,true);txt(s,it[2],420,y+18,770,48,17,C.muted,false,"left","middle");});
  txt(s,"目标：从“产物完整”推进到“证据完整、缺口可关闭、结论可审计”。",48,620,1184,34,22,C.blue,true,"center");
  note(s,["reports/full_benchmark_20260808/Target_project_full_visualization_report_20260808.docx","DECISION_LOG.md"]);
}

const pptx=await PresentationFile.exportPptx(deck);
await pptx.save(OUT);
console.log(OUT);
