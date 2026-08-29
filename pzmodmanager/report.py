"""Output rendering: self-contained HTML report, JSON export, console summary."""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path

from .analyzers import AnalysisContext, risk_by_mod
from .fonts import display_stack, font_face_css, find_font
from .posters import embed_poster, find_poster
from .selection import workshop_search_url
from .models import Finding, Severity
from .pipeline import ScanResult

log = logging.getLogger(__name__)

SEVERITY_ORDER = [
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
]

RICH_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "bold dark_orange",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "cyan",
    Severity.INFO: "dim",
}


def counts_by_severity(findings: list[Finding]) -> dict[Severity, int]:
    counts = {s: 0 for s in SEVERITY_ORDER}
    for finding in findings:
        counts[finding.severity] += 1
    return counts


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9; --card: #ffffff; --ink: #1b1f24; --muted: #5c6672;
  --line: #dfe3e8; --accent: #2f6f4f;
  /* Overridden at render time when a display font is available. */
  --display: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#15181c; --card:#1d2126; --ink:#e8eaed; --muted:#9aa3ad;
          --line:#2c3238; --accent:#7fc4a0; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.55
  -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 32px 20px 80px; }
h1 { font-size: 26px; margin: 0 0 6px; letter-spacing:0; font-family: var(--display); }
.wrap > h2 { font-family: var(--display); letter-spacing:0; }
.sub { color: var(--muted); margin: 0 0 28px; }
.meta { display:flex; flex-wrap:wrap; gap:10px 28px; padding:16px 18px;
  background:var(--card); border:1px solid var(--line); border-radius:10px; margin-bottom:22px; }
.meta div { font-size:13px; }
.meta b { display:block; font-size:21px; font-weight:600; font-family: var(--display); }
.filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:18px; }
.chip { border:1px solid var(--line); background:var(--card); color:var(--ink);
  padding:5px 12px; border-radius:999px; cursor:pointer; font-size:13px; }
.chip[aria-pressed="true"] { background:var(--ink); color:var(--bg); border-color:var(--ink); }
.f { background:var(--card); border:1px solid var(--line); border-left-width:4px;
  border-radius:8px; padding:14px 16px; margin-bottom:12px; }
.f h3 { margin:0 0 6px; font-size:15px; font-weight:600; }
.tag { display:inline-block; font-size:11px; font-weight:700; letter-spacing:.04em;
  text-transform:uppercase; padding:2px 8px; border-radius:4px; color:#fff; margin-right:8px;
  font-family: var(--display); }
.f p { margin:0 0 8px; color:var(--ink); }
.f .advice { color:var(--muted); font-style:italic; }
details { margin-top:8px; }
summary { cursor:pointer; color:var(--accent); font-size:13px; }
a { color:var(--accent); text-decoration:none; border-bottom:1px solid transparent; }
a:hover { border-bottom-color:var(--accent); }
td.thumb { width:56px; padding:4px 6px 4px 12px; }
td.thumb img { width:44px; height:44px; object-fit:cover; border-radius:4px;
  display:block; background:rgba(127,127,127,.12); }
td.thumb .noimg { width:44px; height:44px; border-radius:4px;
  background:rgba(127,127,127,.12); display:block; }
ul.ev { margin:8px 0 0; padding-left:18px; font-family:ui-monospace,SFMono-Regular,
  Menlo,Consolas,monospace; font-size:12px; color:var(--muted); }
ul.ev li { overflow-wrap:anywhere; }
.scroll { overflow-x:auto; }
table { width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:8px; overflow:hidden; }
th, td { text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); font-size:13px; }
th { background:rgba(127,127,127,.08); font-weight:600; }
tr:last-child td { border-bottom:none; }
.wrap > h2 { font-size:16px; margin:34px 0 12px; }
footer { color:var(--muted); font-size:12px; margin-top:36px; }
.empty { background:var(--card); border:1px solid var(--line); border-radius:8px;
  padding:24px; text-align:center; color:var(--muted); }
"""

_JS = """
document.querySelectorAll('.chip').forEach(function (chip) {
  chip.addEventListener('click', function () {
    var on = chip.getAttribute('aria-pressed') === 'true';
    chip.setAttribute('aria-pressed', on ? 'false' : 'true');
    var active = Array.prototype.map.call(
      document.querySelectorAll('.chip[aria-pressed="true"]'),
      function (c) { return c.dataset.sev; });
    document.querySelectorAll('.f').forEach(function (card) {
      card.hidden = active.length > 0 && active.indexOf(card.dataset.sev) === -1;
    });
  });
});
"""


def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _link(label: str, url: str | None) -> str:
    """A link to the Workshop, or plain text for a mod that has no page."""
    if not url:
        return _esc(label)
    return f"<a href='{_esc(url)}' target='_blank' rel='noopener'>{_esc(label)}</a>"


def render_html(
    result: ScanResult,
    log_path: Path | None = None,
    font_path: Path | None = None,
    embed_images: bool = False,
) -> str:
    findings = result.findings
    ctx = result.ctx
    counts = counts_by_severity(findings)
    generated = datetime.now().strftime("%d %b %Y at %H:%M")
    order_line = _esc(ctx.order.source) if ctx.has_order else "not provided"

    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    parts.append("<title>pzmodmanager report</title>")
    font_file = find_font(font_path)
    style = font_face_css(font_file) + _CSS
    style += f":root{{--display: {display_stack()};}}"
    parts.append(f"<style>{style}</style></head><body><div class='wrap'>")
    parts.append("<h1>Mod compatibility report</h1>")
    parts.append(
        f"<p class='sub'>Project Zomboid, generated on {generated} by pzmodmanager "
        f"in {result.duration:.1f}s</p>"
    )

    parts.append("<div class='meta'>")
    parts.append(f"<div><b>{len(ctx.mods)}</b>mods scanned</div>")
    parts.append(f"<div><b>{result.file_count}</b>files indexed</div>")
    for severity in SEVERITY_ORDER:
        parts.append(
            f"<div><b style='color:{severity.color}'>{counts[severity]}</b>"
            f"{_esc(severity.label)}</div>"
        )
    parts.append(f"<div><b>&nbsp;</b>load order: {order_line}</div>")
    parts.append("</div>")

    if findings:
        parts.append("<div class='filters'>")
        for severity in SEVERITY_ORDER:
            if counts[severity]:
                parts.append(
                    f"<button class='chip' aria-pressed='false' data-sev='{severity.name}'>"
                    f"{_esc(severity.label)} ({counts[severity]})</button>"
                )
        parts.append("</div>")

        for finding in findings:
            sev = finding.severity
            parts.append(
                f"<div class='f' data-sev='{sev.name}' style='border-left-color:{sev.color}'>"
            )
            parts.append(
                f"<h3><span class='tag' style='background:{sev.color}'>"
                f"{_esc(sev.label)}</span>{_esc(finding.title)}</h3>"
            )
            parts.append(f"<p>{_esc(finding.detail)}</p>")
            if finding.mods:
                # Each named mod links straight to its Workshop page.
                linked = []
                for mod_id in dict.fromkeys(finding.mods):
                    mod = ctx.by_key.get(mod_id.strip().lower())
                    linked.append(_link(mod_id, mod.workshop_url) if mod else _esc(mod_id))
                parts.append(
                    "<p class='advice'>Mods involved: " + ", ".join(linked) + "</p>"
                )
            if finding.advice:
                parts.append(f"<p class='advice'>{_esc(finding.advice)}</p>")
            if finding.evidence:
                parts.append(
                    f"<details><summary>Details ({len(finding.evidence)} item(s))</summary>"
                    "<ul class='ev'>"
                )
                for item in finding.evidence:
                    # A missing dependency has no page of its own, so offer a
                    # Workshop search for its id instead of plain dead text.
                    if finding.rule == "missing_dependency":
                        parts.append(
                            f"<li>{_link(item, workshop_search_url(item))}</li>"
                        )
                    else:
                        parts.append(f"<li>{_esc(item)}</li>")
                parts.append("</ul></details>")
            parts.append("</div>")
    else:
        parts.append("<div class='empty'>No overlap detected across this mod set.</div>")

    scores = risk_by_mod(findings)
    if scores:
        parts.append(
            "<h2>Most involved mods</h2><div class='scroll'><table><thead><tr>"
            "<th>Mod id</th><th>Name</th><th>Risk score</th><th>Load position</th>"
            "</tr></thead><tbody>"
        )
        for mod_id, score in sorted(scores.items(), key=lambda kv: -kv[1])[:25]:
            mod = ctx.by_key.get(mod_id.lower())
            name = _link(mod.workshop_title or mod.name, mod.workshop_url) if mod else ""
            pos = str(mod.order_index + 1) if mod and mod.order_index is not None else ""
            parts.append(
                f"<tr><td>{_esc(mod_id)}</td><td>{name}</td><td>{score}</td><td>{pos}</td></tr>"
            )
        parts.append("</tbody></table></div>")

    parts.append(
        "<h2>Inventory</h2><div class='scroll'><table><thead><tr><th></th>"
        "<th>Mod</th><th>Id</th>"
        "<th>Source</th><th>Layout</th><th>Branch used</th><th>Files</th>"
        "<th>Script objects</th><th>Workshop updated</th>"
        "</tr></thead><tbody>"
    )
    for mod in sorted(ctx.mods, key=lambda m: m.name.lower()):
        name = mod.workshop_title or mod.name
        branch = ", ".join(mod.build_targets) or "none"
        if mod.workshop_missing:
            updated = "item removed"
        elif mod.workshop_updated:
            updated = datetime.fromtimestamp(mod.workshop_updated).strftime("%d %b %Y")
        else:
            updated = ""
        # The Workshop preview keeps the report small; embedding the local
        # poster is offered for a report that has to work with no network.
        image = ""
        if embed_images:
            image = embed_poster(find_poster(mod)) or ""
        elif mod.workshop_preview:
            image = mod.workshop_preview
        if image:
            thumb = (
                f"<td class='thumb'><img src='{_esc(image)}' alt='' loading='lazy'></td>"
            )
        else:
            thumb = "<td class='thumb'><span class='noimg'></span></td>"
        parts.append(
            f"<tr>{thumb}<td>{_link(name, mod.workshop_url)}</td><td>{_esc(mod.mod_id)}</td>"
            f"<td>{_esc(mod.source)}</td><td>{_esc(mod.layout)}</td>"
            f"<td>{_esc(branch)}</td>"
            f"<td>{len(mod.assets)}</td><td>{len(mod.script_objects)}</td>"
            f"<td>{_esc(updated)}</td></tr>"
        )
    parts.append("</tbody></table></div>")

    parts.append("<footer>Scanned folders: " + _esc(", ".join(result.scanned) or "none"))
    if log_path:
        parts.append(f"<br>Log file: {_esc(log_path)}")
    parts.append(
        "<br>An overlap is not an incompatibility: Project Zomboid has no such notion. "
        "This report shows what overwrites what; whether that is intended is your call."
        "</footer>"
    )
    parts.append(f"</div><script>{_JS}</script></body></html>")
    return "".join(parts)


def write_html(
    path: Path,
    result: ScanResult,
    log_path: Path | None = None,
    font_path: Path | None = None,
    embed_images: bool = False,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_html(result, log_path, font_path, embed_images), encoding="utf-8"
    )
    log.info("HTML report written to %s", path)
    return path


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def to_dict(result: ScanResult) -> dict:
    ctx = result.ctx
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": round(result.duration, 2),
        "scanned_folders": result.scanned,
        "files_indexed": result.file_count,
        "load_order": {
            "source": ctx.order.source if ctx.order else None,
            "kind": ctx.order.kind if ctx.order else None,
            "list_name": ctx.order.list_name if ctx.order else None,
            "mods": ctx.order.mod_ids if ctx.order else [],
        },
        "mods": [
            {
                "id": m.mod_id,
                "name": m.name,
                "source": m.source,
                "workshop_id": m.workshop_id,
                "path": str(m.root),
                "workshop_url": m.workshop_url,
                "workshop_preview": m.workshop_preview,
                "branches": m.build_targets,
                "requires": m.requires,
                "incompatible": m.incompatible,
                "file_count": len(m.assets),
                "script_object_count": len(m.script_objects),
                "load_position": m.order_index,
                "enabled": m.enabled,
            }
            for m in ctx.mods
        ],
        "findings": [
            {
                "rule": f.rule,
                "severity": f.severity.label,
                "title": f.title,
                "detail": f.detail,
                "mods": list(dict.fromkeys(f.mods)),
                "winner": f.winner,
                "advice": f.advice,
                "evidence": f.evidence,
            }
            for f in result.findings
        ],
    }


def write_json(path: Path, result: ScanResult) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("JSON export written to %s", path)
    return path


# --------------------------------------------------------------------------- #
# Console summary
# --------------------------------------------------------------------------- #


def print_summary(console, result: ScanResult) -> None:
    """A short count table. The findings themselves live in the HTML report."""
    from rich.table import Table

    counts = counts_by_severity(result.findings)
    table = Table(show_header=True, header_style="bold", title=None, box=None, pad_edge=False)
    table.add_column("Severity", style="bold")
    table.add_column("Findings", justify="right")
    for severity in SEVERITY_ORDER:
        table.add_row(
            f"[{RICH_STYLE[severity]}]{severity.label}[/]", str(counts[severity])
        )
    console.print()
    console.print(table)


def print_top_mods(console, result: ScanResult, limit: int = 5) -> None:
    scores = risk_by_mod(result.findings)
    if not scores:
        return
    from rich.table import Table

    table = Table(title="Most involved mods", header_style="bold", box=None, pad_edge=False)
    table.add_column("Mod")
    table.add_column("Score", justify="right")
    for mod_id, score in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
        table.add_row(mod_id, str(score))
    console.print()
    console.print(table)


def print_console_report(findings, ctx: AnalysisContext, console) -> None:
    """Full findings on the console. Only used with --print-findings."""
    for finding in findings:
        style = RICH_STYLE[finding.severity]
        console.print(f"[{style}] {finding.severity.label.upper()} [/] {finding.title}")
        console.print(f"    {finding.detail}")
        for line in finding.evidence[:5]:
            console.print(f"      - {line}", style="dim")
        if len(finding.evidence) > 5:
            console.print(f"      - ... {len(finding.evidence) - 5} more", style="dim")
        if finding.advice:
            console.print(f"    [italic]{finding.advice}[/italic]")
        console.print()
