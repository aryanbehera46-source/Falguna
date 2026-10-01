"""Phase 6 final closure: the mandated 10-item multi-connection race matrix.

Every test below opens one SEPARATE StateStore (its own sqlite3.connect())
per worker thread, and synchronizes workers with a threading.Barrier so the
racing calls genuinely overlap in wall-clock time -- never a sequential call
labeled as concurrency. This mirrors the exact pattern already established
in tests/test_phase6_concurrency.py.

Matrix items covered (by test class / method):
 1. refund vs refund                        -> RefundRaceTests.test_two_different_refunds_cannot_jointly_overrefund
 2. refund vs settlement                    -> RefundVsSettlementTests.test_refund_and_settlement_do_not_corrupt_each_other
 3. settlement vs settlement                -> SettlementRaceTests.test_two_settlement_reconciliations_same_ref_only_one_matches
 4. commission release vs commission release-> CommissionReleaseRaceTests.test_two_mark_releasable_calls_produce_one_payable_event
 5. commission release vs clawback          -> CommissionReleaseRaceTests.test_release_vs_concurrent_clawback_never_overpays
 6. approval vs rejection                   -> ApprovalRaceTests.test_approve_vs_reject_only_one_decision_lands
 7. beneficiary change during approval      -> ApprovalRaceTests.test_amend_during_approval_invalidates_old_verification
 8. payable release duplication             -> PayableRaceTests.test_two_mark_execution_ready_calls_produce_one_transition
 9. recurring retry vs delayed original     -> SubscriptionRaceTests.test_concurrent_autopay_attempts_never_collide_on_attempt_number
10. reconciliation vs refund                -> ReconciliationVsRefundTests.test_reconciliation_and_refund_do_not_corrupt_each_other
"""

import tempfile
import threading
import unittest
from pathlib import Path

from falguna.audit import AuditLog
from falguna.phase6_commercial import (AccessContext, ApprovalWorkflow, CommercialIdentityStore,
                                       CommercialOperations, CommercialSecurityError, PaymentOrchestrator)
from falguna.phase6_financial_flows import (CommissionReleaseService, PayableService, RefundService,
                                            SubscriptionService)
from falguna.store import StateStore, utcnow


class RaceMatrixCase(unittest.TestCase):
    """Shared fixture: one organization, one maker/verifier/owner identity
    set, one client/opportunity/invoice, one partner/referral/commission --
    the same shape FinancialFlowsCase uses, factored out here so every race
    test builds the exact same known-good starting state before racing
    genuinely separate connections against it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state.db"
        self.audit_path = self.root / "audit.jsonl"
        store = StateStore(self.db_path)
        store.migrate()
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        self.maker_id = identities.create("ttt", "STAFF", "maker", "Maker", "FINANCE_OPERATOR", "test")
        self.verifier_id = identities.create("ttt", "STAFF", "verifier", "Verifier", "FINANCE_OPERATOR", "test")
        self.owner_id = identities.create("ttt", "STAFF", "aryan", "Aryan", "OWNER", "test")
        self.maker = AccessContext(self.maker_id, "ttt")
        self.verifier = AccessContext(self.verifier_id, "ttt")
        self.owner = AccessContext(self.owner_id, "ttt")
        now = utcnow()
        self.client_id = store.create("clients", {"name": "Synthetic", "primary_contact": None, "contact_channel": None,
            "status": "ACTIVE", "total_won_value": 1000, "created_at": now, "updated_at": now})
        store.create("comm_organizations", {"name": "Synthetic Org", "domain": "synthetic.invalid",
            "linked_client_id": self.client_id, "notes": None, "created_at": now, "updated_at": now}, record_id="ttt")
        self.opp_id = store.create("rh_opportunities", {"source": "partner_referral", "source_url": None,
            "client_name": "Synthetic", "title": "Synthetic", "description": None, "budget_rate": None,
            "required_skills": None, "deadline": None, "contract_type": None, "location_timezone": None,
            "urgency": None, "stage": "Won", "final_price": 1000, "lost_reason": None, "created_at": now, "updated_at": now})
        self.invoice_id = store.create("rh_invoices", {"client_id": self.client_id, "opportunity_id": self.opp_id,
            "active_job_id": None, "amount": 1000, "currency": "INR", "milestone": None, "due_date": None,
            "amount_received": 1000, "status": "PAID", "evidence_json": "[]", "created_at": now, "updated_at": now})
        self.partner_id = store.create("pm_partners", {"full_name": "Partner", "organization_name": None,
            "email": "p@invalid.test", "phone": None, "region": None, "country": None, "service_categories_json": "[]",
            "assigned_manager": None, "verification_status": "VERIFIED", "agreement_accepted": 1,
            "agreement_accepted_at": now, "agreement_reference": None, "status": "APPROVED", "approved_at": now,
            "approved_by": "test", "suspended_at": None, "suspended_by": None, "suspension_reason": None,
            "terminated_at": None, "terminated_by": None, "termination_reason": None,
            "created_at": now, "updated_at": now})
        self.referral_id = store.create("pm_referrals", {"partner_id": self.partner_id, "prospect_name": "Lead",
            "organization_name": "Synthetic", "contact_email": None, "contact_phone": None, "region": None,
            "requested_service": "Build", "estimated_value": 1000, "referral_source": "partner", "notes": None,
            "normalized_email": None, "normalized_phone": None, "normalized_domain": None, "normalized_org": "synthetic",
            "attribution_status": "ATTRIBUTED", "attribution_start": now, "attribution_expiry": None,
            "opportunity_id": self.opp_id, "duplicate_flag": 0, "created_at": now, "updated_at": now})
        self.commission_id = store.create("pm_commissions", {"referral_id": self.referral_id, "partner_id": self.partner_id,
            "opportunity_id": self.opp_id, "invoice_id": self.invoice_id, "rate": 0.1, "status": "ELIGIBLE",
            "eligible_amount": 100, "refunded_amount": 0, "hold_reason": None, "created_at": now, "updated_at": now})
        self.payment_id = store.create("p6_payment_intents", {"organization_id": "ttt", "customer_ref": "customer",
            "invoice_id": self.invoice_id, "kind": "ONE_TIME", "amount": 1000, "currency": "INR", "status": "SETTLED",
            "capture_mode": "MANUAL", "allowed_methods_json": "[]", "provider": "SANDBOX_ADAPTER",
            "provider_session_ref": None, "provider_transaction_ref": "txn", "payment_method_token_ref": None,
            "mandate_token_ref": None, "milestone_ref": None, "legal_owner_name": "TTT", "beneficiary_ref": "official",
            "idempotency_key": "payment", "metadata_json": "{}", "actor": self.maker_id,
            "created_at": now, "updated_at": now})
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _worker(self, function, barrier, results, errors):
        """Each worker opens its OWN StateStore -> its own sqlite3.connect()
        -> a genuinely separate database connection, not a shared one
        passed across threads. The barrier blocks every worker until all N
        have arrived, so the racing calls start as close to simultaneously
        as Python threading allows, instead of running one after another."""
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        try:
            barrier.wait(timeout=10)
            results.append(function(store, audit))
        except Exception as exc:
            errors.append(exc)
        finally:
            store.close()

    def run_concurrent(self, functions):
        barrier = threading.Barrier(len(functions))
        results = []
        errors = []
        threads = [threading.Thread(target=self._worker, args=(fn, barrier, results, errors)) for fn in functions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        return results, errors

    def approve(self, store, audit, approval_id):
        identities = CommercialIdentityStore(store, audit)
        approvals = ApprovalWorkflow(store, audit, identities)
        approvals.verify(self.verifier, approval_id)
        return approvals.approve_by_aryan(self.owner, approval_id)


class RefundRaceTests(RaceMatrixCase):
    """Item 1: refund vs refund -- two DIFFERENT, independently approved
    refund requests against the same settled payment, confirmed from
    separate connections at the same instant. request()'s own eligibility
    check only looks at refunds already CONFIRMED at request time, so both
    refund requests (300 + 800 = 1100 > the 1000 settled amount) can be
    legitimately requested and approved before either confirms. The real
    race is at confirm_sandbox() time: it must be impossible for both to
    land and jointly exceed the settled amount."""

    def setUp(self):
        super().setUp()
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        service = RefundService(store, audit, identities)
        self.refund_a = service.request(self.maker, self.payment_id, 300, "INR", "race-a", "refund-race-a")
        self.refund_b = service.request(self.maker, self.payment_id, 800, "INR", "race-b", "refund-race-b")
        self.approve(store, audit, self.refund_a["approval_request_id"])
        self.approve(store, audit, self.refund_b["approval_request_id"])
        store.close()

    def test_two_different_refunds_cannot_jointly_overrefund(self):
        def confirm_a(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return RefundService(store, audit, identities).confirm_sandbox(
                self.maker, self.refund_a["id"], "provider-race-a", {"verified": True})

        def confirm_b(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return RefundService(store, audit, identities).confirm_sandbox(
                self.maker, self.refund_b["id"], "provider-race-b", {"verified": True})

        results, errors = self.run_concurrent([confirm_a, confirm_b])
        # Exactly one of the two must win; the other must fail closed with
        # the combined-total guard, never silently truncate or allow both.
        self.assertEqual(len(results), 1, f"expected exactly one confirmation to succeed, got {results!r} errors={errors!r}")
        self.assertEqual(len(errors), 1)
        self.assertIn("exceed the settled payment amount", str(errors[0]))
        check = StateStore(self.db_path)
        confirmed = check.list("p6_refunds", "payment_intent_id=? AND status='CONFIRMED'", (self.payment_id,))
        total = sum(float(r["amount"]) for r in confirmed)
        check.close()
        self.assertLessEqual(total, 1000.0 + 1e-9)
        self.assertEqual(len(confirmed), 1)


class SettlementRaceTests(RaceMatrixCase):
    """Item 3: settlement vs settlement -- two genuine double-submissions of
    the SAME underlying settlement (same settlement_ref) under DIFFERENT
    idempotency_keys, racing from separate connections. Without the
    BEGIN IMMEDIATE fix, both could pass the duplicate-settlement_ref check
    before either committed, producing two MATCHED reconciliations and two
    receipts for one real settlement."""

    def setUp(self):
        super().setUp()
        # reconcile_settlement() -> billing.record_payment() requires the
        # invoice to NOT already be PAID (the shared fixture's invoice is
        # pre-settled for the commission-release tests' benefit). Settlement
        # reconciliation tests need a not-yet-collected invoice instead.
        store = StateStore(self.db_path)
        store.update("rh_invoices", self.invoice_id, status="SENT", amount_received=0)
        store.close()

    def test_two_settlement_reconciliations_same_ref_only_one_matches(self):
        def recon_a(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return CommercialOperations(store, audit, identities).reconcile_settlement(
                self.maker, self.payment_id, "settlement-race-ref", 1000, "INR", {"verified": True}, "recon-race-a")

        def recon_b(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return CommercialOperations(store, audit, identities).reconcile_settlement(
                self.maker, self.payment_id, "settlement-race-ref", 1000, "INR", {"verified": True}, "recon-race-b")

        results, errors = self.run_concurrent([recon_a, recon_b])
        self.assertEqual(errors, [], f"reconcile_settlement must never raise on a race, got {errors!r}")
        self.assertEqual(len(results), 2)
        statuses = sorted(r["status"] for r in results)
        # Exactly one of the two distinct-idempotency-key submissions may
        # land MATCHED; the other must be caught as a duplicate settlement
        # and sent to review, never silently accepted a second time.
        self.assertEqual(statuses, ["MATCHED", "REVIEW_REQUIRED"])
        check = StateStore(self.db_path)
        matched = check.list("p6_reconciliations", "settlement_ref='settlement-race-ref' AND status='MATCHED'")
        receipts = check.list("p6_receipts", "payment_intent_id=?", (self.payment_id,))
        cash_events = check.list("p6_financial_events", "event_type='SETTLED_COLLECTION'")
        check.close()
        self.assertEqual(len(matched), 1)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(cash_events), 1)

    def test_identical_idempotency_key_replay_returns_same_row_not_a_crash(self):
        # (invoice reset to not-yet-PAID happens in setUp above)
        def recon(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return CommercialOperations(store, audit, identities).reconcile_settlement(
                self.maker, self.payment_id, "settlement-replay-ref", 1000, "INR", {"verified": True}, "recon-same-key")

        results, errors = self.run_concurrent([recon, recon])
        self.assertEqual(errors, [], f"a same-idempotency-key replay race must never raise, got {errors!r}")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], results[1]["id"])
        check = StateStore(self.db_path)
        self.assertEqual(len(check.list("p6_reconciliations", "idempotency_key='recon-same-key'")), 1)
        check.close()


class RefundVsSettlementTests(RaceMatrixCase):
    """Item 2: refund vs settlement -- a refund confirmation and an
    (independent, differently-keyed) settlement reconciliation racing
    against the same payment at the same instant. They touch disjoint
    idempotency keys and disjoint finance-event buckets (CASH DEBIT vs CASH
    CREDIT), so the invariant under test is that neither corrupts the
    other's bookkeeping: both must be individually recorded, exactly once
    each, with no lost update and no crash."""

    def setUp(self):
        super().setUp()
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        # Settlement reconciliation here needs a not-yet-collected invoice
        # (see SettlementRaceTests.setUp for why); the refund still targets
        # the already-SETTLED payment, which is independent of invoice
        # status.
        store.update("rh_invoices", self.invoice_id, status="SENT", amount_received=0)
        refund = RefundService(store, audit, identities).request(
            self.maker, self.payment_id, 100, "INR", "race", "refund-vs-settlement")
        self.approve(store, audit, refund["approval_request_id"])
        self.refund = refund
        store.close()

    def test_refund_and_settlement_do_not_corrupt_each_other(self):
        def confirm_refund(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return RefundService(store, audit, identities).confirm_sandbox(
                self.maker, self.refund["id"], "provider-rvs", {"verified": True})

        def reconcile(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return CommercialOperations(store, audit, identities).reconcile_settlement(
                self.maker, self.payment_id, "settlement-rvs", 1000, "INR", {"verified": True}, "recon-rvs")

        results, errors = self.run_concurrent([confirm_refund, reconcile])
        self.assertEqual(errors, [], f"independent refund/settlement operations must not collide, got {errors!r}")
        self.assertEqual(len(results), 2)
        check = StateStore(self.db_path)
        refund_confirmed = check.list("p6_refunds", "id=? AND status='CONFIRMED'", (self.refund["id"],))
        settlement_matched = check.list("p6_reconciliations", "idempotency_key='recon-rvs' AND status='MATCHED'")
        debit_events = check.list("p6_financial_events", "event_type='REFUND_CONFIRMED'")
        credit_events = check.list("p6_financial_events", "event_type='SETTLED_COLLECTION'")
        check.close()
        self.assertEqual(len(refund_confirmed), 1)
        self.assertEqual(len(settlement_matched), 1)
        self.assertEqual(len(debit_events), 1)
        self.assertEqual(len(credit_events), 1)


class ReconciliationVsRefundTests(RaceMatrixCase):
    """Item 10: reconciliation vs refund -- same pairing as item 2 but
    asserted from the reconciliation/ledger-consistency angle: after both
    race, cash_position()'s CASH bucket must reflect exactly one credit and
    one debit (no false MATCHED state, no duplicate economic mutation)."""

    def setUp(self):
        super().setUp()
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        store.update("rh_invoices", self.invoice_id, status="SENT", amount_received=0)
        refund = RefundService(store, audit, identities).request(
            self.maker, self.payment_id, 250, "INR", "race", "refund-vs-recon")
        self.approve(store, audit, refund["approval_request_id"])
        self.refund = refund
        store.close()

    def test_reconciliation_and_refund_do_not_corrupt_each_other(self):
        def confirm_refund(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return RefundService(store, audit, identities).confirm_sandbox(
                self.maker, self.refund["id"], "provider-rvr", {"verified": True})

        def reconcile(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return CommercialOperations(store, audit, identities).reconcile_settlement(
                self.maker, self.payment_id, "settlement-rvr", 1000, "INR", {"verified": True}, "recon-rvr")

        results, errors = self.run_concurrent([confirm_refund, reconcile])
        self.assertEqual(errors, [])
        from falguna.phase6_commercial import FinanceAccounts
        check = StateStore(self.db_path)
        identities = CommercialIdentityStore(check, AuditLog(self.audit_path))
        position = FinanceAccounts(check, AuditLog(self.audit_path), identities).cash_position(self.maker, "INR")
        check.close()
        # One CREDIT of 1000 (settlement) and one DEBIT of 250 (refund),
        # applied exactly once each -- never double-applied, never lost.
        self.assertEqual(position["cash_balance"], 750.0)


class CommissionReleaseRaceTests(RaceMatrixCase):
    """Items 4 and 5: commission release vs commission release, and
    commission release vs clawback."""

    def test_two_mark_releasable_calls_produce_one_payable_event(self):
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        service = CommissionReleaseService(store, audit, identities)
        release = service.prepare(self.maker, self.commission_id, "INR", 0, "release-race")
        self.approve(store, audit, release["approval_request_id"])
        store.close()

        def mark(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return CommissionReleaseService(store, audit, identities).mark_releasable(self.maker, release["id"])

        results, errors = self.run_concurrent([mark, mark])
        self.assertEqual(len(errors), 0, f"a second mark_releasable() call should observe RELEASABLE and no-op, not error: {errors!r}")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["status"] == "RELEASABLE" for r in results))
        check = StateStore(self.db_path)
        events = check.list("p6_financial_events", "event_type='COMMISSION_PAYABLE' AND source_id=?", (release["id"],))
        check.close()
        self.assertEqual(len(events), 1)

    def test_release_vs_concurrent_clawback_never_overpays(self):
        """A commission release is prepared (snapshotting refunded_amount=0)
        and approved. Concurrently with mark_releasable(), a refund against
        the underlying payment confirms and claws back part of the
        commission. mark_releasable() must detect that the clawback
        invalidated its prepare-time snapshot and fail closed rather than
        release a now-stale (too-high) commission_amount."""
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        release = CommissionReleaseService(store, audit, identities).prepare(
            self.maker, self.commission_id, "INR", 0, "release-vs-clawback")
        self.approve(store, audit, release["approval_request_id"])
        refund = RefundService(store, audit, identities).request(
            self.maker, self.payment_id, 500, "INR", "clawback-race", "refund-clawback-race")
        self.approve(store, audit, refund["approval_request_id"])
        store.close()

        def mark_releasable(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return CommissionReleaseService(store, audit, identities).mark_releasable(self.maker, release["id"])

        def confirm_refund(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return RefundService(store, audit, identities).confirm_sandbox(
                self.maker, refund["id"], "provider-clawback-race", {"verified": True})

        results, errors = self.run_concurrent([mark_releasable, confirm_refund])
        check = StateStore(self.db_path)
        release_row = check.get("p6_commission_releases", release["id"])
        payable_events = check.list("p6_financial_events", "event_type='COMMISSION_PAYABLE' AND source_id=?", (release["id"],))
        refund_row = check.get("p6_refunds", refund["id"])
        check.close()
        # Both orderings of this genuine race are individually legitimate
        # (SQLite's BEGIN IMMEDIATE serializes the two atomic blocks one way
        # or the other; which one wins is nondeterministic by design). What
        # must hold regardless of order: the refund itself always confirms
        # (its own correctness doesn't depend on the release), and the
        # commission-payable event is NEVER emitted more than once -- i.e.
        # mark_releasable() either (a) committed its RELEASABLE transition
        # before the clawback's BEGIN IMMEDIATE could run, validly reading
        # refunded_amount=0 at that serialization point and releasing
        # exactly once, or (b) its re-check inside the atomic block saw the
        # clawback that had already committed first and correctly failed
        # closed with no event at all. A genuine bug would show up here as
        # *two* payable events, or a RELEASABLE status with *zero* events,
        # or an unexpected exception type -- none of which this asserts are
        # acceptable.
        self.assertEqual(refund_row["status"], "CONFIRMED")
        self.assertLessEqual(len(payable_events), 1)
        if release_row["status"] == "RELEASABLE":
            self.assertEqual(len(payable_events), 1)
        else:
            self.assertEqual(len(payable_events), 0)
            # The clawback landing first can be caught by mark_releasable()
            # two different, equally valid ways depending on exactly how far
            # record_refund() got before the release's atomic block ran:
            # either the refund/clawback-amount mismatch check ("re-prepare
            # the release"), or the commission's status having already
            # flipped off ELIGIBLE (to HELD/REVERSED) by record_refund()
            # itself ("no longer eligible"). Both are correct fail-closed
            # outcomes for this race; only a different exception type, or no
            # exception at all, would indicate a real defect.
            self.assertTrue(any(isinstance(e, CommercialSecurityError) and
                                ("changed concurrently" in str(e) or "re-prepare" in str(e) or "no longer eligible" in str(e))
                                for e in errors),
                            f"expected a fail-closed CommercialSecurityError, got {errors!r}")


class ApprovalRaceTests(RaceMatrixCase):
    """Items 6 and 7: approval vs rejection, and beneficiary change during
    approval."""

    def test_approve_vs_reject_only_one_decision_lands(self):
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        service = PayableService(store, audit, identities)
        payable = service.create(self.maker, "VENDOR", "vendor:race", 300, "INR", {"expense": "race"}, "payable-approve-reject-race")
        approvals = ApprovalWorkflow(store, audit, identities)
        approvals.verify(self.verifier, payable["approval_request_id"])
        store.close()

        def do_approve(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return ApprovalWorkflow(store, audit, identities).approve_by_aryan(self.owner, payable["approval_request_id"])

        def do_reject(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return ApprovalWorkflow(store, audit, identities).reject(self.verifier, payable["approval_request_id"], "race rejection")

        results, errors = self.run_concurrent([do_approve, do_reject])
        self.assertEqual(len(results), 1, f"exactly one of approve/reject must win, got results={results!r} errors={errors!r}")
        self.assertEqual(len(errors), 1)
        self.assertIn("changed concurrently", str(errors[0]))
        check = StateStore(self.db_path)
        final = check.get("p6_approval_requests", payable["approval_request_id"])
        check.close()
        self.assertIn(final["status"], {"APPROVED_PENDING_EXECUTION", "REJECTED"})
        # Whichever won, there must be exactly one terminal decision -- not
        # both a rejection reason recorded AND an approved_pending_execution
        # final state.
        if final["status"] == "REJECTED":
            self.assertIsNotNone(final["rejection_reason"])
        else:
            self.assertIsNotNone(final["approved_at"])

    def test_amend_during_approval_invalidates_old_verification(self):
        """The maker amends the beneficiary (material change) at the exact
        moment Aryan tries to give final approval using the pre-amend
        verification. The old verification must never be able to approve
        the changed request -- approve_by_aryan's CAS checks
        verifier_identity_id, which amend() clears, so whichever commits
        second must lose."""
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        service = PayableService(store, audit, identities)
        payable = service.create(self.maker, "VENDOR", "vendor:original", 300, "INR", {"expense": "race"}, "payable-amend-race")
        approvals = ApprovalWorkflow(store, audit, identities)
        approvals.verify(self.verifier, payable["approval_request_id"])
        store.close()

        def do_approve(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return ApprovalWorkflow(store, audit, identities).approve_by_aryan(self.owner, payable["approval_request_id"])

        def do_amend(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return ApprovalWorkflow(store, audit, identities).amend(
                self.maker, payable["approval_request_id"], {"expense": "race", "changed": True},
                amount=300, currency="INR", beneficiary_ref="vendor:changed-beneficiary")

        results, errors = self.run_concurrent([do_approve, do_amend])
        check = StateStore(self.db_path)
        final = check.get("p6_approval_requests", payable["approval_request_id"])
        check.close()
        if final["status"] == "APPROVED_PENDING_EXECUTION":
            # Approval only legitimately won if it committed BEFORE the
            # amend -- in which case the beneficiary must still be the
            # original one, never the changed one slipping through under
            # a stale verification.
            self.assertEqual(final["beneficiary_ref"], "vendor:original")
        else:
            # The amend won: the request must be back in PENDING_
            # VERIFICATION with the new beneficiary, requiring fresh
            # independent verification before it can ever be approved
            # again -- the old verifier_identity_id/approval can never
            # reach forward and authorize this changed request.
            self.assertEqual(final["status"], "PENDING_VERIFICATION")
            self.assertEqual(final["beneficiary_ref"], "vendor:changed-beneficiary")
            self.assertIsNone(final["verifier_identity_id"])
            self.assertIn("changed concurrently", str(errors[0]))


class PayableRaceTests(RaceMatrixCase):
    """Item 8: payable release duplication."""

    def test_two_mark_execution_ready_calls_produce_one_transition(self):
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        service = PayableService(store, audit, identities)
        payable = service.create(self.maker, "VENDOR", "vendor:payable-race", 300, "INR", {"expense": "race"}, "payable-exec-race")
        self.approve(store, audit, payable["approval_request_id"])
        store.close()

        def mark(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return PayableService(store, audit, identities).mark_execution_ready(self.maker, payable["id"])

        results, errors = self.run_concurrent([mark, mark])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["status"] == "SANDBOX_EXECUTION_READY" for r in results))
        check = StateStore(self.db_path)
        final = check.get("p6_payables", payable["id"])
        check.close()
        self.assertEqual(final["status"], "SANDBOX_EXECUTION_READY")


class SubscriptionRaceTests(RaceMatrixCase):
    """Item 9: recurring retry vs delayed original event -- two concurrent
    create_autopay_attempt() calls for the same subscription+cycle, each
    with its own idempotency_key (modeling an automatic retry racing a
    delayed original provider-triggered attempt), must never collide on
    attempt_number."""

    def test_concurrent_autopay_attempts_never_collide_on_attempt_number(self):
        store = StateStore(self.db_path)
        audit = AuditLog(self.audit_path)
        identities = CommercialIdentityStore(store, audit)
        service = SubscriptionService(store, audit, identities)
        sub = service.create(self.maker, self.client_id, "Monthly", 100, "INR", "MONTHLY",
                             "2026-10-01", "token_ref", "mandate_ref", self.opp_id)
        service.create_due_invoice(self.maker, sub["id"], "2026-10-01")
        store.close()

        def attempt_a(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return SubscriptionService(store, audit, identities).create_autopay_attempt(
                self.maker, sub["id"], "autopay-retry-a")

        def attempt_b(store, audit):
            identities = CommercialIdentityStore(store, audit)
            return SubscriptionService(store, audit, identities).create_autopay_attempt(
                self.maker, sub["id"], "autopay-retry-b")

        results, errors = self.run_concurrent([attempt_a, attempt_b])
        self.assertEqual(errors, [], f"concurrent distinct autopay attempts must both succeed with distinct numbers, got {errors!r}")
        self.assertEqual(len(results), 2)
        numbers = sorted(r["attempt_number"] for r in results)
        self.assertEqual(numbers, [1, 2])
        check = StateStore(self.db_path)
        attempts = check.list("p6_subscription_attempts", "subscription_id=?", (sub["id"],))
        check.close()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sorted(a["attempt_number"] for a in attempts), [1, 2])


if __name__ == "__main__":
    unittest.main()
