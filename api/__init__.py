# KIS Trading API package

# Workaround for the pandas-ta-openbb fork (imported as `pandas_ta`): its maps.py references
# importlib.metadata without importing it, raising AttributeError on a clean interpreter.
# Importing it here — before api.routers.* run `import pandas_ta` — guarantees the name is
# bound process-wide. Remove once the upstream fork fixes maps.py.
import importlib.metadata  # noqa: F401
