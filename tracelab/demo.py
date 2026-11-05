"""打开即用的体验页：把一次评测结果渲染成单文件 HTML（审计「可体验性」）。

设计约束：
- 只依赖标准库与评测结果 JSON，**不引入前端依赖**；
- 单文件、可离线打开，无 JavaScript、无外链；
- 输出确定性：不含时间戳、耗时、绝对路径，便于提交与测试比对。
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_TITLES = {
    "candidate_better": "候选版更好",
    "baseline_better": "候选版退化",
    "equal": "无明显差异",
    "insufficient_evidence": "证据不足（拒判）",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _verdict(assessment: Mapping[str, Any]) -> str:
    if assessment.get("agent_task_success") is True:
        return "PASS"
    labels = {
        str(cause.get("label"))
        for cause in assessment.get("root_causes", [])
        if isinstance(cause, Mapping)
    }
    return "UNKNOWN" if "INSUFFICIENT_EVIDENCE" in labels else "FAIL"


def build_demo_html(payload: Mapping[str, Any], *, source: str = "data/sample/sample-traces.jsonl") -> str:
    """从 `results.json` 的内容生成单文件体验页（确定性输出）。"""
    manifest = payload.get("manifest") or {}
    cases = payload.get("cases") or []
    versions = f"{manifest.get('tracelab_evaluator_version', '?')}　规则集 {manifest.get('rulepack_version', '?')}"

    verdicts = {"PASS": 0, "FAIL": 0, "UNKNOWN": 0}
    directions: dict[str, int] = {}
    rows: list[str] = []
    example: tuple[str, Mapping[str, Any]] | None = None
    for case in cases:
        comparison = case.get("comparison") or {}
        direction = str(comparison.get("direction", "?"))
        directions[direction] = directions.get(direction, 0) + 1
        marks: list[str] = []
        for assessment in case.get("assessments", []):
            verdict = _verdict(assessment)
            verdicts[verdict] += 1
            if verdict == "FAIL" and example is None:
                example = (str(case.get("case_id")), assessment)
            marks.append(verdict)
        while len(marks) < 2:
            marks.append("—")
        rows.append(
            "<tr>"
            f"<td><code>{_esc(case.get('case_id'))}</code></td>"
            f"<td>{_esc(case.get('scenario'))}</td>"
            f"<td>{_esc(marks[0])}</td><td>{_esc(marks[1])}</td>"
            f"<td>{_esc(direction)}</td>"
            f"<td>{_esc(comparison.get('release_recommendation', '?'))}</td>"
            f"<td>{_esc(comparison.get('quality_delta', ''))}</td>"
            "</tr>"
        )

    evidence_html = "<p>本次样例没有失败运行（或没有可展示的根因）。</p>"
    if example is not None:
        case_id, assessment = example
        causes = [
            f"<li><b>{_esc(cause.get('label'))}</b>（规则 {_esc(cause.get('rule_id'))}，"
            f"首因事件 <code>{_esc(cause.get('first_fault_event_id'))}</code>，"
            f"证据事件 {_esc(', '.join(map(str, cause.get('evidence_event_ids') or [])) or '—')}）"
            f"<br><span class='muted'>{_esc(cause.get('explanation', ''))}</span></li>"
            for cause in assessment.get("root_causes", [])
            if isinstance(cause, Mapping)
        ]
        evidence_html = (
            f"<p><b>Case {_esc(case_id)}</b> / run <code>{_esc(assessment.get('run_id'))}</code>："
            f"判定 <b>{_esc(_verdict(assessment))}</b>，质量分 {_esc(assessment.get('quality_score'))}。</p>"
            f"<ul>{''.join(causes) or '<li>无根因记录</li>'}</ul>"
            f"<p class='muted'>传播边 {_esc(len(assessment.get('propagation') or []))} 条；"
            f"工具拦截 {_esc(len(assessment.get('guards') or []))} 条；"
            f"不确定性 {_esc(len(assessment.get('uncertainties') or []))} 条。</p>"
        )

    direction_text = "，".join(f"{_esc(k)} {v}" for k, v in sorted(directions.items())) or "无"
    table = "\n".join(rows) or "<tr><td colspan='7'>没有 Case</td></tr>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>YAI TraceLab · 打开即用体验页</title>
<style>
:root{{color-scheme:light;--paper:#f6f3ea;--ink:#171715;--line:#c8c3b4;--dim:#5c584f;--seal:#8f2f22}}
*{{box-sizing:border-box}}
body{{margin:0 auto;padding:30px 24px 60px;max-width:1080px;background:var(--paper);color:var(--ink);
border-top:3px solid var(--ink);font-family:"Noto Serif CJK SC","Source Han Serif SC","SimSun",serif;line-height:1.7}}
h1{{margin:16px 0 6px;font-size:38px;font-weight:500;letter-spacing:.03em;line-height:1.2}}
h2{{margin:32px 0 12px;padding-top:10px;border-top:1px solid var(--ink);font-size:18px;font-weight:500;letter-spacing:.06em}}
.muted{{color:var(--dim);font-size:12px;line-height:1.8}}
.card{{display:inline-block;border:1px solid var(--ink);border-radius:0;padding:8px 14px;
margin:0 8px 8px 0;background:transparent;min-width:108px;text-align:left}}
.card b{{display:block;font-family:ui-monospace,Consolas,monospace;font-size:38px;line-height:1.1;font-weight:500;letter-spacing:-.04em}}
table{{border-collapse:collapse;width:100%;font-size:12px;background:transparent}}
th,td{{border-bottom:1px solid var(--line);padding:7px 10px 7px 0;text-align:left;vertical-align:top}}
th{{border-bottom:1px solid var(--ink);color:var(--dim);font-weight:400;letter-spacing:.06em}}
code{{background:transparent;border-bottom:1px solid var(--line);padding:0 2px;border-radius:0;font-family:ui-monospace,Consolas,monospace}}
pre{{background:var(--ink);color:#efe9dc;padding:14px 16px;border-radius:0;overflow:auto;font-size:12px;line-height:1.8}}
.note{{border:1px solid var(--ink);border-left:3px solid var(--seal);background:transparent;border-radius:0;padding:10px 14px;font-size:13px;line-height:1.8}}
ul{{padding-left:20px}}
li{{margin:5px 0}}
b{{font-weight:500}}
@media(max-width:600px){{body{{padding:22px 16px 40px}}h1{{font-size:28px}}.card b{{font-size:30px}}}}
body{{border-bottom:3px solid var(--ink)}}
.tear{{height:9px;border-bottom:1px dashed var(--line);
background-image:radial-gradient(circle at 7px 4px,var(--line) 2px,transparent 2.5px);
background-size:14px 9px;margin-bottom:18px}}
.docno{{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:var(--dim);
letter-spacing:.06em;border-bottom:1px solid var(--line);padding-bottom:6px;margin-bottom:12px}}
.note{{position:relative}}
.note::after{{content:"数据边界";position:absolute;right:12px;top:-11px;
border:1px solid var(--seal);color:var(--seal);padding:2px 7px;font-size:11px;
transform:rotate(-4deg);background:var(--paper)}}
th{{border-bottom:2px solid var(--ink)}}
.card{{border-bottom:2px solid var(--ink)}}
.card b,table{{font-variant-numeric:tabular-nums}}
*::-webkit-scrollbar{{width:9px;height:9px}}
*::-webkit-scrollbar-track{{background:var(--paper);border-left:1px solid var(--line)}}
*::-webkit-scrollbar-thumb{{background:var(--dim);border:2px solid var(--paper)}}
*::-webkit-scrollbar-thumb:hover{{background:var(--seal)}}
*{{scrollbar-width:thin;scrollbar-color:var(--dim) var(--paper)}}
</style></head><body>
<div class="tear"></div>
<div class="docno">票据编号 / {_esc(str(manifest.get('result_hash',''))[:8] or '—')}　官方报告模板 7 节　三版读者报告</div>
<h1>YAI TraceLab · 打开即用体验页</h1>
<p class="muted">证据驱动、业务规则感知的开票 Agent 离线轨迹评测 —— 本页由一次真实评测生成，可离线打开。</p>

<div class="note">
<b>数据边界：</b>本页使用仓库自带的<b>自造样例</b> <code>{_esc(source)}</code>（非赛事资料包），
只用于演示"怎么判、怎么归因、怎么比版本"；官方训练集指标请按 README 用 <code>data/official/</code> 复跑。
</div>

<h2>这次跑出了什么</h2>
<span class="card"><b>{len(cases)}</b>Case</span>
<span class="card"><b>{sum(verdicts.values())}</b>运行</span>
<span class="card"><b>{verdicts['PASS']}</b>PASS</span>
<span class="card"><b>{verdicts['FAIL']}</b>FAIL</span>
<span class="card"><b>{verdicts['UNKNOWN']}</b>UNKNOWN</span>
<p>版本方向：{direction_text}</p>
<p class="muted">评测器 {_esc(versions)}　Trace IR {_esc(manifest.get('trace_ir_version', '?'))}
　适配器 {_esc((manifest.get('adapter') or {}).get('name', '?'))}
　结果哈希 <code>{_esc(str(manifest.get('result_hash', ''))[:16])}</code></p>

<h2>逐 Case 版本对比</h2>
<table>
<tr><th>Case</th><th>场景</th><th>基线</th><th>候选</th><th>方向</th><th>建议</th><th>质量分变化</th></tr>
{table}
</table>

<h2>证据链示例（判断不是拍脑袋）</h2>
{evidence_html}

<h2>自己再跑一遍（复制即可）</h2>
<pre># 1) 体验（等价于本页的生成过程）
python -m tracelab demo

# 2) 看完整报告
out/demo/report-official.md    # 官方 7 节模板
out/demo/report.md             # 综合版
out/demo/report-business.md    # 业务版（以及 engineering / product）

# 3) 有官方数据时复跑训练集指标
python -m tracelab evaluate --cases "data/official/student-1.0/02_公开训练轨迹.jsonl" \\
  --labels "data/official/student-1.0/03_公开训练参考标注.jsonl" --out out/train
python scripts/ci_verify.py metrics --dir out/train   # 期望六项全 1.0000

# 4) 评测器自身是否稳定
python -m pytest tests/unit -q</pre>

<h2>这个页面说明的评测原则</h2>
<ul>
<li><b>三态判定</b>：PASS / FAIL / UNKNOWN，UNKNOWN 不进准确率分母，也不被当作"Agent 做错"。</li>
<li><b>证据可复核</b>：每条根因都带规则 ID、首因事件、证据事件编号，可回原始轨迹逐条核对。</li>
<li><b>首因 vs 传播</b>：只在参数真的流动到下游时建立传播边，不按时间先后推因果。</li>
<li><b>工具拦截不是故障</b>：保护性拒绝单独记录为 guard，归因落在 Agent 侧。</li>
<li><b>版本比较</b>：同 Case 双版本按内部三态与质量分比较；证据不足时拒判而不是硬判。</li>
</ul>
<p class="muted">本页不含时间戳与耗时，便于归档与比对。完整方法见 <code>docs/method.md</code>。</p>
</body></html>
"""


def write_demo_html(payload: Mapping[str, Any], path: Path, *, source: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = build_demo_html(payload, source=source) if source else build_demo_html(payload)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
