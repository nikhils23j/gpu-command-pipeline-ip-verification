# N2 — GPU command pipeline IP verification

A small SystemVerilog compute pipeline and self-checking unit-level verification environment. Four commands (add, XOR, shift left, subtract) pass through two ready/valid stages. Tags preserve transaction order. The pipeline supports output backpressure, reset, and synchronous flush.

## Run

Install Icarus Verilog 13 or newer and run `make test`. The testbench runs directed boundary inputs and a deterministic pseudo-random sequence of 1,500 cycles. It maintains an independent reference model and scoreboard, checks every emitted result/tag/overflow flag, asserts output stability under backpressure, and requires minimum transaction, opcode, per-opcode stall, flush, reset, and carry/borrow counts. `make clean` removes simulation output. On the local Icarus 13 run, 527 transactions passed; the four opcodes had 120, 142, 130, and 141 accepted inputs, and each appeared in more than 100 stalled cycles.

## Verification plan

| Requirement | Stimulus and checker |
| --- | --- |
| Arithmetic and bitwise correctness | All four opcodes; independent 17-bit reference model |
| Ordering and latency | Input tags recorded at handshake and compared at output |
| Backpressure | Random output readiness, scoreboard and hold behavior |
| Pipeline cancellation | Three flushes discard in-flight expected transactions |
| Recovery | Initial and midstream reset clear the pipeline and scoreboard |
| Boundary values | Directed ADD carry and SUB borrow plus random operands |

This is a personal unit-level learning project. It is not a GPU product, UVM environment, formal coverage model, or professional ASIC verification experience. The testbench uses coverage counters as a practical approximation to functional coverage, not a SystemVerilog covergroup.
