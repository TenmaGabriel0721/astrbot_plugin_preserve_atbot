"""Expose this checkout through the import path used by AstrBot plugins."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent
for name in ("data", "data.plugins", "data.plugins.astrbot_plugin_preserve_atbot"):
    if name not in sys.modules:
        module = types.ModuleType(name)
        module.__path__ = [str(ROOT)]
        sys.modules[name] = module
    if "." in name:
        parent, _, child = name.rpartition(".")
        setattr(sys.modules[parent], child, sys.modules[name])
