"""Pipeline entry point: ingest -> anonymize -> evaluate -> report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from screener import anonymize, evaluate, evaluate_ollama, ingest, report


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="screener",
        description="Resume Screening & Talent Matching Agent with Bias Mitigation",
    )
    parser.add_argument("--jd", required=True, help="Job description (.json or .txt)")
    parser.add_argument("--resumes", required=True, help="Directory containing resume PDFs")
    parser.add_argument("--out", default="output", help="Output directory (default: output)")
    parser.add_argument("--top", type=int, default=10, help="How many candidates to show in the dashboard")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of PDFs processed (cost control)")
    parser.add_argument(
        "--backend",
        choices=["auto", "claude", "ollama"],
        default="auto",
        help="Evaluation engine: 'claude' (paid API), 'ollama' (free, local, needs 'ollama serve'), "
        "or 'auto' (Claude if ANTHROPIC_API_KEY is set, else Ollama) (default: auto)",
    )
    parser.add_argument("--model", default=None, help="Model id/name; defaults per backend")
    parser.add_argument("--ollama-host", default=evaluate_ollama.DEFAULT_HOST, help="Ollama server URL")
    parser.add_argument("--no-eval", action="store_true", help="Stop after anonymization (no API calls)")
    parser.add_argument("--keep-pronouns", action="store_true", help="Do not neutralize gendered pronouns")
    parser.add_argument(
        "--dump-anonymized", action="store_true", help="Write anonymized text files to <out>/anonymized/ for auditing"
    )
    args = parser.parse_args(argv)

    # 1. Ingestion
    jd = ingest.load_job_description(args.jd)
    print(f"Job: {jd.title}")
    resumes = ingest.load_resumes(args.resumes, limit=args.limit)
    if not resumes:
        print(f"No PDF resumes found in {args.resumes}", file=sys.stderr)
        return 1
    parsed = [r for r in resumes if not r.parse_error]
    print(f"Resumes: {len(resumes)} found, {len(parsed)} parsed, {len(resumes) - len(parsed)} unparseable")

    # 2. Anonymization
    print("Anonymizing (names, locations, universities, contact details)...")
    for r in parsed:
        anonymize.anonymize_resume(r, neutralize_pronouns=not args.keep_pronouns)
        total = sum(rec.count for rec in r.redactions)
        print(f"  {r.candidate_id}: {total} redactions ({Path(r.source_path).name})")

    if args.dump_anonymized:
        dump_dir = Path(args.out) / "anonymized"
        dump_dir.mkdir(parents=True, exist_ok=True)
        for r in parsed:
            (dump_dir / f"{r.candidate_id.replace(' ', '_')}.txt").write_text(r.anonymized_text, encoding="utf-8")
        print(f"Anonymized payloads written to {dump_dir}")

    # 3. Semantic evaluation
    results = [report.CandidateResult(resume=r) for r in resumes]
    if args.no_eval:
        print("Skipping evaluation (--no-eval).")
    else:
        backend = args.backend
        if backend == "auto":
            backend = "claude" if evaluate.has_api_credentials() else "ollama"

        if backend == "claude" and not evaluate.has_api_credentials():
            print(
                "No ANTHROPIC_API_KEY found (.env or environment) - falling back to Ollama (free, local).",
                file=sys.stderr,
            )
            backend = "ollama"

        if backend == "ollama" and not evaluate_ollama.has_ollama(args.ollama_host):
            print(f"Ollama not reachable at {args.ollama_host} - skipping evaluation.", file=sys.stderr)
            print(
                "Start it with 'ollama serve' (install from https://ollama.com), "
                f"then 'ollama pull {evaluate_ollama.DEFAULT_MODEL}'.",
                file=sys.stderr,
            )
            print("Everything up to anonymization completed; results below reflect no scoring.", file=sys.stderr)
            backend = None

        if backend == "claude":
            model = args.model or evaluate.DEFAULT_MODEL
            client = evaluate.build_client()
            print(f"Evaluating {len(parsed)} candidates with Claude ({model})...")
            for result in results:
                r = result.resume
                if r.parse_error:
                    continue
                result.evaluation = evaluate.evaluate_resume(client, jd, r, model=model)
                ev = result.evaluation
                status = f"overall {ev.overall}" if not ev.error else f"ERROR: {ev.error}"
                print(f"  {r.candidate_id}: {status}")
        elif backend == "ollama":
            model = args.model or evaluate_ollama.DEFAULT_MODEL
            print(f"Evaluating {len(parsed)} candidates with Ollama ({model}, free/local)...")
            for result in results:
                r = result.resume
                if r.parse_error:
                    continue
                result.evaluation = evaluate_ollama.evaluate_resume(jd, r, model=model, host=args.ollama_host)
                ev = result.evaluation
                status = f"overall {ev.overall}" if not ev.error else f"ERROR: {ev.error}"
                print(f"  {r.candidate_id}: {status}")

    # 4. Output
    json_path, html_path = report.write_report(jd, results, args.out, top=args.top)
    print(f"\nResults: {json_path}\nDashboard: {html_path}")

    ranked = [x for x in results if x.evaluation and not x.evaluation.error]
    if ranked:
        ranked.sort(key=lambda x: x.evaluation.overall, reverse=True)
        print("\nTop candidates:")
        print(f"  {'Rank':<5}{'Candidate':<15}{'Overall':<9}{'Skills':<8}{'Exp':<6}{'Impact'}")
        for i, x in enumerate(ranked[: args.top], start=1):
            e = x.evaluation
            print(
                f"  {i:<5}{x.resume.candidate_id:<15}{e.overall:<9}{e.skill_match:<8}{e.experience_relevance:<6}{e.project_impact}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
