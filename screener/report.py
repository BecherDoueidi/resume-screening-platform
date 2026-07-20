"""Stage 4 — write results.json and a self-contained HTML dashboard."""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from screener.models import CandidateResult, JobDescription

CSS = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#1c2333; --muted:#68718a; --accent:#2f5fe0;
        --good:#1e9e5a; --mid:#d9930d; --bad:#cc4433; --line:#e4e7ee; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font:15px/1.55 "Segoe UI",system-ui,sans-serif; background:var(--bg); color:var(--ink); padding:32px 16px; }
.wrap { max-width:960px; margin:0 auto; }
h1 { font-size:26px; margin-bottom:4px; }
.sub { color:var(--muted); margin-bottom:28px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:22px 24px; margin-bottom:18px; box-shadow:0 1px 3px rgba(20,30,60,.06); }
.rank { display:flex; align-items:baseline; gap:12px; margin-bottom:10px; flex-wrap:wrap; }
.rank .pos { font-size:22px; font-weight:700; color:var(--accent); }
.rank .name { font-size:19px; font-weight:600; }
.rank .file { color:var(--muted); font-size:12px; }
.rank .overall { margin-left:auto; font-size:24px; font-weight:700; }
.bars { display:grid; grid-template-columns:170px 1fr 44px; gap:6px 12px; align-items:center; margin:12px 0 16px; }
.bars .lbl { color:var(--muted); font-size:13px; }
.bar { height:9px; background:var(--line); border-radius:5px; overflow:hidden; }
.bar > span { display:block; height:100%; border-radius:5px; }
.bars .val { font-variant-numeric:tabular-nums; font-size:13px; text-align:right; }
h3 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:16px 0 6px; }
ul { padding-left:20px; }
li { margin-bottom:4px; }
details { margin-top:14px; border-top:1px solid var(--line); padding-top:10px; }
summary { cursor:pointer; color:var(--accent); font-size:13px; }
table { border-collapse:collapse; margin-top:8px; font-size:13px; width:100%; }
td, th { border:1px solid var(--line); padding:5px 10px; text-align:left; }
th { background:var(--bg); }
.err { color:var(--bad); }
.badge { display:inline-block; background:#e9efff; color:var(--accent); border-radius:20px;
         padding:2px 10px; font-size:12px; margin-right:6px; }
"""


def _score_color(v: int) -> str:
    return "var(--good)" if v >= 70 else ("var(--mid)" if v >= 45 else "var(--bad)")


def _bar_row(label: str, value: int) -> str:
    return (
        f'<div class="lbl">{label}</div>'
        f'<div class="bar"><span style="width:{value}%;background:{_score_color(value)}"></span></div>'
        f'<div class="val">{value}</div>'
    )


def candidate_card_html(pos: int, r: CandidateResult) -> str:
    res, ev = r.resume, r.evaluation
    file_name = html.escape(Path(res.source_path).name)
    parts = [
        f'<div class="card"><div class="rank"><span class="pos">#{pos}</span>'
        f'<span class="name">{html.escape(res.candidate_id)}</span>'
        f'<span class="file">{file_name}</span>'
    ]
    if ev and not ev.error:
        parts.append(f'<span class="overall" style="color:{_score_color(ev.overall)}">{ev.overall}</span></div>')
        parts.append('<div class="bars">')
        parts.append(_bar_row("Skill match", ev.skill_match))
        parts.append(_bar_row("Experience relevance", ev.experience_relevance))
        parts.append(_bar_row("Project impact", ev.project_impact))
        parts.append("</div>")
        parts.append(f"<h3>Why this ranking</h3><p>{html.escape(ev.justification)}</p>")
        if ev.gaps:
            parts.append("<h3>Gaps vs. requirements</h3><ul>")
            parts += [f"<li>{html.escape(g)}</li>" for g in ev.gaps]
            parts.append("</ul>")
        if ev.interview_questions:
            parts.append("<h3>Suggested interview questions</h3><ul>")
            parts += [f"<li>{html.escape(q)}</li>" for q in ev.interview_questions]
            parts.append("</ul>")
    else:
        msg = ev.error if ev else res.parse_error or "Not evaluated"
        parts.append(f'</div><p class="err">{html.escape(msg)}</p>')
    # Redaction audit — proof that bias mitigation ran before evaluation.
    if res.redactions:
        total = sum(rec.count for rec in res.redactions)
        parts.append(f"<details><summary>Bias-mitigation audit: {total} redactions applied before evaluation</summary>")
        parts.append("<table><tr><th>Indicator type</th><th>Replaced with</th><th>Count</th></tr>")
        for rec in sorted(res.redactions, key=lambda x: -x.count):
            repl = html.escape(rec.replacement) or "<i>(removed)</i>"
            parts.append(
                f"<tr><td>{html.escape(rec.kind.replace('_', ' '))}</td><td>{repl}</td><td>{rec.count}</td></tr>"
            )
        parts.append(
            "</table><p style='color:var(--muted);font-size:12px;margin-top:6px'>"
            "Original values are never stored or sent to the model; the audit keeps only counts and hashes.</p></details>"
        )
    parts.append("</div>")
    return "".join(parts)


def write_report(
    jd: JobDescription,
    results: list[CandidateResult],
    out_dir: str | Path,
    top: int | None = None,
) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ranked = sorted(
        [r for r in results if r.evaluation and not r.evaluation.error],
        key=lambda r: r.evaluation.overall,
        reverse=True,
    )
    skipped = [r for r in results if not (r.evaluation and not r.evaluation.error)]
    shown = ranked[:top] if top else ranked

    json_path = out_dir / "results.json"
    json_path.write_text(
        json.dumps(
            {
                "job_title": jd.title,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "candidates_evaluated": len(ranked),
                "candidates_skipped": len(skipped),
                "ranking": [r.to_dict() for r in ranked],
                "skipped": [r.to_dict() for r in skipped],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    body = [f'<div class="wrap"><h1>Candidate ranking &mdash; {html.escape(jd.title)}</h1>']
    body.append(
        f'<p class="sub"><span class="badge">{len(ranked)} evaluated</span>'
        f'<span class="badge">{len(skipped)} skipped</span>'
        f"Generated {datetime.now():%Y-%m-%d %H:%M} &middot; all resumes anonymized before evaluation</p>"
    )
    for i, r in enumerate(shown, start=1):
        body.append(candidate_card_html(i, r))
    if skipped:
        body.append('<h1 style="font-size:20px;margin:24px 0 12px">Skipped</h1>')
        for r in skipped:
            body.append(candidate_card_html(0, r))
    body.append("</div>")

    html_doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Resume screening — {html.escape(jd.title)}</title>"
        f"<style>{CSS}</style></head><body>{''.join(body)}</body></html>"
    )
    html_path = out_dir / "report.html"
    html_path.write_text(html_doc, encoding="utf-8")
    return json_path, html_path
