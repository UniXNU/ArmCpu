#!/usr/bin/env python3
"""Host tests of the platform sleep logic (tests/armcpu_sleep_host.c, ArmCpuSleep.h)."""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory() as tmp:
    exe = Path(tmp) / 'armcpu-sleep-host'
    build = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O1', '-g', '-fsanitize=address,undefined',
                            str(ROOT / 'tests/armcpu_sleep_host.c'), '-o', str(exe)], capture_output=True, text=True)
    if build.returncode != 0:
        sys.stderr.write(build.stderr)
        raise SystemExit('FAIL: platform sleep host tests (build)')
    result = subprocess.run([str(exe)], capture_output=True, text=True)
sys.stderr.write(result.stderr)
if result.returncode != 0:
    raise SystemExit('FAIL: platform sleep host tests')
print(result.stdout.strip())
print('PASS: platform sleep host tests')
