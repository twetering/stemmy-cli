"""
benchmark/dashboard.py — Live Rich terminal dashboard + HTML/JSON export.

Gebruik:
    python benchmark/dashboard.py              # Eenmalige weergave
    python benchmark/dashboard.py --watch      # Live updates elke 3 seconden
    python benchmark/dashboard.py --export-html results/report.html
    python benchmark/dashboard.py --export-json results/summary.json
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from evaluate import ExperimentResult, load_all_results, load_baseline_score

RESULTS_DIR = Path(__file__).parent / "results"
EXPERIMENTS_DIR = RESULTS_DIR / "experiments"

# Unicode sparkline tekens
SPARK_CHARS = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def _sparkline(values: List[float], width: int = 30) -> str:
    """Genereer een Unicode sparkline string."""
    if not values:
        return "─" * width
    min_v = min(values)
    max_v = max(values)
    span = max_v - min_v or 1

    result = []
    for v in values[-width:]:
        idx = int((v - min_v) / span * (len(SPARK_CHARS) - 1))
        result.append(SPARK_CHARS[idx])
    return "".join(result)


def _git_log(n: int = 8) -> List[str]:
    """Haal recente benchmark-commits op."""
    try:
        out = subprocess.check_output(
            ["git", "log", "--oneline", f"-{n}", "--grep=benchmark:"],
            cwd=REPO_ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
        return out.split("\n") if out else []
    except Exception:
        return []


def _format_score(score: float, baseline: float) -> str:
    if baseline <= 0:
        return f"{score:.4f}"
    pct = (score - baseline) / baseline * 100
    sign = "+" if pct >= 0 else ""
    return f"{score:.4f} ({sign}{pct:.1f}%)"


def _elapsed(timestamp: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo)
        delta = now - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s geleden"
        elif secs < 3600:
            return f"{secs//60}m geleden"
        else:
            return f"{secs//3600}u{(secs%3600)//60}m geleden"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Rich terminal dashboard
# ---------------------------------------------------------------------------

def render_dashboard(watch: bool = False, refresh_sec: int = 3) -> None:
    try:
        from rich.console import Console
        from rich.layout import Layout
        from rich.live import Live
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
        from rich import box
    except ImportError:
        print("Rich niet geïnstalleerd. Gebruik: pip install rich")
        sys.exit(1)

    console = Console()

    def build_layout() -> Layout:
        results = load_all_results()
        baseline = load_baseline_score()
        n_total = len(results)
        committed = [r for r in results if r.extraction_score > baseline * 1.005]
        best = max(results, key=lambda r: r.extraction_score) if results else None
        scores = [r.extraction_score for r in results]
        spark = _sparkline(scores, width=40)

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=4),
            Layout(name="metrics", size=7),
            Layout(name="trend", size=5),
            Layout(name="tables", ratio=1),
            Layout(name="git", size=12),
        )

        # Header
        start_time = results[0].timestamp if results else "—"
        elapsed_total = _elapsed(start_time) if results else "—"
        header_text = Text()
        header_text.append("  STEMMY AUTORESEARCH DASHBOARD", style="bold cyan")
        header_text.append(f"   |   Experimenten: {n_total}", style="white")
        header_text.append(f"   |   Committed: {len(committed)}", style="green")
        header_text.append(f"   |   Gestart: {elapsed_total}", style="dim")
        layout["header"].update(Panel(header_text, style="cyan"))

        # Metrics
        metrics_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        metrics_table.add_column(style="bold yellow", width=20)
        metrics_table.add_column(style="bold white", width=20)
        metrics_table.add_column(style="bold yellow", width=20)
        metrics_table.add_column(style="bold white", width=20)

        best_score_str = _format_score(best.extraction_score, baseline) if best else "—"
        best_fps_str = f"{best.fragments_per_second:.3f} fps" if best else "—"
        last_score = f"{results[-1].extraction_score:.4f}" if results else "—"
        last_fps = f"{results[-1].fragments_per_second:.3f} fps" if results else "—"

        metrics_table.add_row("BESTE SCORE", best_score_str, "BASELINE", f"{baseline:.4f}")
        metrics_table.add_row("BESTE fps", best_fps_str, "LAATSTE EXP", last_score)
        if best:
            metrics_table.add_row(
                "BESTE CONFIG",
                best.config_summary[:35] + "..." if len(best.config_summary) > 35 else best.config_summary,
                "LAATSTE fps",
                last_fps,
            )

        layout["metrics"].update(Panel(metrics_table, title="[bold]Metrics", border_style="yellow"))

        # Trend sparkline
        trend_text = Text()
        if scores:
            lo = f"{min(scores):.3f}"
            hi = f"{max(scores):.3f}"
            trend_text.append(f"  {lo} ", style="dim")
            trend_text.append(spark, style="cyan")
            trend_text.append(f" {hi}", style="dim")
            trend_text.append(f"\n  Laatste {min(40, n_total)} experimenten  |  ", style="dim")
            improving = sum(1 for i in range(1, len(scores)) if scores[i] > scores[i-1])
            trend_text.append(f"Verbeterend: {improving}/{max(n_total-1,1)}", style="green" if improving > n_total//2 else "yellow")
        else:
            trend_text.append("  Nog geen experimenten")
        layout["trend"].update(Panel(trend_text, title="[bold]Score Trend", border_style="blue"))

        # Tabellen: top configs + recente experimenten
        layout["tables"].split_row(
            Layout(name="top5", ratio=1),
            Layout(name="recent", ratio=1),
        )

        # Top 5 configs
        top_table = Table(box=box.SIMPLE_HEAD, show_lines=False)
        top_table.add_column("Score", style="bold green", width=10)
        top_table.add_column("fps", width=7)
        top_table.add_column("Config samenvatting", style="dim", min_width=35)

        sorted_results = sorted(results, key=lambda r: r.extraction_score, reverse=True)
        seen_configs = set()
        shown = 0
        for r in sorted_results:
            cfg_key = r.config_summary[:40]
            if cfg_key in seen_configs:
                continue
            seen_configs.add(cfg_key)
            is_committed = "✓ " if r.extraction_score > baseline * 1.005 else "  "
            score_style = "bold green" if r.extraction_score > baseline * 1.005 else "white"
            top_table.add_row(
                f"{is_committed}{r.extraction_score:.4f}",
                f"{r.fragments_per_second:.3f}",
                r.config_summary[:45],
                style=score_style if is_committed.strip() else None,
            )
            shown += 1
            if shown >= 5:
                break
        layout["top5"].update(Panel(top_table, title="[bold]Top 5 Configuraties", border_style="green"))

        # Recente experimenten
        recent_table = Table(box=box.SIMPLE_HEAD, show_lines=False)
        recent_table.add_column("Experiment", width=22)
        recent_table.add_column("Score", width=8)
        recent_table.add_column("Status", width=8)
        recent_table.add_column("fps", width=7)

        for r in reversed(results[-10:]):
            is_better = r.extraction_score > baseline * 1.005
            status = "[green]COMMIT" if is_better else "[dim]skip"
            score_style = "bold green" if is_better else "white"
            recent_table.add_row(
                r.experiment_id[-22:],
                Text(f"{r.extraction_score:.4f}", style=score_style),
                Text(status),
                f"{r.fragments_per_second:.3f}",
            )
        layout["recent"].update(Panel(recent_table, title="[bold]Recente Experimenten", border_style="blue"))

        # Git log
        git_lines = _git_log(8)
        git_text = Text()
        for line in git_lines:
            git_text.append(f"  {line}\n", style="dim green" if "benchmark:" in line else "dim")
        if not git_lines:
            git_text.append("  (Nog geen benchmark-commits)", style="dim")
        layout["git"].update(Panel(git_text, title="[bold]Git Log (benchmark commits)", border_style="dim"))

        return layout

    if watch:
        with Live(build_layout(), refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(refresh_sec)
                live.update(build_layout())
    else:
        console.print(build_layout())


# ---------------------------------------------------------------------------
# HTML export
# ---------------------------------------------------------------------------

def export_html(output_path: Path) -> None:
    results = load_all_results()
    baseline = load_baseline_score()

    if not results:
        print("Geen experimenten gevonden voor HTML-export.")
        return

    # Data voor grafieken
    exp_labels = [r.experiment_id[-12:] for r in results]
    scores = [r.extraction_score for r in results]
    fps_vals = [r.fragments_per_second for r in results]
    silence_vals = [r.silence_ratio for r in results]
    committed_flags = [r.extraction_score > baseline * 1.005 for r in results]

    # Tabel HTML
    rows_html = ""
    sorted_results = sorted(results, key=lambda r: r.extraction_score, reverse=True)
    for r in sorted_results:
        is_committed = r.extraction_score > baseline * 1.005
        row_class = 'class="committed"' if is_committed else ""
        pct = (r.extraction_score - baseline) / max(baseline, 0.001) * 100
        pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
        rows_html += f"""
        <tr {row_class}>
            <td>{"✓" if is_committed else ""}</td>
            <td>{r.experiment_id[-18:]}</td>
            <td><strong>{r.extraction_score:.4f}</strong> ({pct_str})</td>
            <td>{r.fragments_per_second:.3f}</td>
            <td>{r.success_rate*100:.1f}%</td>
            <td>{r.silence_ratio:.3f}</td>
            <td>{r.clipping_ratio:.3f}</td>
            <td style="font-size:11px;max-width:300px;overflow:hidden">{r.config_summary}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<title>Stemmy Autoresearch Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }}
  h1 {{ color: #58a6ff; font-size: 1.8em; border-bottom: 1px solid #30363d; padding-bottom: 10px; }}
  h2 {{ color: #8b949e; font-size: 1.1em; text-transform: uppercase; letter-spacing: 1px; margin-top: 30px; }}
  .metrics-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 20px 0; }}
  .metric-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
  .metric-card .value {{ font-size: 2em; font-weight: bold; color: #58a6ff; }}
  .metric-card .label {{ color: #8b949e; font-size: 0.85em; margin-top: 4px; }}
  .charts {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin: 20px 0; }}
  .chart-box {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #21262d; color: #8b949e; text-align: left; padding: 8px 12px;
        border-bottom: 1px solid #30363d; position: sticky; top: 0; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #21262d; }}
  tr:hover {{ background: #21262d; }}
  tr.committed {{ background: rgba(35, 134, 54, 0.1); }}
  tr.committed td:nth-child(3) {{ color: #3fb950; }}
  .footer {{ margin-top: 30px; color: #8b949e; font-size: 0.8em; text-align: center; }}
  canvas {{ max-height: 250px; }}
</style>
</head>
<body>
<h1>🎵 Stemmy Autoresearch Dashboard</h1>
<p style="color:#8b949e">Gegenereerd: {datetime.now().strftime('%Y-%m-%d %H:%M')} |
   Baseline: <strong>{baseline:.4f}</strong> |
   Experimenten: <strong>{len(results)}</strong> |
   Beste: <strong>{max(scores):.4f}</strong> (+{(max(scores)/max(baseline,0.001)-1)*100:.1f}%)</p>

<div class="metrics-grid">
  <div class="metric-card">
    <div class="value">{len(results)}</div>
    <div class="label">Totale experimenten</div>
  </div>
  <div class="metric-card">
    <div class="value" style="color:#3fb950">{sum(committed_flags)}</div>
    <div class="label">Commits (verbeteringen)</div>
  </div>
  <div class="metric-card">
    <div class="value">{max(scores):.4f}</div>
    <div class="label">Beste score</div>
  </div>
  <div class="metric-card">
    <div class="value" style="color:#3fb950">+{(max(scores)/max(baseline,0.001)-1)*100:.1f}%</div>
    <div class="label">Verbetering vs baseline</div>
  </div>
</div>

<div class="charts">
  <div class="chart-box">
    <h2>Score over Experimenten</h2>
    <canvas id="scoreChart"></canvas>
  </div>
  <div class="chart-box">
    <h2>fps vs Silence Ratio</h2>
    <canvas id="scatterChart"></canvas>
  </div>
</div>

<h2>Alle Experimenten (gesorteerd op score)</h2>
<div style="overflow-x:auto">
<table>
<thead>
  <tr>
    <th>✓</th><th>Experiment</th><th>Score</th><th>fps</th>
    <th>Success%</th><th>Silence</th><th>Clipping</th><th>Config</th>
  </tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</div>

<div class="footer">
  Gegenereerd door stemmy benchmark/dashboard.py |
  <a href="https://github.com/karpathy/autoresearch" style="color:#58a6ff">Gebaseerd op Karpathy autoresearch</a>
</div>

<script>
const scores = {json.dumps(scores)};
const labels = {json.dumps(exp_labels)};
const baseline = {baseline};
const committed = {json.dumps(committed_flags)};
const fpsVals = {json.dumps(fps_vals)};
const silenceVals = {json.dumps(silence_vals)};

// Score lijndiagram
new Chart(document.getElementById('scoreChart'), {{
  type: 'line',
  data: {{
    labels: labels,
    datasets: [
      {{
        label: 'extraction_score',
        data: scores,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88,166,255,0.1)',
        tension: 0.3,
        fill: true,
        pointRadius: committed.map(c => c ? 5 : 2),
        pointBackgroundColor: committed.map(c => c ? '#3fb950' : '#58a6ff'),
      }},
      {{
        label: 'baseline',
        data: Array(scores.length).fill(baseline),
        borderColor: '#f85149',
        borderDash: [5, 5],
        pointRadius: 0,
        tension: 0,
      }}
    ]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#c9d1d9' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#8b949e', maxTicksLimit: 10 }}, grid: {{ color: '#21262d' }} }},
      y: {{ ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }}
    }}
  }}
}});

// Scatter fps vs silence
new Chart(document.getElementById('scatterChart'), {{
  type: 'scatter',
  data: {{
    datasets: [{{
      label: 'experiments',
      data: fpsVals.map((f, i) => ({{ x: f, y: silenceVals[i] }})),
      backgroundColor: committed.map(c => c ? '#3fb950' : 'rgba(88,166,255,0.7)'),
      pointRadius: 5,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#c9d1d9' }} }} }},
    scales: {{
      x: {{ title: {{ display: true, text: 'fps', color: '#8b949e' }},
             ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }},
      y: {{ title: {{ display: true, text: 'silence_ratio', color: '#8b949e' }},
             ticks: {{ color: '#8b949e' }}, grid: {{ color: '#21262d' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html)
    print(f"✓ HTML-rapport: {output_path}")
    print(f"  Open met: open {output_path}")


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_json(output_path: Path) -> None:
    results = load_all_results()
    baseline = load_baseline_score()
    best = max(results, key=lambda r: r.extraction_score) if results else None

    summary = {
        "generated_at": datetime.now().isoformat(),
        "baseline_score": baseline,
        "best_score": best.extraction_score if best else 0,
        "total_experiments": len(results),
        "committed_experiments": sum(1 for r in results if r.extraction_score > baseline * 1.005),
        "best_config": best.config_summary if best else None,
        "best_experiment_id": best.experiment_id if best else None,
        "experiments": [asdict(r) for r in results],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"✓ JSON-samenvatting: {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Stemmy Benchmark Dashboard")
    parser.add_argument("--watch", action="store_true",
                        help="Live dashboard (iedere 3 seconden bijwerken)")
    parser.add_argument("--refresh", type=int, default=3,
                        help="Refresh-interval in seconden (default: 3)")
    parser.add_argument("--export-html", type=Path, default=None,
                        help="Exporteer HTML-rapport naar opgegeven pad")
    parser.add_argument("--export-json", type=Path, default=None,
                        help="Exporteer JSON-samenvatting naar opgegeven pad")
    args = parser.parse_args()

    if args.export_html:
        export_html(args.export_html)
    elif args.export_json:
        export_json(args.export_json)
    else:
        render_dashboard(watch=args.watch, refresh_sec=args.refresh)


if __name__ == "__main__":
    main()
