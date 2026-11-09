"""Merge results/{regression,mutation,throughput}.json into results/results.json
with the headline numbers quoted in the README."""
import json

from common import RESULTS, tool_versions

reg = json.loads((RESULTS / "regression.json").read_text())
mut = json.loads((RESULTS / "mutation.json").read_text())
tp = json.loads((RESULTS / "throughput.json").read_text())
cov = reg["coverage"]
c, s = reg["cocotb"]["totals"], reg["sv"]["totals"]
row = lambda d, p, m: next(r for r in tp["rows"] if r["rob_depth"] == d and r["ready_p"] == p and r["div_mix"] == m)
headline = {
    "regression_all_passed": reg["all_passed"],
    "regression_wall_seconds": reg["wall_seconds"],
    "cocotb_random_seeds": len(reg["cocotb"]["random_seeds"]),
    "cocotb_directed_tests": len(reg["cocotb"]["directed_tests"]),
    "cocotb_cycles": c["cycles"], "cocotb_outputs_checked": c["outputs"],
    "cocotb_stall_cycles": c["stall_cycles"], "cocotb_flushes": c["flushes"], "cocotb_resets": c["resets"],
    "cocotb_ooo_completions": c["ooo_completions"],
    "sv_seeds": len(reg["sv"]["seeds"]), "sv_cycles": s["cycles"], "sv_outputs_checked": s["outputs"],
    "total_outputs_checked": c["outputs"] + s["outputs"],
    "total_cycles": c["cycles"] + s["cycles"],
    "coverage_bins_hit": cov["bins_hit"], "coverage_bins_total": cov["bins_total"],
    "coverage_closed_after": cov["closed_after"],
    "bugs_injected": mut["bugs_total"], "bugs_detected": mut["bugs_detected"],
    "baseline_false_alarms": mut["baseline_false_alarms"],
    "tput_fast_ready1": row(8, 1.0, 0.0)["ops_per_cycle"],
    "tput_fast_ready0.5": row(8, 0.5, 0.0)["ops_per_cycle"],
    "tput_div5_ready1_by_rob": {d: row(d, 1.0, 0.05)["ops_per_cycle"] for d in (4, 8, 16)},
    "tput_div20_ready1_by_rob": {d: row(d, 1.0, 0.2)["ops_per_cycle"] for d in (4, 8, 16)},
    "fast_latency_cycles_ready1": row(8, 1.0, 0.0)["fast_lat_min"],
}
out = {"tools": tool_versions(),
       "commands": {"regression": reg["commands"], "mutation": "python scripts/mutate.py",
                    "throughput": tp["command"]},
       "seeds": {"sv": reg["sv"]["seeds"], "cocotb_random": reg["cocotb"]["random_seeds"],
                 "mutation_sv": mut["sv_seeds"], "mutation_random": mut["random_seeds"]},
       "headline": headline}
(RESULTS / "results.json").write_text(json.dumps(out, indent=1))
print(json.dumps(headline, indent=1))
