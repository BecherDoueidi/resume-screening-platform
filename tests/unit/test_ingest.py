"""Unit tests for screener.ingest — PDF text extraction and JD loading."""

from __future__ import annotations

import json

from screener import ingest


class TestExtractPdfText:
    def test_extracts_real_text_from_pdf(self, make_pdf):
        path = make_pdf(["Backend Engineer", "10 years of Python experience"])
        text = ingest.extract_pdf_text(path)
        assert "Backend Engineer" in text
        assert "10 years of Python experience" in text

    def test_multi_paragraph_pdf_preserves_all_content(self, make_pdf):
        lines = [f"Line number {i}" for i in range(5)]
        path = make_pdf(lines)
        text = ingest.extract_pdf_text(path)
        for line in lines:
            assert line in text

    def test_blank_pdf_yields_empty_string(self, make_pdf):
        path = make_pdf([])
        text = ingest.extract_pdf_text(path)
        assert text == ""


class TestLoadJobDescription:
    def test_loads_json_job_description(self, tmp_path):
        data = {
            "title": "Senior Backend Engineer",
            "summary": "Own the payments platform.",
            "required_skills": ["Python", "PostgreSQL"],
            "nice_to_have": ["Kubernetes"],
            "min_years_experience": 5,
            "responsibilities": ["Design APIs"],
        }
        path = tmp_path / "jd.json"
        path.write_text(json.dumps(data), encoding="utf-8")

        jd = ingest.load_job_description(path)

        assert jd.title == "Senior Backend Engineer"
        assert jd.required_skills == ["Python", "PostgreSQL"]
        assert jd.min_years_experience == 5
        assert jd.nice_to_have == ["Kubernetes"]

    def test_json_missing_optional_fields_uses_defaults(self, tmp_path):
        path = tmp_path / "jd.json"
        path.write_text(json.dumps({"title": "Minimal Role"}), encoding="utf-8")

        jd = ingest.load_job_description(path)

        assert jd.title == "Minimal Role"
        assert jd.required_skills == []
        assert jd.min_years_experience is None

    def test_json_missing_title_falls_back(self, tmp_path):
        path = tmp_path / "jd.json"
        path.write_text(json.dumps({}), encoding="utf-8")

        jd = ingest.load_job_description(path)

        assert jd.title == "Untitled role"

    def test_loads_plain_text_job_description(self, tmp_path):
        path = tmp_path / "jd.txt"
        path.write_text("Senior Backend Engineer\n\nFull job text here.", encoding="utf-8")

        jd = ingest.load_job_description(path)

        assert jd.title == "Senior Backend Engineer"
        assert "Full job text here." in jd.raw_text

    def test_to_prompt_text_uses_raw_text_when_present(self):
        from screener.models import JobDescription

        jd = JobDescription(title="X", raw_text="RAW TEXT VERBATIM")
        assert jd.to_prompt_text() == "RAW TEXT VERBATIM"

    def test_to_prompt_text_builds_structured_text_without_raw(self):
        from screener.models import JobDescription

        jd = JobDescription(
            title="Backend Engineer",
            summary="Summary here",
            required_skills=["Python"],
            min_years_experience=3,
        )
        prompt = jd.to_prompt_text()
        assert "Job Title: Backend Engineer" in prompt
        assert "Summary here" in prompt
        assert "- Python" in prompt
        assert "Minimum years of relevant experience: 3" in prompt


class TestLoadResumes:
    def test_loads_all_pdfs_in_directory_with_sequential_candidate_ids(self, tmp_path, make_pdf):
        make_pdf(["Resume A content"], filename="a.pdf")
        make_pdf(["Resume B content"], filename="b.pdf")
        # make_pdf writes into tmp_path already via its own fixture-provided tmp_path;
        # reuse that same directory here for load_resumes to scan.
        resumes = ingest.load_resumes(tmp_path)

        assert len(resumes) == 2
        assert {r.candidate_id for r in resumes} == {"Candidate 01", "Candidate 02"}
        assert all(r.parse_error == "" for r in resumes)

    def test_respects_limit(self, tmp_path, make_pdf):
        for i in range(3):
            make_pdf([f"Resume {i}"], filename=f"r{i}.pdf")

        resumes = ingest.load_resumes(tmp_path, limit=2)

        assert len(resumes) == 2

    def test_corrupt_pdf_recorded_as_parse_error_not_raised(self, tmp_path):
        bad = tmp_path / "corrupt.pdf"
        bad.write_text("not a real pdf")

        resumes = ingest.load_resumes(tmp_path)

        assert len(resumes) == 1
        assert resumes[0].parse_error != ""
