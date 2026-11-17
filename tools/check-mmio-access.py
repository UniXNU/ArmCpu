#!/usr/bin/env python3
"""Check that ArmCpu reaches device registers only with accesses a hypervisor can emulate.

Under a hypervisor (Gunyah on Snapdragon) the GIC distributor and redistributors are trapped and emulated. The hypervisor decodes
a trapped access from its syndrome, which the CPU gives only for a single load or store of one general register without
writeback (ESR ISV=1). A load or store with pre- or post-index writeback, a pair (LDP/STP) or a SIMD register gives none; the
hypervisor then injects a synchronous external abort (ESR 0x96000010), as it did on the first GICD_ISENABLER read of a sleep.

1. Every device register access in ArmCpu.cpp goes through the accessors read8/write8/read32/write32/read64/write64 (no volatile
   cast anywhere else).
2. Those accessors, inlined into the loops and neighbouring accesses the driver uses, compile (arm64e, -Os and -O2) to single
   loads and stores of one register with no writeback: no `[xN], #imm`, `[xN, #imm]!`, LDP/STP or SIMD access off a device
   base. Stack accesses (sp, x29) are not device accesses and are skipped.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'ArmCpu.cpp').read_text()
NAMES = ('read8', 'write8', 'read32', 'write32', 'read64', 'write64')

failures = []
accessors = {}
for match in re.finditer(r'^static (?:inline )?(?:uint8_t|uint32_t|uint64_t|void) (\w+) \(uintptr_t base, unsigned offset[^)]*\)\n\{\n.*?^\}\n',
                         SOURCE, re.S | re.M):
    if match.group(1) in NAMES:
        accessors[match.group(1)] = match.group(0)
missing = [n for n in NAMES if n not in accessors]
if missing:
    failures.append(f'accessors missing from ArmCpu.cpp: {", ".join(missing)}')

outside = SOURCE
for body in accessors.values():
    outside = outside.replace(body, '')
for number, line in enumerate(outside.splitlines(), 1):
    if re.search(r'reinterpret_cast<\s*volatile\s+uint(8|16|32|64)_t\s*\*|\(\s*volatile\s+uint(8|16|32|64)_t\s*\*\s*\)', line):
        failures.append(f'a device access outside the accessors: {line.strip()}')

# The shapes ArmCpu uses, each with the accessors it needs: a register file walked word by word (ISENABLER/ICENABLER/ISPENDR),
# neighbouring words, byte priorities, 64-bit routers and redistributor TYPERs walked frame by frame.
PROBES = [
    (('read32',), r'''
// The word count is read from GICD_TYPER and clamped to a constant, as sleepBootWait does; that bound is what lets the compiler walk
// the file with a post-indexed load.
static unsigned probe_words (uintptr_t base)
{
  const unsigned lines = ((read32 (base, 4) & 31) + 1) * 32;
  return lines / 32 < 32u ? lines / 32 : 32u;
}
extern "C" uint32_t probe_read_words (uintptr_t base)
{
  const unsigned words = probe_words (base);
  uint32_t x = 0;
  for (unsigned w = 1; w < words; w++) { x ^= read32 (base, 0x100 + 4 * w); }
  return x;
}
extern "C" uint64_t probe_neighbours (uintptr_t base)
{
  return read32 (base, 0x200) + (static_cast<uint64_t> (read32 (base, 0x204)) << 32) + read32 (base, 0x208);
}
'''),
    (('read32', 'write32'), r'''
extern "C" void probe_write_words (uintptr_t base)
{
  const unsigned words = probe_words (base);
  for (unsigned w = 1; w < words; w++) { write32 (base, 0x180 + 4 * w, 0xffffffff); }
}
extern "C" void probe_write_values (uintptr_t base)
{
  const unsigned words = probe_words (base);
  for (unsigned w = 1; w < words; w++) { write32 (base, 0x100 + 4 * w, w * 3u + 1u); }
}
extern "C" void probe_neighbour_writes (uintptr_t base, uint32_t a, uint32_t b)
{
  write32 (base, 0x80, a);
  write32 (base, 0x84, b);
}
'''),
    (('read8', 'write8'), r'''
extern "C" uint32_t probe_bytes (uintptr_t base, unsigned count)
{
  uint32_t x = 0;
  count = count < 64u ? count : 64u;
  for (unsigned i = 0; i < count; i++) { write8 (base, 0x400 + i, 0xa0); x += read8 (base, 0x400 + i); }
  return x;
}
'''),
    (('read64', 'write64'), r'''
extern "C" uint64_t probe_frames (uintptr_t base, unsigned frames, uint64_t aff)
{
  uint64_t x = 0;
  frames = frames < 8u ? frames : 8u;
  for (unsigned i = 0; i < frames; i++) { x ^= read64 (base, 0x20000 * i + 8); write64 (base, 0x6000 + 8 * i, aff); }
  return x;
}
'''),
]
PROBE = '#include <stdint.h>\n' + ''.join(accessors.values()) + ''.join(code for needs, code in PROBES
                                                                      if all(n in accessors for n in needs))

MEMORY = re.compile(r'^\s*(ld|st)[a-z0-9]*\s')
WRITEBACK = re.compile(r'\]!|\],\s*#|\],\s*x')
STACK = re.compile(r'\[(sp|x29|fp)\b')

if 'read32' in accessors:
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / 'probe.cpp'
        probe.write_text(PROBE)
        for level in ('-Os', '-O2'):
            obj = Path(tmp) / f'probe{level}.o'
            build = subprocess.run(['xcrun', 'clang++', '-target', 'arm64e-apple-macos', '-std=c++17', level, '-ffreestanding',
                                    '-fno-exceptions', '-fno-rtti', '-Wall', '-Wextra', '-Werror', '-c', str(probe), '-o', str(obj)],
                                   capture_output=True, text=True)
            if build.returncode != 0:
                sys.stderr.write(build.stderr)
                raise SystemExit(f'FAIL: MMIO access check (probe build {level})')
            listing = subprocess.run(['xcrun', 'otool', '-tvV', str(obj)], capture_output=True, text=True, check=True).stdout
            function = '?'
            for line in listing.splitlines():
                if line.endswith(':') and not line.startswith(('\t', ' ')) and '(' not in line:
                    function = line[:-1]
                    continue
                text = line.split('\t', 1)[1] if '\t' in line else line
                if not MEMORY.match(text) or STACK.search(text):
                    continue
                op = text.split()[0]
                if WRITEBACK.search(text) or op in ('ldp', 'stp', 'ldnp', 'stnp') or re.search(r'\b[qdsbh]\d+\b', text.split(',')[0]):
                    failures.append(f'{level} {function}: {" ".join(text.split())}')

if failures:
    for failure in failures:
        print(failure)
    raise SystemExit('FAIL: MMIO access check')
print('PASS: MMIO access check: every device access goes through the accessors; at -Os and -O2 they stay single loads and stores '
      'without writeback')
