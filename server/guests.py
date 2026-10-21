"""«Консьєрж»: who on the team's list a name someone gives belongs to — decided here, not by the model.

A heard name matches a record's first name directly, through its short forms («Катя» → Катерина, «Діма» →
Дмитро, server/names_uk.py), a case ending («Олегові»), or a near spelling from speech recognition («Алєг»).
A surname narrows several people with the name. The answer tells the backend what to say: not on the list,
ask the surname, or the one person — with the fact to ask about.
"""
from __future__ import annotations

import difflib
import random
from dataclasses import dataclass
from typing import Any

from server.knowledge import Record
from server.names_uk import full_forms, norm


def _words(text: str) -> list[str]:
    out = []
    for raw in (text or "").replace(",", " ").replace(".", " ").split():
        w = norm(raw)
        if len(w) >= 2 and any(ch.isalpha() for ch in w):
            out.append(w)
    return out


_SOUND = str.maketrans({"є": "е", "ї": "і", "и": "і", "ґ": "г", "а": "о", "'": ""})


def _sound(word: str) -> str:
    """How a name sounds, roughly: recognisers write «Алєг» for Олег, «Ірина»/«Ирина», «Ґалина»/«Галина»."""
    return word.translate(_SOUND)


def _close(heard: str, written: str) -> bool:
    """One word for one name: the same, a case ending on it («олегові»), or a near spelling («алєг»)."""
    if heard == written:
        return True
    if len(written) >= 3 and heard.startswith(written[:-1]) and 0 <= len(heard) - len(written) <= 3:
        return True  # олегові, ірино, тарасе, катерину
    a, b = _sound(heard), _sound(written)
    # A near spelling only between words of about the same length: «Петро» is not «Петренко».
    return a == b or (len(a) >= 4 and a[0] == b[0] and abs(len(a) - len(b)) <= 1
                      and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def _case_forms(word: str) -> set[str]:
    """A short name with a case ending back to its base: «каті», «катю» → «катя»; «олю» → «оля»."""
    forms = {word}
    if len(word) >= 3:
        stem = word[:-1]
        forms |= {stem + "я", stem + "а"}
    return forms


def _first_name_match(heard: list[str], title_words: list[str]) -> str | None:
    """The heard word that is this record's first name (via short forms too), or None."""
    for h in heard:
        for full in {f for form in _case_forms(h) for f in full_forms(form)}:
            for t in title_words:
                if _close(full, t) or _close(h, t):
                    return h
    return None


def _exact_first_name(heard: list[str], title_words: list[str]) -> bool:
    """The name as written, or a short form of it — not just close: «Олександра» is exactly Олександра,
    only close to Олександр (it looks like his name with a case ending)."""
    return any(f in title_words for h in heard for form in _case_forms(h) for f in full_forms(form))


def _surname_match(heard: list[str], title_words: list[str], used: str | None) -> bool:
    return any(_close(h, t) for h in heard if h != used for t in title_words)


@dataclass
class Found:
    status: str  # "not_found" | "ask_surname" | "found"
    record: Record | None = None
    count: int = 0
    short_form: str = ""  # «Катя → Катерина» when the name came as a short form


def find_guest(name: str, surname: str, records: list[Record]) -> Found:
    heard_name = _words(name)
    heard_surname = _words(surname)
    heard = heard_name + heard_surname
    if not heard:
        return Found("not_found")
    by_name: list[tuple[Record, str]] = []
    for r in records:
        title_words = _words(r.title)
        used = _first_name_match(heard_name or heard, title_words)
        if used:
            by_name.append((r, used))
    if not by_name and heard_surname:  # only the surname was caught
        by_name = [(r, "") for r in records if _surname_match(heard_surname, _words(r.title), None)]
    if not by_name:
        return Found("not_found")
    exact = [(r, u) for r, u in by_name if u and _exact_first_name(heard_name or heard, _words(r.title))]
    if exact:  # a name said in full wins over ones it only looks like
        by_name = exact
    if heard_surname or len(heard) > 1:
        narrowed = [(r, u) for r, u in by_name if _surname_match(heard, _words(r.title), u)]
        if narrowed:
            by_name = narrowed
        elif heard_surname:  # «Владислав Сеньків»: eleven Владислави, none of them Сеньків — not on the list
            return Found("not_found")
    if len(by_name) > 1:
        return Found("ask_surname", count=len(by_name))
    record, used = by_name[0]
    short = ""
    if used:
        title_words = _words(record.title)
        full = next((f for form in _case_forms(used) for f in full_forms(form) if f in title_words), "")
        if full and full != used:
            short = f"«{used.capitalize()}» — коротка форма імені {full.capitalize()}"
    return Found("found", record=record, count=1, short_form=short)


def is_on_list(full_name: str, records: list[Record]) -> bool:
    """confirm_guest only for a person who is really on the list (the record's own name, any word order)."""
    want = sorted(_words(full_name))
    return any(sorted(_words(r.title)) == want for r in records)


def pick_fact(title: str, keys: list[str], conversation_id: str) -> str:
    """The fact to ask this guest about: random per guest and visit (one question for everyone could be
    learned), the same while this guest talks (their answer is checked against it)."""
    keys = [k for k in keys if k.strip()]
    if not keys:
        return ""
    return random.Random(f"{conversation_id}:{' '.join(sorted(_words(title)))}").choice(keys)


def as_dict(found: Found) -> dict[str, Any]:
    return {"status": found.status, "count": found.count, "name": found.record.title if found.record else ""}
