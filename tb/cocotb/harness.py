"""Driver + monitor + scoreboard + protocol checks + manual functional coverage.

One coroutine does everything per clock so the Python overhead stays small:

    falling edge : drive inputs for this cycle
    ReadOnly     : everything is settled and is exactly what the DUT will see
                   at the next rising edge -> sample handshakes, check, cover
    rising edge  : DUT updates

Checks (any failure raises AssertionError with the cycle number):
  * scoreboard: every output must match the oldest outstanding accepted
    command in tag, result and flag (=> ordering + values + tag integrity)
  * no output when nothing is outstanding; nothing left over after drain
  * protocol: once out_valid && !out_ready, out_valid must stay high and
    tag/result/flag must not change until accepted (flush/reset excepted)
  * in_ready and out_valid must be low in any flush/reset cycle
  * watchdog: with out_ready high, an outstanding command must come out
    within WATCHDOG cycles
"""

import json
import os
import random
from collections import deque

from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from model import DIV_OPS, OP, OPS, golden, s16

WATCHDOG = 80
READY_MODES = ["always", "p90", "p50", "p25", "bursty"]

# ---------------------------------------------------------------------------
# Functional coverage (manual bins; iverilog has no covergroups)
# ---------------------------------------------------------------------------
EVENT_BINS = [
    # value corners (sampled when the command is accepted)
    "val:add_carry", "val:sub_borrow", "val:adds_sat_pos", "val:adds_sat_neg",
    "val:mul_overflow", "val:mac_ovf_from_add_only", "val:mac_ovf_from_product",
    "val:sra_negative", "val:shift_by_0", "val:shift_by_15",
    "val:minmax_mixed_sign", "val:cmplt_true", "val:cmplt_false",
    "val:divu_by_zero", "val:remu_by_zero", "val:div_a_lt_b",
    # micro-architecture events (sampled from internal state)
    "uarch:skid_full", "uarch:rob_full_stall", "uarch:div_busy_stall",
    "uarch:div_issue_on_writeback", "uarch:ooo_completion",
    "uarch:simultaneous_writeback", "uarch:output_stall_8plus",
    "uarch:accept_8_back_to_back",
    # flush / reset while busy
    "kill:flush_while_div_busy", "kill:flush_while_rob_full",
    "kill:flush_while_output_stalled", "kill:flush_while_skid_full",
    "kill:flush_2_cycles_in_a_row", "kill:reset_with_inflight",
]


def all_bins():
    bins = []
    for name in OPS:
        for kind in ("accepted", "stalled_at_output", "back_to_back", "flushed_in_flight"):
            bins.append(f"op:{name}:{kind}")
        for mode in READY_MODES:
            bins.append(f"opxready:{name}:{mode}")
    return bins + EVENT_BINS


class Coverage:
    def __init__(self):
        self.bins = {b: 0 for b in all_bins()}

    def hit(self, name, n=1):
        if name not in self.bins:
            raise KeyError(f"unknown coverage bin {name}")
        self.bins[name] += n

    def merge_into_file(self, path, stats):
        data = {"bins": {}, "stats": {}}
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f)
        for k, v in self.bins.items():
            data["bins"][k] = data["bins"].get(k, 0) + v
        for k, v in stats.items():
            data["stats"][k] = data["stats"].get(k, 0) + v
        with open(path, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)


# ---------------------------------------------------------------------------
# Stimulus helpers
# ---------------------------------------------------------------------------
CORNERS = [0x0000, 0x0001, 0x0002, 0x7FFF, 0x8000, 0x8001, 0xFFFF, 0xFFFE, 0x00FF, 0x0100]
FAST_OPS = [i for i in range(16) if i not in DIV_OPS]


def operand(rng):
    r = rng.random()
    if r < 0.30:
        return rng.choice(CORNERS)
    if r < 0.45:
        return rng.randrange(17)
    if r < 0.55:
        return 0xFFFF - rng.randrange(17)
    return rng.randrange(1 << 16)


def random_cmd(rng, div_weight=0.08, fast_only=False):
    if not fast_only and rng.random() < div_weight:
        op = rng.choice(sorted(DIV_OPS))
    else:
        op = rng.choice(FAST_OPS)
    a, b, c = operand(rng), operand(rng), operand(rng)
    if op in (OP["MUL"], OP["MAC"]) and rng.random() < 0.35:
        a, b = rng.randrange(256), rng.randrange(256)
        c = rng.choice([0xFFFF, 0xFF00, 0xF000, rng.randrange(1 << 16)])
    if op in DIV_OPS and rng.random() < 0.10:
        b = 0
    if op in (OP["SHL"], OP["SHR"], OP["SRA"]) and rng.random() < 0.25:
        b = rng.choice([0, 15])
    return op, a, b, c


class ReadyPattern:
    """Output backpressure generator."""
    PROB = {"always": 1.0, "p90": 0.9, "p50": 0.5, "p25": 0.25}

    def __init__(self, mode, rng):
        self.mode, self.rng, self.state = mode, rng, True

    def __call__(self):
        if self.mode == "bursty":
            # long ready runs and long stall runs
            if self.rng.random() < (0.06 if self.state else 0.12):
                self.state = not self.state
            return self.state
        return self.rng.random() < self.PROB[self.mode]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class Harness:
    def __init__(self, dut, cov=None, ready_mode="always", tag_w=8, rob_depth=8, check=True):
        self.dut = dut
        self.cov = cov if cov is not None else Coverage()
        self.ready_mode = ready_mode
        self.tag_mask = (1 << tag_w) - 1
        self.rob_depth = rob_depth
        self.check = check
        self.expected = deque()        # (tag, op, result, flag, accept_cycle)
        self.latencies = {"fast": [], "div": []}
        self.pending = None            # command currently offered on the input
        self.next_tag = 0
        self.cycle = 0
        self.prev_stall = None         # payload seen while stalled last cycle
        self.stall_run = 0
        self.accept_run = 0
        self.prev_accept = False
        self.prev_flush = False
        self.no_progress = 0
        self.stats = dict(cycles=0, accepted=0, outputs=0, stall_cycles=0,
                          flushes=0, resets=0, dropped_by_kill=0, ooo_completions=0)
        # cache handles (lookups are slow-ish in cocotb)
        d = dut
        self.h = dict(
            clk=d.clk, reset_n=d.reset_n, flush=d.flush, in_valid=d.in_valid,
            in_ready=d.in_ready, in_tag=d.in_tag, in_op=d.in_op, in_a=d.in_a,
            in_b=d.in_b, in_c=d.in_c, out_valid=d.out_valid, out_ready=d.out_ready,
            out_tag=d.out_tag, out_result=d.out_result, out_flag=d.out_flag,
            s_valid=d.s_valid, m_valid=d.m_valid, m_is_div=d.m_is_div,
            rob_full=d.rob_full, div_busy=d.div_busy, div_wb=d.div_wb,
            issue_fire=d.issue_fire, e2_valid=d.e2_valid, e2_idx=d.e2_idx,
            div_idx=d.div_idx, rob_head=d.rob_head,
        )

    # -- small helpers ------------------------------------------------------
    def _b(self, name):
        v = self.h[name].value
        return v.is_resolvable and int(v) == 1

    def _u(self, name):
        return int(self.h[name].value)

    def fail(self, msg):
        raise AssertionError(f"[cycle {self.cycle}] {msg}")

    async def reset(self, cycles=3):
        for _ in range(cycles):
            await self.tick(None, ready=True, reset=True)

    # -- one clock ----------------------------------------------------------
    async def tick(self, gen=None, ready=True, flush=False, reset=False):
        h = self.h
        await FallingEdge(h["clk"])
        if self.pending is None and gen is not None:
            cmd = gen()
            if cmd is not None:
                op, a, b, c = cmd
                self.pending = (self.next_tag, op, a, b, c)
                self.next_tag = (self.next_tag + 1) & self.tag_mask
        h["reset_n"].value = 0 if reset else 1
        h["flush"].value = 1 if flush else 0
        h["out_ready"].value = 1 if ready else 0
        if self.pending is not None:
            tag, op, a, b, c = self.pending
            h["in_valid"].value = 1
            h["in_tag"].value = tag
            h["in_op"].value = op
            h["in_a"].value = a
            h["in_b"].value = b
            h["in_c"].value = c
        else:
            h["in_valid"].value = 0

        await ReadOnly()
        self._sample(ready, flush, reset)
        await RisingEdge(h["clk"])
        self.cycle += 1

    def _sample(self, ready, flush, reset):
        st, cov = self.stats, self.cov
        st["cycles"] += 1
        kill = flush or reset
        in_ready = self._b("in_ready")
        out_valid = self._b("out_valid")

        if kill:
            if self.check and (in_ready or out_valid):
                self.fail(f"in_ready={in_ready} out_valid={out_valid} during flush/reset")
            if flush:
                st["flushes"] += 1
                if self._b("div_busy"):
                    cov.hit("kill:flush_while_div_busy")
                if self._b("rob_full"):
                    cov.hit("kill:flush_while_rob_full")
                if self._b("s_valid"):
                    cov.hit("kill:flush_while_skid_full")
                if self.prev_stall is not None:
                    cov.hit("kill:flush_while_output_stalled")
                if self.prev_flush:
                    cov.hit("kill:flush_2_cycles_in_a_row")
            else:
                st["resets"] += 1
                if self.expected:
                    cov.hit("kill:reset_with_inflight")
            for _, op, _, _, _ in self.expected:
                cov.hit(f"op:{OPS[op]}:flushed_in_flight")
            st["dropped_by_kill"] += len(self.expected)
            self.expected.clear()
            self.prev_stall = None
            self.stall_run = 0
            self.accept_run = 0
            self.prev_accept = False
            self.no_progress = 0
            self.prev_flush = flush
            return
        self.prev_flush = False

        # ---------------- output side ----------------
        if out_valid:
            payload = (self._u("out_tag"), self._u("out_result"), self._u("out_flag"))
        else:
            payload = None
        if self.check and self.prev_stall is not None:
            if not out_valid:
                self.fail("PROTOCOL: out_valid dropped while stalled (valid must hold until accepted)")
            if payload != self.prev_stall:
                self.fail(f"PROTOCOL: payload changed while stalled: {self.prev_stall} -> {payload}")

        if out_valid and not ready:
            self.prev_stall = payload
            self.stall_run += 1
            st["stall_cycles"] += 1
            if self.stall_run == 8:
                cov.hit("uarch:output_stall_8plus")
            if self.expected:
                cov.hit(f"op:{OPS[self.expected[0][1]]}:stalled_at_output")
        else:
            self.prev_stall = None
            self.stall_run = 0

        if out_valid and ready:
            if not self.expected:
                if self.check:
                    self.fail(f"SCOREBOARD: unexpected output tag={payload[0]:#x} with nothing outstanding")
            else:
                etag, eop, eres, eflag, t0 = self.expected.popleft()
                self.latencies["div" if eop in DIV_OPS else "fast"].append(self.cycle - t0)
                if self.check and payload != (etag, eres, eflag):
                    self.fail(f"SCOREBOARD: {OPS[eop]} expected tag={etag:#x} result={eres:#06x} "
                              f"flag={eflag} got tag={payload[0]:#x} result={payload[1]:#06x} "
                              f"flag={payload[2]}")
            st["outputs"] += 1
            self.no_progress = 0
        elif self.expected and ready:
            self.no_progress += 1
            if self.check and self.no_progress > WATCHDOG:
                self.fail(f"WATCHDOG: {len(self.expected)} commands outstanding, "
                          f"no output for {WATCHDOG} ready cycles")

        # ---------------- input side ----------------
        accepted = self.pending is not None and in_ready
        if accepted:
            tag, op, a, b, c = self.pending
            res, flag = golden(op, a, b, c)
            self.expected.append((tag, op, res, flag, self.cycle))
            self.pending = None
            st["accepted"] += 1
            name = OPS[op]
            cov.hit(f"op:{name}:accepted")
            cov.hit(f"opxready:{name}:{self.ready_mode}")
            if self.prev_accept:
                cov.hit(f"op:{name}:back_to_back")
            self.accept_run += 1
            if self.accept_run == 8:
                cov.hit("uarch:accept_8_back_to_back")
            self._value_bins(op, a, b, c, flag)
        else:
            self.accept_run = 0
        self.prev_accept = accepted

        # ---------------- micro-architecture events ----------------
        if self._b("s_valid"):
            cov.hit("uarch:skid_full")
        m_valid = self._b("m_valid")
        if m_valid and self._b("rob_full"):
            cov.hit("uarch:rob_full_stall")
        div_busy, div_wb = self._b("div_busy"), self._b("div_wb")
        if m_valid and self._b("m_is_div") and div_busy and not div_wb:
            cov.hit("uarch:div_busy_stall")
        if self._b("issue_fire") and self._b("m_is_div") and div_wb:
            cov.hit("uarch:div_issue_on_writeback")
        e2_valid = self._b("e2_valid")
        if e2_valid and div_wb:
            cov.hit("uarch:simultaneous_writeback")
        if e2_valid and div_busy:
            head = self._u("rob_head")
            age_div = (self._u("div_idx") - head) % self.rob_depth
            age_fast = (self._u("e2_idx") - head) % self.rob_depth
            if age_div < age_fast:   # older divide still running, younger op finishes
                cov.hit("uarch:ooo_completion")
                st["ooo_completions"] += 1

    def _value_bins(self, op, a, b, c, flag):
        cov, name = self.cov, OPS[op]
        if name == "ADD" and flag:
            cov.hit("val:add_carry")
        elif name == "SUB" and flag:
            cov.hit("val:sub_borrow")
        elif name == "ADDS" and flag:
            cov.hit("val:adds_sat_pos" if s16(a) >= 0 else "val:adds_sat_neg")
        elif name == "MUL" and flag:
            cov.hit("val:mul_overflow")
        elif name == "MAC" and flag:
            cov.hit("val:mac_ovf_from_add_only" if a * b <= 0xFFFF else "val:mac_ovf_from_product")
        elif name in ("SHL", "SHR", "SRA"):
            if b & 0xF == 0:
                cov.hit("val:shift_by_0")
            if b & 0xF == 15:
                cov.hit("val:shift_by_15")
            if name == "SRA" and a & 0x8000 and b & 0xF:
                cov.hit("val:sra_negative")
        elif name in ("MIN", "MAX") and (a ^ b) & 0x8000:
            cov.hit("val:minmax_mixed_sign")
        elif name == "CMPLT":
            cov.hit("val:cmplt_true" if s16(a) < s16(b) else "val:cmplt_false")
        elif name in ("DIVU", "REMU"):
            if b == 0:
                cov.hit("val:divu_by_zero" if name == "DIVU" else "val:remu_by_zero")
            elif a < b:
                cov.hit("val:div_a_lt_b")

    async def drain(self, max_cycles=400):
        for _ in range(max_cycles):
            if not self.expected and self.pending is None:
                break
            await self.tick(None, ready=True)
        if self.check and (self.expected or self.pending is not None):
            self.fail(f"DRAIN: {len(self.expected)} accepted commands never came out")
        for _ in range(12):   # nothing extra may appear afterwards
            await self.tick(None, ready=True)


def env_int(name, default):
    return int(os.environ.get(name, default))


def save_coverage(h):
    path = os.environ.get("N2_COV_OUT")
    if path:
        h.cov.merge_into_file(path, h.stats)


def make_rng(seed):
    return random.Random(seed)
