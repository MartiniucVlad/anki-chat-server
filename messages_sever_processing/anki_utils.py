import simplemma


def detect_deck_language(notes: list) -> str:
    """
    Detects language based on the 'front' field of the first 20 notes.
    Returns a 2-letter code (e.g., 'de', 'fr', 'en').
    """
    if not notes:
        return "en"

    # Concatenate first 20 cards to give the detector enough context
    sample_text = " ".join([n.get('front', '') for n in notes[:20]])

    # simplemma.simple_langdetect returns a tuple like ('de', 0.98) or None
    try:
        lang = simplemma.simple_langdetect(sample_text)
        return lang[0] if lang else "en"
    except:
        return "en"