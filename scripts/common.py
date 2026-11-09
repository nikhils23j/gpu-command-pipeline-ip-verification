"""Shared helpers: build + run the cocotb bench with Icarus via cocotb_tools.runner."""

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RTL = ROOT / "rtl" / "gpu_command_pipeline.sv"
TB_DIR = ROOT / "tb" / "cocotb"
BUILD = ROOT / "build"
RESULTS = ROOT / "results"
TOP = "gpu_command_pipeline"


def tool_versions():
    def first_line(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0].strip()
        except Exception as e:  # noqa: BLE001
            return f"unavailable ({e})"
    import cocotb
    return {
        "iverilog": first_line(["iverilog", "-V"]),
        "python": sys.version.split()[0],
        "cocotb": cocotb.__version__,
    }


def build(tag, defines=None, parameters=None):
    from cocotb_tools.runner import get_runner
    bdir = BUILD / f"cocotb_{tag}"
    runner = get_runner("icarus")
    runner.build(sources=[RTL], hdl_toplevel=TOP, build_dir=bdir, always=True,
                 defines=defines or {}, parameters=parameters or {},
                 timescale=("1ns", "1ps"), build_args=["-g2012"],
                 log_file=bdir / "build.log")
    return runner, bdir


def run(runner, bdir, name, testcase=None, env=None, parameters=None, module="test_pipeline"):
    """Run the cocotb module; returns (n_tests, n_failed, {test: failure_msg})."""
    xml = bdir / f"{name}.xml"
    if xml.exists():
        xml.unlink()
    full_env = {"PYTHONPATH": str(TB_DIR)}
    full_env.update(env or {})
    runner.test(test_module=module, hdl_toplevel=TOP, build_dir=bdir,
                test_dir=TB_DIR, testcase=testcase, extra_env=full_env,
                results_xml=str(xml), parameters=parameters,
                log_file=bdir / f"{name}.log", timescale=("1ns", "1ps"))
    failures = {}
    n = 0
    if xml.exists():
        for tc in ET.parse(xml).getroot().iter("testcase"):
            n += 1
            f = tc.find("failure")
            if f is None:
                f = tc.find("error")
            if f is not None:
                failures[tc.get("name")] = (f.get("message") or "").strip()[:300]
    else:
        failures["<simulator>"] = "no results.xml produced"
    return n, len(failures), failures


def fresh(path):
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return path


os.environ.setdefault("COCOTB_REDUCED_LOG_FMT", "1")


def build_sv(out, defines=()):
    """Compile the pure-SV testbench with iverilog; returns the command used."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["iverilog", "-g2012", *[f"-D{d}" for d in defines], "-s", "gpu_command_pipeline_tb",
           "-o", str(out), str(RTL), str(ROOT / "tb" / "gpu_command_pipeline_tb.sv")]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return cmd


def run_sv(sim, seed, cycles):
    """Run the SV bench; returns (passed, first relevant line)."""
    r = subprocess.run(["vvp", "-n", str(sim), f"+SEED={seed}", f"+CYCLES={cycles}"],
                       capture_output=True, text=True)
    lines = r.stdout.splitlines()
    for ln in lines:
        if "FATAL" in ln or "Error" in ln:
            return False, ln.strip()
    for ln in lines:
        if ln.startswith("SV_PASS"):
            cov = next((x for x in lines if x.startswith("SV_COVERAGE")), "")
            return True, ln.strip() + " | " + cov.strip()
    return False, "no SV_PASS line: " + " / ".join(lines[-3:])
