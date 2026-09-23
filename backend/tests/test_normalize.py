from app.rag.normalize import normalize, normalize_with_map


def test_case_insensitive():
    assert normalize("FIRAS") == normalize("firas") == normalize("Firas") == "firas"


def test_accent_insensitive():
    assert normalize("Café") == "cafe"
    assert normalize("Étudiant") == "etudiant"
    assert normalize("Développeur") == "developpeur"


def test_alef_variants_unify():
    # alef with hamza above/below/madda -> bare alef
    assert normalize("أحمد") == normalize("إحمد") == normalize("آحمد")


def test_ta_marbuta_and_alef_maqsura():
    assert normalize("مدرسة") == "مدرسه"  # ta marbuta -> ha
    assert normalize("على") == "علي"  # alef maqsura -> ya


def test_tashkeel_removed():
    with_diacritics = "مَدْرَسَة"
    without_diacritics = "مدرسه"  # also exercises ta marbuta unification
    assert normalize(with_diacritics) == without_diacritics


def test_normalize_with_map_positions_align_to_original():
    original = "Café Firas"
    normalized, index_map = normalize_with_map(original)
    pos = normalized.index("firas")
    assert original[index_map[pos]] == "F"


def test_normalize_with_map_handles_dropped_diacritics():
    # length changes (5 tashkeel marks dropped), map must still be valid
    original = "مَدْرَسَة test"
    normalized, index_map = normalize_with_map(original)
    pos = normalized.index("test")
    assert original[index_map[pos] : index_map[pos] + 4] == "test"
