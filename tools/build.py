"""Build the profile README and its figures.

Reads data/tools.toml, data/evidence.toml, and tools/template.md, plus weekly
GitHub contribution counts cached in .cache/contributions/. Writes
assets/{header,evidence,map}-{light,dark}.svg and README.md.

Contribution counts come from the GitHub GraphQL API through the `gh` command.
A year is fetched only when its cache file is missing; --refresh refetches the
current year. Given the same cache and data, the output is byte-identical.

    python3 tools/build.py
    python3 tools/build.py --refresh
"""

import argparse
import json
import math
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
CACHE = ROOT / ".cache" / "contributions"

LOGIN = "eneskemalergin"
FIRST_YEAR = 2014  # the account was created in May 2014

# Line colours pass colour-blind separation and 3:1 contrast against GitHub's
# light (#ffffff) and dark (#0d1117) page backgrounds.
THEMES = {
    "light": dict(
        surface="#ffffff", ink="#1f2328", ink2="#59636e", ink3="#818b98", grid="#d1d9e0",
        analysis="#7c4dcc", ms="#008f86", seq="#c26a00", ngs="#b6203e", base="#8c959f",
        ridge_back="#008f86", ridge_front="#c26a00",
    ),
    "dark": dict(
        surface="#0d1117", ink="#e6edf3", ink2="#9198a1", ink3="#6e7681", grid="#21262d",
        analysis="#9479ea", ms="#1aa89a", seq="#cf7f12", ngs="#c61047", base="#6e7681",
        ridge_back="#1aa89a", ridge_front="#cf7f12",
    ),
}

# An SVG shown as an image cannot load fonts, so text uses the reader's system sans.
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
WIDTH = 960
EVIDENCE_MAX = 300  # right end of the evidence figure's log scale


def fail(msg):
    sys.exit(f"build.py: {msg}")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def svg_open(t, height, title, desc, css):
    # The figure paints its own background: ridges and stations are filled with
    # it to hide what lies behind them, and it keeps the figure readable on page
    # colours other than GitHub's default light and dark.
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" height="{height}" '
        'role="img" aria-labelledby="t d">',
        f'<title id="t">{esc(title)}</title>',
        f'<desc id="d">{esc(desc)}</desc>',
        f"<style>text{{font-family:{FONT}}} {css}</style>",
        f'<rect width="{WIDTH}" height="{height}" rx="6" fill="{t["surface"]}"/>',
    ]


def mix(a, b, f):
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(pa, pb))


def ratio(v):
    return f"{v:.0f}x" if v >= 100 or (v >= 10 and v == round(v)) else f"{v:.1f}x"


def ratio_range(lo, hi):
    return ratio(lo) if lo == hi else f"{ratio(lo)} to {ratio(hi)}"


# ------------------------------------------------------------------ data

def load_data():
    tools = tomllib.loads((ROOT / "data" / "tools.toml").read_text())
    evidence = tomllib.loads((ROOT / "data" / "evidence.toml").read_text())["rows"]
    lines = {line["id"]: line for line in tools["lines"]}
    if "base" not in lines:
        fail("data/tools.toml needs a line with id = \"base\" for the shared libraries")
    for t in tools["tools"]:
        if t["line"] not in lines:
            fail(f"{t['name']}: unknown line {t['line']!r}")
        if t["status"] not in ("public", "dev", "hidden"):
            fail(f"{t['name']}: status must be public, dev, or hidden")
        if t["status"] == "public" and not t.get("url", "").startswith("https://"):
            fail(f"{t['name']}: a public tool needs an https url")
    for r in evidence:
        if r["line"] not in lines:
            fail(f"evidence {r['tool']}: unknown line {r['line']!r}")
        for key in ("fast", "small"):
            lo, hi = r[key]
            if not 0 < lo <= hi:
                fail(f"evidence {r['tool']} vs {r['peer']}: {key} must be [low, high] with 0 < low <= high")
            if hi > EVIDENCE_MAX:
                fail(f"evidence {r['tool']} vs {r['peer']}: {key} is past the figure's {EVIDENCE_MAX}x scale")
        if not r["source"].startswith("https://"):
            fail(f"evidence {r['tool']}: source must be an https url")
    shown = [t for t in tools["tools"] if t["status"] != "hidden"]
    return lines, shown, evidence


def fetch_year(year):
    query = (
        f'{{user(login:"{LOGIN}"){{contributionsCollection(from:"{year}-01-01T00:00:00Z",to:"{year}-12-31T23:59:59Z")'
        "{contributionCalendar{weeks{contributionDays{date contributionCount}}}}}}"
    )
    try:
        out = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"],
                             check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        fail(f"fetching {year} contributions with gh failed: {e}")
    weeks = json.loads(out)["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    days = [[d["date"], d["contributionCount"]] for w in weeks for d in w["contributionDays"]]
    return {"fetched": date.today().isoformat(), "days": days}


def weekly(counts):
    """Contributions per week of one year, from its daily counts.

    Weeks run seven days from 1 January. The one or two days left at the end of
    a full year join week 52, and a week still in progress is left off. Each
    week counts as its daily mean times seven, so a longer week 52 is not
    overstated.
    """
    weeks = [counts[k:k + 7] for k in range(0, len(counts), 7)]
    if len(weeks) > 52:
        weeks[51:] = [sum(weeks[51:], [])]
    elif weeks and len(weeks[-1]) < 7:
        weeks.pop()
    return [sum(w) * 7 / len(w) for w in weeks]


def contributions(refresh):
    """Weekly counts per year, the newest year's total so far, and the date it was fetched."""
    CACHE.mkdir(parents=True, exist_ok=True)
    this_year = date.today().year
    years, total, fetched = {}, 0, None
    for year in range(FIRST_YEAR, this_year + 1):
        path = CACHE / f"{year}.json"
        cached = json.loads(path.read_text()) if path.exists() else None
        # a year fetched before it ended is incomplete: refetch it once it is over
        complete = cached is not None and cached["fetched"] > f"{year}-12-31"
        if cached is None or (not complete and (year < this_year or refresh)):
            cached = fetch_year(year)
            path.write_text(json.dumps(cached))
        fetched = cached["fetched"]
        # days after the fetch date are not over yet; leave them off the ridge
        counts = [n for d, n in cached["days"] if d <= cached["fetched"]]
        years[year], total = weekly(counts), sum(counts)
    return years, total, fetched


# ------------------------------------------------------------------ header

def header(t, years, total, fetched):
    height = 250
    x0, x1, amp = 330, 906, 150
    order = sorted(years)
    peak = max(max(w) for w in years.values() if w)
    # the oldest and newest baselines stay put; each new year narrows the spacing
    top, bottom = 60, height - 34
    step = (bottom - top) / max(1, len(order) - 1)
    newest = order[-1]
    asof = date.fromisoformat(fetched)
    o = svg_open(t, height, "Enes K. Ergin, PhD",
                 f"Bioinformatician in Vancouver: correct, fast, lightweight tools for biological data. "
                 f"Behind the name, one ridge per year of weekly GitHub contributions from {order[0]} to {asof:%B %Y}, "
                 f"newest in front; {total:,} contributions in {newest} so far.",
                 f".nm{{font-size:30px;font-weight:600;fill:{t['ink']}}} .tg{{font-size:15px;fill:{t['ink2']}}} "
                 f".yr{{font-size:10px;fill:{t['ink3']}}} .cp{{font-size:11px;fill:{t['ink3']}}}")
    for i, year in enumerate(order):
        by = top + i * step
        f = i / max(1, len(order) - 1)
        colour = mix(t["ridge_back"], t["ridge_front"], f)
        if year in (order[0], order[len(order) // 2], newest):
            o.append(f'<text class="yr" x="{x1 + 8}" y="{by + 3:.1f}">{year}</text>')
        weeks = years[year]
        if not weeks:
            continue
        # three-week moving average, on one linear scale shared by every year
        smooth = [sum(weeks[max(0, k - 1):k + 2]) / len(weeks[max(0, k - 1):k + 2]) for k in range(len(weeks))]
        pts = [(x0 + k * (x1 - x0) / 51, by - v / peak * amp) for k, v in enumerate(smooth)]
        # each segment leaves and enters its points level; with even spacing, S
        # reflects the previous control point to exactly that tangent
        curve = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
        for k, ((ax, ay), (bx, bb)) in enumerate(zip(pts, pts[1:])):
            mx = (ax + bx) / 2
            curve += (f"C{mx:.1f},{ay:.1f} " if k == 0 else "S") + f"{mx:.1f},{bb:.1f} {bx:.1f},{bb:.1f}"
        # the area is filled to hide the ridges behind it; only the curve is stroked
        area = f"M{x0},{by:.1f}L{curve[1:]}L{pts[-1][0]:.1f},{by:.1f}Z"
        if year == newest:
            o.append(f'<path d="{area}" fill="{mix(t["surface"], colour, 0.12)}"/>')
            o.append(f'<path d="{curve}" fill="none" stroke="{colour}" stroke-width="2.2" stroke-linejoin="round" '
                     'stroke-linecap="round"/>')
            # the newest ridge stops at the last full week; a dot marks it
            o.append(f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3.5" fill="{colour}"/>')
        else:
            o.append(f'<path d="{area}" fill="{t["surface"]}"/>')
            o.append(f'<path d="{curve}" fill="none" stroke="{colour}" stroke-width="1.4" '
                     f'stroke-opacity="{0.35 + 0.65 * f:.2f}" stroke-linejoin="round"/>')
    o.append('<text class="nm" x="24" y="58">Enes K. Ergin, PhD</text>')
    o.append('<text class="tg" x="24" y="86">Bioinformatician in Vancouver</text>')
    o.append('<text class="tg" x="24" y="108">Correct, fast, lightweight tools</text>')
    o.append('<text class="tg" x="24" y="130">for biological data</text>')
    # the caption sits under the plot, flush with the right end of the ridges
    o.append(f'<text class="cp" x="{x1}" y="{height - 14}" text-anchor="end">Ridges: weekly GitHub contributions '
             f'per year, {order[0]} to {asof:%B %Y}</text>')
    o.append("</svg>")
    return "\n".join(o)


# ------------------------------------------------------------------ evidence

def evidence_summary(rows):
    return "; ".join(f"{r['tool']} against {r['peer']} ({r['task']}): {r['correct']}, "
                     f"{ratio_range(*r['fast'])} faster, {ratio_range(*r['small'])} less memory" for r in rows)


def evidence(t, rows):
    top, row_h = 104, 50
    n = len(rows)
    height = top + row_h * (n - 1) + 40
    label_x, correct_x, plot_w = 24, 262, 196
    panels = [("Fast", "peer time / tool time", "fast", 476), ("Small", "peer peak memory / tool's", "small", 726)]
    hi = math.log10(EVIDENCE_MAX)
    o = svg_open(t, height, "Correct, fast, and small, against named tools", evidence_summary(rows) + ".",
                 f".n{{font-size:14px;font-weight:600;fill:{t['ink']}}} .r{{font-size:12px;fill:{t['ink2']}}} "
                 f".h{{font-size:15px;font-weight:600;fill:{t['ink']}}} .k{{font-size:11px;fill:{t['ink3']}}} "
                 f".v{{font-size:12px;font-weight:600;fill:{t['ink']}}} .c{{font-size:12px;fill:{t['ink']}}}")
    o.append(f'<text class="h" x="{correct_x}" y="32">Correct</text>')
    o.append(f'<text class="r" x="{correct_x}" y="50">checked before anything is timed</text>')
    grid_bottom = top + row_h * (n - 1) + 22
    for title, sub, key, px in panels:
        def X(v, px=px):
            return px + math.log10(v) / hi * plot_w
        o.append(f'<text class="h" x="{px}" y="32">{title}</text>')
        o.append(f'<text class="r" x="{px}" y="50">{sub}</text>')
        for tick in (1, 3, 10, 30, 100, 300):
            x = X(tick)
            o.append(f'<line x1="{x:.1f}" y1="{top - 26}" x2="{x:.1f}" y2="{grid_bottom}" stroke="{t["grid"]}" stroke-width="1"/>')
            o.append(f'<text class="k" x="{x:.1f}" y="{top - 32}" text-anchor="middle">{tick}x</text>')
        for i, r in enumerate(rows):
            y = top + i * row_h
            colour = t[r["line"]]
            lo, hi_v = r[key]
            o.append(f'<line x1="{X(1):.1f}" y1="{y}" x2="{X(hi_v):.1f}" y2="{y}" stroke="{colour}" stroke-width="2" '
                     'stroke-linecap="round" opacity=".5"/>')
            if lo == hi_v:
                o.append(f'<circle cx="{X(hi_v):.1f}" cy="{y}" r="6" fill="{colour}" stroke="{t["surface"]}" stroke-width="2"/>')
            else:
                # a range is a dot stretched from low to high: a capsule, outlined like the dots
                for width, stroke in ((16, t["surface"]), (12, colour)):
                    o.append(f'<line x1="{X(lo):.1f}" y1="{y}" x2="{X(hi_v):.1f}" y2="{y}" stroke="{stroke}" '
                             f'stroke-width="{width}" stroke-linecap="round"/>')
            o.append(f'<text class="v" x="{X(hi_v) + 11:.1f}" y="{y + 4}">{ratio_range(lo, hi_v)}</text>')
    for i, r in enumerate(rows):
        y = top + i * row_h
        o.append(f'<rect x="{label_x}" y="{y - 7}" width="4" height="14" rx="2" fill="{t[r["line"]]}"/>')
        o.append(f'<text class="n" x="{label_x + 14}" y="{y - 1}">{esc(r["tool"])} '
                 f'<tspan class="r" font-weight="400">vs {esc(r["peer"])}</tspan></text>')
        o.append(f'<text class="r" x="{label_x + 14}" y="{y + 15}">{esc(r["task"])}</text>')
        o.append(f'<path d="M{correct_x} {y - 1} l4 4 l8 -9" fill="none" stroke="{t["ink2"]}" stroke-width="2" '
                 'stroke-linecap="round" stroke-linejoin="round"/>')
        o.append(f'<text class="c" x="{correct_x + 18}" y="{y + 4}">{esc(r["correct"])}</text>')
    o.append("</svg>")
    return "\n".join(o)


def evidence_note(rows):
    months = sorted({date.fromisoformat(r["measured"]).strftime("%Y-%m") for r in rows})
    first, last = (date.fromisoformat(m + "-01") for m in (months[0], months[-1]))
    when = f"in {first:%B %Y}" if first == last else f"from {first:%B %Y} to {last:%B %Y}"
    sources = {}
    for r in rows:
        sources.setdefault(r["tool"], r["source"])
    links = " | ".join(f"[{tool}]({url})" for tool, url in sources.items())
    return f"Same machine and inputs for both tools, measured {when}. Methods and caveats: {links}"


# ------------------------------------------------------------------ map

def transit(t, lines, tools):
    order = list(lines)
    trunk_x, gap, bracket_extra = 232, 118, 44
    ys, y = {}, 92
    for key in order:
        ys[key] = y
        has_bracket = any(s.get("focus") for s in tools if s["line"] == key)
        y += gap + (bracket_extra if has_bracket else 0)
    height = ys[order[-1]] + 92
    branches = [k for k in order if lines[k].get("branch")]
    o = svg_open(t, height, "Map of the work",
                 f"{len(order)} lines of tools: " + "; ".join(
                     f"{lines[k]['title']} ({', '.join(s['name'] for s in tools if s['line'] == k)})" for k in order)
                 + ". The " + ", ".join(lines[k]["title"] for k in branches)
                 + " lines branch from the shared libraries. Solid rings are public, dashed rings are in development, and larger rings mark tools others build on.",
                 f".n{{font-size:13px;font-weight:600;fill:{t['ink']}}} .r{{font-size:11px;fill:{t['ink2']}}} "
                 f".h{{font-size:12px;font-weight:700;fill:{t['ink']};letter-spacing:.04em}} .m{{font-size:11px;fill:{t['ink3']}}}")
    if branches:
        top = min(ys[k] for k in branches)
        o.append(f'<path d="M{trunk_x},{ys["base"]} L{trunk_x},{top + 14} Q{trunk_x},{top} {trunk_x + 14},{top}" '
                 f'fill="none" stroke="{t["base"]}" stroke-width="7" stroke-linecap="round"/>')
    for key in order:
        line, y, colour = lines[key], ys[key], t[key]
        st = [s for s in tools if s["line"] == key]
        if not st:
            continue
        first = trunk_x + (0 if key == "base" else 58)
        # the last station stays far enough from the right edge to center its labels
        step = min(130, (890 - first) / max(1, len(st) - 1))
        xs = [first + i * step for i in range(len(st))]
        o.append(f'<text class="h" x="24" y="{y - 4}">{esc(line["title"].upper())}</text>')
        o.append(f'<text class="m" x="24" y="{y + 12}">{esc(line["stack"])}, since {line["since"]}</text>')
        o.append(f'<line x1="{trunk_x}" y1="{y}" x2="{xs[-1]:.1f}" y2="{y}" stroke="{colour}" stroke-width="7" stroke-linecap="round"/>')
        if key in branches:
            o.append(f'<circle cx="{trunk_x}" cy="{y}" r="7" fill="{t["surface"]}" stroke="{t["base"]}" stroke-width="3"/>')
        for i, (s, x) in enumerate(zip(st, xs)):
            if s["status"] == "public":
                o.append(f'<circle cx="{x:.1f}" cy="{y}" r="{10 if s.get("hub") else 7}" fill="{t["surface"]}" '
                         f'stroke="{colour}" stroke-width="4"/>')
            else:
                o.append(f'<circle cx="{x:.1f}" cy="{y}" r="6" fill="{t["surface"]}" stroke="{colour}" stroke-width="2.5" '
                         'stroke-dasharray="2.6 2.4"/>')
            # labels alternate above and below; the base line starts below, clear of the trunk
            above = i % 2 == (1 if key == "base" else 0)
            ny = y - 36 if above else y + 30
            o.append(f'<text class="n" x="{x:.1f}" y="{ny}" text-anchor="middle">{esc(s["name"])}</text>')
            o.append(f'<text class="r" x="{x:.1f}" y="{ny + 14}" text-anchor="middle">{esc(s["role"])}</text>')
        groups = {}
        for s, x in zip(st, xs):
            if s.get("focus"):
                groups.setdefault(s["focus"], []).append(x)
        for focus, gx in groups.items():
            bx0, bx1, by = min(gx) - 14, max(gx) + 14, y + 58
            o.append(f'<path d="M{bx0:.1f},{by - 6} L{bx0:.1f},{by} L{bx1:.1f},{by} L{bx1:.1f},{by - 6}" fill="none" '
                     f'stroke="{t["ink3"]}" stroke-width="1.2"/>')
            o.append(f'<text class="r" x="{(bx0 + bx1) / 2:.1f}" y="{by + 15}" text-anchor="middle">'
                     f'{esc(focus[0].lower() + focus[1:])}</text>')
    ly = height - 16
    o.append(f'<circle cx="32" cy="{ly - 4}" r="6" fill="{t["surface"]}" stroke="{t["ink2"]}" stroke-width="3.5"/>')
    o.append(f'<text class="r" x="46" y="{ly}">public</text>')
    o.append(f'<circle cx="108" cy="{ly - 4}" r="6" fill="{t["surface"]}" stroke="{t["ink2"]}" stroke-width="2.5" stroke-dasharray="2.6 2.4"/>')
    o.append(f'<text class="r" x="122" y="{ly}">in development</text>')
    o.append(f'<circle cx="236" cy="{ly - 4}" r="8" fill="{t["surface"]}" stroke="{t["ink2"]}" stroke-width="4"/>')
    o.append(f'<text class="r" x="252" y="{ly}">other tools build on it</text>')
    o.append("</svg>")
    return "\n".join(o)


# ------------------------------------------------------------------ README lists

def details(lines, tools):
    def item(s, pad=""):
        if s["status"] == "public":
            return f"{pad}- **[{s['name']}]({s['url']})**: {s['summary']}"
        return f"{pad}- **{s['name']}** *(in development)*: {s['summary']}"

    blocks = []
    for key, line in lines.items():
        st = [s for s in tools if s["line"] == key]
        if not st:
            continue
        public = sum(s["status"] == "public" for s in st)
        count = ", ".join(c for c in (f"{public} public" if public else "",
                                      f"{len(st) - public} in development" if len(st) > public else "") if c)
        rows, seen = [], set()
        for s in st:
            focus = s.get("focus")
            if not focus:
                rows.append(item(s))
            elif focus not in seen:
                seen.add(focus)
                rows.append(f"- {focus}:")
                rows.extend(item(x, "  ") for x in st if x.get("focus") == focus)
        blocks.append(f"<details>\n<summary><b>{line['title']}</b> | {line['stack']} | {count}</summary>\n\n"
                      + "\n".join(rows) + "\n\n</details>")
    return "\n\n".join(blocks)


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refresh", action="store_true", help="refetch this year's contribution counts")
    args = ap.parse_args()

    lines, tools, rows = load_data()
    years, total, fetched = contributions(args.refresh)

    ASSETS.mkdir(exist_ok=True)
    for theme, t in THEMES.items():
        for name, svg in (("header", header(t, years, total, fetched)), ("evidence", evidence(t, rows)),
                          ("map", transit(t, lines, tools))):
            ET.fromstring(svg)  # raises on malformed markup before it is written
            (ASSETS / f"{name}-{theme}.svg").write_text(svg + "\n")

    readme = (ROOT / "tools" / "template.md").read_text()
    for key, value in (("{{DETAILS}}", details(lines, tools)), ("{{EVIDENCE_NOTE}}", evidence_note(rows)),
                       ("{{EVIDENCE_ALT}}", esc(evidence_summary(rows)))):
        readme = readme.replace(key, value)
    if "{{" in readme:
        fail("tools/template.md has a placeholder build.py does not fill")
    if not readme.isascii():
        fail("README.md would contain non-ASCII text")
    (ROOT / "README.md").write_text(readme)


if __name__ == "__main__":
    main()
