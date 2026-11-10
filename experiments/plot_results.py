#!/usr/bin/env python3
"""Plots for the experiment results (works on any machine with matplotlib).

    python3 experiments/plot_results.py results/<timestamp>
"""

import json
import os
import sys

import matplotlib
import matplotlib.ticker

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

INK = "#1f1f1e"
MUTED = "#6b6a63"
GRID = "#e4e3de"
BEST_EFFORT = "#8a8981"
SLICE_COLORS = {"video": "#2a78d6", "control": "#eb6834", "iot": "#1baf7a", "backup": "#eda100"}


def color_for(label):
    for name, color in SLICE_COLORS.items():
        if name in label.lower() or name.upper() in label:
            return color
    if "rtt" in label.lower():
        return SLICE_COLORS["control"]
    return BEST_EFFORT


def style(ax, ylabel):
    ax.set_ylabel(ylabel, color=MUTED)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, length=0)


def shade_active(ax, events):
    """Shade the interval in which the slice was active and label the toggles."""
    if len(events) >= 2:
        ax.axvspan(events[0][0], events[1][0], color="#f1f0eb", zorder=0)
        ax.text((events[0][0] + events[1][0]) / 2, 1.01, events[0][1].replace(" ON", " active"),
                transform=ax.get_xaxis_transform(), ha="center", va="bottom", color=MUTED, fontsize=9)


def timeseries(ax, data, ylabel, log=False):
    ends = []
    for label, points in data["series"].items():
        if not points:
            continue
        xs, ys = zip(*points)
        color = color_for(label)
        ax.plot(xs, ys, color=color, linewidth=2, label=label)
        ends.append([ys[-1], xs[-1], label.split(" (")[0]])
    shade_active(ax, data["events"])
    if log:
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: "%g" % v))
    else:
        ax.set_ylim(bottom=0)
    # direct labels at the line ends, pushed apart when two lines end close together
    ends.sort()
    gap = 0.07 * (ax.get_ylim()[1] - ax.get_ylim()[0])
    for i in range(1, len(ends)):
        if ends[i][0] - ends[i - 1][0] < gap:
            ends[i][0] = ends[i - 1][0] + gap
    for y, x, text in ends:
        ax.annotate(text, (x, y), xytext=(6, 0), textcoords="offset points",
                    va="center", color=INK, fontsize=9, annotation_clip=False)
    ax.set_xlabel("time (s)", color=MUTED)
    style(ax, ylabel)
    if len(data["series"]) > 1:
        ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, -0.15), fontsize=9, ncol=2)


def plot_all(outdir):
    with open(os.path.join(outdir, "results.json")) as fh:
        res = json.load(fh)

    fig, ax = plt.subplots(figsize=(8, 4.2))
    timeseries(ax, res["E1"], "received throughput (Mbit/s)")
    ax.set_title("E1 - video throughput against a best-effort flood", loc="left", color=INK, pad=20)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "e1_guarantee.png"), dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.2))
    timeseries(ax, res["E2"], "round-trip time (ms)")
    ax.set_title("E2 - control traffic latency while path A is congested", loc="left", color=INK, pad=20)
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "e2_latency.png"), dpi=160)
    plt.close(fig)

    tp = res["E3"]["throughput"]
    labels = list(tp)
    fig, ax = plt.subplots(figsize=(8, 2.6))
    bars = ax.barh(range(len(labels)), [tp[k] for k in labels], height=0.5,
                   color=[color_for(k) for k in labels])
    for bar, label in zip(bars, labels):
        ax.text(bar.get_width() + 0.2, bar.get_y() + bar.get_height() / 2,
                "%.1f Mbit/s" % bar.get_width(), va="center", color=INK, fontsize=9)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=9, color=INK)
    ax.invert_yaxis()
    ax.set_xlabel("mean throughput h1 -> h4 (Mbit/s)", color=MUTED)
    style(ax, "")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    fig.suptitle("E3 - same hosts, different service, different path", x=0.01, ha="left", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    fig.savefig(os.path.join(outdir, "e3_steering.png"), dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    plot_all(sys.argv[1])
