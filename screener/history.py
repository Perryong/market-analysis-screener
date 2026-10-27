"""Persist per-symbol chart + analysis as Markdown history files, committed to git.

Each report run writes one <timestamp>.md + <timestamp>.png per symbol under
history/<SYM>/, then git add/commit/push the whole history/ tree (best-effort —
a git failure never blocks the Telegram send). Read-only market data.
"""
import shutil
import subprocess
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .reporting import number as _fmt
from .shared import progress

SGT = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent  # repo root
HISTORY = ROOT / "history"


def _fvg_text(fvgs):
    if not fvgs:
        return "no open FVG near price"
    return " · ".join(f"{t}: {_fmt(bot)}–{_fmt(top)}" for t, bot, top in fvgs)


def write(symbol, fields, png, now):
    """Write history/<SYM>/<stamp>.md (+ .png) and return the md path."""
    progress('publisher','archive',symbol)
    ts = datetime.fromtimestamp(now, SGT)
    stamp = ts.strftime("%Y-%m-%d_%H%M%S")
    d = HISTORY / symbol
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stamp}.png").write_bytes(png)
    if fields.get('snapshot') is not None:
        (d / f'{stamp}.json').write_text(json.dumps(fields['snapshot'], indent=2, allow_nan=False), encoding='utf-8')
    trend = "↑ (up)" if fields["trend_up"] else "↓ (down)"
    lines = [
        f"# {symbol} · {ts.strftime('%Y-%m-%d %H:%M:%S')} +08",
        "",
        f"**Signal:** {fields['top']}",
        "",
        f"**{fields['dir_label']}:** {fields['d_txt']}",
        "",
        f"**{fields['comb_txt']}**",
        "",
        f"- Price: {fields['price']}",
        f"- Upper pivot: {fields['upper']} · Lower pivot: {fields['lower']}",
        f"- 1H trend: {trend}",
        f"- 1H closed: {fields['closed_open']} → {fields['closed_close']} ({fields['chg']:+.2f}%)",
        f"- Open FVG: {_fvg_text(fields['fvgs_near'])}",
        f"- Setup: {fields['setup_txt']}",
        f"- Evidence: {fields.get('data_note', 'Unavailable')}",
        f"- Read mode: {fields.get('read_mode', 'Legacy unvalidated read')}",
        f"- Last completed: 1H {fields['last_1h']} · {fields['tf']} {fields['last_tf']}",
        "",
        f"![Chart]({stamp}.png)",
        "",
    ]
    if fields["prev_read"]:
        lines += ["## Previous read", "", fields["prev_read"], ""]
    lines += ["## Verified read", "", fields["ai_read"] or "—", ""]
    if fields.get('snapshot') is not None:
        lines += [f'[Evidence snapshot]({stamp}.json)', '']
    lines += ["", "*Analysis only · No orders placed*", "", fields["tv_link"], ""]
    md_path = d / f"{stamp}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


PUBLIC = ROOT / ".screener" / "live" / "public"
SITE = ROOT / "docs"  # GitHub Pages source


def publish_site(public=PUBLIC, site=SITE, history=HISTORY):
    """Copy the latest dashboard to docs/ and rebuild the read-only office there. Returns paths to stage."""
    from .office import export
    paths = []
    for name in ("index.html", "latest.json"):
        if (public / name).exists():
            shutil.copyfile(public / name, site / name)
            paths.append(f"docs/{name}")
    export(site / "office", history)
    return paths + ["docs/office/"]


def git_commit(now, count):
    """Stage, commit and push history/ (+ the Pages site). Returns an error string, or None on success/nothing-new."""
    msg = f"history: {datetime.fromtimestamp(now, SGT):%Y-%m-%d %H:%M} ({count} symbols)"
    try:
        try:
            site = publish_site()
        except (OSError, ValueError) as exc:  # the site is best-effort; history still commits
            site, msg = [], msg + f" · site not updated: {exc}"
        subprocess.run(["git", "add", "history/", *site], cwd=ROOT, check=True, capture_output=True, timeout=60)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, capture_output=True, timeout=30)
        if staged.returncode == 0:
            return None  # nothing new staged
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, check=True, capture_output=True, timeout=60)
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ROOT, check=True, capture_output=True, timeout=120)
        return None
    except Exception as exc:
        return f"git history: {exc}"
