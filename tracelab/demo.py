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
:root{{color-scheme:light}}
body{{font-family:system-ui,'Microsoft YaHei',sans-serif;max-width:1080px;margin:28px auto;
padding:0 18px;line-height:1.55;color:#1f2328;background:#fbfbfa}}
h1{{margin:0 0 4px;font-size:26px}}
h2{{margin-top:34px;border-left:6px solid #2f6f4e;padding-left:10px;font-size:19px}}
.muted{{color:#57606a;font-size:12px}}
.card{{display:inline-block;border:1px solid #d0d7de;border-radius:8px;padding:10px 16px;
margin:6px 8px 6px 0;background:#fff;min-width:110px;text-align:center}}
.card b{{display:block;font-size:26px}}
table{{border-collapse:collapse;width:100%;font-size:13px;background:#fff}}
th,td{{border:1px solid #d0d7de;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f6f8fa}}
code{{background:#f6f8fa;padding:1px 4px;border-radius:4px}}
pre{{background:#0d1117;color:#e6edf3;padding:12px 14px;border-radius:8px;overflow:auto;font-size:12px}}
.note{{border:1px solid #e0c060;background:#fffbe6;border-radius:8px;padding:10px 14px}}
</style></head><body>
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
