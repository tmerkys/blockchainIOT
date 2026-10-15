"""M5 analysis: per-condition statistics and the thesis figures.

    python experiments/analyze.py

Reads every `experiments/results/m5_*.csv` the runner wrote, and produces

  * a printed table per condition — n, mean, sd, 95% CI, median, min, max —
    for the reported latency and each term of its decomposition;
  * `experiments/analysis/summary.csv`, the same numbers in long form, which
    is what the thesis tables are typeset from;
  * `experiments/analysis/tests.csv`, Welch t-tests between transports within
    each (chain, config);
  * three figures, as PDF (for LaTeX) and PNG (for reading).

**A condition is (chain, transport, config).** `config` separates the Pi's
stock settings from the controlled ones (WiFi power save off, `performance`
governor), because docs/measurements.md shows both move the numbers. The first
M5 pair predates the `wifiPowersave`/`cpuGovernor` columns; those rows ran on
NM `default` / `ondemand`, and are filled in as such here rather than in the
raw CSVs, which are never edited.

**Only `status == "ok"` rows enter the statistics.** Failures are counted and
reported per condition — a transport that drops 1 run in 10 is a finding even
if its successful runs are fast.

**The 95% CI is the t-interval on the mean**, `t(0.975, n-1) · sd / √n`. The
latency distribution is not normal (inclusion is close to uniform over a
block period), but at n=30 the sample mean is, which is all the interval
claims. Medians are reported next to it for the skewed terms.
"""

from __future__ import annotations

import argparse
import glob
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "experiments" / "results"
OUT_DIR = REPO_ROOT / "experiments" / "analysis"

CONFIG_DEFAULT = "default"
CONFIG_CONTROLLED = "controlled"

# Terms of the reported latency, in pipeline order. `other_ms` is the residual:
# the merchant countersignature and request building between t1 and the send,
# plus framing — everything the marks do not separate.
TERMS = [
    ("signing_ms", "Signing (Pi)"),
    ("network_ms", "Network, both legs"),
    ("build_ms", "Gateway build + broadcast"),
    ("inclusion_ms", "Block inclusion"),
    ("other_ms", "Other (countersign, framing)"),
]
# Device CPU time per run (run_batch.py docstring): the M7 fallback, a proxy for
# device energy, not a measurement of it. Only sessions after a0ea140 have it.
CPU_METRICS = ["cpu_sign_ms", "cpu_device_ms", "cpu_countersign_ms"]
METRICS = (
    ["latency_ms"] + [t for t, _ in TERMS]
    + ["confirm_ms", "clock_offset_ms", "gasUsed", "fee_wei"] + CPU_METRICS
)

TRANSPORT_LABEL = {"eth": "Ethernet", "wifi": "WiFi", "ble": "Bluetooth"}
TRANSPORT_ORDER = ["eth", "wifi", "ble"]
CHAIN_LABEL = {"base-sepolia": "L2 Base", "sepolia": "L1 Ethereum", "anvil": "anvil"}


def cond_label(chain: str, transport: str, config: str, sep: str = "\n") -> str:
    return sep.join([CHAIN_LABEL.get(chain, chain), TRANSPORT_LABEL.get(transport, transport), config])

# Reference palette (dataviz skill, light mode). Categorical slots in fixed
# order; validated for adjacent use as a 5-slot set. Slots 3–5 are under 3:1
# on the surface, so every segment carries a visible label.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
TRANSPORT_COLOR = {"eth": SLOTS[0], "wifi": SLOTS[1], "ble": SLOTS[2]}


# --- loading ---------------------------------------------------------------


def load(results_dir: Path) -> pd.DataFrame:
    files = sorted(glob.glob(str(results_dir / "m5_*.csv")))
    if not files:
        raise SystemExit(f"no m5_*.csv in {results_dir}")
    frames = []
    for f in files:
        d = pd.read_csv(f)
        d["file"] = Path(f).name
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)

    # Columns added after the first M5 pair; see module docstring.
    for col, legacy in (("wifiPowersave", "default"), ("cpuGovernor", "ondemand")):
        if col not in df:
            df[col] = legacy
        df[col] = df[col].fillna(legacy)
    for col in CPU_METRICS:
        if col not in df:
            df[col] = np.nan

    controlled = (df["cpuGovernor"] == "performance") & (df["wifiPowersave"] == "disable")
    df["config"] = np.where(controlled, CONFIG_CONTROLLED, CONFIG_DEFAULT)

    # Gateway-internal spans: differences of gateway marks only, one clock.
    df["build_ms"] = (df["t3"] - df["t2"]) * 1000
    df["inclusion_ms"] = (df["t4"] - df["t3"]) * 1000
    df["confirm_ms"] = (df["t5"] - df["t4"]) * 1000
    df["other_ms"] = df["latency_ms"] - df[["signing_ms", "network_ms", "build_ms", "inclusion_ms"]].sum(axis=1)
    df["fee_wei"] = df["gasUsed"] * df["effectiveGasPrice"] + df["l1Fee"].fillna(0)
    return df


# --- statistics ------------------------------------------------------------


def describe(x: pd.Series) -> dict[str, float]:
    x = x.dropna().astype(float)
    n = len(x)
    sd = x.std(ddof=1) if n > 1 else math.nan
    half = stats.t.ppf(0.975, n - 1) * sd / math.sqrt(n) if n > 1 else math.nan
    return {
        "n": n,
        "mean": x.mean(),
        "sd": sd,
        "ci95": half,
        "ci_low": x.mean() - half,
        "ci_high": x.mean() + half,
        "median": x.median(),
        "min": x.min(),
        "max": x.max(),
    }


KEYS = ["chain", "transport", "config"]


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby(KEYS):
        ok = g[g["status"] == "ok"]
        counts = g["status"].value_counts().to_dict()
        for metric in METRICS:
            if metric in CPU_METRICS and ok[metric].isna().all():
                continue  # condition measured before CPU time was recorded
            rows.append(
                {
                    **dict(zip(KEYS, key)),
                    "metric": metric,
                    **describe(ok[metric]),
                    "attempted": len(g),
                    "failed": len(g) - counts.get("ok", 0),
                    "sessions": g["session"].nunique(),
                    "commits": ";".join(sorted(g["commit"].astype(str).str[:7].unique())),
                }
            )
    return pd.DataFrame(rows)


def _welch_rows(
    a: pd.DataFrame, b: pd.DataFrame, la: str, lb: str, scope: str, config: str, metrics: list[str]
) -> list[dict]:
    rows = []
    for metric in metrics:
        res = stats.ttest_ind(b[metric], a[metric], equal_var=False)  # sign follows b - a
        rows.append(
            {
                "scope": scope,
                "config": config,
                "a": la,
                "b": lb,
                "metric": metric,
                "mean_a": a[metric].mean(),
                "mean_b": b[metric].mean(),
                "diff_b_minus_a": b[metric].mean() - a[metric].mean(),
                "t": res.statistic,
                "df": res.df,
                "p": res.pvalue,
            }
        )
    return rows


def welch(df: pd.DataFrame) -> pd.DataFrame:
    """Two families of Welch t-tests, each holding everything else fixed.

    * transport: eth vs every other transport, within one (chain, config);
      `scope` is the chain.
    * chain: L2 (Base Sepolia) vs L1 (Ethereum Sepolia), within one
      (transport, config) — the H1 comparison; `scope` is the transport.
    """
    ok = df[df["status"] == "ok"]
    rows: list[dict] = []
    for (chain, config), g in ok.groupby(["chain", "config"]):
        if "eth" not in set(g["transport"]):
            continue
        base = g[g["transport"] == "eth"]
        for other in sorted(set(g["transport"]) - {"eth"}):
            rows += _welch_rows(
                base, g[g["transport"] == other], "eth", other, chain, config,
                ["latency_ms", "network_ms", "signing_ms"],
            )
    for (transport, config), g in ok.groupby(["transport", "config"]):
        if not {"base-sepolia", "sepolia"} <= set(g["chain"]):
            continue
        rows += _welch_rows(
            g[g["chain"] == "base-sepolia"], g[g["chain"] == "sepolia"],
            "base-sepolia", "sepolia", transport, config,
            ["latency_ms", "inclusion_ms", "build_ms", "gasUsed"],
        )
    return pd.DataFrame(rows)


def print_summary(summary: pd.DataFrame, tests: pd.DataFrame) -> None:
    show = ["latency_ms"] + [t for t, _ in TERMS] + ["clock_offset_ms", "gasUsed"] + CPU_METRICS
    order = {k: i for i, k in enumerate(condition_order(summary))}
    groups = sorted(summary.groupby(KEYS, sort=False), key=lambda kg: order[kg[0]])
    for key, g in groups:
        first = g.iloc[0]
        print(
            f"\n== {first.chain} / {TRANSPORT_LABEL.get(first.transport, first.transport)} / "
            f"{first.config}   ok {first.attempted - first.failed}/{first.attempted}   "
            f"commit {first.commits}"
        )
        print(f"  {'metric':<16}{'mean':>12}{'± 95% CI':>11}{'sd':>10}{'median':>11}{'min':>10}{'max':>10}")
        for _, r in g[g["metric"].isin(show)].iterrows():
            print(
                f"  {r.metric:<16}{r['mean']:>12.1f}{r.ci95:>11.1f}{r.sd:>10.1f}"
                f"{r['median']:>11.1f}{r['min']:>10.1f}{r['max']:>10.1f}"
            )
    if not tests.empty:
        print("\n== Welch t-tests (b - a)")
        for _, r in tests.iterrows():
            print(
                f"  {r.scope:<13}{r.config:<11}{r.metric:<13} {r.b} - {r.a}: "
                f"{r.diff_b_minus_a:+10.1f}   t={r.t:+7.2f}  p={r.p:.2g}"
            )


# --- figures ---------------------------------------------------------------


def style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.edgecolor": BASELINE,
            "axes.labelcolor": INK_2,
            "axes.titlecolor": INK,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.labelsize": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": INK_2,
            "ytick.labelcolor": INK_2,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "font.size": 9,
        }
    )


def save(fig: plt.Figure, out: Path, name: str) -> None:
    for ext in ("pdf", "png"):
        fig.savefig(out / f"{name}.{ext}", dpi=200, bbox_inches="tight")
    plt.close(fig)


def condition_order(frame: pd.DataFrame) -> list[tuple[str, str, str]]:
    keys = frame.groupby(KEYS).size().index.tolist()
    return sorted(
        keys,
        key=lambda k: (k[0], 0 if k[2] == CONFIG_DEFAULT else 1, TRANSPORT_ORDER.index(k[1])),
    )


def by_chain(conds: list[tuple[str, str, str]]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """Conditions grouped per chain, L2 before L1 — one panel each.

    L1 latency is ~8x L2's, so a shared axis flattens every L2 condition into
    the baseline. Separate panels with their own scale keep both readable; the
    panel titles say which chain, and the figure subtitle says the scales differ.
    """
    chains: dict[str, list] = {}
    for k in conds:
        chains.setdefault(k[0], []).append(k)
    return sorted(chains.items(), key=lambda kv: list(CHAIN_LABEL).index(kv[0]) if kv[0] in CHAIN_LABEL else 99)


def select(ok: pd.DataFrame, key: tuple[str, str, str]) -> pd.DataFrame:
    chain, transport, config = key
    return ok[(ok.chain == chain) & (ok.transport == transport) & (ok.config == config)]


def fig_latency(ok: pd.DataFrame, out: Path) -> None:
    """Every run as a dot, the mean and its 95% CI as a bar, per condition."""
    panels = by_chain(condition_order(ok))
    rng = np.random.default_rng(0)  # jitter only; fixed so the figure is reproducible
    fig, axes = plt.subplots(
        1, len(panels), figsize=(3.0 + 1.1 * sum(len(c) for _, c in panels), 3.6),
        gridspec_kw={"width_ratios": [len(c) for _, c in panels]}, squeeze=False,
    )
    for ax, (chain, conds) in zip(axes[0], panels):
        ax.grid(axis="x", visible=False)
        for i, key in enumerate(conds):
            x = select(ok, key)["latency_ms"]
            d = describe(x)
            ax.scatter(
                i - 0.1 + rng.uniform(-0.15, 0.15, len(x)), x, s=14, color=TRANSPORT_COLOR[key[1]],
                alpha=0.55, edgecolor=SURFACE, linewidth=0.6, zorder=2,
            )
            ax.errorbar(
                i + 0.18, d["mean"], yerr=d["ci95"], fmt="o", color=INK, markersize=4.5,
                capsize=3, elinewidth=1.2, capthick=1.2, zorder=3,
            )
            ax.annotate(
                f"{d['mean']:.0f}\n± {d['ci95']:.0f}", (i + 0.22, d["mean"]), xytext=(3, 0),
                textcoords="offset points", va="center", fontsize=7.5, color=INK,
            )
        ax.set_xticks(range(len(conds)))
        ax.set_xticklabels([f"{TRANSPORT_LABEL[t]}\n{c}" for _, t, c in conds], fontsize=8)
        ax.set_xlim(-0.55, len(conds) - 0.15)
        ax.set_ylim(bottom=0)
        ax.set_title(CHAIN_LABEL.get(chain, chain) + " Sepolia", fontsize=9.5)
    axes[0][0].set_ylabel("t4 − t0 (ms)")
    fig.suptitle("End-to-end latency per run", x=0.01, ha="left", fontsize=11, fontweight="bold", color=INK)
    fig.text(
        0.01, 0.905, "dots: individual runs · black: mean with 95% CI · panels have separate y scales",
        fontsize=8, color=INK_2, ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    save(fig, out, "fig_latency")


def fig_decomposition(ok: pd.DataFrame, out: Path) -> None:
    """Mean latency split into its terms, one stacked bar per condition."""
    panels = by_chain(condition_order(ok))
    n_rows = sum(len(c) for _, c in panels)
    fig, axes = plt.subplots(
        len(panels), 1, figsize=(6.6, 0.5 * n_rows + 0.9 * len(panels) + 1.0),
        gridspec_kw={"height_ratios": [len(c) for _, c in panels]}, squeeze=False,
    )
    for ax, (chain, conds) in zip(axes[:, 0], panels):
        ax.grid(axis="y", visible=False)
        totals = [sum(select(ok, k)[t].mean() for t, _ in TERMS) for k in conds]
        span = max(totals)
        for row, key in enumerate(conds):
            g = select(ok, key)
            left = 0.0
            for slot, (term, _) in enumerate(TERMS):
                w = g[term].mean()
                ax.barh(
                    row, w, left=left, height=0.62, color=SLOTS[slot], edgecolor=SURFACE,
                    linewidth=1.5, zorder=2,
                )
                if w >= 0.07 * span:  # label only segments wide enough to hold the text
                    ax.text(left + w / 2, row, f"{w:.0f}", ha="center", va="center", fontsize=7.5, color=INK)
                left += w
            ax.text(left + 0.012 * span, row, f"{left:.0f} ms", va="center", fontsize=8, color=INK)
        ax.set_yticks(range(len(conds)))
        ax.set_yticklabels([f"{TRANSPORT_LABEL[t]}, {c}" for _, t, c in conds])
        ax.invert_yaxis()
        ax.set_xlim(0, span * 1.14)
        ax.set_title(CHAIN_LABEL.get(chain, chain) + " Sepolia", fontsize=9.5)
    axes[-1, 0].set_xlabel("mean time (ms) — panels have separate x scales")
    fig.suptitle("Where the latency goes (mean per term)", x=0.01, ha="left", fontsize=11,
                 fontweight="bold", color=INK)
    handles = [plt.Rectangle((0, 0), 1, 1, color=SLOTS[i]) for i in range(len(TERMS))]
    fig.legend(
        handles, [lbl for _, lbl in TERMS], ncol=3, loc="lower left", bbox_to_anchor=(0.01, 0.0),
        handlelength=1, columnspacing=1.2,
    )
    fig.tight_layout(rect=(0, 0.09, 1, 0.97))
    save(fig, out, "fig_decomposition")


def fig_device_terms(ok: pd.DataFrame, out: Path) -> None:
    """Signing and network per run — the two terms the Pi's config moves.

    Two panels, not two axes: different measures, different scales. Network is
    on a log scale because power save puts a cluster two orders of magnitude
    above the rest, and a linear axis would flatten everything else to zero.
    """
    conds = condition_order(ok)
    # Stacked, not side by side: six conditions need the full width for labels.
    fig, axes = plt.subplots(2, 1, figsize=(6.8, 5.4))
    rng = np.random.default_rng(1)
    for ax, (metric, title, log) in zip(
        axes,
        [("signing_ms", "Signing on the Pi (ms)", False), ("network_ms", "Network, both legs (ms, log)", True)],
    ):
        ax.grid(axis="x", visible=False)
        for i, (chain, transport, config) in enumerate(conds):
            x = ok[(ok.chain == chain) & (ok.transport == transport) & (ok.config == config)][metric]
            ax.scatter(
                i + rng.uniform(-0.2, 0.2, len(x)), x, s=12, color=TRANSPORT_COLOR[transport],
                alpha=0.6, edgecolor=SURFACE, linewidth=0.5, zorder=2,
            )
            ax.hlines(x.median(), i - 0.3, i + 0.3, color=INK, linewidth=1.4, zorder=3)
        if log:
            ax.set_yscale("log")
        else:
            ax.set_ylim(bottom=0)
        ax.set_xticks(range(len(conds)))
        ax.set_xticklabels([cond_label(*k) for k in conds], fontsize=7.5)
        ax.set_title(title, fontsize=9.5)
    fig.suptitle("Device-side terms per run (black line: median)", x=0.01, ha="left", fontsize=11,
                 fontweight="bold", color=INK)
    fig.tight_layout()
    save(fig, out, "fig_device_terms")


# --- main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5 statistics and figures.")
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args(argv)

    df = load(args.results)
    summary = summarise(df)
    tests = welch(df)
    print_summary(summary, tests)

    args.out.mkdir(parents=True, exist_ok=True)
    summary.to_csv(args.out / "summary.csv", index=False, float_format="%.4f")
    tests.to_csv(args.out / "tests.csv", index=False, float_format="%.6g")

    if not args.no_figures:
        style()
        ok = df[df["status"] == "ok"]
        fig_latency(ok, args.out)
        fig_decomposition(ok, args.out)
        fig_device_terms(ok, args.out)
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
