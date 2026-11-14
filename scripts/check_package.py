"""Install the built wheel in an isolated environment and check its public CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import venv


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    wheel = root / "dist" / f"gcolab-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit("Build the wheel with 'uv build' first.")
    with tempfile.TemporaryDirectory(prefix="gcolab-package-") as temp_dir:
        temp = Path(temp_dir)
        venv.EnvBuilder(with_pip=True).create(temp / "env")
        bin_dir = temp / "env" / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        cli = bin_dir / ("gcolab.exe" if os.name == "nt" else "gcolab")
        subprocess.run([str(python), "-m", "pip", "install", str(wheel)], check=True)
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env["GCOLAB_HOME"] = str(temp / "state")

        def run(*args: str) -> str:
            return subprocess.check_output([str(cli), *args], cwd=temp, env=env, text=True, encoding="utf-8")

        assert run("--version").strip() == f"gcolab {version}"
        assert "doctor" in run("--help")
        assert json.loads(run("gpus", "-o", "json"))["accelerators"]
        expected_skill = (root / "skills/gcp-colab-operator/SKILL.md").read_text(encoding="utf-8")
        assert run("skill").strip() == expected_skill.strip()
    print(f"Packaged gcolab {version}: installation, CLI, catalog, and bundled skill passed.")


if __name__ == "__main__":
    main()
