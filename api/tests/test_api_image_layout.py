"""The API image can import the app it ships (#127 follow-up).

``Dockerfile.api`` copied ``kis_adapter/``, ``kiwoom_adapter/``, ``strategy/``
and ``api/`` but not ``backend/``, which ``api/`` imports — so ``import
api.main`` failed and the container could not start, while every test passed
from a full checkout. This rebuilds the image's ``/app`` from the Dockerfile's
own ``COPY`` lines and imports the app there, in a fresh interpreter that
cannot see the rest of the repository.

Third-party packages are covered by the ``pytest-api`` CI job, which installs
only ``requirements-api.txt``.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile.api"


def _copied_dirs() -> list[str]:
    """``COPY <dir>/ ./<dir>/`` lines; files such as requirements are skipped."""
    found = re.findall(r"^COPY\s+(\S+)/\s+\./(\S+)/\s*$", DOCKERFILE.read_text(), re.M)
    assert found, "no directory COPY lines found in Dockerfile.api"
    for src, dst in found:
        assert src == dst, f"COPY {src}/ ./{dst}/ renames a package"
    return [src for src, _ in found]


def test_the_image_copies_backend():
    assert "backend" in _copied_dirs()


def test_the_app_imports_from_only_what_the_image_copies(tmp_path):
    for name in _copied_dirs():
        (tmp_path / name).symlink_to(ROOT / name, target_is_directory=True)
    env = {
        k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")
    }
    env.update(
        JWT_SECRET_KEY="test-only-jwt-secret-not-for-production-use-32c",
        KIS_CREDENTIAL_KEY=os.environ["KIS_CREDENTIAL_KEY"],
        PYTHONDONTWRITEBYTECODE="1",
    )
    # -P adds no implicit path entry; the fake /app is put first explicitly, and
    # the repository itself must not be reachable.
    code = (
        "import sys; sys.path.insert(0, '.');"
        "assert not any(p.startswith(%r) for p in sys.path if p), sys.path;"
        "import api.main" % str(ROOT)
    )
    proc = subprocess.run(
        [sys.executable, "-P", "-c", code],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
