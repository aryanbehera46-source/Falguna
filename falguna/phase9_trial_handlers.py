"""Synthetic-only handler factory used to prove process restart recovery."""
import time
from pathlib import Path


def build(control, store, organization_id, approved_roots):
    def restart_probe(inputs, context):
        marker = Path(inputs["checkpoint_marker"])
        side_effect = Path(inputs["side_effect_file"])
        if not marker.exists():
            marker.write_text("meaningful work completed before forced process death\n")
            time.sleep(float(inputs.get("first_process_delay", 30)))
        duplicate = side_effect.exists()
        if not duplicate:
            side_effect.write_text(context["idempotency_key"] + "\n")
        return {"restart_recovered": True, "side_effect_created": not duplicate,
                "idempotency_key": context["idempotency_key"]}
    return {"RESTART_PROBE": restart_probe}
