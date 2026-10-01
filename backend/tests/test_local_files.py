"""Tests for app/agent/local_files.py - the deterministic (no-LLM, no
real filesystem) parts of the Local File Agent: detecting a message is
about a local file, resolving a filename against the client-supplied
workspace index, and validating a relative_path string defensively."""

from dataclasses import dataclass

from app.agent.local_files import detect_local_file_intent, is_safe_relative_path, resolve_file_in_index


@dataclass
class _Entry:
    name: str
    relative_path: str


# --- detect_local_file_intent -----------------------------------------------


def test_lis_triggers_intent():
    r = detect_local_file_intent("Lis rapport.pdf")
    assert r is not None
    assert r.filename_hint == "rapport.pdf"


def test_analyse_le_fichier_triggers_intent():
    r = detect_local_file_intent("Analyse le fichier budget.xlsx")
    assert r is not None
    assert r.filename_hint == "budget.xlsx"


def test_resume_triggers_intent():
    r = detect_local_file_intent("Résume contrat.docx s'il te plaît")
    assert r is not None
    assert r.filename_hint == "contrat.docx"


def test_ouvre_triggers_intent():
    r = detect_local_file_intent("Ouvre image1.jpg")
    assert r is not None
    assert r.filename_hint == "image1.jpg"


def test_no_trigger_verb_no_intent():
    assert detect_local_file_intent("Quel est le budget marketing 2025 ?") is None


def test_trigger_verb_without_filename_falls_through_to_rag():
    # "lis" present but no extension-shaped token - this is a company
    # document question (RAG), not a local file - must not be captured.
    assert detect_local_file_intent("Lis la politique de congés") is None


def test_accented_filename_captured():
    r = detect_local_file_intent("Lis congés.xlsx")
    assert r is not None
    assert r.filename_hint == "congés.xlsx"


# --- resolve_file_in_index ---------------------------------------------------


def test_exact_match():
    index = [_Entry("rapport.pdf", "rapport.pdf"), _Entry("budget.xlsx", "RH/budget.xlsx")]
    result = resolve_file_in_index("rapport.pdf", index)
    assert result.relative_path == "rapport.pdf"


def test_case_and_accent_insensitive_match():
    index = [_Entry("Congés.xlsx", "RH/Congés.xlsx")]
    result = resolve_file_in_index("conges.xlsx", index)
    assert result.name == "Congés.xlsx"


def test_partial_match_when_no_exact():
    index = [_Entry("rapport_final_2025.pdf", "docs/rapport_final_2025.pdf")]
    result = resolve_file_in_index("rapport", index)
    assert result.relative_path == "docs/rapport_final_2025.pdf"


def test_ambiguous_match_returns_list():
    index = [_Entry("rapport.pdf", "a/rapport.pdf"), _Entry("rapport.pdf", "b/rapport.pdf")]
    result = resolve_file_in_index("rapport.pdf", index)
    assert isinstance(result, list)
    assert len(result) == 2


def test_no_match_returns_none():
    index = [_Entry("budget.xlsx", "budget.xlsx")]
    assert resolve_file_in_index("rapport.pdf", index) is None


def test_empty_index_returns_none():
    assert resolve_file_in_index("rapport.pdf", []) is None


# --- is_safe_relative_path --------------------------------------------------


def test_safe_relative_paths_accepted():
    assert is_safe_relative_path("rapport.pdf")
    assert is_safe_relative_path("RH/contrats/contrat1.pdf")
    assert is_safe_relative_path("Projet/données.xlsx")


def test_parent_traversal_rejected():
    assert not is_safe_relative_path("../../etc/passwd")
    assert not is_safe_relative_path("RH/../../../secrets.txt")


def test_absolute_path_rejected():
    assert not is_safe_relative_path("/etc/passwd")
    assert not is_safe_relative_path("\\\\server\\share\\file.txt")


def test_windows_drive_path_rejected():
    assert not is_safe_relative_path("C:\\Users\\someone\\Desktop\\file.txt")


def test_empty_path_rejected():
    assert not is_safe_relative_path("")
