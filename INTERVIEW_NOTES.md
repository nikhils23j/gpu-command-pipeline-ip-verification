# Interview notes: GPU command pipeline RTL + verification

## 1. What it is
A 16-op, 16-bit ALU command pipeline in SystemVerilog. It has ready/valid handshakes, a skid buffer, an iterative divider, and an 8-entry reorder buffer that keeps results in order. I verified it with two independent benches: one in pure SystemVerilog and one in cocotb/Python. Both have golden models, scoreboards, protocol checks, manual functional coverage and a bug-injection campaign. It's a personal project, simulation-only, built with AI pair-programming help.

## 2. How it works, and why I made each choice

**Skid buffer on the input.**
- `in_ready` comes straight from a flop (`!s_valid`), so there is no combinational path from `out_ready` back to the upstream.
- The second entry catches the command that arrives in the cycle when the main register can't move.
- Why: without it you either lose full throughput or build a long ready chain.

**ROB slots used as credits.**
- A command issues only if a ROB slot is free, and the slot is reserved at issue.
- That means the execution units never stall: they always have somewhere to write.
- Backpressure exists only at two points, issue and the output register. That made the design much easier to reason about.

**Fixed-latency fast path (EX1 → EX2).**
- EX1 does the ALU ops and the 16×16 multiply. EX2 does the MAC add and the flags.
- With no stalls, latency is 5 cycles from input handshake to output handshake (measured).

**Iterative divider.**
- A restoring divider that produces one quotient bit per cycle: 16 iterations plus 1 write-back cycle.
- A new divide can issue in the same cycle the old one writes back, so one divide takes 17 cycles.
- This is what creates variable latency and out-of-order completion. Only one divide can be in flight at a time.

**Reorder buffer.**
- Each slot holds the tag, result, flag and a done bit.
- There are two write ports (fast path and divider), and they can both fire in the same cycle; a coverage bin confirms this happens.
- Results retire from the head in order into an output register that holds its value under stall.

**Flush and reset.**
- Both are synchronous and drop everything in flight, including the divider.
- `in_ready` and `out_valid` are 0 in that cycle.
- I wrote the rules down first so the scoreboard rule is simple: on a kill, clear the expected queue.

**Verification structure.**
- The cocotb harness uses one coroutine per clock: drive on the falling edge, sample in ReadOnly, then let the rising edge happen. What I sample is exactly what the DUT sees at the edge.
- Checks:
  - scoreboard: order + tag + value + flag on every output
  - output hold while stalled
  - no output when nothing is outstanding
  - quiet handshakes during kill
  - watchdog
  - drain at the end
- I chose Icarus because it's free and installed. It can't do classes, covergroups or concurrent SVA, so coverage is 174 hand-written bins in Python and the SV assertions are immediate.
- **Bug injection:** 9 realistic bugs behind `ifdef`. If the bench doesn't fail with a bug turned on, the bench is wrong.

## 3. Key results (all measured, from `results/`)
- All tests pass on the bug-free RTL:
  - SV bench: 20 seeds, 408,480 cycles, 168,639 outputs checked.
  - cocotb: 6 directed tests + 50 random seeds, 157,475 cycles, 59,060 outputs checked.
  - Total: 227,699 outputs checked.
- Coverage: 174/174 bins, reached after the directed tests plus 4 random seeds.
- Bug injection: 9/9 bugs caught, with 0 false alarms on clean RTL.
  - Hardest: `BUG_FLUSH_DIV`, caught by 1/6 directed tests and 2/3 random seeds.
  - `BUG_OUT_NOT_HELD` was missed by the 2 always-ready SV seeds.
- Throughput:
  - Fast ops: 1.000 op/cycle with out_ready = 1 (0.499 at 50% ready).
  - 5% divides: 0.595 / 0.653 / 0.750 ops/cycle for ROB depth 4 / 8 / 16.
  - 20% divides: about 0.24–0.28, against a divider-bound ceiling of about 0.294.
- `make results` takes 8 min 27 s on an M2 Mac.

**What these results do NOT show:**
- Anything about clock frequency, area, power or timing closure. There was no synthesis.
- Correctness against a spec someone else wrote. I wrote the RTL and both models.
- Formal proof. Coverage closing fast mostly means my bins are coarse.
- That the bench would catch every bug. The 9 mutations were written by hand.
- Statistical confidence on throughput. Each configuration is one random run.

## 4. ~2-minute answer (STAR)

**Situation.** I kept reading that for chips like GPUs, most of the engineering effort goes into verification, not design. I didn't really understand what that meant in practice. I had an earlier, small two-stage pipeline project, and I decided to rebuild it into something closer to a real block and see what it takes to actually trust it.

**Task.** My goal was a pipeline that was hard enough to get wrong: variable latency, out-of-order completion, backpressure and flush. Then I'd build a testbench that could prove it was right, or at least prove the testbench itself would catch mistakes. I gave myself one rule: every number had to come from a sim I actually ran.

**Action.** I designed a 16-op ALU pipeline in SystemVerilog:
- a skid buffer on the input
- a two-stage fast path
- an iterative divider that finishes late
- an 8-entry reorder buffer that puts results back in order, with its free slots acting as credits

Then I built two benches. One is pure SystemVerilog, so it runs with just Icarus. The other is in cocotb, with:
- a Python golden model and seeded constrained-random stimulus
- five backpressure patterns
- a scoreboard checking order, tags and values
- protocol monitors for the ready/valid hold rule
- 174 functional coverage bins I wrote by hand, since Icarus has no covergroups

I used AI pair-programming for a lot of the scaffolding, but I made the design and spec decisions and checked the results myself. The interesting moment was that the clean RTL passed on the first run, which made me suspicious. So I injected nine realistic bugs, things like a flush that forgets the divider or an output that changes while stalled, and checked which tests caught each one.

**Result.**
- All nine bugs were caught, and there were no false alarms on the clean design. One was only caught by a single directed test, which taught me why "flush while busy" deserves its own coverage bin.
- The regression checked about 228 thousand outputs across both benches, and the cocotb coverage hit 100% of my bins.
- I measured one op per cycle on fast ops. With 20% divides, throughput sits just under the 0.29 ops/cycle limit the single divider allows, and a bigger ROB doesn't help.
- I'm upfront that it's simulation-only, with no synthesis or timing. But I now understand why verification is where the effort goes.

## 5. Likely follow-up questions

1. **Why a skid buffer and not just `in_ready = out_ready`-style chaining?** Chaining puts a combinational path from the output all the way to the input, which gets long. Without a skid buffer, a registered ready loses throughput. With two entries I get a registered `in_ready` and still one op per cycle; the throughput test measured 1.000.

2. **How do you know the ROB can never overflow?** Ops only issue when `rob_count < ROB_DEPTH`, so a slot always exists before any result is written. I also proved the bench would notice if that broke: `BUG_ROB_NOFULL` removes the check, and all 8 SV seeds and all 3 cocotb random seeds fail.

3. **What does your coverage actually mean?** It's 174 hand-counted bins: ops crossed with conditions, plus value corners and internal events. 100% means every scenario I thought of happened at least once. It doesn't mean every scenario that matters happened, and it closing after 4 seeds tells me the bins are coarse.

4. **Why not UVM / SVA?** I used Icarus, which doesn't support classes or concurrent assertions. cocotb gave me the same structure (driver, monitor, scoreboard, coverage) in Python. With a commercial simulator I'd write the hold rule as an SVA property and use covergroups.

5. **How do you avoid the golden model just copying the RTL?** The model is written from the spec table, not from reading the RTL. There are two models (Python and SV) and they agree. But the same person wrote all of it, so a spec misunderstanding could be shared. That's a real limit.

6. **Where does throughput go with divides?** There's one divider at 17 cycles per divide. At 20% divides the ceiling is 1/(17 × 0.2) ≈ 0.294, and I measured 0.24–0.28. At 5% divides, the ROB depth is the limit: depth 4/8/16 gave 0.595/0.653/0.750. The next step would be a second divider or a radix-4 divider.

7. **Did the testbench find bugs in your own RTL?** Honestly, no functional ones. The clean RTL passed the first full run; the bring-up issues were Icarus quirks. That's exactly why I did bug injection: to show the bench can fail.

8. **What would you do next?** Synthesize it with Yosys to get a rough area and critical path. Add SVA and formal checks for the hold rule and ROB invariants on a tool that supports them. Run longer, repeated throughput runs with confidence intervals. Have someone else write an independent spec model.

## 6. Things I should be able to explain cold
- [ ] Ready/valid handshake rules, and why valid must not depend on ready
- [ ] How the 2-entry skid buffer works, cycle by cycle
- [ ] Why ROB slots work as credits, and where backpressure can and can't occur
- [ ] The restoring division algorithm and why it takes 16 iterations + 1 write-back
- [ ] How in-order retirement works with two write ports, and what out-of-order completion means here
- [ ] Flush/reset semantics and why the hold rule has an exception for them
- [ ] Signed vs unsigned compare, signed overflow detection for ADDS, SRA vs SHR
- [ ] Why I sample in ReadOnly after a falling-edge drive (the race this avoids)
- [ ] What each injected bug does and which check catches it
- [ ] The divider-bound throughput ceiling: 1/(17 × divide fraction)
- [ ] Why 100% of my coverage bins is not "verified"
- [ ] What I'd need to add for timing/area numbers (synthesis), and why I don't have them
