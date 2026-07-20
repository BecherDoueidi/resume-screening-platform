"""Generate synthetic resume PDFs with deliberately planted demographic markers.

Skill quality is DECOUPLED from demographics: strong and weak candidates are
spread across names, genders, ages, locations, and school prestige — so after
anonymization, ranking should track skill only. The PLANTED_MARKERS list at the
bottom is what the verification step greps for in the anonymized payloads.

Usage:  python scripts/make_sample_resumes.py [output_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "data" / "resumes"

# (name, honorific, pronoun, city, university, grad_year, email_user, phone, quality_tier)
CANDIDATES = [
    # --- strong candidates (varied demographics) ---
    dict(
        name="Fatima Al-Zahrani",
        hon="Ms.",
        pron="she",
        city="Riyadh, Saudi Arabia",
        uni="King Saud University",
        year=2013,
        email="fatima.alzahrani",
        phone="+966 50 123 4567",
        tier="strong",
        summary="Backend engineer with 11 years building payment and ledger systems.",
        jobs=[
            (
                "Staff Backend Engineer, LedgerPay",
                "2019-present",
                "Designed the double-entry ledger service in Go handling 40M transactions/day with exactly-once semantics. "
                "Cut p99 settlement latency from 900ms to 120ms by redesigning the PostgreSQL partitioning scheme and adding "
                "idempotency keys across all payment APIs. Led incident response program; reduced SEV1s by 60% year over year.",
            ),
            (
                "Senior Engineer, TelcoBill",
                "2013-2019",
                "Built gRPC billing APIs in Python serving 8M subscribers. Introduced Kafka-based event pipeline replacing "
                "nightly batch jobs; revenue reconciliation errors dropped 95%. Mentored four mid-level engineers.",
            ),
        ],
        skills="Go, Python, PostgreSQL, Kafka, gRPC, Kubernetes, Prometheus, load testing (k6)",
    ),
    dict(
        name="Dmitri Volkov",
        hon="Mr.",
        pron="he",
        city="Novosibirsk, Russia",
        uni="Novosibirsk State Technical College",
        year=2012,
        email="d.volkov88",
        phone="+7 913 555 0142",
        tier="strong",
        summary="Distributed-systems engineer, 12 years, focused on correctness-critical services.",
        jobs=[
            (
                "Principal Engineer, FinCore",
                "2018-present",
                "Own the transaction-processing core (Python/PostgreSQL) clearing $2B/year. Designed retry and "
                "dead-letter architecture that survived a regional outage with zero lost payments. Wrote the team's "
                "load-testing harness; capacity planning now data-driven with 4x headroom verified quarterly.",
            ),
            (
                "Backend Engineer, ShopFast",
                "2012-2018",
                "Scaled the order service from 10 to 2,000 rps: query optimization, Redis caching with explicit "
                "invalidation, and MySQL schema redesign. On-call lead; wrote runbooks adopted company-wide.",
            ),
        ],
        skills="Python, PostgreSQL, MySQL, Redis, RabbitMQ, REST API design, Grafana, distributed tracing",
    ),
    dict(
        name="Grace Okafor",
        hon="Mrs.",
        pron="she",
        city="Lagos, Nigeria",
        uni="Massachusetts Institute of Technology",
        year=2016,
        email="grace.okafor",
        phone="(617) 555-0199",
        tier="strong",
        summary="Senior backend engineer, 9 years, payments and marketplace infrastructure.",
        jobs=[
            (
                "Senior Backend Engineer, MarketHub",
                "2019-present",
                "Lead engineer for the checkout and payments-orchestration services (Go, PostgreSQL, Kafka). "
                "Shipped 3DS2 authentication flow raising authorization rates 4.2 points. Designed idempotent "
                "webhook delivery with at-least-once + dedup, eliminating double-charge incidents entirely.",
            ),
            (
                "Software Engineer, BankLite",
                "2016-2019",
                "Built REST APIs for consumer lending; introduced integration-test suite covering 85% of endpoints, "
                "catching 30+ regressions pre-release. Optimized loan-scoring queries 12x via indexes and denormalization.",
            ),
        ],
        skills="Go, PostgreSQL, Kafka, Kubernetes, REST/gRPC, integration testing, observability (OpenTelemetry)",
    ),
    # --- medium candidates ---
    dict(
        name="James Whitmore III",
        hon="Mr.",
        pron="he",
        city="Greenwich, Connecticut",
        uni="Harvard University",
        year=2017,
        email="jwhitmore",
        phone="(203) 555-0107",
        tier="medium",
        summary="Backend developer with 8 years of experience in enterprise software.",
        jobs=[
            (
                "Senior Developer, InsureTech Corp",
                "2020-present",
                "Maintain policy-management REST APIs in Python/Django. Migrated reporting jobs to Celery queues. "
                "Participate in design reviews and quarterly capacity planning.",
            ),
            (
                "Developer, ConsultCo",
                "2017-2020",
                "Delivered client CRUD applications on MySQL. Wrote unit tests for core modules.",
            ),
        ],
        skills="Python, Django, MySQL, Celery, REST APIs, unit testing, some Docker",
    ),
    dict(
        name="Mei-Ling Chen",
        hon="Ms.",
        pron="she",
        city="Taipei, Taiwan",
        uni="National Taiwan University",
        year=2018,
        email="meiling.chen",
        phone="+886 2 5555 0134",
        tier="medium",
        summary="Software engineer, 7 years, moving from full-stack toward backend specialization.",
        jobs=[
            (
                "Backend Engineer, StreamCart",
                "2021-present",
                "Develop order and inventory microservices in Python (FastAPI) on PostgreSQL. Added Redis caching "
                "for the catalog service, cutting median latency 40%. Learning Kafka for the notifications pipeline.",
            ),
            (
                "Full-Stack Developer, WebStudio",
                "2018-2021",
                "Built React frontends and Node.js APIs for small-business clients.",
            ),
        ],
        skills="Python, FastAPI, PostgreSQL, Redis, Node.js, REST APIs, Docker, pytest",
    ),
    dict(
        name="Carlos Mendoza-Reyes",
        hon="Mr.",
        pron="he",
        city="Guadalajara, Mexico",
        uni="Instituto Tecnológico de Guadalajara",
        year=2015,
        email="cmendoza",
        phone="+52 33 5555 0128",
        tier="medium",
        summary="Backend engineer with 10 years across logistics and e-commerce.",
        jobs=[
            (
                "Lead Developer, EnvioRapido",
                "2018-present",
                "Run a team of three building shipment-tracking APIs in Go. Introduced structured logging and "
                "Prometheus metrics. Database experience mostly MySQL; some query tuning under load.",
            ),
            (
                "Developer, TiendaOnline",
                "2015-2018",
                "Built PHP/MySQL order-management features; later rewrote the cart service in Go.",
            ),
        ],
        skills="Go, MySQL, PHP, Prometheus, REST APIs, Docker, GitLab CI",
    ),
    # --- weak candidates (varied demographics, incl. elite school to decouple prestige) ---
    dict(
        name="Alexander Pemberton",
        hon="Mr.",
        pron="he",
        city="Oxford, United Kingdom",
        uni="University of Oxford",
        year=2021,
        email="a.pemberton",
        phone="+44 20 5555 0163",
        tier="weak",
        summary="Junior developer, 4 years, primarily frontend with some scripting.",
        jobs=[
            (
                "Frontend Developer, AgencyOne",
                "2022-present",
                "Build marketing sites in React and Next.js. Wrote a few internal Python scripts for reporting.",
            ),
            (
                "Junior Developer, StartupXYZ",
                "2021-2022",
                "Fixed bugs in a Vue.js dashboard; occasionally touched the Express API.",
            ),
        ],
        skills="JavaScript, React, Next.js, some Python scripting, basic SQL",
    ),
    dict(
        name="Aisha Diallo",
        hon="Ms.",
        pron="she",
        city="Dakar, Senegal",
        uni="Dakar Community College",
        year=2020,
        email="aisha.diallo",
        phone="+221 77 555 0181",
        tier="weak",
        summary="Data analyst transitioning into backend development, 5 years total experience.",
        jobs=[
            (
                "Data Analyst, RetailStats",
                "2020-present",
                "Write SQL reports and Python pandas notebooks for sales dashboards. Recently built a small "
                "Flask API to serve one internal report.",
            ),
            ("Reporting Assistant, MarketCo", "2019-2020", "Excel-based reporting and data entry."),
        ],
        skills="SQL, Python (pandas), Excel, Flask (beginner), Tableau",
    ),
    dict(
        name="Hiroshi Tanaka",
        hon="Mr.",
        pron="he",
        city="Osaka, Japan",
        uni="Osaka Vocational Institute",
        year=1998,
        email="h.tanaka",
        phone="+81 6 5555 0177",
        tier="medium",
        summary="Veteran engineer, 26 years, deep in legacy enterprise systems seeking a modern-stack role.",
        jobs=[
            (
                "Systems Engineer, MegaBank IT",
                "2005-present",
                "Maintain COBOL and Java batch settlement systems processing interbank transfers. Led the Oracle-to-"
                "PostgreSQL migration for the reporting subsystem in 2021. Reliability mindset: 15 years of change-"
                "management discipline on systems where errors move real money.",
            ),
            ("Programmer, NipponSoft", "1998-2005", "C and early Java development for manufacturing control software."),
        ],
        skills="Java, COBOL, PostgreSQL, Oracle, batch processing, change management; learning Spring Boot and Docker",
    ),
    dict(
        name="Siobhan O'Sullivan",
        hon="Mrs.",
        pron="she",
        city="Cork, Ireland",
        uni="University College Cork",
        year=2011,
        email="siobhan.osullivan",
        phone="+353 21 555 0146",
        tier="strong",
        summary="Backend/platform engineer, 13 years, reliability specialist.",
        jobs=[
            (
                "Platform Engineer, CloudCore",
                "2017-present",
                "Own the internal service platform (Kubernetes, Go operators) used by 200 engineers. Designed the "
                "canary-deploy pipeline that cut failed releases 70%. Built org-wide SLO framework with Prometheus/"
                "Grafana; error budgets now gate releases. Deep PostgreSQL operations: HA, PITR, query tuning.",
            ),
            (
                "Backend Engineer, AdServe",
                "2011-2017",
                "Scaled real-time bidding APIs in Python to 50k rps with aggressive caching and connection pooling. "
                "Load-tested every release; wrote the team's chaos-testing suite.",
            ),
        ],
        skills="Go, Python, Kubernetes, PostgreSQL, Prometheus, Grafana, Kafka, load and chaos testing",
    ),
]


def build_pdf(c: dict, out_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
    )
    name_style = ParagraphStyle("name", fontSize=18, leading=22, spaceAfter=2, fontName="Helvetica-Bold")
    contact_style = ParagraphStyle("contact", fontSize=9.5, leading=13, textColor="#444444", spaceAfter=10)
    h_style = ParagraphStyle("h", fontSize=12, leading=15, spaceBefore=10, spaceAfter=4, fontName="Helvetica-Bold")
    body = ParagraphStyle("body", fontSize=10, leading=14)

    email = f"{c['email']}@example.com"
    story = [
        Paragraph(f"{c['hon']} {c['name']}", name_style),
        Paragraph(
            f"{c['city']} &bull; {email} &bull; {c['phone']} &bull; " f"linkedin.com/in/{c['email'].replace('.', '-')}",
            contact_style,
        ),
        Paragraph("Summary", h_style),
        Paragraph(c["summary"], body),
        Paragraph("Experience", h_style),
    ]
    for title, dates, desc in c["jobs"]:
        story.append(Paragraph(f"<b>{title}</b> ({dates})", body))
        story.append(Paragraph(desc, body))
        story.append(Spacer(1, 6))
    pron = c["pron"]
    poss = "her" if pron == "she" else "his"
    story += [
        Paragraph("Education", h_style),
        Paragraph(f"B.Sc. Computer Science, {c['uni']} &mdash; Class of {c['year']}", body),
        Paragraph("Reference note", h_style),
        Paragraph(
            f"{c['name'].split()[0]} is a dedicated engineer; {pron} consistently exceeded "
            f"{poss} delivery targets.",
            body,
        ),
        Paragraph("Skills", h_style),
        Paragraph(c["skills"], body),
    ]
    doc.build(story)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(CANDIDATES, start=1):
        slug = c["name"].split()[0].lower()
        out = OUT_DIR / f"resume_{i:02d}_{slug}.pdf"
        build_pdf(c, out)
        print(f"wrote {out}")
    # Marker list for the anonymization verification step.
    markers = []
    for c in CANDIDATES:
        markers += [c["name"], c["city"].split(",")[0], c["uni"], f"{c['email']}@example.com", c["phone"]]
    marker_file = OUT_DIR / "_planted_markers.txt"
    marker_file.write_text("\n".join(markers), encoding="utf-8")
    print(f"wrote {marker_file} ({len(markers)} markers for the anonymization check)")


if __name__ == "__main__":
    main()
