import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "tools" / "strategy_recommendation_file_bridge.py"


def test_file_bridge_exchanges_exact_prompt_and_json_response_atomically(
    tmp_path: Path,
) -> None:
    exchange = tmp_path / "exchange"
    output = tmp_path / "model-output.json"
    prompt = "fresh trusted GOLD context"
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    process = subprocess.Popen(
        [
            sys.executable,
            str(BRIDGE),
            "--exchange-dir",
            str(exchange),
            "--bridge-timeout-seconds",
            "5",
            "--output-last-message",
            str(output),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(prompt)
    process.stdin.close()
    request_path = exchange / "request.json"
    for _ in range(100):
        if request_path.exists():
            break
        time.sleep(0.02)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["prompt"] == prompt
    assert request["prompt_sha256"] == digest

    response = {
        "direction": "neutral",
        "style": "steady",
        "rationale": "range",
        "key_levels": [4050],
        "ai_self_assessment": 7,
        "evidence_used": ["D1"],
    }
    temporary = exchange / f"response-{digest}.json.next"
    temporary.write_text(json.dumps(response), encoding="utf-8")
    temporary.replace(exchange / request["response_filename"])

    assert process.wait(timeout=5) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == response
