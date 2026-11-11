"""Execute the real shell helper with a recording curl substitute, never a ping."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "private-check-token"


def run_heartbeat(tmp_path, *, url=f"https://hc-ping.com/{SECRET}", body="OK", status="200", curl_exit=0, event=None):
    script = (REPO_ROOT / "update-translations.sh").read_text()
    start = script.index("send_heartbeat_if_configured() {")
    end = script.index("\n}\n", start) + len("\n}\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "with pathlib.Path(os.environ['CURL_CALLS']).open('a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stdout.write(os.environ['RESPONSE_BODY'])\n"
        "if '--write-out' in sys.argv:\n"
        "    sys.stdout.write('\\n' + os.environ['RESPONSE_STATUS'])\n"
        "sys.stderr.write('curl diagnostic: ' + os.environ['HEALTHCHECK_URL'])\n"
        "sys.exit(int(os.environ['CURL_EXIT']))\n"
    )
    curl.chmod(0o755)
    calls_path = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CURL_CALLS": str(calls_path),
        "RESPONSE_BODY": body,
        "RESPONSE_STATUS": status,
        "CURL_EXIT": str(curl_exit),
    }
    env.pop("HEALTHCHECK_URL", None)
    if url is not None:
        env["HEALTHCHECK_URL"] = url
    harness = "set -euo pipefail\nlog() { printf '%s\\n' \"$1\"; }\n"
    harness += script[start:end]
    harness += '\nsend_heartbeat_if_configured "$@"\nprintf "HELPER_COMPLETED\\n"\n'
    command = ["bash", "-c", harness, "heartbeat-test"]
    if event is not None:
        command.append(event)
    result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
    assert result.returncode == 0, result.stderr
    assert "HELPER_COMPLETED" in result.stdout
    assert SECRET not in result.stdout + result.stderr
    assert result.stderr == ""
    return result.stdout, calls


@pytest.mark.parametrize("event", [None, "pending_pr_guard", "no_source_changes", "smoke_only_ok"])
def test_healthchecks_receipt_preserves_event_and_single_request(tmp_path, event):
    output, calls = run_heartbeat(tmp_path, event=event)
    assert f"event '{event or 'heartbeat'}'" in output
    assert "Health check provider accepted receipt" in output
    assert "HTTP 200" in output
    assert len(calls) == 1
    assert calls[0] == ["-fsS", "--retry", "3", "--max-time", "10", "--write-out", "\n%{http_code}", f"https://hc-ping.com/{SECRET}"]


@pytest.mark.parametrize("body", ["", "OK (not found)", "OK (rate limited)", SECRET, "OK\n"])
def test_healthchecks_wrong_or_empty_body_never_proves_acceptance(tmp_path, body):
    output, calls = run_heartbeat(tmp_path, body=body)
    assert "Warning: Health check ping failed" in output
    assert "HTTP 200" in output
    assert "provider accepted receipt" not in output
    assert len(calls) == 1


@pytest.mark.parametrize(("status", "curl_exit"), [("204", 0), ("302", 0), ("404", 22), ("500", 22), ("000", 28), ("000", 6), ("200", 28)])
def test_nonaccepted_status_and_transport_errors_are_nonfatal(tmp_path, status, curl_exit):
    output, calls = run_heartbeat(tmp_path, status=status, curl_exit=curl_exit)
    assert "Warning: Health check ping failed" in output
    assert f"HTTP {status}" in output
    assert "provider accepted receipt" not in output
    assert len(calls) == 1


@pytest.mark.parametrize("body", ["", "OK", SECRET])
def test_generic_endpoint_records_only_http_receipt(tmp_path, body):
    output, calls = run_heartbeat(tmp_path, url=f"https://monitor.example/{SECRET}", status="204", body=body)
    assert "Health check HTTP receipt" in output
    assert "HTTP 204" in output
    assert "provider accepted" not in output
    assert len(calls) == 1


@pytest.mark.parametrize(("status", "curl_exit"), [("302", 0), ("500", 22), ("000", 28)])
def test_generic_endpoint_failure_is_nonfatal(tmp_path, status, curl_exit):
    output, calls = run_heartbeat(tmp_path, url=f"https://monitor.example/{SECRET}", status=status, curl_exit=curl_exit)
    assert "Warning: Health check ping failed" in output
    assert f"HTTP {status}" in output
    assert "Health check HTTP receipt" not in output
    assert len(calls) == 1


@pytest.mark.parametrize("url", [None, ""])
def test_unconfigured_heartbeat_makes_no_request(tmp_path, url):
    output, calls = run_heartbeat(tmp_path, url=url)
    assert "No HEALTHCHECK_URL configured" in output
    assert calls == []


def test_host_lookalike_does_not_claim_provider_acceptance(tmp_path):
    output, _ = run_heartbeat(tmp_path, url=f"https://hc-ping.com.example/{SECRET}")
    assert "Health check HTTP receipt" in output
    assert "provider accepted" not in output


def test_invalid_status_cannot_leak_response_data(tmp_path):
    output, _ = run_heartbeat(tmp_path, status=SECRET)
    assert "Warning: Health check ping failed" in output
    assert "provider accepted" not in output
