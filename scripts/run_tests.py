"""`make test`: build the DUT (optionally with -DBUG_x) and run every cocotb test once."""
import os
import sys

from common import build, run

bug = os.environ.get("BUG", "").strip()
seed = os.environ.get("SEED", "1")
runner, bdir = build("test" + (f"_{bug}" if bug else ""), defines={bug: 1} if bug else {})
n, nf, fails = run(runner, bdir, "test", env={"N2_SEED": seed, "N2_CYCLES": os.environ.get("CYCLES", "3000")})
print(f"\ncocotb: {n - nf}/{n} tests passed" + (f" (RTL built with -D{bug})" if bug else ""))
for t, m in fails.items():
    print(f"  FAIL {t}: {m}")
print(f"log: {bdir / 'test.log'}")
sys.exit(1 if nf or n == 0 else 0)
