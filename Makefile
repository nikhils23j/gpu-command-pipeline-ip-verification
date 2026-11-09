# GPU command pipeline -- RTL + verification
#   make sv            pure-SV bench, only needs iverilog   (SEED=, CYCLES=, BUG=)
#   make test          all cocotb tests once                (SEED=, BUG=)
#   make regress       SV seeds + cocotb directed + 50 random seeds + coverage closure
#   make mutate        bug-injection campaign
#   make throughput    ops/cycle vs stall rate, divide mix and ROB depth
#   make results       regress + mutate + throughput + results/results.json
#   make sv BUG=BUG_SRA_LOGICAL     <- break it yourself
PY      := .venv/bin/python
SEED    ?= 1
CYCLES  ?= 20000
BUG     ?=
DEFS    := $(if $(BUG),-D$(BUG),)
export BUG SEED

.PHONY: all sv test regress mutate throughput results venv clean

all: sv test

$(PY):
	python3 -m venv .venv
	.venv/bin/pip install -q cocotb

venv: $(PY)

sv:
	mkdir -p build
	iverilog -g2012 $(DEFS) -s gpu_command_pipeline_tb -o build/sv_sim_cli rtl/gpu_command_pipeline.sv tb/gpu_command_pipeline_tb.sv
	vvp -n build/sv_sim_cli +SEED=$(SEED) +CYCLES=$(CYCLES)

test: $(PY)
	cd scripts && ../$(PY) run_tests.py

regress: $(PY)
	cd scripts && ../$(PY) regress.py

mutate: $(PY)
	cd scripts && ../$(PY) mutate.py

throughput: $(PY)
	cd scripts && ../$(PY) throughput.py

results: regress mutate throughput
	cd scripts && ../$(PY) summarize.py

clean:
	rm -rf build scripts/__pycache__ tb/cocotb/__pycache__
