"""Throughput / latency sweep (not a pass/fail regression test, but the
scoreboard and protocol checks stay on, so every measured op is also checked).

Input is always valid; out_ready is random with probability P; DIV_MIX is the
fraction of DIVU/REMU commands. Results are appended to $N2_TPUT_OUT as JSON lines.
"""
import json
import os

import cocotb
from cocotb.clock import Clock

from harness import Harness, env_int, make_rng, random_cmd
from model import DIV_OPS

ROB_DEPTH = env_int("N2_ROB_DEPTH", 8)
READY_P = [1.0, 0.9, 0.75, 0.5, 0.25]
DIV_MIX = [0.0, 0.05, 0.2]
WARMUP, WINDOW = 50, 4000


@cocotb.test()
async def test_throughput_sweep(dut):
    Clock(dut.clk, 10, unit="ns").start(start_high=False)
    dut.in_valid.value = 0
    dut.flush.value = 0
    out_path = os.environ.get("N2_TPUT_OUT")
    for mix in DIV_MIX:
        for p in READY_P:
            h = Harness(dut, rob_depth=ROB_DEPTH)
            await h.reset()
            rng = make_rng(1000 + int(p * 100) + int(mix * 1000))
            gen = lambda: random_cmd(rng, div_weight=mix, fast_only=(mix == 0.0))
            for _ in range(WARMUP):
                await h.tick(gen, ready=rng.random() < p)
            o0 = h.stats["outputs"]
            n_lat0 = len(h.latencies["fast"]), len(h.latencies["div"])
            for _ in range(WINDOW):
                await h.tick(gen, ready=rng.random() < p)
            outs = h.stats["outputs"] - o0
            fl = h.latencies["fast"][n_lat0[0]:]
            dl = h.latencies["div"][n_lat0[1]:]
            await h.drain()
            row = dict(rob_depth=ROB_DEPTH, ready_p=p, div_mix=mix, cycles=WINDOW,
                       outputs=outs, ops_per_cycle=round(outs / WINDOW, 4),
                       fast_lat_min=min(fl) if fl else None,
                       fast_lat_avg=round(sum(fl) / len(fl), 2) if fl else None,
                       div_lat_min=min(dl) if dl else None,
                       div_lat_avg=round(sum(dl) / len(dl), 2) if dl else None)
            dut._log.info("TPUT %s", row)
            if out_path:
                with open(out_path, "a") as f:
                    f.write(json.dumps(row) + "\n")
