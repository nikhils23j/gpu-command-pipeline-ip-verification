"""Full regression on the bug-free RTL.

  1. pure-SV bench, SV_SEEDS seeds x SV_CYCLES cycles
  2. cocotb directed tests once
  3. cocotb test_random for COCOTB_SEEDS seeds x COCOTB_CYCLES cycles
  4. merge functional coverage, build the closure curve, write
     results/regression.json + results/coverage.md

Usage: python scripts/regress.py [--seeds N] [--sv-seeds N] [--cycles N]
"""
import argparse
import json
import re
import sys
import time

from common import BUILD, RESULTS, build, build_sv, fresh, run, run_sv, tool_versions

DIRECTED = ["test_directed_ops", "test_full_throughput", "test_backpressure_freeze",
            "test_flush_corners", "test_reset_midstream", "test_div_chain"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--sv-seeds", type=int, default=20)
    ap.add_argument("--cycles", type=int, default=3000)
    ap.add_argument("--sv-cycles", type=int, default=20000)
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    t_start = time.time()
    ok = True

    # ---- 1. SV bench --------------------------------------------------------
    sim = BUILD / "sv_sim"
    sv_cmd = build_sv(sim)
    sv_rows = []
    for seed in range(1, args.sv_seeds + 1):
        passed, line = run_sv(sim, seed, args.sv_cycles)
        nums = dict(re.findall(r"(\w+)=(\d+)", line.split("|")[0])) if passed else {}
        sv_rows.append({"seed": seed, "pass": passed, "line": line,
                        **{k: int(v) for k, v in nums.items()}})
        ok &= passed
        print(f"[sv] seed {seed:3d} {'PASS' if passed else 'FAIL'} {line[:110]}")
    sv_tot = {k: sum(r.get(k, 0) for r in sv_rows)
              for k in ("cycles", "accepted", "outputs", "stalls", "flushes", "resets", "ooo")}

    # ---- 2+3. cocotb --------------------------------------------------------
    runner, bdir = build("main")
    covdir = fresh(BUILD / "cov")
    covdir.mkdir(parents=True)
    runs = []
    cov_file = covdir / "directed.json"
    n, nf, fails = run(runner, bdir, "directed", testcase=DIRECTED, env={"N2_COV_OUT": str(cov_file)})
    print(f"[cocotb] directed: {n - nf}/{n} passed {fails if fails else ''}")
    ok &= (nf == 0 and n == len(DIRECTED))
    runs.append(("directed", cov_file, n, nf, fails))
    for seed in range(1, args.seeds + 1):
        cov_file = covdir / f"seed{seed}.json"
        n, nf, fails = run(runner, bdir, f"seed{seed}", testcase="test_random",
                           env={"N2_SEED": str(seed), "N2_CYCLES": str(args.cycles),
                                "N2_COV_OUT": str(cov_file)})
        ok &= (nf == 0 and n == 1)
        runs.append((f"seed{seed}", cov_file, n, nf, fails))
        print(f"[cocotb] random seed {seed:3d}: {'PASS' if nf == 0 and n == 1 else 'FAIL ' + str(fails)}")

    # ---- 4. coverage merge + closure ---------------------------------------
    merged, stats, closure = {}, {}, []
    for name, path, *_ in runs:
        data = json.loads(path.read_text()) if path.exists() else {"bins": {}, "stats": {}}
        for k, v in data["bins"].items():
            merged[k] = merged.get(k, 0) + v
        for k, v in data["stats"].items():
            stats[k] = stats.get(k, 0) + v
        hit = sum(1 for v in merged.values() if v > 0)
        closure.append({"after": name, "bins_hit": hit, "bins_total": len(merged)})
    total = len(merged)
    hit = sum(1 for v in merged.values() if v > 0)
    full_at = next((c["after"] for c in closure if c["bins_hit"] == total), None)
    random_only = {}
    for name, path, *_ in runs[1:]:
        for k, v in json.loads(path.read_text())["bins"].items():
            random_only[k] = random_only.get(k, 0) + v
    groups = {}
    for k, v in merged.items():
        g = k.split(":")[0] + (":" + k.split(":")[2] if k.startswith("op:") else "")
        a = groups.setdefault(g, [0, 0, None])
        a[0] += v > 0
        a[1] += 1
        a[2] = v if a[2] is None else min(a[2], v)

    elapsed = round(time.time() - t_start, 1)
    out = {
        "tools": tool_versions(),
        "all_passed": bool(ok),
        "wall_seconds": elapsed,
        "commands": {
            "sv_build": " ".join(sv_cmd),
            "sv_run": f"vvp -n build/sv_sim +SEED=<1..{args.sv_seeds}> +CYCLES={args.sv_cycles}",
            "cocotb": f"python scripts/regress.py --seeds {args.seeds} --sv-seeds {args.sv_seeds} "
                      f"--cycles {args.cycles} --sv-cycles {args.sv_cycles}",
        },
        "sv": {"seeds": list(range(1, args.sv_seeds + 1)), "cycles_per_seed": args.sv_cycles,
               "totals": sv_tot, "runs": sv_rows},
        "cocotb": {"directed_tests": DIRECTED, "random_seeds": list(range(1, args.seeds + 1)),
                   "random_cycles_per_seed": args.cycles, "totals": stats,
                   "runs": [{"name": r[0], "tests": r[2], "failed": r[3], "failures": r[4]} for r in runs]},
        "coverage": {"bins_total": total, "bins_hit": hit, "closed_after": full_at,
                     "random_only_bins_hit": sum(1 for v in random_only.values() if v > 0),
                     "groups": {g: {"hit": a[0], "total": a[1], "min_hits": a[2]} for g, a in sorted(groups.items())},
                     "closure_curve": closure, "bins": merged},
    }
    (RESULTS / "regression.json").write_text(json.dumps(out, indent=1))

    # markdown summary
    md = ["# Regression summary (generated by scripts/regress.py)", "",
          f"- tools: {out['tools']}", f"- all passed: **{ok}**, wall time {elapsed} s", "",
          "| bench | runs | cycles | commands accepted | outputs checked | output-stall cycles | flushes | resets |",
          "|---|---|---|---|---|---|---|---|",
          f"| pure SV | {args.sv_seeds} seeds | {sv_tot['cycles']} | {sv_tot['accepted']} | {sv_tot['outputs']} | "
          f"{sv_tot['stalls']} | {sv_tot['flushes']} | {sv_tot['resets']} |",
          f"| cocotb | {len(DIRECTED)} directed + {args.seeds} random seeds | {stats.get('cycles', 0)} | "
          f"{stats.get('accepted', 0)} | {stats.get('outputs', 0)} | {stats.get('stall_cycles', 0)} | "
          f"{stats.get('flushes', 0)} | {stats.get('resets', 0)} |", "",
          f"## Functional coverage (cocotb): {hit}/{total} bins hit; 100% reached after `{full_at}`", "",
          "| group | bins hit | min hits in a bin |", "|---|---|---|"]
    md += [f"| {g} | {a[0]}/{a[1]} | {a[2]} |" for g, a in sorted(groups.items())]
    md += ["", "## Closure curve", "", "| after | bins hit |", "|---|---|"]
    picks = [c for i, c in enumerate(closure) if i < 6 or i % 10 == 0 or i == len(closure) - 1]
    md += [f"| {c['after']} | {c['bins_hit']}/{c['bins_total']} |" for c in picks]
    md += ["", "## Event bins", "", "| bin | hits |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in sorted(merged.items()) if not k.startswith("op")]
    (RESULTS / "coverage.md").write_text("\n".join(md) + "\n")
    print(f"\nREGRESSION {'PASS' if ok else 'FAIL'}: coverage {hit}/{total} bins, {elapsed}s "
          f"-> results/regression.json, results/coverage.md")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
