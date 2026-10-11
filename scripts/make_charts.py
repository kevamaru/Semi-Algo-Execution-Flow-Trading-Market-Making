"""
Build the README figures from data/sessions.csv.

Usage:  python scripts/make_charts.py
Output: figures/pnl_by_session.png, figures/fines_by_session.png,
        figures/cumulative_pnl.png
"""
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "figures"

# palette (validated: categorical slots 1-2 on the light surface)
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
PHASE_COLOR = {"manual": "#eb6834", "semi-algo": "#2a78d6"}
PHASE_LABEL = {"manual": "Manual (discretionary)", "semi-algo": "Semi-algo (with the tool)"}

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.titlecolor": INK, "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
})
money = FuncFormatter(lambda v, _: f"{v / 1000:+.0f}k" if v else "0")


def load():
    with open(ROOT / "data" / "sessions.csv") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k in ("pnl", "total_fines", "passive_share", "passive_volume", "active_volume"):
            r[k] = float(r[k])
    return rows


def style(ax, title, subtitle=None):
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", pad=22 if subtitle else 10)
    if subtitle:
        ax.text(0, 1.02, subtitle, transform=ax.transAxes, color=INK2, fontsize=9.5)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)


def labels(rows):
    out, m, a = [], 0, 0
    for r in rows:
        if r["phase"] == "manual":
            m += 1
            out.append(f"M{m}")
        else:
            a += 1
            out.append(f"A{a}")
    return out


def legend(ax, loc="upper left"):
    ax.legend(handles=[Patch(color=PHASE_COLOR[p], label=PHASE_LABEL[p]) for p in PHASE_COLOR],
              frameon=False, loc=loc, fontsize=9, labelcolor=INK2)


def divider(ax, rows):
    k = sum(r["phase"] == "manual" for r in rows) - 0.5
    ax.axvline(k, color=AXIS, linewidth=1, linestyle=(0, (3, 3)))
    ax.text(k + 0.15, ax.get_ylim()[1], "tool introduced", color=MUTED, fontsize=8.5, va="top")


def bar_chart(rows, key, title, subtitle, fname, signed=True, mark_zero=False):
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=160)
    x = range(len(rows))
    vals = [r[key] for r in rows]
    ax.bar(x, vals, width=0.72, color=[PHASE_COLOR[r["phase"]] for r in rows],
           edgecolor=SURFACE, linewidth=2)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.set_xticks(list(x), labels(rows))
    ax.yaxis.set_major_formatter(money if signed else FuncFormatter(lambda v, _: f"{v / 1000:.0f}k"))
    style(ax, title, subtitle)
    lo, hi = min(vals + [0]), max(vals)
    ax.set_ylim(lo * 1.15 if lo < 0 else 0, hi * 1.3)
    # selective direct labels: best and worst session only (largest only for an all-positive measure)
    picks = (vals.index(max(vals)), vals.index(min(vals))) if signed else (vals.index(max(vals)),)
    for i in picks:
        v = vals[i]
        ax.text(i, v + (3000 if v >= 0 else -3000), f"{v / 1000:+.0f}k" if signed else f"{v / 1000:.0f}k",
                ha="center", va="bottom" if v >= 0 else "top", fontsize=8.5, color=INK)
    if mark_zero:
        for i, v in enumerate(vals):
            if v == 0:
                ax.text(i, hi * 0.02, "0", ha="center", va="bottom", fontsize=8.5, color=INK2)
    legend(ax, loc="upper right" if not signed else "upper left")
    divider(ax, rows)
    fig.tight_layout()
    fig.savefig(FIG / fname)
    plt.close(fig)


def cumulative(rows):
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=160)
    total, ys = 0.0, []
    for r in rows:
        total += r["pnl"]
        ys.append(total)
    n_m = sum(r["phase"] == "manual" for r in rows)
    xs = list(range(1, len(rows) + 1))
    ax.plot(xs[:n_m], ys[:n_m], color=PHASE_COLOR["manual"], linewidth=2, marker="o", markersize=5,
            markeredgecolor=SURFACE, markeredgewidth=1.5)
    ax.plot(xs[n_m - 1:], ys[n_m - 1:], color=PHASE_COLOR["semi-algo"], linewidth=2, marker="o", markersize=5,
            markeredgecolor=SURFACE, markeredgewidth=1.5, markevery=list(range(1, len(xs) - n_m + 1)))
    ax.plot(xs[n_m - 1], ys[n_m - 1], "o", color=PHASE_COLOR["manual"], markersize=5,
            markeredgecolor=SURFACE, markeredgewidth=1.5)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.set_xticks(xs, labels(rows))
    ax.yaxis.set_major_formatter(money)
    style(ax, "Cumulative P&L", "Net of fines, across every session with a saved RIT report")
    ax.text(xs[-1] + 0.2, ys[-1], f"{ys[-1] / 1000:+.0f}k", va="center", fontsize=9, color=INK)
    ax.set_xlim(0.5, len(rows) + 1.2)
    legend(ax)
    ax.axvline(n_m + 0.5, color=AXIS, linewidth=1, linestyle=(0, (3, 3)))
    ax.text(n_m + 0.65, ax.get_ylim()[1], "tool introduced", color=MUTED, fontsize=8.5, va="top")
    fig.tight_layout()
    fig.savefig(FIG / "cumulative_pnl.png")
    plt.close(fig)


def passive(rows):
    fig, ax = plt.subplots(figsize=(9, 4.2), dpi=160)
    x = range(len(rows))
    vals = [r["passive_share"] * 100 for r in rows]
    ax.bar(x, vals, width=0.72, color=[PHASE_COLOR[r["phase"]] for r in rows], edgecolor=SURFACE, linewidth=2)
    ax.set_ylim(0, 105)
    ax.set_xticks(list(x), labels(rows))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0f}%"))
    style(ax, "Share of unwind volume executed passively",
          "Limit orders resting in the book vs. orders crossing the spread")
    legend(ax, loc="lower left")
    divider(ax, rows)
    fig.tight_layout()
    fig.savefig(FIG / "passive_share.png")
    plt.close(fig)


def main():
    FIG.mkdir(exist_ok=True)
    rows = load()
    bar_chart(rows, "pnl", "P&L per session", "Net of fines · M = manual, A = semi-algo", "pnl_by_session.png")
    bar_chart(rows, "total_fines", "Fines per session", "Case fines + trading-limit fines",
              "fines_by_session.png", signed=False, mark_zero=True)
    cumulative(rows)
    print("figures written to", FIG)


if __name__ == "__main__":
    main()
