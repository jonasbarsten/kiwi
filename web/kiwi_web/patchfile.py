"""Parses host/kiwi.patch: plugin instances, baseline values and loaded presets."""
from dataclasses import dataclass, field


@dataclass
class Patch:
    instances: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    patch_baseline: dict = field(default_factory=dict)
    presets: dict = field(default_factory=dict)   # instance -> preset URI loaded by the patch


def parse_patch(text):
    patch = Patch()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        command = parts[0]
        if command == 'add' and len(parts) == 3:
            patch.instances[int(parts[2])] = parts[1]
        elif command == 'param_set' and len(parts) == 4:
            patch.baseline[(int(parts[1]), parts[2])] = float(parts[3])
        elif command == 'patch_set' and len(parts) >= 4:
            patch.patch_baseline[(int(parts[1]), parts[2])] = ' '.join(parts[3:])
        elif command == 'preset_load' and len(parts) == 3:
            patch.presets[int(parts[1])] = parts[2]
    return patch
