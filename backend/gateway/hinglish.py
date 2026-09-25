"""Indian-script text to English letters ("Hinglish" style romanisation).

Used to type Hindi as Hinglish ("क्या आप मेरी मदद कर सकते हैं" -> "Kya aap meri
madad kar sakte hain"), and to keep English output in English letters when
Whisper writes a word in Devanagari. It runs after recognition, so it costs no
inference time. Other Indian scripts (Bengali, Gurmukhi, Gujarati, Oriya,
Tamil, Telugu, Kannada, Malayalam) share Devanagari's Unicode layout and are
mapped onto it first.

The rules follow how people write Hindi in English letters:
- the inherent "a" is dropped at the end of a word and between syllables
  where it isn't pronounced (schwa deletion): कर -> kar, करना -> karna,
  सकते -> sakte, समझना -> samajhna, मदद -> madad;
- long vowels are doubled only before the word's final consonant: नाम -> naam,
  आप -> aap, बाज़ार -> bazaar, but मेरा -> mera, जाना -> jana, चाहता -> chahta.
"""
import re
import unicodedata

CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "ऩ": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ऱ": "r", "ल": "l", "ळ": "l", "ऴ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}
NUKTA_FORMS = {"क": "क़", "ख": "ख़", "ग": "ग़", "ज": "ज़", "ड": "ड़", "ढ": "ढ़", "फ": "फ़", "य": "य़"}
# vowel sign / independent vowel -> (short spelling, long spelling or None)
VOWELS = {
    "": ("a", None), "ा": ("a", "aa"), "ि": ("i", None), "ी": ("i", "ee"), "ु": ("u", None),
    "ू": ("u", "oo"), "ृ": ("ri", None), "े": ("e", None), "ै": ("ai", None), "ो": ("o", None),
    "ौ": ("au", None), "ॉ": ("o", None), "ॅ": ("e", None), "ॆ": ("e", None), "ॊ": ("o", None),
}
INDEPENDENT = {
    "अ": "", "आ": "ा", "इ": "ि", "ई": "ी", "उ": "ु", "ऊ": "ू", "ऋ": "ृ",
    "ए": "े", "ऐ": "ै", "ओ": "ो", "औ": "ौ", "ऑ": "ॉ", "ऍ": "ॅ", "ऎ": "ॆ", "ऒ": "ॊ",
}
VIRAMA, NUKTA, ANUSVARA, CHANDRABINDU, VISARGA = "्", "़", "ं", "ँ", "ः"
PUNCTUATION = {"।": ".", "॥": ".", "॰": "."}
DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}

# Indian scripts laid out like Devanagari (U+0900); offset of each block.
SCRIPT_BLOCKS = [0x0980, 0x0A00, 0x0A80, 0x0B00, 0x0B80, 0x0C00, 0x0C80, 0x0D00]
INDIC = re.compile(r"[ऀ-ൿ]+")


def _to_devanagari(text: str) -> str:
    out = []
    for c in text:
        code = ord(c)
        for base in SCRIPT_BLOCKS:
            if base <= code < base + 0x80:
                c = chr(code - base + 0x0900)
                break
        out.append(c)
    return "".join(out)


def _aksharas(word: str):
    """Split a Devanagari word into [consonant letters, vowel sign or None, nasal/visarga]."""
    units, i = [], 0
    while i < len(word):
        c = word[i]
        if i + 1 < len(word) and word[i + 1] == NUKTA and c in NUKTA_FORMS:
            c, i = NUKTA_FORMS[c], i + 1
        if c in CONSONANTS:
            cluster = [c]
            vowel = ""
            i += 1
            while i < len(word):
                n = word[i]
                if n == VIRAMA:
                    if i + 1 < len(word) and word[i + 1] in CONSONANTS:
                        nxt = word[i + 1]
                        if i + 2 < len(word) and word[i + 2] == NUKTA and nxt in NUKTA_FORMS:
                            nxt, i = NUKTA_FORMS[nxt], i + 1
                        cluster.append(nxt)
                        i += 2
                        continue
                    vowel, i = None, i + 1  # explicit "no vowel"
                    break
                if n == NUKTA:
                    i += 1
                    continue
                if n in VOWELS:
                    vowel, i = n, i + 1
                break
            units.append([cluster, vowel, ""])
        elif c in INDEPENDENT:
            units.append([[], INDEPENDENT[c], ""])
            i += 1
        elif c in (ANUSVARA, CHANDRABINDU, VISARGA) and units:
            units[-1][2] += "h" if c == VISARGA else "n"
            i += 1
        else:
            units.append([None, c, ""])  # anything else passes through
            i += 1
    return units


def _romanise_word(word: str, hindi: bool = True) -> str:
    units = _aksharas(word)
    letters = [u for u in units if u[0] is not None]
    # Schwa deletion: final inherent "a", then medial ones between two vowels.
    inherent = [u[1] == "" and u[0] != [] for u in letters]
    deleted = [False] * len(letters)
    if len(letters) > 1 and inherent[-1] and not letters[-1][2]:
        deleted[-1] = True
    for i in range(len(letters) - 2, 0, -1):
        prev_has_vowel = letters[i - 1][1] is not None and not deleted[i - 1]
        next_has_vowel = letters[i + 1][1] is not None and not deleted[i + 1]
        if inherent[i] and not letters[i][2] and prev_has_vowel and next_has_vowel and len(letters[i][0]) == 1:
            deleted[i] = True

    out, k = [], 0
    for u in units:
        if u[0] is None:
            out.append(u[1])
            continue
        index = k
        k += 1
        cluster, vowel, tail = u
        out.append("".join(CONSONANTS[c] for c in cluster))
        if vowel is None or deleted[index]:
            continue
        short, long_ = VOWELS.get(vowel, ("", None))
        # Doubled only when closed by the word's last consonant: naam, aap, bazaar;
        # a medial closure stays single: chahta, janta.
        closed = index + 1 == len(letters) - 1 and (letters[index + 1][1] is None or deleted[index + 1])
        initial = hindi and index == 0 and not cluster  # Hindi writes a word-initial long vowel long: aaj, aaega
        out.append(long_ if long_ and (initial or (closed and not tail)) else short)
        out.append(tail)
    return "".join(out)


def _strip_accents(text: str) -> str:
    """ā -> a and similar, for letters beyond Latin-1 (keeps é, ñ)."""
    return "".join(
        unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode() or c if ord(c) > 0xFF else c
        for c in text
    )


def _capitalise_sentences(text: str) -> str:
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)


def to_latin(text: str, capitalise: bool = True) -> str:
    """Romanise any Indian-script words in text; other text is kept."""
    if not text:
        return text
    had_indic = bool(INDIC.search(text))

    def romanise(match):
        hindi = all(0x0900 <= ord(c) <= 0x097F for c in match.group(0))
        chunk = _to_devanagari(match.group(0))
        for src, dst in {**PUNCTUATION, **DIGITS}.items():
            chunk = chunk.replace(src, dst)
        return re.sub(r"[ऀ-ॿ]+", lambda w: _romanise_word(w.group(0), hindi), chunk)

    result = _strip_accents(INDIC.sub(romanise, text))
    return _capitalise_sentences(result) if capitalise and had_indic else result


if __name__ == "__main__":
    samples = {
        "क्या आप मेरी मदद कर सकते हैं?": "Kya aap meri madad kar sakte hain?",
        "मुझे कल सुबह दिल्ली जाना है।": "Mujhe kal subah dilli jana hai.",
        "मेरा नाम नितिन है": "Mera naam nitin hai",
        "मैं समझना चाहता हूँ": "Main samajhna chahta hun",
        "यह बहुत अच्छा है": "Yah bahut achchha hai",
        "आज रात हम बाज़ार जाएंगे": "Aaj raat ham bazaar jaenge",
        "मुझे पता नहीं, शायद वो आएगा": "Mujhe pata nahin, shayad vo aaega",
        "काम ठीक है": "Kaam theek hai",
        "Hello दोस्तों, meeting कल है": "Hello doston, meeting kal hai",
        "Kjā mērīmā dot": "Kja merima dot",
        "আমি ভালো আছি": "Ami bhalo achhi",
    }
    for src, want in samples.items():
        got = to_latin(src)
        print(("ok  " if got == want else "DIFF"), f"{src!r} -> {got!r}" + ("" if got == want else f"   (want {want!r})"))
