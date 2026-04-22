"""
Test suite for extract_vocabulary — focused on edge cases where extraction
can silently fail or return wrong/incomplete data.

Run with:
    pytest testextract_vocabulary.py -v
"""

import pytest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Minimal spaCy token/doc stubs (no spaCy install required)
# ---------------------------------------------------------------------------

class FakeToken:
    def __init__(self, text: str, lemma: str, pos: str, is_alpha: bool = True):
        self.text = text
        self.lemma_ = lemma
        self.pos_ = pos
        self.is_alpha = is_alpha

class FakeDoc:
    def __init__(self, tokens: list):
        self._tokens = tokens
    def __iter__(self):
        return iter(self._tokens)


# ---------------------------------------------------------------------------
# Import target (adjust path if needed)
# ---------------------------------------------------------------------------
from routers.stories import extract_vocabulary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db(entries: list) -> MagicMock:
    """
    Returns a mock db whose dictionary.find() yields the given entries.
    Each entry must have at least {"_id": ObjectId-like, "word": str, "pos": str}.
    """
    from bson import ObjectId

    for i, e in enumerate(entries):
        if "_id" not in e:
            e["_id"] = ObjectId()

    async def fake_find(*args, **kwargs):
        for entry in entries:
            yield entry

    mock_collection = MagicMock()
    mock_collection.find = MagicMock(return_value=fake_find())

    mock_db = MagicMock()
    mock_db.dictionary = mock_collection
    return mock_db


def entry(word, pos, definitions, gender=None, plurals=None):
    from bson import ObjectId
    return {
        "_id": ObjectId(),
        "word": word,
        "pos": pos,
        "definitions": definitions,
        "gender": gender,
        "plurals": plurals or [],
    }


# ===========================================================================
# 1. HAPPY PATH
# ===========================================================================

@pytest.mark.asyncio
async def test_basic_noun():
    """A capitalized German noun should match spaCy's lowercase lemma."""
    doc = FakeDoc([FakeToken("Freund", "freund", "NOUN")])
    db = make_db([entry("Freund", "noun", ["friend"], gender="der", plurals=["Freunde"])])

    result = await extract_vocabulary(doc, db)

    assert len(result) == 1
    r = result[0]
    assert r["lemma"] == "freund"
    assert r["pos"] == "noun"
    assert r["gender"] == "der"
    assert "Freunde" in r["plurals"]
    assert r["definitions"]


@pytest.mark.asyncio
async def test_basic_verb():
    doc = FakeDoc([FakeToken("geht", "gehen", "VERB")])
    db = make_db([entry("gehen", "verb", ["to go", "to walk"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "verb"


# ===========================================================================
# 2. PARTICIPLE / INFLECTED FORM → BASE VERB FALLBACK
#    "einführend" is tagged VERB by spaCy with lemma "einführen", but the
#    dictionary only has "einführen" (not the participle form).
#    If spaCy yields the base lemma this works; if it yields the participle
#    form itself we need to detect the failure and communicate it.
# ===========================================================================

@pytest.mark.asyncio
async def test_present_participle_resolves_via_lemma():
    """
    spaCy should lemmatize 'einführend' → 'einführen'.
    If it does, we find the base verb entry in the dictionary.
    """
    doc = FakeDoc([FakeToken("einführend", "einführen", "VERB")])
    db = make_db([entry("einführen", "verb", ["to introduce", "to import"])])

    result = await extract_vocabulary(doc, db)

    assert len(result) == 1
    assert result[0]["lemma"] == "einführen"
    assert "to introduce" in result[0]["definitions"]


@pytest.mark.asyncio
async def test_present_participle_spacy_fails_to_lemmatize():
    """
    spaCy sometimes leaves the participle form as the lemma ('einführend').
    The dictionary won't have this form — entry should be skipped, not crash.
    """
    doc = FakeDoc([FakeToken("einführend", "einführend", "VERB")])
    db = make_db([entry("einführen", "verb", ["to introduce"])])

    result = await extract_vocabulary(doc, db)
    # No match found — should return empty, not raise
    assert result == []


@pytest.mark.asyncio
async def test_past_participle_resolves_via_lemma():
    """'eingeführt' → spaCy lemmatizes to 'einführen'."""
    doc = FakeDoc([FakeToken("eingeführt", "einführen", "VERB")])
    db = make_db([entry("einführen", "verb", ["to introduce"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["lemma"] == "einführen"


@pytest.mark.asyncio
async def test_conjugated_verb_resolves():
    """'liest' → 'lesen'"""
    doc = FakeDoc([FakeToken("liest", "lesen", "VERB")])
    db = make_db([entry("lesen", "verb", ["to read"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["lemma"] == "lesen"


# ===========================================================================
# 3. POS MISMATCH — spaCy tags noun as VERB
# ===========================================================================

@pytest.mark.asyncio
async def test_noun_mistagged_as_verb_fallback():
    """
    'Hunger' tagged as VERB by spaCy; dictionary returns a noun entry.
    The fallback should trust the dictionary and return the noun.
    """
    doc = FakeDoc([FakeToken("Hunger", "hunger", "VERB")])
    db = make_db([entry("Hunger", "noun", ["hunger", "starvation"], gender="der")])

    result = await extract_vocabulary(doc, db)

    assert len(result) == 1
    assert result[0]["pos"] == "noun"
    assert result[0]["gender"] == "der"


@pytest.mark.asyncio
async def test_verb_mistagged_as_noun():
    """
    Reverse mismatch: a verb entry found when spaCy says NOUN.
    Should correct POS to verb.
    """
    doc = FakeDoc([FakeToken("laufen", "laufen", "NOUN")])
    db = make_db([entry("laufen", "verb", ["to run", "to walk"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "verb"


# ===========================================================================
# 4. POS DISAMBIGUATION — same lemma, multiple entries
# ===========================================================================

@pytest.mark.asyncio
async def test_pos_disambiguation_prefers_matching_pos():
    """
    'gut' exists as both adj and adv. spaCy says ADJ → pick adj entry.
    """
    doc = FakeDoc([FakeToken("gut", "gut", "ADJ")])
    db = make_db([
        entry("gut", "adv", ["well"]),
        entry("gut", "adj", ["good", "fine"]),
    ])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "adj"
    assert "good" in result[0]["definitions"]


@pytest.mark.asyncio
async def test_pos_disambiguation_falls_back_to_first():
    """
    If no entry matches the POS, return the first entry rather than nothing.
    """
    doc = FakeDoc([FakeToken("nach", "nach", "ADP")])
    db = make_db([entry("nach", "conj", ["after", "according to"])])  # wrong POS in dict

    result = await extract_vocabulary(doc, db)
    assert len(result) == 1  # didn't drop it entirely


# ===========================================================================
# 5. FUNCTION WORDS (ADP, CONJ, DET, PRON, PART)
# ===========================================================================

@pytest.mark.asyncio
async def test_preposition_extracted():
    doc = FakeDoc([FakeToken("mit", "mit", "ADP")])
    db = make_db([entry("mit", "prep", ["with", "together with"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "prep"


@pytest.mark.asyncio
async def test_conjunction_extracted():
    doc = FakeDoc([FakeToken("während", "während", "SCONJ")])
    db = make_db([entry("während", "conj", ["while", "during"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "conj"


@pytest.mark.asyncio
async def test_particle_nicht():
    doc = FakeDoc([FakeToken("nicht", "nicht", "PART")])
    db = make_db([entry("nicht", "particle", ["not"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "particle"


@pytest.mark.asyncio
async def test_determiner_extracted():
    doc = FakeDoc([FakeToken("jede", "jede", "DET")])
    db = make_db([entry("jede", "det", ["every", "each"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "det"


# ===========================================================================
# 6. DEDUPLICATION — same lemma appears multiple times in doc
# ===========================================================================

@pytest.mark.asyncio
async def test_duplicate_surface_forms_deduplicated():
    """
    'Freund' appears three times; should produce one vocabulary entry
    with all surface forms collected.
    """
    doc = FakeDoc([
        FakeToken("Freund",   "freund", "NOUN"),
        FakeToken("Freunde",  "freund", "NOUN"),
        FakeToken("Freundes", "freund", "NOUN"),
    ])
    db = make_db([entry("Freund", "noun", ["friend"], gender="der", plurals=["Freunde"])])

    result = await extract_vocabulary(doc, db)

    assert len(result) == 1
    assert set(result[0]["surfaces"]) == {"Freund", "Freunde", "Freundes"}


# ===========================================================================
# 7. NON-ALPHA TOKENS — must be skipped
# ===========================================================================

@pytest.mark.asyncio
async def test_punctuation_skipped():
    doc = FakeDoc([
        FakeToken(".",   ".",   "PUNCT", is_alpha=False),
        FakeToken(",",   ",",   "PUNCT", is_alpha=False),
        FakeToken("123", "123", "NUM",   is_alpha=False),
        FakeToken("Hund", "hund", "NOUN"),
    ])
    db = make_db([entry("Hund", "noun", ["dog"], gender="der")])

    result = await extract_vocabulary(doc, db)
    assert len(result) == 1
    assert result[0]["lemma"] == "hund"


# ===========================================================================
# 8. EMPTY / EDGE INPUTS
# ===========================================================================

@pytest.mark.asyncio
async def test_empty_doc_returns_empty_list():
    doc = FakeDoc([])
    db = make_db([])

    result = await extract_vocabulary(doc, db)
    assert result == []


@pytest.mark.asyncio
async def test_all_tokens_non_alpha_returns_empty():
    doc = FakeDoc([
        FakeToken("123", "123", "NUM",   is_alpha=False),
        FakeToken("...", "...", "PUNCT", is_alpha=False),
    ])
    db = make_db([])

    result = await extract_vocabulary(doc, db)
    assert result == []


@pytest.mark.asyncio
async def test_no_dictionary_entries_returns_empty():
    """Words not in the dictionary should be silently skipped."""
    doc = FakeDoc([FakeToken("Zymurgie", "zymurgie", "NOUN")])
    db = make_db([])  # empty dictionary

    result = await extract_vocabulary(doc, db)
    assert result == []


# ===========================================================================
# 9. UNKNOWN / X POS — should not crash
# ===========================================================================

@pytest.mark.asyncio
async def test_unknown_pos_x_does_not_crash():
    """
    spaCy X tag = foreign/unknown. SPACY_TO_KAIKKI_POS maps it to [].
    Should not raise regardless of result.
    """
    doc = FakeDoc([FakeToken("okay", "okay", "X")])
    db = make_db([entry("okay", "intj", ["okay"])])

    result = await extract_vocabulary(doc, db)
    assert isinstance(result, list)


# ===========================================================================
# 10. DEFINITIONS CAPPED AT 3
# ===========================================================================

@pytest.mark.asyncio
async def test_definitions_capped_at_three():
    doc = FakeDoc([FakeToken("laufen", "laufen", "VERB")])
    db = make_db([entry("laufen", "verb", ["to run", "to walk", "to go", "to flow", "to operate"])])

    result = await extract_vocabulary(doc, db)
    assert len(result[0]["definitions"]) <= 3


# ===========================================================================
# 11. CASE-INSENSITIVE MATCHING — the core collation feature
# ===========================================================================

@pytest.mark.asyncio
async def test_lowercase_lemma_matches_capitalized_dict_entry():
    """
    spaCy lemmatizes to lowercase 'mutter'; kaikki stores 'Mutter'.
    Simulate collation by returning the capitalized entry from the mock.
    """
    doc = FakeDoc([FakeToken("Mutter", "mutter", "NOUN")])
    db = make_db([entry("Mutter", "noun", ["mother"], gender="die", plurals=["Mütter"])])

    result = await extract_vocabulary(doc, db)
    assert len(result) == 1
    assert result[0]["lemma"] == "mutter"
    assert result[0]["gender"] == "die"


# ===========================================================================
# 12. MIXED SENTENCE — realistic multi-token scenario
# ===========================================================================

@pytest.mark.asyncio
async def test_mixed_sentence():
    """
    Simulates: 'Der Hund läuft schnell durch den Park'
    Verifies multiple different POS are all extracted correctly.
    'Der' and 'den' share lemma 'der' → should produce only one entry.
    """
    doc = FakeDoc([
        FakeToken("Der",     "der",     "DET"),
        FakeToken("Hund",    "hund",    "NOUN"),
        FakeToken("läuft",   "laufen",  "VERB"),
        FakeToken("schnell", "schnell", "ADV"),
        FakeToken("durch",   "durch",   "ADP"),
        FakeToken("den",     "der",     "DET"),  # same lemma as "Der" → deduplicated
        FakeToken("Park",    "park",    "NOUN"),
    ])
    db = make_db([
        entry("der",     "article", ["the"]),
        entry("Hund",    "noun",    ["dog"],   gender="der", plurals=["Hunde"]),
        entry("laufen",  "verb",    ["to run", "to walk"]),
        entry("schnell", "adv",     ["quickly", "fast"]),
        entry("durch",   "prep",    ["through", "by means of"]),
        entry("Park",    "noun",    ["park"],  gender="der", plurals=["Parks"]),
    ])

    result = await extract_vocabulary(doc, db)
    lemmas = {r["lemma"] for r in result}

    assert "hund" in lemmas
    assert "laufen" in lemmas
    assert "schnell" in lemmas
    assert "durch" in lemmas
    assert "park" in lemmas
    # "der" appears twice as surface but should only be one entry
    der_entries = [r for r in result if r["lemma"] == "der"]
    assert len(der_entries) <= 1


# ===========================================================================
# 13. PROPER NOUN — PROPN
# ===========================================================================

@pytest.mark.asyncio
async def test_proper_noun_extracted_if_in_dict():
    doc = FakeDoc([FakeToken("Berlin", "berlin", "PROPN")])
    db = make_db([entry("Berlin", "name", ["capital of Germany"])])

    result = await extract_vocabulary(doc, db)
    assert len(result) == 1
    assert result[0]["pos"] == "name"


@pytest.mark.asyncio
async def test_proper_noun_not_in_dict_skipped():
    doc = FakeDoc([FakeToken("Müller", "müller", "PROPN")])
    db = make_db([])

    result = await extract_vocabulary(doc, db)
    assert result == []


# ===========================================================================
# 14. VERB SURFACE FORMS (different tenses, same lemma)
# ===========================================================================

@pytest.mark.asyncio
async def test_multiple_verb_tenses_same_lemma():
    """
    Past, present, perfect forms of 'haben' all map to same lemma.
    Should produce one entry with all surface forms.
    """
    doc = FakeDoc([
        FakeToken("habe",   "haben", "VERB"),
        FakeToken("hatte",  "haben", "VERB"),
        FakeToken("gehabt", "haben", "VERB"),
    ])
    db = make_db([entry("haben", "verb", ["to have", "to possess"])])

    result = await extract_vocabulary(doc, db)
    assert len(result) == 1
    assert set(result[0]["surfaces"]) == {"habe", "hatte", "gehabt"}


# ===========================================================================
# 15. ADJECTIVE WITH INFLECTED FORMS
# ===========================================================================

@pytest.mark.asyncio
async def test_adjective_inflections_deduplicated():
    """
    'kleine', 'kleinen', 'kleiner' all → 'klein'.
    Should produce one entry.
    """
    doc = FakeDoc([
        FakeToken("kleine",  "klein", "ADJ"),
        FakeToken("kleinen", "klein", "ADJ"),
        FakeToken("kleiner", "klein", "ADJ"),
    ])
    db = make_db([entry("klein", "adj", ["small", "little"])])

    result = await extract_vocabulary(doc, db)
    assert len(result) == 1
    assert set(result[0]["surfaces"]) == {"kleine", "kleinen", "kleiner"}


# ===========================================================================
# 16. POSTP (postposition) — rarer ADP subtype
# ===========================================================================

@pytest.mark.asyncio
async def test_postposition_extracted():
    """'gegenüber' can be a postposition in German."""
    doc = FakeDoc([FakeToken("gegenüber", "gegenüber", "ADP")])
    db = make_db([entry("gegenüber", "postp", ["opposite", "across from"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "postp"


# ===========================================================================
# 17. NUMERAL
# ===========================================================================

@pytest.mark.asyncio
async def test_numeral_extracted():
    doc = FakeDoc([FakeToken("drei", "drei", "NUM")])
    db = make_db([entry("drei", "num", ["three"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "num"


# ===========================================================================
# 18. INTERJECTION
# ===========================================================================

@pytest.mark.asyncio
async def test_interjection_extracted():
    doc = FakeDoc([FakeToken("ach", "ach", "INTJ")])
    db = make_db([entry("ach", "intj", ["oh", "ah", "alas"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "intj"


# ===========================================================================
# 19. SEPARABLE VERB — prefix variant
# ===========================================================================

@pytest.mark.asyncio
async def test_separable_verb_correctly_lemmatized():
    """
    'führt ein' (split form of einführen) — spaCy should reassemble lemma.
    Simulated by providing the correct compound lemma directly.
    """
    doc = FakeDoc([FakeToken("einführt", "einführen", "VERB")])
    db = make_db([entry("einführen", "verb", ["to introduce", "to implement"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["lemma"] == "einführen"
    assert "to introduce" in result[0]["definitions"]


# ===========================================================================
# 20. dict_id IS ALWAYS A STRING
# ===========================================================================

@pytest.mark.asyncio
async def test_dict_id_is_string():
    """dict_id must be str(ObjectId), never a raw ObjectId object."""
    doc = FakeDoc([FakeToken("Hund", "hund", "NOUN")])
    db = make_db([entry("Hund", "noun", ["dog"], gender="der")])

    result = await extract_vocabulary(doc, db)
    assert isinstance(result[0]["dict_id"], str)


# ===========================================================================
# 21. REFLEXIVE VERB — sich-prefix dropped by spaCy
# ===========================================================================

@pytest.mark.asyncio
async def test_reflexive_verb_lemmatized_without_sich():
    """
    'erinnert sich' — spaCy lemmatizes to 'erinnern' (dropping 'sich').
    Dictionary entry is 'erinnern', not 'sich erinnern'.
    """
    doc = FakeDoc([FakeToken("erinnert", "erinnern", "VERB")])
    db = make_db([entry("erinnern", "verb", ["to remember", "to remind"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["lemma"] == "erinnern"


# ===========================================================================
# 22. MODAL VERB
# ===========================================================================

@pytest.mark.asyncio
async def test_modal_verb_extracted():
    """'kann' → 'können'"""
    doc = FakeDoc([FakeToken("kann", "können", "VERB")])
    db = make_db([entry("können", "verb", ["can", "to be able to"])])

    result = await extract_vocabulary(doc, db)
    assert result[0]["lemma"] == "können"
    assert result[0]["pos"] == "verb"


# ===========================================================================
# 23. COMPOUND NOUN — spaCy may fail to split
# ===========================================================================

@pytest.mark.asyncio
async def test_compound_noun_treated_as_single_token():
    """
    'Bundesregierung' is a compound noun. spaCy treats it as one token.
    If it's in the dictionary, it should be extracted normally.
    """
    doc = FakeDoc([FakeToken("Bundesregierung", "bundesregierung", "NOUN")])
    db = make_db([entry("Bundesregierung", "noun", ["federal government"], gender="die")])

    result = await extract_vocabulary(doc, db)
    assert result[0]["gender"] == "die"


@pytest.mark.asyncio
async def test_compound_noun_not_in_dict_skipped():
    """
    Very rare compound not in dictionary → silently skipped.
    """
    doc = FakeDoc([FakeToken("Donaudampfschifffahrtsgesellschaft", "donaudampfschifffahrtsgesellschaft", "NOUN")])
    db = make_db([])

    result = await extract_vocabulary(doc, db)
    assert result == []


# ===========================================================================
# 24. UMLAUT / SPECIAL CHARACTERS IN LEMMA
# ===========================================================================

@pytest.mark.asyncio
async def test_umlaut_lemma_matches():
    doc = FakeDoc([FakeToken("Mütter", "mutter", "NOUN")])
    db = make_db([entry("Mutter", "noun", ["mother"], gender="die", plurals=["Mütter"])])

    result = await extract_vocabulary(doc, db)
    assert "Mütter" in result[0]["plurals"]


# ===========================================================================
# 25. AMBIGUOUS HOMOGRAPH — noun and verb with same spelling
# ===========================================================================

@pytest.mark.asyncio
async def test_homograph_noun_verb_correct_pos_selected():
    """
    'Laufen' exists as both noun ("das Laufen") and verb.
    When spaCy says NOUN, pick the noun entry.
    """
    doc = FakeDoc([FakeToken("Laufen", "laufen", "NOUN")])
    db = make_db([
        entry("laufen", "verb", ["to run"]),
        entry("Laufen", "noun", ["running", "the act of running"], gender="das"),
    ])

    result = await extract_vocabulary(doc, db)
    assert result[0]["pos"] == "noun"
    assert result[0]["gender"] == "das"