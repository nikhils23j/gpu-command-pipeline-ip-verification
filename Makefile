.PHONY: test clean
test:
	mkdir -p build
	iverilog -g2012 -Wall -s gpu_command_pipeline_tb -o build/sim rtl/gpu_command_pipeline.sv tb/gpu_command_pipeline_tb.sv
	vvp build/sim
clean:
	rm -rf build
