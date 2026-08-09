"""Read-only share portal: one offline HTML review view of a project.

The portal is a review view, not a live system:
- it is rendered from the durable project ledger (safe projection) and from
  an optional bounded preview of report/brief artifacts;
- it never executes tools, never calls the network and loads no external
  scripts or styles;
- secrets, absolute paths, session raw logs and internal tool run ids are
  scrubbed before rendering;
- the page carries a canonical snapshot fingerprint so two renderings of the
  same ledger state are verifiably identical.

Authoritative sources remain the project ledger and the exported zip package;
this page is a human-readable mirror of that state at one point in time.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .research_store import ResearchProjectStore

_BLOCKED_KEYS = frozenset({
    "tool_run_id", "event_id", "job_id", "run_dir", "cache_dir",
    "absolute_path", "password", "authorization", "api_key",
    "step_api_key", "openai_api_key", "ncbi_api_key", "secret", "token",
})

_ABS_PATH_RE = re.compile(
    r"(?i)((?<![a-z0-9])[a-z]:[\\/]"
    r"|(?<![a-z0-9])/(?:home|root|users|tmp|var|etc|opt|srv|data)/"
    r"|(?<![a-z0-9])\\\\)"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_SSH_PUB_RE = re.compile(r"ssh-(?:ed25519|rsa|ecdsa)\s+[A-Za-z0-9+/=]+")
_KEY_VALUE_RE = re.compile(
    r"(?i)((?:api[_-]?key|secret|password|token|authorization|credential)"
    r"[^\n]{0,20}[:=]\s*)[A-Za-z0-9_\-./+]{8,}"
)

PREVIEW_LOGICAL_NAMES = frozenset({"project_brief", "research_report"})
_EVENT_LIMIT = 200
_PREVIEW_DEFAULT_BYTES = 65536
_OUTPUT_JSON_LIMIT = 20000


def _blocked_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _BLOCKED_KEYS or lowered.endswith(("_key", "_token", "_secret"))


def _scrub_string(value: str) -> str:
    value = _ABS_PATH_RE.sub("[redacted path]", value)
    value = _EMAIL_RE.sub("[email redacted]", value)
    value = _IP_RE.sub("[ip redacted]", value)
    value = _SSH_PUB_RE.sub("ssh-public-key [redacted]", value)
    value = _KEY_VALUE_RE.sub(r"\1[redacted]", value)
    return value


def _scrub_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_value(item)
            for key, item in value.items()
            if not _blocked_key(str(key))
        }
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def _artifact_previews(
    store: ResearchProjectStore,
    records: list[Any],
    max_bytes: int,
) -> dict[str, dict[str, Any]]:
    previews: dict[str, dict[str, Any]] = {}
    for record in records:
        logical_name = getattr(record, "logical_name", None)
        if logical_name not in PREVIEW_LOGICAL_NAMES:
            continue
        try:
            path = store.artifact_path(record)
        except Exception:
            continue
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        truncated = len(raw) > max_bytes
        text = raw[:max_bytes].decode("utf-8", errors="replace")
        previews[logical_name] = {
            "artifact_id": record.artifact_id,
            "logical_name": logical_name,
            "text": text,
            "truncated": truncated,
        }
    return previews


def build_portal_payload(
    snapshot: dict[str, Any],
    *,
    previews: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    spec = snapshot.get("spec") or {}
    state = snapshot.get("state") or {}
    plan = snapshot.get("plan") or {}
    events = snapshot.get("events") or []
    payload = {
        "project_id": spec.get("project_id"),
        "title": spec.get("title"),
        "domain": spec.get("domain"),
        "autonomy_mode": spec.get("autonomy_mode"),
        "workflow_template": spec.get("workflow_template"),
        "status": state.get("status") or "draft",
        "checkpoint_kind": state.get("checkpoint_kind"),
        "terminal_reason": state.get("terminal_reason"),
        "updated_at": state.get("updated_at") or spec.get("created_at"),
        "goal": spec.get("goal"),
        "context": spec.get("context"),
        "plan": {
            "items": plan.get("items") or [],
            "planner_backend": plan.get("planner_backend"),
            "rationale": plan.get("rationale"),
            "revisions": snapshot.get("plan_revisions") or [],
            "branches": snapshot.get("plan_branches") or [],
        },
        "work_item_results": snapshot.get("work_item_results") or [],
        "assessments": snapshot.get("assessments") or [],
        "events": events[-_EVENT_LIMIT:],
        "decisions": snapshot.get("decisions") or [],
        "repair_requests": snapshot.get("repair_requests") or [],
        "repair_resolutions": snapshot.get("repair_resolutions") or [],
        "review_targets": snapshot.get("review_targets") or [],
        "artifacts": snapshot.get("artifacts") or [],
        "artifact_versions": snapshot.get("artifact_versions") or [],
        "domain_stage_summary": snapshot.get("domain_stage_summary") or {},
        "next_actions": snapshot.get("next_actions") or [],
        "active_work_item_ids": snapshot.get("active_work_item_ids") or [],
        "active_artifact_ids": snapshot.get("active_artifact_ids") or [],
        "release_snapshot_digest": snapshot.get("release_snapshot_digest"),
        "previews": previews or {},
    }
    scrubbed = _scrub_value(payload)
    fingerprint = hashlib.sha256(
        json.dumps(scrubbed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    scrubbed["_portal_fingerprint"] = fingerprint
    scrubbed["generated_at"] = generated_at or datetime.now(timezone.utc).isoformat()
    return scrubbed


def _data_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")


def _html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_PORTAL_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>__PORTAL_TITLE__ · TargetDiscovery 只读审查视图</title>
<style>
:root{--bg:#07111f;--panel:#10243a;--line:#29435f;--text:#f3f7fb;--muted:#9eb0c4;--blue:#58a6ff;--cyan:#3dd9c4;--green:#57d68d;--amber:#ffca68;--red:#ff7185}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(180deg,#07111f,#091725);color:var(--text);font:15px/1.6 Inter,"Noto Sans SC","Microsoft YaHei","Segoe UI",sans-serif}
header{padding:30px 36px 22px;border-bottom:1px solid var(--line);background:#0a1a2b}
.brand{color:var(--cyan);font-weight:800;letter-spacing:.08em}
h1{font-size:25px;margin:10px 0 8px}
.meta{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:8px 20px}
.badge{display:inline-block;padding:2px 11px;border-radius:999px;border:1px solid var(--line);font-size:12px;color:var(--muted)}
.badge.done{color:var(--green);border-color:var(--green)}
.badge.warn{color:var(--amber);border-color:var(--amber)}
.badge.bad{color:var(--red);border-color:var(--red)}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#c5d0dc}
main{max-width:1180px;margin:0 auto;padding:24px 36px 56px}
section{background:rgba(16,36,58,.86);border:1px solid var(--line);border-radius:16px;padding:22px 24px;margin:18px 0;box-shadow:0 18px 45px rgba(0,0,0,.2)}
h2{font-size:18px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
ul.sub{padding-left:20px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:700}
.status{padding:1px 9px;border-radius:999px;font-size:12px;border:1px solid var(--line);white-space:nowrap}
.status.done{color:var(--green);border-color:var(--green)}
.status.warn{color:var(--amber);border-color:var(--amber)}
.status.bad{color:var(--red);border-color:var(--red)}
.card{border:1px solid var(--line);border-radius:12px;padding:13px 15px;margin:10px 0;background:#0b1d30}
.card b{display:block;margin-bottom:5px}
pre{background:#08141f;border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}
details{border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-top:10px;background:#0b1d30}
summary{cursor:pointer;color:var(--blue);font-weight:700}
.muted{color:var(--muted)}
.danger{color:var(--red)}
.toolbar{margin-bottom:12px}
.toolbar input{background:#08141f;border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 13px;min-width:280px}
footer{max-width:1180px;margin:0 auto;padding:0 36px 44px;color:var(--muted);font-size:12px}
@media (max-width:760px){main,footer{padding:16px}h1{font-size:20px}table{font-size:12px}}
</style>
</head>
<body>
<header>
  <div class="brand">TargetDiscovery Agent · 只读审查视图</div>
  <h1 id="portal-title">__PORTAL_TITLE__</h1>
  <div class="meta">
    <span id="m-project"></span><span id="m-status" class="badge"></span>
    <span id="m-domain"></span><span id="m-autonomy"></span>
    <span id="m-updated"></span><span id="m-generated"></span>
  </div>
  <div class="meta" style="margin-top:10px">快照指纹 <code id="m-fingerprint"></code></div>
</header>
<main>
  <section>
    <h2>数据边界</h2>
    <p class="sub" id="notice-text"></p>
    <ul class="sub">
      <li>本页是项目账本的只读审查视图，不是实时运行界面。</li>
      <li>结论可回链到执行计划、工作项结果、评估、决策与产物清单；完整可移植副本请使用导出项目包。</li>
      <li>密钥、绝对路径、会话原始消息与工具运行内部 ID 已从本页移除。</li>
    </ul>
  </section>

  <section>
    <h2>01 · 研究问题与上下文</h2>
    <p class="sub" id="q-question"></p>
    <div class="card"><b>交付物</b><ul id="q-deliverables" class="sub"></ul></div>
    <div class="card"><b>成功标准</b><ul id="q-criteria" class="sub"></ul></div>
    <div class="card" id="q-constraints-wrap"><b>约束</b><ul id="q-constraints" class="sub"></ul></div>
    <details><summary>输入上下文（JSON）</summary><div id="q-context"></div></details>
  </section>

  <section>
    <h2>02 · 执行计划</h2>
    <p class="sub">Planner：<span id="planner-backend"></span>；计划理由：<span id="plan-rationale"></span></p>
    <div class="toolbar"><input id="filter" type="search" placeholder="过滤计划、结果、事件、产物…"></div>
    <table>
      <thead><tr><th>必需</th><th>工作项</th><th>标题</th><th>模块</th><th>依赖</th><th>验收标准</th></tr></thead>
      <tbody id="plan-rows"></tbody>
    </table>
    <h2 style="margin-top:20px">计划修订</h2>
    <p class="sub" id="revision-count"></p>
    <table><thead><tr><th>版本</th><th>操作</th><th>取代工作项</th><th>时间</th></tr></thead><tbody id="revision-rows"></tbody></table>
    <h2 style="margin-top:20px">回退分支</h2>
    <table><thead><tr><th>分支</th><th>回退点</th><th>状态</th><th>理由</th></tr></thead><tbody id="branch-rows"></tbody></table>
  </section>

  <section>
    <h2>03 · 工作项结果与证据</h2>
    <p class="sub" id="result-count"></p>
    <div id="result-cards"></div>
    <h2 style="margin-top:20px">评估记录</h2>
    <table><thead><tr><th>目标</th><th>维度</th><th>等级</th><th>结果</th><th>执行者</th><th>理由</th></tr></thead><tbody id="assessment-rows"></tbody></table>
  </section>

  <section>
    <h2>04 · 事件时间线</h2>
    <p class="sub" id="event-count"></p>
    <table><thead><tr><th>序号</th><th>类型</th><th>工作项</th><th>事件</th><th>时间</th></tr></thead><tbody id="event-rows"></tbody></table>
  </section>

  <section>
    <h2>05 · 决策记录</h2>
    <table><thead><tr><th>决策</th><th>动作</th><th>执行者</th><th>目标</th><th>理由</th><th>时间</th></tr></thead><tbody id="decision-rows"></tbody></table>
  </section>

  <section>
    <h2>06 · 产物清单与报告预览</h2>
    <p class="sub" id="artifact-count"></p>
    <table><thead><tr><th>产物</th><th>逻辑名</th><th>类型</th><th>大小</th><th>SHA-256 前缀</th><th>状态</th></tr></thead><tbody id="artifact-rows"></tbody></table>
    <div id="preview-rows"></div>
  </section>

  <section>
    <h2>07 · 审查边界与证据缺口</h2>
    <p class="sub" id="terminal-reason"></p>
    <p class="sub" id="gap-count"></p>
    <table><thead><tr><th>工作项</th><th>状态</th><th>摘要</th></tr></thead><tbody id="gap-rows"></tbody></table>
    <h2 style="margin-top:20px">待处理修复请求</h2>
    <p class="sub" id="repair-count"></p>
    <table><thead><tr><th>请求</th><th>目标工作项</th><th>失败类别</th><th>动作</th><th>理由</th></tr></thead><tbody id="repair-rows"></tbody></table>
    <h2 style="margin-top:20px">评审目标</h2>
    <table><thead><tr><th>目标</th><th>工作项</th><th>状态</th><th>理由</th></tr></thead><tbody id="review-rows"></tbody></table>
    <h2 style="margin-top:20px">待办动作</h2>
    <table><thead><tr><th>动作</th><th>目标</th><th>理由</th></tr></thead><tbody id="next-rows"></tbody></table>
  </section>
</main>
<footer>TargetDiscovery Agent · 只读审查视图 · 快照指纹 <code id="foot-fingerprint"></code></footer>
<script>
'use strict';
const PORTAL_DATA = __PORTAL_DATA__;
function $(id){return document.getElementById(id);}
function esc(v){return String(v==null?'':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function text(id,v){const el=$(id);if(el && v!=null)el.textContent=v;}
function statusCls(s){s=String(s||'').toLowerCase();if(/completed|resolved|approved|success|released|applied/.test(s))return 'done';if(/failed|rejected|refused|blocked|error|cancelled/.test(s))return 'bad';return 'warn';}
function rows(items,fn){return (items||[]).map(fn).join('');}
function jsonPre(value){return '<pre>'+esc(JSON.stringify(value,null,2))+'</pre>';}
text('portal-title', PORTAL_DATA.title || PORTAL_DATA.project_id);
text('m-project', '项目：' + (PORTAL_DATA.project_id||''));
text('m-domain', '领域：' + (PORTAL_DATA.domain||''));
text('m-autonomy', '模式：' + (PORTAL_DATA.autonomy_mode||''));
text('m-updated', '账本更新：' + (PORTAL_DATA.updated_at||''));
text('m-generated', '生成时间：' + (PORTAL_DATA.generated_at||''));
text('m-fingerprint', PORTAL_DATA._portal_fingerprint||'');
text('foot-fingerprint', PORTAL_DATA._portal_fingerprint||'');
const st=$('m-status');
st.textContent='状态：'+(PORTAL_DATA.status||'');
st.className='badge '+statusCls(PORTAL_DATA.status);
text('notice-text', '本页由 TargetDiscovery 于 '+(PORTAL_DATA.generated_at||'')+' 生成，数据截至该时刻；权威来源是项目账本（'+(PORTAL_DATA.project_id||'')+'）。完整可移植副本请使用 target-agent project-export。');
const goal=PORTAL_DATA.goal||{};
text('q-question', goal.question||'');
$('q-deliverables').innerHTML=rows(goal.deliverables,x=>'<li>'+esc(x)+'</li>');
$('q-criteria').innerHTML=rows(goal.success_criteria,x=>'<li>'+esc(x)+'</li>');
if(goal.constraints && goal.constraints.length){$('q-constraints').innerHTML=rows(goal.constraints,x=>'<li>'+esc(x)+'</li>');}else{$('q-constraints-wrap').classList.add('hidden');}
$('q-context').innerHTML=jsonPre(PORTAL_DATA.context||{});
const plan=PORTAL_DATA.plan||{};
text('planner-backend', plan.planner_backend||'—');
text('plan-rationale', plan.rationale||'—');
const planItems=plan.items||[];
$('plan-rows').innerHTML=planItems.length?rows(planItems,it=>'<tr data-filter-row><td><span class="status '+(it.required?'warn':'done')+'">'+(it.required?'必需':'可选')+'</span></td><td>'+esc(it.item_id)+'</td><td>'+esc(it.title)+'</td><td>'+esc(it.module)+'</td><td>'+esc((it.dependencies||[]).join('、')||'—')+'</td><td>'+esc((it.acceptance_criteria||[]).join('；'))+'</td></tr>'):'<tr><td colspan="6" class="muted">尚无计划</td></tr>';
const revisions=plan.revisions||[];
text('revision-count', revisions.length?('共 '+revisions.length+' 次计划修订'):'无计划修订');
$('revision-rows').innerHTML=rows(revisions,r=>'<tr data-filter-row><td>'+esc(r.revision_number)+'</td><td>'+esc(r.operation)+'</td><td>'+esc((r.superseded_item_ids||[]).join('、'))+'</td><td>'+esc(r.created_at)+'</td></tr>');
const branches=plan.branches||[];
$('branch-rows').innerHTML=rows(branches,b=>'<tr data-filter-row><td>'+esc(b.branch_id)+'</td><td>'+esc(b.fork_point_item_id||'')+'</td><td><span class="status '+statusCls(b.status)+'">'+esc(b.status)+'</span></td><td>'+esc(b.rationale||b.reason||'')+'</td></tr>');
const results=PORTAL_DATA.work_item_results||[];
text('result-count', results.length?('共 '+results.length+' 个工作项结果'):'尚无结果');
$('result-cards').innerHTML=rows(results,r=>{
  const outOk=r.outputs && JSON.stringify(r.outputs).length<20000;
  const limits=(r.limitations||[]).length?'<div class="muted">限制：'+esc((r.limitations||[]).join('；'))+'</div>':'';
  const err=r.error?'<div class="danger">错误：'+esc(r.error)+'</div>':'';
  return '<div class="card" data-filter-row><b>'+esc(r.item_id)+' · '+esc(r.module)+' <span class="status '+statusCls(r.status)+'">'+esc(r.status)+'</span></b><div>'+esc(r.summary||'')+'</div>'+limits+err+(outOk?'<details><summary>输出 JSON</summary>'+jsonPre(r.outputs)+'</details>':'')+'</div>';
});
$('assessment-rows').innerHTML=rows(PORTAL_DATA.assessments,a=>'<tr data-filter-row><td>'+esc(a.target_id||'')+'</td><td>'+esc(a.dimension)+'</td><td>'+esc(a.level)+'</td><td>'+esc(a.result)+'</td><td>'+esc(a.actor)+'</td><td>'+esc(a.rationale)+'</td></tr>');
const events=PORTAL_DATA.events||[];
text('event-count', events.length?('显示最近 '+events.length+' 条事件'):'无事件');
$('event-rows').innerHTML=rows(events,e=>'<tr data-filter-row><td>'+esc(e.sequence)+'</td><td>'+esc(e.kind)+'</td><td>'+esc(e.work_item_id||'')+'</td><td>'+esc(e.message||'')+'</td><td>'+esc(e.created_at||'')+'</td></tr>');
$('decision-rows').innerHTML=rows(PORTAL_DATA.decisions,d=>'<tr data-filter-row><td>'+esc(d.decision_id)+'</td><td>'+esc(d.action)+'</td><td>'+esc(d.actor)+'</td><td>'+esc((d.target_ids||[]).join('、'))+'</td><td>'+esc(d.rationale)+'</td><td>'+esc(d.created_at)+'</td></tr>');
const artifacts=PORTAL_DATA.artifacts||[];
text('artifact-count', artifacts.length?('共 '+artifacts.length+' 个产物'):'暂无产物');
$('artifact-rows').innerHTML=rows(artifacts,a=>{const sha=String(a.sha256||'').slice(0,12);return '<tr data-filter-row><td>'+esc(a.artifact_id||'')+'</td><td>'+esc(a.logical_name||'')+'</td><td>'+esc(a.media_type||'')+'</td><td>'+(a.size_bytes||0)+'</td><td><code>'+esc(sha)+'</code></td><td>'+esc(a.status||'')+'</td></tr>';});
const previews=PORTAL_DATA.previews||{};
$('preview-rows').innerHTML=rows(Object.keys(previews),key=>{const p=previews[key];return '<details><summary>'+esc(p.logical_name||key)+' 预览（'+esc(p.artifact_id||'')+'）'+(p.truncated?'，已截断':'')+'</summary><pre>'+esc(p.text||'')+'</pre></details>';});
const terminal=PORTAL_DATA.terminal_reason;
text('terminal-reason', terminal?('终态原因：'+terminal):'');
const gapResults=results.filter(r=>/completed_with_gaps|failed|needs_input/.test(r.status)||/缺口|不足|not_covered|context mismatch|拒绝|不可用/.test(r.summary||''));
text('gap-count', gapResults.length?('共 '+gapResults.length+' 个缺口/降级工作项'):'未发现显式缺口');
$('gap-rows').innerHTML=rows(gapResults,r=>'<tr data-filter-row><td>'+esc(r.item_id)+'</td><td><span class="status '+statusCls(r.status)+'">'+esc(r.status)+'</span></td><td>'+esc(r.summary||'')+'</td></tr>');
const resolutions=PORTAL_DATA.repair_resolutions||[];
const unresolved=(PORTAL_DATA.repair_requests||[]).filter(r=>!resolutions.some(x=>(x.repair_request_id||x.request_id)===r.repair_request_id));
text('repair-count', (PORTAL_DATA.repair_requests||[]).length?('共 '+(PORTAL_DATA.repair_requests||[]).length+' 条修复请求，未解决 '+unresolved.length+' 条'):'无修复请求');
$('repair-rows').innerHTML=rows(unresolved,r=>'<tr data-filter-row><td>'+esc(r.repair_request_id)+'</td><td>'+esc(r.target_work_item_id||'')+'</td><td>'+esc(r.failure_class||'')+'</td><td>'+esc(r.action||'')+'</td><td>'+esc(r.rationale||'')+'</td></tr>');
$('review-rows').innerHTML=rows(PORTAL_DATA.review_targets,t=>'<tr data-filter-row><td>'+esc(t.review_target_id||t.target_id||'')+'</td><td>'+esc(t.work_item_id||'')+'</td><td><span class="status '+statusCls(t.status)+'">'+esc(t.status||'')+'</span></td><td>'+esc(t.rationale||'')+'</td></tr>');
$('next-rows').innerHTML=rows(PORTAL_DATA.next_actions,n=>'<tr data-filter-row><td>'+esc(n.action)+'</td><td>'+esc(n.target_id||n.repair_request_id||n.branch_id||'')+'</td><td>'+esc(n.reason||'')+'</td></tr>');
$('filter').addEventListener('input',()=>{const q=$('filter').value.trim().toLowerCase();document.querySelectorAll('[data-filter-row]').forEach(row=>{row.style.display=(!q||(row.textContent||'').toLowerCase().indexOf(q)>=0)?'':'none';});});
</script>
</body>
</html>
"""


def render_share_portal(
    snapshot: dict[str, Any],
    *,
    previews: dict[str, Any] | None = None,
    generated_at: str | None = None,
    title: str | None = None,
) -> str:
    payload = build_portal_payload(snapshot, previews=previews, generated_at=generated_at)
    portal_title = title or payload.get("title") or payload.get("project_id") or "TargetDiscovery 项目审查"
    return (
        _PORTAL_HTML
        .replace("__PORTAL_TITLE__", _html_escape(portal_title))
        .replace("__PORTAL_DATA__", _data_json(payload))
    )


def render_share_portal_for_project(
    projects_dir: Path | str,
    project_id: str,
    *,
    output: Path | str | None = None,
    max_preview_bytes: int = _PREVIEW_DEFAULT_BYTES,
) -> str:
    """Render one durable project as an offline HTML review view."""
    from .research_runtime import ResearchProjectRuntime
    from .research_service import ResearchProjectService

    projects_dir = Path(projects_dir).expanduser().resolve()
    runtime = ResearchProjectRuntime(projects_dir=projects_dir)
    snapshot = ResearchProjectService(runtime).snapshot(project_id)
    store = ResearchProjectStore(projects_dir, project_id)
    previews = _artifact_previews(store, store.read_artifacts(), max_preview_bytes)
    html = render_share_portal(snapshot, previews=previews)
    if output is not None:
        out = Path(output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
    return html


def render_share_portal_from_package(
    archive: Path | str,
    *,
    output: Path | str | None = None,
    max_preview_bytes: int = _PREVIEW_DEFAULT_BYTES,
) -> str:
    """Render a portable project package as an offline HTML review view.

    The package is verified read-only (manifest + per-file SHA-256) and
    extracted to a temporary directory; nothing is imported into a store and
    no project is mutated.
    """
    from .project_package import inspect_package

    archive = Path(archive).expanduser().resolve()
    metadata = inspect_package(archive)
    if not metadata.get("checksums_valid"):
        raise ValueError("package checksums are not valid; refusing to render")
    project_id = metadata["project_id"]
    with tempfile.TemporaryDirectory(prefix="target-share-") as tmp:
        root = Path(tmp)
        project_dir = root / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(project_dir)
        return render_share_portal_for_project(
            root,
            project_id,
            output=output,
            max_preview_bytes=max_preview_bytes,
        )


__all__ = [
    "build_portal_payload",
    "render_share_portal",
    "render_share_portal_for_project",
    "render_share_portal_from_package",
]