"""Make the README figures from the saved results in results/.

    python3 scripts/make_figures.py        # needs matplotlib; writes docs/img/*.png

This script only *reads* results/throughput.json, results/mutation.json and
results/regression.json. It never runs a simulation, and no plotted number is
typed in by hand: if a results file changes, rerun this and the charts follow.
"""
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
OUT = ROOT / "docs" / "img"

# Colours: one blue ramp for ordered things (ROB depth, hit fraction), one
# accent for "attention". Text stays in neutral ink, never in series colours.
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
BLUE_RAMP = ["#86b6ef", "#2a78d6", "#104281"]  # light -> dark, ordinal

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "font.size": 11, "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.edgecolor": INK2, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
    "legend.frameon": False,
})


def load(name):
    return json.loads((RES / name).read_text())


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.savefig(path, dpi=170, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


def fig_throughput():
    tp = load("throughput.json")
    rows = tp["rows"]
    mixes = sorted({r["div_mix"] for r in rows})
    depths = sorted({r["rob_depth"] for r in rows})
    fig, axes = plt.subplots(1, len(mixes), figsize=(12, 4.3), sharey=True)
    for ax, mix in zip(axes, mixes):
        for colour, d in zip(BLUE_RAMP, depths):
            pts = sorted((r["ready_p"], r["ops_per_cycle"]) for r in rows
                         if r["div_mix"] == mix and r["rob_depth"] == d)
            xs, ys = zip(*pts)
            ax.plot(xs, ys, color=colour, lw=2, marker="o", ms=6,
                    markeredgecolor="white", markeredgewidth=1.2, label=f"ROB = {d}")
        ax.set_title(f"{mix:.0%} divides")
        ax.set_xlabel("out_ready probability (1.0 = never stalled)")
        ax.set_xlim(0.2, 1.05)
        ax.set_ylim(0, 1.05)
    axes[0].set_ylabel("throughput (ops / cycle)")
    axes[0].legend(loc="upper left")
    fig.suptitle("Throughput: output stalls, divide mix and ROB depth "
                 f"({rows[0]['cycles']:,}-cycle window per point, one run each)",
                 x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout()
    save(fig, "throughput.png")


def fig_mutation():
    mu = load("mutation.json")
    cols = [("SV seeds", len(mu["sv_seeds"]), "sv_caught"),
            ("cocotb directed tests", len(mu["directed_tests"]), "directed_caught"),
            ("cocotb random seeds", len(mu["random_seeds"]), "random_caught")]
    rows = mu["rows"]
    labels = [r["bug"].replace("none (bug-free RTL)", "bug-free RTL (baseline)") for r in rows]
    frac = [[len(r[key]) / n for _, n, key in cols] for r in rows]
    cmap = LinearSegmentedColormap.from_list("blue", ["#f4f3f0", "#cde2fb", "#5598e7", "#104281"])
    fig, ax = plt.subplots(figsize=(9.5, 5.6))
    ax.imshow(frac, cmap=cmap, vmin=0, vmax=1, aspect="auto")
    ax.grid(False)
    for i, r in enumerate(rows):
        for j, (_, n, key) in enumerate(cols):
            k = len(r[key])
            ax.text(j, i, f"{k}/{n}", ha="center", va="center", fontsize=11,
                    color="white" if frac[i][j] > 0.6 else INK, fontweight="bold" if k else "normal")
        caught = ", ".join(r["check_types"]) or "no failures (correct)"
        ax.text(len(cols) - 0.4, i, caught, ha="left", va="center", fontsize=9.5, color=INK2)
    ax.set_xticks(range(len(cols)), [f"{c.replace(' ', chr(10), 1)}\n(fail / {n})" for c, n, _ in cols])
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(rows)), labels, fontsize=10)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_xlim(-0.5, len(cols) + 0.9)
    ax.text(len(cols) - 0.4, -0.75, "caught by", fontsize=10, color=INK2, fontweight="bold")
    ax.set_title(f"Bug injection: {mu['bugs_detected']}/{mu['bugs_total']} bugs caught, "
                 f"baseline false alarms: {'yes' if mu['baseline_false_alarms'] else 'none'}",
                 pad=62)
    fig.tight_layout()
    save(fig, "bug_matrix.png")


def fig_coverage():
    cov = load("regression.json")["coverage"]
    curve = cov["closure_curve"]
    total = cov["bins_total"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.3), gridspec_kw={"width_ratios": [1.15, 1]})
    # x = number of random seeds run after the directed tests (0 = directed only).
    pts = [(0 if c["after"] == "directed" else int(c["after"].removeprefix("seed")), c["bins_hit"])
           for c in curve]
    closed_at = next(x for x, y in pts if y == total)
    shown = [(x, y) for x, y in pts if x <= closed_at + 6]
    xs, ys = zip(*shown)
    a1.axhline(total, color=INK2, lw=1, ls="--")
    a1.plot(xs, ys, color=BLUE_RAMP[1], lw=2, marker="o", ms=7, markeredgecolor="white",
            markeredgewidth=1.2)
    for x, y in shown:
        if x <= closed_at:
            a1.annotate(f"{y}", (x, y), textcoords="offset points", xytext=(10, -14),
                        ha="left", fontsize=9.5, color=INK2)
    last_x, last_y = pts[-1]
    a1.text(max(xs), total - 6, f"still {last_y}/{total} after seed {last_x}", ha="right",
            va="top", color=INK2, fontsize=10)
    a1.set_xticks(xs, ["directed" if x == 0 else f"+{x}" for x in xs])
    a1.set_xlabel("random seeds run after the directed tests")
    a1.set_ylim(0, total * 1.12)
    a1.set_ylabel("coverage bins hit (cumulative)")
    a1.set_title(f"Closure: all {total} bins after directed tests + {closed_at} random seeds")

    groups = sorted(cov["groups"].items(), key=lambda kv: kv[1]["min_hits"])
    names = [g for g, _ in groups]
    mins = [v["min_hits"] for _, v in groups]
    a2.barh(names, mins, color=BLUE_RAMP[1], height=0.6)
    a2.set_xscale("log")
    a2.grid(axis="y", visible=False)
    for i, (g, v) in enumerate(groups):
        a2.text(v["min_hits"] * 1.12, i, f"{v['min_hits']:,}  ({v['hit']}/{v['total']} bins)",
                va="center", fontsize=9.5, color=INK2)
    a2.set_xlim(1, max(mins) * 20)
    a2.set_xlabel("hits in the least-hit bin of each group (log scale)")
    a2.set_title("How deep is each group? (min hits per bin)")
    fig.tight_layout()
    save(fig, "coverage.png")


if __name__ == "__main__":
    fig_throughput()
    fig_mutation()
    fig_coverage()
