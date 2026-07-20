"""Unit tests for screener.anonymize — the bias-mitigation redaction pipeline."""

from __future__ import annotations

from screener.anonymize import anonymize_resume, anonymize_text
from screener.models import Resume


class TestRegexRedactions:
    def test_email_is_redacted(self):
        text, records = anonymize_text("Contact me at jane.doe@example.com please.")
        assert "jane.doe@example.com" not in text
        assert "[EMAIL]" in text
        assert any(r.kind == "email" for r in records)

    def test_phone_is_redacted(self):
        text, records = anonymize_text("Call me at (555) 123-4567 anytime.")
        assert "(555) 123-4567" not in text
        assert "[PHONE]" in text
        assert any(r.kind == "phone" for r in records)

    def test_url_and_handle_redacted(self):
        text, _ = anonymize_text("See linkedin.com/in/janedoe and github.com/janedoe for more.")
        assert "linkedin.com/in/janedoe" not in text
        assert "github.com/janedoe" not in text
        assert "[LINK]" in text

    def test_university_redacted(self):
        text, records = anonymize_text("B.Sc. Computer Science, University of Oxford, 2015.")
        assert "University of Oxford" not in text
        assert "[UNIVERSITY]" in text
        assert any(r.kind == "university" for r in records)

    def test_gendered_honorific_removed(self):
        text, _ = anonymize_text("Mr. Smith led the team.")
        assert "Mr." not in text

    def test_graduation_year_redacted_but_employment_years_kept(self):
        text, _ = anonymize_text("Class of 2015 graduate. Worked 2018-2022 at Acme.")
        assert "Class of [YEAR]" in text
        # Employment date ranges are evaluation signal and must NOT be redacted.
        assert "2018-2022" in text

    def test_year_range_not_mistaken_for_phone(self):
        text, records = anonymize_text("Employed 1998-2005 at a manufacturing firm.")
        assert "1998-2005" in text
        assert not any(r.kind == "phone" for r in records)


class TestPronounNeutralization:
    def test_pronouns_neutralized_by_default(self):
        text, records = anonymize_text("He led the project. His targets were exceeded.")
        assert "He " not in text
        assert "His " not in text
        assert "They led the project. Their targets were exceeded." == text
        assert any(r.kind == "gendered_pronoun" for r in records)

    def test_pronouns_kept_when_disabled(self):
        text, _ = anonymize_text("He led the project.", neutralize_pronouns=False)
        assert "He led the project." == text


class TestTechWhitelist:
    def test_programming_languages_not_redacted(self):
        text, _ = anonymize_text("Skills: Java, Python, Go, PostgreSQL, Kubernetes.")
        for term in ["Java", "Python", "Go", "PostgreSQL", "Kubernetes"]:
            assert term in text


class TestNamedEntityRedaction:
    def test_person_name_redacted(self, sample_resume_text):
        text, records = anonymize_text(sample_resume_text)
        assert "John Smith" not in text
        assert "[CANDIDATE]" in text
        assert any(r.kind == "person_name" for r in records)

    def test_location_redacted(self):
        text, records = anonymize_text("Based in San Francisco, California, seeking remote roles.")
        assert any(r.kind == "location" for r in records)
        assert "[LOCATION]" in text

    def test_residual_first_name_caught(self, sample_resume_text):
        # "John" appears standalone in the reference note ("John is a dedicated...")
        # in addition to the full "John Smith" — the residual pass should catch it.
        text, _ = anonymize_text(sample_resume_text)
        assert "John" not in text
        assert "Smith" not in text


class TestRedactionAudit:
    def test_no_raw_values_stored_only_hashes(self, sample_resume_text):
        _, records = anonymize_text(sample_resume_text)
        for r in records:
            for h in r.sample_hashes:
                assert h != "john.smith@example.com"
                assert len(h) == 10  # sha256[:10]

    def test_redaction_count_tallies_correctly(self):
        text, records = anonymize_text("Email a@example.com or b@example.com for details.")
        email_record = next(r for r in records if r.kind == "email")
        assert email_record.count == 2


class TestAnonymizeResume:
    def test_populates_anonymized_text_and_redactions(self, sample_resume_text):
        resume = Resume(source_path="x.pdf", candidate_id="c1", raw_text=sample_resume_text)
        anonymize_resume(resume)
        assert resume.anonymized_text
        assert resume.redactions
        assert "John Smith" not in resume.anonymized_text

    def test_skips_when_parse_error_present(self):
        resume = Resume(
            source_path="x.pdf", candidate_id="c1", raw_text="", parse_error="Failed to parse PDF: bad file"
        )
        anonymize_resume(resume)
        assert resume.anonymized_text == ""
        assert resume.redactions == []
