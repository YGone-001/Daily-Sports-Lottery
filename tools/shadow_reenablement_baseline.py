"""Reproduce the disable/re-enable race against the exact, unmodified baseline.

The historical reader runs only in an exported temporary checkout. Its unsafe
result is evidence of a defect, never an authority for the current application.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

BASELINE = "3fc6683081560936dc0ea6e7650155e8cb4123ee"
SCRIPT = r'''
import json, sys
from pathlib import Path
from tools.shadow_snapshot_control_validation import SyntheticControlScenario
from tools.shadow_trusted_time_validation import NOW
from utils.recovery_authority import build_recovery_evidence
s = SyntheticControlScenario(Path(sys.argv[1]))
if not (s.publish()["control_committed"] and s.select()["control_committed"] and s.read().is_resolved):
    raise RuntimeError("baseline positive chain failed")
s.controller.disable(at=NOW)
s.control_checkpoint = s.checkpoint()
if not s.select(operation="RECOVER")["control_committed"] or s.reader.enabled:
    raise RuntimeError("baseline disabled reconciliation failed")
checkpoint = s.audit.export_checkpoint(s.checkpoint_private, issued_at=NOW)
recovery = build_recovery_evidence(audit_store=s.audit, audit_checkpoint=checkpoint,
    checkpoint_trusted_public_key_path=s.checkpoint_public, signing_private_key_path=s.recovery_private,
    current_time=NOW, recovery_actor="synthetic-recovery", selected_snapshot_id=s.registry.active_snapshot_id,
    effective_snapshot_id=s.registry.active_snapshot_id, max_age_seconds=60)
original = s.reader._reenable_rejections
def interleave(**kwargs):
    codes = original(**kwargs)
    if codes:
        raise RuntimeError("baseline recovery evidence was not valid")
    s.reader.disable(at=NOW, reason="newer emergency while re-enable was pending")
    return codes
s.reader._reenable_rejections = interleave
result = s.reader.re_enable(at=NOW, read_context=s.read_context(), evidence=s.evidence, recovery_evidence=recovery)
print(json.dumps(dict(reenable_returned_success=result["enabled"], reader_enabled=s.reader.enabled,
    resolution_status=s.read().resolution_status), sort_keys=True))
'''


def run_baseline():
    root = Path(__file__).resolve().parents[1]
    archive = subprocess.run(["git", "archive", BASELINE], cwd=root, check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory(prefix="shadow-disable-baseline-") as temp:
        checkout = Path(temp) / "checkout"
        checkout.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(checkout, filter="data")
        process = subprocess.run([sys.executable, "-c", SCRIPT, str(Path(temp) / "isolated-scene")],
            cwd=checkout, capture_output=True, text=True, timeout=45, check=True)
        observed = json.loads(process.stdout)
    return dict(baseline=BASELINE, historical_export_only=True, observed=observed,
        defect_reproduced=observed == dict(reenable_returned_success=True, reader_enabled=True,
            resolution_status="RESOLVED_SHADOW"), production_activation="NOT_ENABLED")


def main():
    try:
        result = run_baseline()
    except Exception as exc:
        result = dict(defect_reproduced=False, error=str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["defect_reproduced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
