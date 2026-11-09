"""cocotb tests for gpu_command_pipeline.

Environment knobs (set by scripts/run.py):
  N2_SEED      seed for test_random (default 1)
  N2_CYCLES    cycles of random traffic in test_random (default 3000)
  N2_COV_OUT   JSON file to merge functional coverage into
  N2_ROB_DEPTH ROB_DEPTH the DUT was built with (default 8)
"""

import cocotb
from cocotb.clock import Clock

from harness import (READY_MODES, Harness, ReadyPattern, env_int, make_rng,
                     random_cmd, save_coverage, CORNERS)
from model import OP, OPS

ROB_DEPTH = env_int("N2_ROB_DEPTH", 8)


async def setup(dut, **kw):
    Clock(dut.clk, 10, unit="ns").start(start_high=False)
    dut.in_valid.value = 0
    dut.flush.value = 0
    dut.out_ready.value = 1
    h = Harness(dut, rob_depth=ROB_DEPTH, **kw)
    await h.reset()
    return h


def script(cmds):
    it = iter(cmds)
    return lambda: next(it, None)


@cocotb.test()
async def test_directed_ops(dut):
    """Every op against a table of corner operands, no backpressure."""
    h = await setup(dut)
    vals = CORNERS + [0x1234, 0x00F0, 0x0010]
    cmds = []
    for op in range(16):
        for a in vals:
            for b in vals[::2]:
                cmds.append((op, a, b, (a ^ 0xA5A5) & 0xFFFF))
    # hand-picked ones the table might miss
    cmds += [(OP["MAC"], 0x00FF, 0x00FF, 0xFFFF),   # product fits, add overflows
             (OP["ADDS"], 0x7FFF, 0x0001, 0), (OP["ADDS"], 0x8000, 0xFFFF, 0),
             (OP["SRA"], 0x8000, 15, 0), (OP["DIVU"], 0xFFFF, 0, 0), (OP["REMU"], 0x1234, 0, 0)]
    gen = script(cmds)
    while h.stats["accepted"] < len(cmds):
        await h.tick(gen, ready=True)
    await h.drain()
    assert h.stats["outputs"] == len(cmds)
    dut._log.info("directed ops: %d commands checked", len(cmds))
    save_coverage(h)


@cocotb.test()
async def test_full_throughput(dut):
    """Fast ops, always valid, always ready: must sustain 1 op/cycle."""
    h = await setup(dut)
    rng = make_rng(7)
    gen = lambda: random_cmd(rng, fast_only=True)
    for _ in range(20):
        await h.tick(gen, ready=True)
    a0, o0 = h.stats["accepted"], h.stats["outputs"]
    n = 400
    for _ in range(n):
        await h.tick(gen, ready=True)
    acc = h.stats["accepted"] - a0
    out = h.stats["outputs"] - o0
    assert acc == n and out == n, f"expected {n} in/out in {n} cycles, got in={acc} out={out}"
    await h.drain()
    save_coverage(h)


@cocotb.test()
async def test_backpressure_freeze(dut):
    """Hold out_ready low for a long time: buffers fill, nothing is lost or reordered."""
    h = await setup(dut)
    rng = make_rng(11)
    gen = lambda: random_cmd(rng, div_weight=0.15)
    for _ in range(10):
        await h.tick(gen, ready=True)
    saw_full = False
    for _ in range(60):
        await h.tick(gen, ready=False)
        saw_full |= h._b("rob_full")
    assert saw_full, "ROB never filled while output was frozen"
    in_flight = len(h.expected)
    # out reg + ROB entries are the only places accepted work can wait, plus skid/main
    assert in_flight <= ROB_DEPTH + 3, f"accepted {in_flight} commands with nowhere to put them"
    for _ in range(100):
        await h.tick(gen, ready=rng.random() < 0.3)
    await h.drain()
    save_coverage(h)


@cocotb.test()
async def test_flush_corners(dut):
    """Flush in the awkward moments; the stale divide must never land."""
    h = await setup(dut)
    rng = make_rng(3)
    # 1) flush while a divide is running, then refill with fast ops that reuse the ROB slots
    gen = script([(OP["DIVU"], 0xFFFF, 3, 0), (OP["ADD"], 1, 2, 0)])
    for _ in range(6):
        await h.tick(gen, ready=True)
    assert h._b("div_busy")
    await h.tick(None, flush=True)
    gen = lambda: random_cmd(rng, fast_only=True)
    for _ in range(40):
        await h.tick(gen, ready=True)
    await h.drain()
    # 2) flush while output is stalled and ROB is full
    gen = lambda: random_cmd(rng, div_weight=0.1)
    for _ in range(30):
        await h.tick(gen, ready=False)
    await h.tick(gen, flush=True)
    # 3) two flushes back to back, input held valid across them
    await h.tick(gen, flush=True)
    await h.tick(gen, flush=True)
    for _ in range(50):
        await h.tick(gen, ready=rng.random() < 0.5)
    await h.drain()
    save_coverage(h)


@cocotb.test()
async def test_reset_midstream(dut):
    h = await setup(dut)
    rng = make_rng(5)
    gen = lambda: random_cmd(rng, div_weight=0.2)
    for _ in range(25):
        await h.tick(gen, ready=rng.random() < 0.5)
    await h.reset(2)
    for _ in range(60):
        await h.tick(gen, ready=rng.random() < 0.7)
    await h.drain()
    save_coverage(h)


@cocotb.test()
async def test_div_chain(dut):
    """Back-to-back divides interleaved with fast ops -> out-of-order completion."""
    h = await setup(dut)
    rng = make_rng(9)
    cmds = []
    for i in range(30):
        cmds.append((OP["DIVU"] if i % 2 else OP["REMU"], rng.randrange(1 << 16), rng.randrange(1, 300), 0))
        cmds += [random_cmd(rng, fast_only=True) for _ in range(rng.randrange(4))]
    gen = script(cmds)
    while h.stats["accepted"] < len(cmds):
        await h.tick(gen, ready=rng.random() < 0.8)
    await h.drain()
    assert h.stats["ooo_completions"] > 0, "never saw a younger op finish before an older divide"
    save_coverage(h)


@cocotb.test()
async def test_random(dut):
    """Constrained-random traffic; profile is picked from the seed."""
    seed = env_int("N2_SEED", 1)
    cycles = env_int("N2_CYCLES", 3000)
    rng = make_rng(seed)
    mode = READY_MODES[seed % len(READY_MODES)]
    h = await setup(dut, ready_mode=mode)
    valid_p = rng.choice([1.0, 0.8, 0.5])
    div_w = rng.choice([0.02, 0.08, 0.2])
    flush_p, reset_p = 0.004, 0.0012
    ready = ReadyPattern(mode, rng)

    def gen():
        return random_cmd(rng, div_weight=div_w) if rng.random() < valid_p else None

    for _ in range(cycles):
        r = rng.random()
        if r < reset_p:
            await h.tick(gen, reset=True)
        elif r < reset_p + flush_p:
            await h.tick(gen, flush=True)
        else:
            await h.tick(gen, ready=ready())
    await h.drain()
    dut._log.info("seed=%d mode=%s valid_p=%.1f div_w=%.2f stats=%s", seed, mode, valid_p, div_w, h.stats)
    save_coverage(h)
