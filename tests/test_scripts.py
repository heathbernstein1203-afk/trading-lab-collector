"""Run each script-style test in its own Python process (see conftest.py)."""
import os, subprocess, sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = ["test_bot.py", "test_demo_sign.py", "test_extras.py", "test_parse.py"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_script(name):
    r = subprocess.run([sys.executable, os.path.join(HERE, name)], cwd=os.path.dirname(HERE),
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"{name} failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}"
