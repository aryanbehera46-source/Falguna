#!/usr/bin/env python3
"""Seed the public website's content tables with real, truthful content
only. No fabricated clients, testimonials, jobs, or blog posts.

Idempotent (ServiceStore/CaseStudyStore/ProductStore upsert by slug) --
safe to re-run. Careers and Insights are deliberately left empty here:
per instruction, we do not publish placeholder vacancies or fabricated
posts. Add real ones later via JobStore.create() / PostStore.create_draft()
+ .publish().

Provenance note (why these specific proof-of-work entries and why none
are labeled as a named external client): the only documented source for
these project names in this repository is
docs/website/../../TTT_SPRINT_V1_M2_SERVICE_CATALOG_INTAKE.md-style
catalog material already in this project's history, which lists them as
TTT's own proof-of-work without a verified, authorized external client
name attached to any of them. Per this sprint's explicit instruction
("Royal Table must be labeled appropriately as our own
demonstration/project unless genuine client authorization exists"), and
absent that authorization for any of the five, ALL are labeled as
Twenty Two Technologies' own projects -- not invented client engagements.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from falguna.runtime import open_control_plane
from falguna.site_content import ServiceStore, CaseStudyStore, ProductStore

ROOT = Path(__file__).resolve().parent.parent


def main():
    control, store = open_control_plane(ROOT)
    try:
        services = ServiceStore(store)
        case_studies = CaseStudyStore(store)
        products = ProductStore(store)
        seed_case_studies(case_studies)
        seed_services(services)
        seed_products(products)
        print("Site content seeded.")
    finally:
        store.close()


def seed_case_studies(cs: CaseStudyStore):
    cs.upsert(dict(
        slug="royal-table", title="Royal Table -- restaurant reservation platform",
        client_label="Twenty Two Technologies project", is_own_project=True,
        summary="A restaurant reservation site used to prove out our delivery process end to end, including a real production security update and a reliability fix.",
        problem="A restaurant reservation flow needed dependency security patching (nodemailer) and a more reliable failure state on the booking form -- a blocking browser alert on submission failure, with no path to retry cleanly.",
        approach="Isolated worktree build against the live branch, an inline non-blocking error message replacing the alert, verified with an automated check plus an independent second review before being queued for merge.",
        outcome="A verified, independently reviewed fix, queued for approval rather than merged on trust -- demonstrating our isolated-build-and-review delivery process on a real codebase.",
        stack=["Node.js", "HTML/CSS/JS", "nodemailer"], division_slugs=["websites-and-web-applications", "cloud-integrations-and-devops"],
        sort_order=1,
    ))
    cs.upsert(dict(
        slug="serviceflow", title="ServiceFlow -- booking and CRM platform",
        client_label="Twenty Two Technologies project", is_own_project=True,
        summary="A full booking and CRM platform: availability management with double-booking protection, billing, and staff dashboards.",
        problem="Service businesses need a single system for scheduling, avoiding double-bookings, billing, and giving staff a working dashboard -- most off-the-shelf tools force a compromise on one of these.",
        approach="Built as a full-stack application with a dedicated availability/conflict-checking engine, integrated billing, and role-based staff views.",
        outcome="A working reference platform we point to for booking/CRM and legacy-modernization engagements of comparable architectural scope.",
        stack=["React", "Node.js", "SQL"], division_slugs=["custom-software-engineering", "saas-and-enterprise-systems"],
        sort_order=2,
    ))
    cs.upsert(dict(
        slug="nivara-commerce", title="Nivara Commerce -- D2C storefront & order management",
        client_label="Twenty Two Technologies project", is_own_project=True,
        summary="A direct-to-consumer storefront and order-management platform: checkout, inventory, fulfillment, and sales analytics.",
        problem="D2C brands need checkout, inventory, and fulfillment to work together reliably, with real analytics on what's actually selling.",
        approach="Built the storefront, checkout, and inventory/fulfillment pipeline as one connected system rather than bolted-together plugins.",
        outcome="A working commerce reference we point to for Shopify/custom e-commerce engagements.",
        stack=["Node.js", "GraphQL", "React"], division_slugs=["websites-and-web-applications"],
        sort_order=3,
    ))
    cs.upsert(dict(
        slug="briefpilot-ai", title="BriefPilot AI -- structured brief intake",
        client_label="Twenty Two Technologies project", is_own_project=True,
        summary="Turns unstructured briefs and notes into structured, reviewable workflows using LLM-backed extraction.",
        problem="Teams collect requirements as free-form notes and briefs that are slow to turn into actionable, reviewable structure.",
        approach="LLM-backed structured extraction plus a review/approval workflow, scoped around one workflow at a time rather than a generic do-everything tool.",
        outcome="A working reference for AI-assisted workflow tooling engagements.",
        stack=["Python", "LLM APIs"], division_slugs=["ai-systems-and-agents"],
        sort_order=4,
    ))
    cs.upsert(dict(
        slug="falguna", title="Falguna -- our own AI workforce platform",
        client_label="Twenty Two Technologies product", is_own_project=True,
        summary="The AI workforce platform we use to run our own sales research, proposal drafting, and engineering QA -- built and operated in-house, the same way we'd build it for a client.",
        problem="Running a growing services business by hand -- sourcing opportunities, drafting proposals, tracking delivery -- doesn't scale without real tooling.",
        approach="A multi-role orchestrator (sales research, proposal drafting, engineering, QA, chief of staff) with persistent state, an audit trail, and isolated, independently reviewed engineering runs.",
        outcome="In active use inside Twenty Two Technologies today for our own pipeline -- described further on the Products page.",
        stack=["Python", "SQLite", "LLM APIs"], division_slugs=["ai-systems-and-agents"],
        sort_order=5,
    ))


_STANDARD_PROCESS = [
    {"step": "Scope exchange", "detail": "Must-haves vs. nice-to-haves, deadline, existing assets, decision process."},
    {"step": "Proposal", "detail": "Named proof of work, phased milestones, explicit assumptions and risks."},
    {"step": "First milestone", "detail": "A small, fixed-scope slice delivered before any larger commitment."},
    {"step": "Delivery & QA", "detail": "Isolated build, an agreed verification method, independent review before handover."},
]


def seed_services(svc: ServiceStore):
    def add(slug, division, tagline, summary, deliverables, status="current", proof=None, sort=0):
        svc.upsert(dict(slug=slug, division=division, tagline=tagline, summary=summary,
                          deliverables=deliverables, process=_STANDARD_PROCESS,
                          proof_slugs=proof or [], status=status, sort_order=sort))

    add("custom-software-engineering", "Custom Software Engineering",
        "Full-stack builds on React/Node or Django/Laravel, with real data, payments, and admin tooling.",
        "End-to-end application builds: front end, back end, database, payments, and the admin dashboard that runs them.",
        ["Full-stack architecture and build", "Database design", "Payments integration", "Admin/staff dashboards", "Deployment"],
        proof=["serviceflow", "nivara-commerce"], sort=1)

    add("websites-and-web-applications", "Websites & Web Applications",
        "Marketing sites, booking flows, and web applications built and hardened for production.",
        "From a single marketing site to a full booking/reservation web application, including post-launch hardening.",
        ["Responsive site/application build", "Booking or reservation flows", "Performance & security hardening", "Post-launch support"],
        proof=["royal-table", "nivara-commerce"], sort=2)

    add("mobile-applications", "Mobile Applications",
        "Mobile builds delivered by the same engineering team behind our web platforms.",
        "We extend our full-stack engineering practice to React Native / native mobile builds. We don't yet have a published mobile case study -- ask us for our current mobile team capacity.",
        ["Cross-platform (React Native) builds", "API integration with an existing backend", "App store submission support"],
        status="in_development", sort=3)

    add("saas-and-enterprise-systems", "SaaS & Enterprise Systems",
        "Multi-tenant SaaS and internal enterprise systems, built for real operational use.",
        "Systems built for ongoing, multi-user operation -- not a one-off script or dashboard.",
        ["Multi-tenant architecture", "Role-based access", "Billing & subscriptions", "Internal enterprise tooling"],
        proof=["serviceflow"], sort=4)

    add("ai-systems-and-agents", "AI Systems, Agents & Business Automation",
        "LLM-backed workflow tooling and agentic automation, scoped around one real workflow at a time.",
        "Structured extraction, review/approval workflows, and multi-step agent orchestration -- including the platform we run our own business on.",
        ["LLM-backed structured extraction", "Agentic workflow automation", "Human-in-the-loop approval flows"],
        proof=["briefpilot-ai", "falguna"], sort=5)

    add("ui-ux-and-product-design", "UI/UX & Digital Product Design",
        "Interface and product design delivered as part of every build we ship.",
        "Design work is embedded in our engineering engagements today; we don't yet offer a design-only standalone track with its own case study.",
        ["Interface design as part of a build", "Design systems for a product", "Usability review"], sort=6)

    add("brand-identity-and-digital-experience", "Brand Identity & Digital Experience",
        "Brand and digital-experience work, in development as a dedicated offering.",
        "We don't yet have a published brand-identity case study; this is an active area of investment, not a proven track record yet.",
        ["Visual identity", "Digital experience design"], status="in_development", sort=7)

    add("cloud-integrations-and-devops", "Cloud, Integrations & DevOps",
        "Deployment, integrations, and operational reliability for what we build.",
        "Every build we ship includes deployment and integration work; this division covers that as a standalone engagement too.",
        ["Cloud deployment", "Third-party integrations", "CI/operational reliability"],
        proof=["royal-table"], sort=8)

    add("cybersecurity-engineering", "Cybersecurity-Related Engineering",
        "Secure coding and dependency hygiene practiced across our builds -- not yet a dedicated security practice.",
        "We apply secure coding and dependency-patching practice within our engineering work (for example, a real nodemailer dependency security update on Royal Table). We do not yet offer a standalone, fully staffed security practice.",
        ["Secure coding practices", "Dependency security patching"], status="in_development", sort=9)

    add("digital-marketing", "Digital Marketing",
        "SEO, content, and conversion fundamentals -- applied first to our own site, offered as a standalone division.",
        "SEO, paid media, social, content, analytics, and conversion optimization. This division is in development as a dedicated client offering; this website itself was built to our own SEO/performance standard as the first demonstration of it.",
        ["Technical SEO", "Content strategy", "Analytics & conversion instrumentation", "Paid media (via qualified partner)"],
        sort=10)

    add("trading-and-financial-technology", "Trading & Financial Technology",
        "Technology, analytics, and operating systems for trading and financial businesses.",
        "Data platforms, workflow systems, dashboards, and digital infrastructure for modern financial operations.",
        ["Financial data platforms", "Trading workflow systems", "Analytics and reporting", "Operations automation"], sort=12)

    add("media-and-entertainment", "Media & Entertainment",
        "Digital platforms and audience experiences for media businesses.",
        "Content platforms, audience products, commerce, and workflow systems for media and entertainment brands.",
        ["Content platforms", "Audience experiences", "Commerce and subscriptions", "Workflow automation"], sort=13)

    add("banking-and-financial-services", "Banking & Financial Services",
        "Secure digital experiences and operational platforms for financial services.",
        "Customer journeys, internal platforms, data experiences, and integrations for banking and financial services.",
        ["Customer portals", "Internal operations platforms", "Data and reporting", "Systems integration"], sort=14)

    add("business-and-strategy-consulting", "Business & Strategy Consulting",
        "Digital strategy and technology direction for companies ready to move.",
        "Practical technology roadmaps, product direction, and operating-model support connected to execution.",
        ["Technology roadmaps", "Product strategy", "Operating-model design", "Transformation planning"], sort=15)

    add("maintenance-support-and-consulting", "Maintenance, Support & Technical Consulting",
        "Ongoing support and technical consulting for what we (or you) have already shipped.",
        "Bug fixes, small feature additions, monitoring, and on-call support for a live platform.",
        ["Bug fixes & small features", "Monitoring", "On-call support", "Technical consulting"],
        proof=["royal-table"], sort=11)


def seed_products(products: ProductStore):
    products.upsert(dict(
        slug="falguna", name="Falguna", tagline="Our in-house AI workforce platform.",
        summary="Falguna runs sales research, proposal drafting, engineering execution, and QA for Twenty Two Technologies' own pipeline -- built and operated in-house, in active daily use.",
        status="in_development", is_internal=True, sort_order=1,
    ))
    products.upsert(dict(
        slug="ttt-hq", name="TTT HQ", tagline="Our private operating system.",
        summary="The internal system we use to run Twenty Two Technologies day to day -- pipeline, delivery, and company operations in one place. Not a public product.",
        status="in_development", is_internal=True, sort_order=2,
    ))
    products.upsert(dict(
        slug="ttt-mail", name="TTT Mail", tagline="An email product, in early research.",
        summary="An early-stage idea on our roadmap. Nothing is built yet -- listed here for transparency about where we're headed, not as an available product.",
        status="research", is_internal=False, sort_order=3,
    ))


if __name__ == "__main__":
    main()
