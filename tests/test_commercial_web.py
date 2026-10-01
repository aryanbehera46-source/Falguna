"""Phase 5 Sprint 1 -- Commercial Operating Foundation, HTTP layer.

Same _LiveServerCase pattern established in test_hq_web.py /
test_partner_management.py: a real TTTHQHandler on a dynamic ephemeral
port, proving the human-approval gate and the refund/financial-integrity
guarantees hold at the HTTP layer itself -- not only inside the Python
store classes -- so a direct API call cannot bypass whatever a UI would
otherwise have disabled. Synthetic data only.
"""

import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from falguna.hq_web import TTTHQHandler


class _LiveCommercialHQServerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "f@test.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "F Test"], check=True)
        (self.repo / "README.md").write_text("seed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "seed"], check=True, capture_output=True)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TTTHQHandler)
        self.server.app_root = self.repo
        self.server.falguna_url = "http://127.0.0.1:1"
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._wait_ready()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _wait_ready(self):
        for _ in range(100):
            try:
                status, _ = self._get("/api/config")
                if status == 200:
                    return
            except Exception:
                pass
            time.sleep(0.05)
        self.fail("TTT HQ server did not become ready")

    def _get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _approved_service(self):
        status, body = self._post("/api/cs/services", {
            "service_key": "http-e2e-website", "category": "web_dev", "title": "Business Website V1",
            "customer_description": "A synthetic HTTP-layer test service.", "pricing_model": "fixed", "delivery_mode": "remote",
        })
        self.assertEqual(status, 201)
        service_id = body["service_id"]
        status, _ = self._post(f"/api/cs/services/{service_id}/approval", {"actor": "Aryan", "approval_status": "APPROVED"})
        self.assertEqual(status, 200)
        return service_id


class CommercialFoundationHTTPTests(_LiveCommercialHQServerCase):
    """Section 18 (tests): 'direct API authorization bypass' -- these
    prove the human-approval gate before a binding commitment, and the
    refund/financial-integrity guarantees, are enforced by the HTTP
    handler's own call into the store layer, not merely by a UI that
    disables a button."""

    def test_service_catalogue_visible_only_after_approval(self):
        status, body = self._get("/api/cs/services?active_only=true")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])
        self._approved_service()
        status, body = self._get("/api/cs/services?active_only=true")
        self.assertEqual(len(body["items"]), 1)

    def test_convert_to_project_over_http_is_rejected_without_human_approval(self):
        status, body = self._post("/api/cs/intakes", {"customer_name": "Http Co", "requested_outcome": "A new site"})
        self.assertEqual(status, 201)
        intake_id = body["intake_id"]
        self._post(f"/api/cs/intakes/{intake_id}/qualify", {"actor": "Aryan", "qualification_status": "QUALIFIED"})
        # No /approve call made -- the API itself must refuse to convert.
        status, body = self._post(f"/api/cs/intakes/{intake_id}/convert-to-project", {"actor": "Aryan", "delivery_route": "CUSTOM_BUILD"})
        self.assertEqual(status, 400)
        self.assertIn("human-approved", body["error"])

    def test_full_journey_over_http_service_to_project_to_invoice_to_refund(self):
        service_id = self._approved_service()
        status, body = self._post("/api/cs/intakes", {
            "customer_name": "Http E2E Customer", "requested_outcome": "A booking website",
            "service_id": service_id, "budget_amount": 900.0, "budget_currency": "USD",
        })
        self.assertEqual(status, 201)
        intake_id = body["intake_id"]
        self._post(f"/api/cs/intakes/{intake_id}/qualify", {"actor": "Aryan", "qualification_status": "QUALIFIED"})
        status, body = self._post(f"/api/cs/intakes/{intake_id}/approve", {"actor": "Aryan"})
        self.assertEqual(status, 200)
        self.assertEqual(body["human_approved"], 1)
        status, body = self._post(f"/api/cs/intakes/{intake_id}/convert-to-project", {"actor": "Aryan", "delivery_route": "CUSTOM_BUILD"})
        self.assertEqual(status, 201)
        project_id = body["project_id"]

        status, project = self._get(f"/api/cs/projects/{project_id}")
        self.assertEqual(status, 200)
        self.assertEqual(project["status"], "SCOPED")

        status, body = self._post(f"/api/cs/projects/{project_id}/transition", {"actor": "Aryan", "to_status": "IN_DELIVERY"})
        self.assertEqual(status, 200)
        status, body = self._post(f"/api/cs/projects/{project_id}/costs", {"actor": "Aryan", "cost_category": "MODEL_API", "amount": 2.0})
        self.assertEqual(status, 201)

        # Invoicing and payment reuse the existing Billing API (/api/rh/...)
        # rather than a parallel cs/* endpoint.
        status, body = self._post(f"/api/rh/clients/{project['client_id']}/invoices", {
            "actor": "Aryan", "amount": 900.0, "opportunity_id": project["opportunity_id"],
        })
        self.assertEqual(status, 201)
        invoice_id = body["invoice_id"]
        self._post(f"/api/rh/invoices/{invoice_id}/ready", {"actor": "Aryan"})
        self._post(f"/api/rh/invoices/{invoice_id}/sent", {"actor": "Aryan"})
        status, body = self._post(f"/api/rh/invoices/{invoice_id}/payment", {"actor": "Aryan", "amount": 900.0, "evidence": {"ref": "http-e2e-payment"}})
        self.assertEqual(status, 201)

        status, econ = self._get(f"/api/cs/projects/{project_id}/economics")
        self.assertEqual(status, 200)
        self.assertEqual(econ["costs"]["MODEL_API"], 2.0)
        self.assertIsNone(econ["costs"]["HUMAN_EXPERT"])
        self.assertEqual(econ["collected_total"], 900.0)
        self.assertEqual(econ["net_collected"], 900.0)

        # Synthetic dispute + partial refund over HTTP, proving the
        # refund never touches billing's own amount_received ledger
        # even when driven purely through the HTTP API.
        status, body = self._post("/api/cs/disputes", {
            "invoice_id": invoice_id, "actor": "Aryan", "reason": "minor scope gap",
            "amount_disputed": 100.0, "evidence": {"ticket": "HTTP-1"},
        })
        self.assertEqual(status, 201)
        dispute_id = body["dispute_id"]
        status, body = self._post(f"/api/cs/disputes/{dispute_id}/resolve", {
            "actor": "Aryan", "resolution": "Partial refund approved over HTTP.",
            "refund_amount": 100.0, "evidence": {"approved_by": "Aryan"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "PARTIAL_REFUND_APPROVED")

        status, invoice_after = self._get("/api/rh/invoices")
        self.assertEqual(status, 200)
        stored_invoice = next(i for i in invoice_after["items"] if i["id"] == invoice_id)
        self.assertEqual(stored_invoice["amount_received"], 900.0)  # billing's real-cash ledger untouched by the refund

        status, econ_after = self._get(f"/api/cs/projects/{project_id}/economics")
        self.assertEqual(econ_after["collected_total"], 900.0)
        self.assertEqual(econ_after["refunds_total"], 100.0)
        self.assertEqual(econ_after["net_collected"], 800.0)

    def test_dispute_refund_over_http_cannot_exceed_amount_received(self):
        service_id = self._approved_service()
        status, body = self._post("/api/cs/intakes", {"customer_name": "Http Overcharge Co", "requested_outcome": "A site", "service_id": service_id})
        intake_id = body["intake_id"]
        self._post(f"/api/cs/intakes/{intake_id}/qualify", {"actor": "Aryan", "qualification_status": "QUALIFIED"})
        self._post(f"/api/cs/intakes/{intake_id}/approve", {"actor": "Aryan"})
        status, body = self._post(f"/api/cs/intakes/{intake_id}/convert-to-project", {"actor": "Aryan", "delivery_route": "CUSTOM_BUILD"})
        project_id = body["project_id"]
        status, project = self._get(f"/api/cs/projects/{project_id}")
        status, body = self._post(f"/api/rh/clients/{project['client_id']}/invoices", {"actor": "Aryan", "amount": 500.0, "opportunity_id": project["opportunity_id"]})
        invoice_id = body["invoice_id"]
        self._post(f"/api/rh/invoices/{invoice_id}/ready", {"actor": "Aryan"})
        self._post(f"/api/rh/invoices/{invoice_id}/sent", {"actor": "Aryan"})
        self._post(f"/api/rh/invoices/{invoice_id}/payment", {"actor": "Aryan", "amount": 200.0, "evidence": {"ref": "partial-only"}})
        status, body = self._post("/api/cs/disputes", {
            "invoice_id": invoice_id, "actor": "Aryan", "reason": "overcharge", "amount_disputed": 400.0, "evidence": {"ticket": "HTTP-2"},
        })
        dispute_id = body["dispute_id"]
        status, body = self._post(f"/api/cs/disputes/{dispute_id}/resolve", {
            "actor": "Aryan", "resolution": "trying to refund more than was ever collected",
            "refund_amount": 400.0, "evidence": {"x": 1},
        })
        self.assertEqual(status, 400)
        self.assertIn("exceeds", body["error"])


class InternationalServicesHTTPTests(_LiveCommercialHQServerCase):
    """Phase 5 Continuation, Section 4: proves the international-profile
    fields and the propose/approve regional-pricing gate are reachable and
    enforced at the HTTP layer, not only inside ServiceCatalogStore
    directly."""

    def test_international_profile_set_over_http(self):
        service_id = self._approved_service()
        status, body = self._post(f"/api/cs/services/{service_id}/international-profile", {
            "actor": "Aryan", "risk_level": "LOW", "baseline_complexity": "SIMPLE",
            "standard_delivery_days": 7, "supported_languages": ["en"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["risk_level"], "LOW")
        self.assertEqual(body["baseline_complexity"], "SIMPLE")
        self.assertEqual(body["standard_delivery_days"], 7)

    def test_international_profile_rejects_invalid_risk_level_over_http(self):
        service_id = self._approved_service()
        status, body = self._post(f"/api/cs/services/{service_id}/international-profile", {
            "actor": "Aryan", "risk_level": "CATASTROPHIC",
        })
        self.assertEqual(status, 400)
        self.assertIn("risk_level", body["error"])

    def test_regional_pricing_propose_then_approve_over_http(self):
        service_id = self._approved_service()
        status, body = self._post(f"/api/cs/services/{service_id}/regional-pricing/propose", {
            "actor": "Falguna", "region": "EU", "currency": "EUR", "price_min": 800, "price_max": 1200,
        })
        self.assertEqual(status, 200)
        status, service = self._get(f"/api/cs/services/{service_id}")
        self.assertEqual(status, 200)
        entries = json.loads(service["regional_pricing_json"])
        self.assertEqual(entries[0]["status"], "PROPOSED")

        status, body = self._post(f"/api/cs/services/{service_id}/regional-pricing/approve", {
            "actor": "Aryan", "region": "EU",
        })
        self.assertEqual(status, 200)
        entries = json.loads(body["regional_pricing_json"])
        approved = [e for e in entries if e["status"] == "APPROVED"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["approved_by"], "Aryan")

    def test_regional_pricing_approve_without_proposal_rejected_over_http(self):
        service_id = self._approved_service()
        status, body = self._post(f"/api/cs/services/{service_id}/regional-pricing/approve", {
            "actor": "Aryan", "region": "EU",
        })
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
