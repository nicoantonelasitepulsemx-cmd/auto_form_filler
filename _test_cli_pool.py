"""Drive auto_fill.py via subprocess in --accounts mode against test_form.html."""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
URL = (HERE / "test_form.html").resolve().as_uri()

# config: tiny v2 config that fills email + name
config = {
    "version": 2,
    "target_url": URL,
    "vars": {},
    "actions": [
        {
            "kind": "fill",
            "field_id": "email",
            "selectors": [{"strategy": "stable_id", "selector": "email", "weight": 95}],
            "fingerprint": None,
            "frame_chain": ["top"],
            "value_template": "{email}",
            "input_method": "fill",
        },
        {
            "kind": "fill",
            "field_id": "full_name",
            "selectors": [{"strategy": "stable_id", "selector": "full_name", "weight": 95}],
            "fingerprint": None,
            "frame_chain": ["top"],
            "value_template": "{full_name}",
            "input_method": "fill",
        },
    ],
}

accounts = [
    {"name": "a1", "headless": True, "vars": {"email": "a1@x.com", "full_name": "A1"}},
    {"name": "a2", "headless": True, "vars": {"email": "a2@x.com", "full_name": "A2"}},
    {"name": "a3", "headless": True, "vars": {"email": "a3@x.com", "full_name": "A3"}},
]

cfg_path = HERE / "samples" / "_cli_pool_config.json"
acc_path = HERE / "samples" / "_cli_pool_accounts.json"
cfg_path.parent.mkdir(exist_ok=True)
cfg_path.write_text(json.dumps(config, indent=2))
acc_path.write_text(json.dumps(accounts, indent=2))

cmd = [
    sys.executable, str(HERE / "auto_fill.py"),
    "--config", str(cfg_path),
    "--accounts", str(acc_path),
    "--workers", "3",
    "--headless",
]
print("running:", " ".join(cmd))
result = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, timeout=120)
print("---- STDOUT ----")
print(result.stdout[-2000:])
print("---- STDERR ----")
print(result.stderr[-1000:])
assert result.returncode == 0, result.returncode
assert "3/3 task(s) succeeded" in result.stdout, "pool didn't succeed for all 3 accounts"
print("OK")
