"""Stage 2 — strip demographic indicators BEFORE any text reaches the LLM.

Three layers:
  1. Regex: emails, phones, URLs/handles, university names, street addresses,
     dates of birth, graduation years, gendered honorifics.
  2. spaCy NER: person names and locations — guarded by a tech-term whitelist
     so skill keywords ("Java", "PostgreSQL") are never redacted.
  3. Residual pass: any token from a detected person name that survives layer 2
     (e.g. a bare first name in a reference note) is replaced too. Optionally,
     gendered pronouns are neutralized.

Every replacement is tallied into RedactionRecords carrying only a short
sha256 hash of the original value — the raw value is never persisted.
"""

from __future__ import annotations

import hashlib
import logging
import re

from screener.models import RedactionRecord, Resume

logger = logging.getLogger(__name__)

_NLP = None
_SPACY_WARNING_SHOWN = False


def _get_nlp():
    global _NLP, _SPACY_WARNING_SHOWN
    if _NLP is None and not _SPACY_WARNING_SHOWN:
        try:
            import spacy

            _NLP = spacy.load("en_core_web_sm")
        except Exception:
            _SPACY_WARNING_SHOWN = True
            logger.warning(
                "spacy_model_unavailable",
                extra={"fallback": "regex_only_anonymization"},
            )
    return _NLP


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


# Skill/technology terms spaCy's small model regularly mislabels as PERSON,
# GPE, or ORG. Redacting these would erase the very signal we're evaluating.
_TECH_WHITELIST = {
    "java",
    "python",
    "go",
    "golang",
    "rust",
    "ruby",
    "php",
    "perl",
    "scala",
    "kotlin",
    "swift",
    "c",
    "c++",
    "c#",
    "cobol",
    "fortran",
    "sql",
    "nosql",
    "javascript",
    "typescript",
    "node.js",
    "node",
    "react",
    "react native",
    "next.js",
    "vue.js",
    "vue",
    "angular",
    "svelte",
    "express",
    "django",
    "flask",
    "fastapi",
    "spring",
    "spring boot",
    "rails",
    "laravel",
    "postgresql",
    "postgres",
    "mysql",
    "mariadb",
    "sqlite",
    "oracle",
    "mongodb",
    "redis",
    "memcached",
    "cassandra",
    "dynamodb",
    "elasticsearch",
    "kafka",
    "rabbitmq",
    "sqs",
    "celery",
    "spark",
    "hadoop",
    "airflow",
    "kubernetes",
    "docker",
    "terraform",
    "ansible",
    "helm",
    "aws",
    "gcp",
    "azure",
    "linux",
    "unix",
    "git",
    "github",
    "gitlab",
    "jenkins",
    "ci/cd",
    "prometheus",
    "grafana",
    "opentelemetry",
    "datadog",
    "splunk",
    "kibana",
    "grpc",
    "rest",
    "graphql",
    "html",
    "css",
    "tailwind",
    "sass",
    "pandas",
    "numpy",
    "pytorch",
    "tensorflow",
    "tableau",
    "excel",
    "k6",
    "jmeter",
    "pytest",
    "junit",
    "selenium",
    "3ds2",
    "webpack",
    "vite",
}


def _is_tech_term(text: str) -> bool:
    return text.strip().lower().rstrip(".,;") in _TECH_WHITELIST


_UNIVERSITY_HINT = re.compile(
    r"\b(universit|college|institute|instituto|polytechnic|school of|academy)",
    re.IGNORECASE,
)

# Matches both prefix form ("King Saud University", "Dakar Community College")
# and suffix form ("University of Oxford", "University College Cork",
# "Instituto Tecnológico de Guadalajara", "Massachusetts Institute of Technology").
_UNIVERSITY_RE = re.compile(
    r"\b(?:[A-Z][\w'&.-]*\s+)*"
    r"(?:University|Universit[aé]t?|Universidad|Universidade|Université|College|"
    r"Institute|Institut|Instituto|Polytechnic|Politecnico|Academy)"
    r"(?:\s+(?:of|de|del|della|für|for|College|[A-Z][\w'&.-]*))*"
)

# Phone shapes: international with +country-code, parenthesized area code, or
# a classic ###-###-#### with two separators. Deliberately does NOT match
# year ranges like "1998-2005" (employment dates are evaluation signal).
_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s.-]?(?:\(\d{1,4}\)[\s.-]?)?(?:\d{1,4}[\s.-]?){1,4}\d{2,4}"
    r"|\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]?\d{3,4}"
    r"|\b\d{3}[-.]\d{3}[-.]\d{4}\b)"
)

# Regex layer: (kind, pattern, replacement); applied in order, before NER.
_REGEX_RULES: list[tuple[str, re.Pattern, str]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (
        "url_or_handle",
        re.compile(r"(?:https?://\S+|www\.\S+|(?:linkedin\.com|github\.com|gitlab\.com)/\S+)", re.IGNORECASE),
        "[LINK]",
    ),
    ("university", _UNIVERSITY_RE, "[UNIVERSITY]"),
    ("phone", _PHONE_RE, "[PHONE]"),
    (
        "street_address",
        re.compile(
            r"\b\d{1,5}\s+[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?\s(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Lane|Ln\.?|Drive|Dr\.?)\b"
        ),
        "[ADDRESS]",
    ),
    (
        "date_of_birth",
        re.compile(r"\b(?:date of birth|dob|born)\b[:\s]*[^\n]{0,30}", re.IGNORECASE),
        "[DOB REDACTED]",
    ),
    ("gendered_honorific", re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx)\.?\s"), ""),
    (
        "graduation_year",
        re.compile(r"(?i)\b((?:class of|graduated(?: in)?|graduation)[:\s]*)((?:19|20)\d{2})\b"),
        r"\1[YEAR]",
    ),
]

_PRONOUN_MAP = {
    "he": "they",
    "she": "they",
    "him": "them",
    "her": "them",
    "his": "their",
    "hers": "theirs",
    "himself": "themself",
    "herself": "themself",
}
_PRONOUN_RE = re.compile(r"\b(" + "|".join(_PRONOUN_MAP) + r")\b", re.IGNORECASE)


def _record(tally: dict[str, RedactionRecord], kind: str, replacement: str, original: str):
    rec = tally.setdefault(kind, RedactionRecord(kind=kind, replacement=replacement, count=0))
    rec.count += 1
    h = _hash(original)
    if h not in rec.sample_hashes and len(rec.sample_hashes) < 5:
        rec.sample_hashes.append(h)


def _apply_ner(text: str, tally: dict[str, RedactionRecord]) -> tuple[str, set[str]]:
    """Returns (redacted text, person-name tokens for the residual pass)."""
    nlp = _get_nlp()
    if nlp is None:
        return text, set()
    doc = nlp(text[:100_000])  # cap for pathological inputs
    replacements: list[tuple[int, int, str, str]] = []
    name_tokens: set[str] = set()
    for ent in doc.ents:
        if _is_tech_term(ent.text):
            continue  # never redact skill keywords the small model mislabels
        if ent.label_ == "PERSON":
            replacements.append((ent.start_char, ent.end_char, "person_name", "[CANDIDATE]"))
            for token in ent.text.split():
                if len(token) > 2 and not _is_tech_term(token):
                    name_tokens.add(token)
        elif ent.label_ in ("GPE", "LOC"):
            replacements.append((ent.start_char, ent.end_char, "location", "[LOCATION]"))
        elif ent.label_ == "ORG" and _UNIVERSITY_HINT.search(ent.text):
            replacements.append((ent.start_char, ent.end_char, "university", "[UNIVERSITY]"))
    # Replace from the end so earlier offsets stay valid.
    for start, end, kind, placeholder in sorted(replacements, reverse=True):
        _record(tally, kind, placeholder, text[start:end])
        text = text[:start] + placeholder + text[end:]
    return text, name_tokens


def anonymize_text(text: str, neutralize_pronouns: bool = True) -> tuple[str, list[RedactionRecord]]:
    tally: dict[str, RedactionRecord] = {}

    # 1. Regex layer first: emails/URLs often embed the candidate's name, and
    # removing them early keeps NER from partially matching inside them.
    for kind, pattern, replacement in _REGEX_RULES:

        def _sub(m: re.Match, kind=kind, replacement=replacement) -> str:
            label = replacement if "\\" not in replacement else "[YEAR]"
            _record(tally, kind, label, m.group(0))
            return m.expand(replacement) if "\\" in replacement else replacement

        text = pattern.sub(_sub, text)

    # 2. NER layer.
    text, name_tokens = _apply_ner(text, tally)

    # 3. Residual pass: name tokens NER caught somewhere but missed elsewhere
    # (e.g. a bare first name in a reference line).
    for token in name_tokens:
        pattern = re.compile(r"\b" + re.escape(token) + r"\b")

        def _residual(m: re.Match) -> str:
            _record(tally, "person_name", "[CANDIDATE]", m.group(0))
            return "[CANDIDATE]"

        text = pattern.sub(_residual, text)

    if neutralize_pronouns:

        def _pron(m: re.Match) -> str:
            repl = _PRONOUN_MAP[m.group(0).lower()]
            _record(tally, "gendered_pronoun", "they/them/their", m.group(0))
            return repl.capitalize() if m.group(0)[0].isupper() else repl

        text = _PRONOUN_RE.sub(_pron, text)

    return text, list(tally.values())


def anonymize_resume(resume: Resume, neutralize_pronouns: bool = True) -> Resume:
    if resume.parse_error:
        return resume
    resume.anonymized_text, resume.redactions = anonymize_text(resume.raw_text, neutralize_pronouns=neutralize_pronouns)
    return resume
