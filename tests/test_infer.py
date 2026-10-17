from concertdb.infer import SetlistFacts, infer_bill


def facts(aid, name, songs=0, tour=None, info=None):
    return SetlistFacts(aid, name, songs, tour, info)


def test_single_act_is_unknown():
    roles = infer_bill([facts("a", "Osees", 12)])
    assert roles["a"].role == "unknown"


def test_info_note_names_headliner():
    roles = infer_bill([
        facts("h", "Iguana Death Cult", 5),
        facts("o", "Flat Worms", 9, info="Opening for Iguana Death Cult."),
    ])
    assert roles["h"].role == "headliner"
    assert roles["o"].role == "opener"
    assert roles["o"].confidence == 0.9


def test_support_wording_variants():
    for info in ("Support for The Osees", "supporting act for Osees", "Warm-up for Osees"):
        roles = infer_bill([facts("h", "Osees"), facts("o", "Flat Worms", info=info)])
        assert roles["h"].role == "headliner", info


def test_tour_name_containing_other_act():
    roles = infer_bill([
        facts("h", "Osees", 12, tour="Spring Tour 2025"),
        facts("o", "Flat Worms", 4, tour="Osees Spring Tour 2025"),
    ])
    assert (roles["h"].role, roles["o"].role) == ("headliner", "opener")
    assert roles["h"].confidence == 0.75


def test_only_act_with_a_tour():
    roles = infer_bill([facts("h", "A", 3, tour="Big Tour"), facts("o", "B", 3)])
    assert roles["h"].role == "headliner"
    assert roles["h"].confidence == 0.6


def test_longest_set():
    roles = infer_bill([facts("h", "A", 15), facts("o", "B", 6)])
    assert roles["h"].role == "headliner"
    assert roles["o"].role == "opener"


def test_similar_set_lengths_no_signal():
    roles = infer_bill([facts("a", "A", 10), facts("b", "B", 9)])
    assert {r.role for r in roles.values()} == {"unknown"}


def test_festival_is_unknown():
    sets = [facts(str(i), f"Band {i}", 10 + i, tour="Fest") for i in range(9)]
    roles = infer_bill(sets)
    assert {r.reason for r in roles.values()} == {"festival"}
