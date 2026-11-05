"""A built core wheel must carry the skill loaded during config resolution."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def test_clean_wheel_contains_importable_cleanup_skill_resource(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    source = tmp_path / "clean-source"
    source.mkdir()
    shutil.copy2(repository / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(repository / "bazarlyk", source / "bazarlyk",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                    "--wheel-dir", str(wheels), str(source)], check=True, capture_output=True,
                   text=True, timeout=120)
    wheel, = wheels.glob("*.whl")
    resource = "bazarlyk/agents/skills/cleanup/SKILL.md"
    with zipfile.ZipFile(wheel) as archive:
        assert resource in archive.namelist()

    target = tmp_path / "installed"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
                   check=True, capture_output=True, text=True, timeout=120)
    code = (
        "import json, pathlib, sys; sys.path.insert(0, sys.argv[1]); "
        "from bazarlyk.agents import cleanup_workflow as w; "
        "print(json.dumps({'module': str(pathlib.Path(w.__file__).resolve()), "
        "'name': w.skill_metadata()['name'], 'owner': w.skill_metadata()['owner'], "
        "'resource': w.skill_files()['SKILL.md'].decode()}))"
    )
    env = dict(os.environ, PYTHONPATH="", PYTHONNOUSERSITE="1")
    result = subprocess.run([sys.executable, "-c", code, str(target)], cwd=tmp_path, env=env,
                            check=True, capture_output=True, text=True, timeout=30)
    loaded = json.loads(result.stdout)
    assert Path(loaded["module"]).is_relative_to(target)
    assert loaded["name"] == "bazarlyk-cleanup"
    assert loaded["owner"] == {"kind": "core", "id": "cleanup-workflow"}
    assert "# Clean up operational work" in loaded["resource"]
