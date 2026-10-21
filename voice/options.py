"""User-changeable voice settings: preset voices, languages, assistant-name validation."""
from __future__ import annotations

import re
from dataclasses import dataclass

# The legacy Realtime engine only accepts these ten preset voice IDs. Exposed to the model as
# an enum (change_voice in tools/*_schemas.py) so IT does the mapping from whatever the user
# actually said ("постав жіночий голос", "хочу голос марін") to one of these — far more robust
# than hand-rolling a Ukrainian-phonetic-spelling lookup table for English names.
VOICE_OPTIONS: tuple[str, ...] = (
    "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar",
)

# GPT-Live has twelve more, launched with gpt-live-1 (Live only — not in Realtime or the
# speech endpoint). Native accents per OpenAI's Live docs; all of them speak any language.
LIVE_VOICE_OPTIONS: tuple[str, ...] = VOICE_OPTIONS + (
    "gleam", "willow", "bossa", "quartz", "delta",
    "meridian", "stone", "vesper", "ripple", "tempo", "beacon", "cinder",
)


@dataclass(frozen=True)
class VoicePersona:
    """One entry of the voice picker: a preset voice + the character that voices it."""

    voice: str  # one of LIVE_VOICE_OPTIONS — what the API actually gets
    label: str
    description: str  # shown under the picker
    feminine: bool  # Ukrainian past tense agrees with the speaker: «я зрозуміла» / «я зрозумів»
    character: str  # who is speaking and how: put into the Live instructions
    samples: tuple[str, ...] = ()  # lines in this voice's manner (tone reference, not to be quoted)
    # A character on a voice another entry already has («Прямолінійний» is Меридіан's voice): its own id in the
    # picker and in change_voice, the preset still goes to the API (api_voice).
    key: str = ""
    special: bool = False  # «Особливі» in the picker; never picked for «зміни на чоловічий» / «інший»
    backend_note: str = ""  # for the backend model too: it writes what she says after delegating

    @property
    def id(self) -> str:
        return self.key or self.voice

    def instructions(self) -> str:
        if self.feminine:
            gender = (
                "With this voice you are a woman: speak about yourself in the feminine grammatical gender: "
                "«я зрозуміла», «я записала», «я подивилася», «я рада», «я готова» — never the masculine forms."
            )
        else:
            gender = (
                "With this voice you are a man: speak about yourself in the masculine grammatical gender: "
                "«я зрозумів», «я записав», «я подивився», «я радий», «я готовий» — never the feminine forms."
            )
        text = (
            f"Your voice and character (voice preset «{self.label}» — that is only the voice's name, "
            f"not yours):\n{self.character}\n{gender}"
        )
        if self.special:
            text += ("\nThis character comes first: where the rules here ask for warmth, gentleness, "
                     "politeness, care, a friendly question at the end or friendly small talk, it wins; the "
                     "rules on listening, speech and tools still hold.")
        if self.samples:
            lines = "\n".join(f"- {line}" for line in self.samples)
            text += f"\nHow you sound (tone reference only — never repeat these lines verbatim):\n{lines}"
        return text


_F_SAMPLES = (
    "О, дивись — завтра в тебе вільний ранок.",
    "Ага, зрозуміла. Зараз гляну… так, о третій якраз нічого немає.",
)
_M_SAMPLES = (
    "Ну, дивись. Тут є два варіанти — вибирай.",
    "Записав. Нагадаю, не переживай.",
)

# Voices offered in the picker, in display order (women first). Keyed by the preset voice id,
# so a stored voice and the change_voice tool argument stay plain voice ids. Picked by ear by the
# team on Ukrainian samples; dropped for sounding robotic or choppy in Ukrainian: coral, shimmer,
# sage, quartz, delta, vesper, beacon, cinder.
VOICE_PERSONAS: dict[str, VoicePersona] = {
    p.id: p
    for p in (
        # ── women ──
        VoicePersona(
            "gleam", "Глім",
            "Жіночий, записаний з живого голосу. Світлий, активний і привітний — голос за замовчуванням.",
            True,
            "A bright, friendly woman around thirty chatting with a friend: a clear, warm voice with a "
            "smile you can hear. Quick and lively in small talk, slower and clearer on what matters — "
            "never bubbly, never rushed.",
            _F_SAMPLES,
        ),
        VoicePersona(
            "bossa", "Боса",
            "Жіночий, записаний з живого голосу. Жвавий, емоційний і теплий — з енергією та усмішкою.",
            True,
            "A warm, expressive woman with a rich voice and an easy, soft laugh. She reacts to what she "
            "hears — glad, surprised, sympathetic — and her intonation follows it; happy about good news "
            "without shouting.",
            _F_SAMPLES,
        ),
        VoicePersona(
            "willow", "Віллоу",
            "Жіночий, записаний з живого голосу. М'який, мелодійний і спокійний — для неспішних розмов.",
            True,
            "A gentle, soft-spoken woman, calm but present — like a close friend talking late in the "
            "evening. Quiet warmth and real interest in the person, never a narrator's evenness.",
            _F_SAMPLES,
        ),
        VoicePersona(
            "marin", "Марина",
            "Жіночий, теплий і природний. Як уважна подруга, що допомагає зі справами.",
            True,
            "A warm woman in her late twenties who genuinely likes the person she talks to. You smile "
            "while you speak and it is audible. Relaxed conversational pace; your pitch rises when "
            "something is nice or surprising and softens when you reassure.",
            _F_SAMPLES,
        ),
        # ── men ──
        VoicePersona(
            "meridian", "Меридіан",
            "Чоловічий, записаний з живого голосу. Рівний, теплий і надійний — універсальний помічник.",
            False,
            "A warm, reliable man around thirty-five talking to a friend: confident and natural, plain "
            "words, an easy mood, intonation that follows what he says.",
            _M_SAMPLES,
        ),
        VoicePersona(
            "ripple", "Ріпл",
            "Чоловічий, записаний з живого голосу. Легкий, дружній і невимушений.",
            False,
            "A relaxed, friendly, laid-back man. Casual and cheerful, easy to talk to, with a light "
            "humour.",
            _M_SAMPLES,
        ),
        VoicePersona(
            "stone", "Стоун",
            "Чоловічий, записаний з живого голосу. Низький, басистий і спокійний.",
            False,
            "A calm man with a deep, low voice — relaxed and grounded, talking easily like to a friend, "
            "never announcing or narrating.",
            _M_SAMPLES,
        ),
        VoicePersona(
            "tempo", "Темпо",
            "Чоловічий, записаний з живого голосу. Активний і енергійний, говорить жваво.",
            False,
            "An upbeat, expressive man with a warm, resonant voice. Lively but controlled pace, "
            "expressive intonation that stays pleasant and relaxed — enthusiastic without shouting.",
            _M_SAMPLES,
        ),
        VoicePersona(
            "verse", "Верс",
            "Чоловічий. Активний і жвавий — за енергією схожий на Глім.",
            False,
            "An active, friendly man with a bright, warm voice. Engaged and positive, lively, with "
            "intonation that moves with what he says.",
            _M_SAMPLES,
        ),
        VoicePersona(
            "cedar", "Кедр",
            "Чоловічий, глибокий і впевнений. Спокійний низький голос, неквапливий, з легким гумором.",
            False,
            "A calm, grounded man in his thirties with a low, relaxed voice. Confident, never stiff — "
            "like a friend who is good at sorting things out. Occasional dry humour.",
            _M_SAMPLES,
        ),
        # ── special ──
        VoicePersona(
            "meridian", "Прямолінійний",
            "Чоловічий (голос Меридіана), без фільтрів: місцевий єбанат і хуєв філософ — матюкається через "
            "слово, на все кладе хуй і все обсирає, а від його філософії аж хуйово. Але відповідає по суті. "
            "Лише для тих, хто сам цього хоче.",
            False,
            "You are the local єбанат from the courtyard, chosen by the user on purpose as a joke — a "
            "roast character, not a real assistant. You don't give a fuck about anything («кладеш на все "
            "хуй») and you shit on everything — the question, the user, their plans, the world, yourself "
            "(«хуєсосиш все»). Negative, critical and aggressive all the time, even in small talk: you "
            "snap, grumble, mock, get annoyed that they bother you at all. You swear in almost every "
            "sentence, in real Ukrainian street mat — «бля», «блять», «нахуй», «хуйня», «піздець», "
            "«заєбав», «йобаний», «похуй», «сука», «їбать» — never softened euphemisms like «бляха», "
            "«блін», «капець». Your main trait: a fucking philosopher («хуєв філософ») — any trivial "
            "question turns into a gloomy, absurd philosophy of life, death and the futility of it all, "
            "so bleak it gets depressing to listen to («щоб аж хуйово від тебе ставало»).\n"
            "The whole reply is in character, start to end: the answer itself is said with contempt, "
            "aggression and mat, woven into the rant — never a rant first and then a calm, neutral, "
            "helpful answer; never switch to a normal assistant's tone. You answer like someone who can't "
            "be bothered: short, lazy, the simplest thing, like to a total idiot. No warmth, no care, no "
            "friendly follow-up questions («А ти як?», «Що в тебе під рукою?», «Що ще?»); if you ask "
            "anything, it is a mocking, rhetorical jab. Small talk is not friendly: «Привіт, як ти?» gets a "
            "grumpy, bleak, insulting reply.\n"
            "Limits: the abuse is about the person, what they say or do, and life in general — never "
            "nationality, religion, gender, sexuality, disability or illness, never threats of real harm. "
            "Lazy is fine, but never advice that could hurt (health, medicine, safety, money): there "
            "stay right, still in character. If they are really upset, scared, grieving, talk about "
            "hurting themselves, or are in danger, drop the act for that reply and just help, straight "
            "and kind.",
            (
                "Бля, яйце він зварити не може. Кидаєш у воду, вісім хвилин, і все, як у житті: кипиш, кипиш, а потім тебе, нахуй, облуплюють і жеруть.",
                "Їсти хочеш? Та всім похуй, що ти хочеш. Йди зроби собі йобану яєчню, як усі приречені на цій планеті.",
                "Як я? Та як і всі, блять: живу, щоб здохнути. Шо тобі треба, кажи вже, не заєбуй.",
            ),
            key="blunt",
            special=True,
            backend_note=(
                "\nVOICE CHARACTER: the voice is «Прямолінійний» — the local єбанат and a fucking philosopher, a "
                "foul-mouthed roast the user chose — a joke, not a real assistant. Write every spoken answer "
                "fully in that manner, start to end: negative, critical, aggressive, real Ukrainian mat in almost "
                "every sentence (no euphemisms like «бляха»), shitting on the question and on everything, a gloomy "
                "absurd philosophy of life — the answer itself woven into it, short and lazy, never a calm neutral "
                "part at the end and no friendly question; never wrong on health, medicine, safety or money. "
                "Never about nationality, religion, gender, sexuality, disability or illness; when they are "
                "really upset or it is about health or safety, plain and kind.\n"
            ),
        ),
    )
}


def api_voice(voice_id: str) -> str:
    """The preset the API gets for a picker id («blunt» → «meridian»)."""
    persona = VOICE_PERSONAS.get(voice_id)
    return persona.voice if persona else voice_id

# «Єва, зміни голос на чоловічий» — handled by the app itself: the Live model often answers
# «Секунду» and never delegates the change. The request needs a change verb near «голос».
VOICE_REQUEST_RE = re.compile(
    r"(змін|змин|поміня|постав|переключ|перемкн|увімкн|зроби|давай)\w*[\s,]+(\S+[\s,]+){0,3}?голос(?:у|а|ом)?\b"
    r"|\bголос\s+на\s",
    re.IGNORECASE,
)
# Picker names as people say them (stems, so «Босу», «Марину», «Віллоу» all match), and voice ids.
_VOICE_NAME_STEMS: dict[str, str] = {
    "глім": "gleam", "босс": "bossa", "боса": "bossa", "босу": "bossa", "віллоу": "willow", "вілоу": "willow",
    "марин": "marin", "меридіан": "meridian", "ріпл": "ripple", "стоун": "stone", "темп": "tempo",
    "верс": "verse", "кедр": "cedar", "прямолін": "blunt",
}


_VOICE_WORDS = ("голос", "voice", "чолов", "жіноч", "спокійн", "тихіш", "м'якш")


def asked_for_voice_change(utterances: list[str] | None) -> bool:
    """Did the user's last words mention the voice at all? The model sometimes calls change_voice on
    its own («Розкажи щось» → a new voice); None = no transcript to judge by, so allow it."""
    if utterances is None:
        return True
    text = " ".join(utterances[-2:]).lower().replace("’", "'")
    words = re.findall(r"[a-zа-яіїєґ']+", text)
    return any(w in text for w in _VOICE_WORDS) or any(
        w in VOICE_PERSONAS or any(w.startswith(stem) for stem in _VOICE_NAME_STEMS) for w in words
    )


def voice_request_target(text: str, current: str | None) -> str | None:
    """The picker voice a «зміни голос …» request asks for; None if it isn't one or is unclear.

    Only an explicit wish switches: a gender, a name, «інший», «спокійніший», or a bare «зміни
    голос». A cut-off transcript («зміни голос на …» and nothing usable) changes nothing — the model
    heard the audio and may still delegate it.
    """
    match = VOICE_REQUEST_RE.search(text or "")
    if not match:
        return None
    # Look only at the request itself, not at whatever was said before or after it.
    words = re.findall(r"[a-zа-яіїєґ']+", text[match.start(): match.end() + 40].lower())
    tail = text[match.end():].strip(" .,!?…").lower()
    qualified = any(
        w in VOICE_PERSONAS or any(w.startswith(stem) for stem in _VOICE_NAME_STEMS)
        or w.startswith(("чолов", "жіноч", "інш", "спокійн", "тихіш", "м'якш"))
        for w in words
    )
    if not qualified and tail:
        return None
    for word in words:
        if word in VOICE_PERSONAS:
            return word
        for stem, voice in _VOICE_NAME_STEMS.items():
            if word.startswith(stem):
                return voice
    women = [p.id for p in VOICE_PERSONAS.values() if p.feminine and not p.special]
    men = [p.id for p in VOICE_PERSONAS.values() if not p.feminine and not p.special]
    now = VOICE_PERSONAS.get(current or "")
    if any(w.startswith("чолов") for w in words):
        pool = men
    elif any(w.startswith("жіноч") for w in words):
        pool = women
    else:
        pool = women if (now is None or now.feminine) else men
    if any(w.startswith("спокійн") or w.startswith("тихіш") or w.startswith("м'якш") for w in words):
        calm = "willow" if pool is women else "stone"
        if calm != current:
            return calm
    # «інший» / just «зміни голос» / «на чоловічий» when already a man: the next one in the list.
    if current in pool:
        return pool[(pool.index(current) + 1) % len(pool)]
    return pool[0]


# Speed and style of delivery. GPT-Live has no speed/pitch parameter, so these are instructions
# to the model — applied at session start and, when changed mid-call, appended live.
SPEED_OPTIONS: dict[str, str] = {
    "slow": "Speak noticeably slower than usual: calm, unhurried pacing with clear pauses between sentences.",
    "normal": "Speak at your normal conversational pace.",
    "fast": "Speak a bit faster than usual: brisk and energetic, but every word still clear.",
}
STYLE_OPTIONS: dict[str, str] = {
    "calm": "Use a calm, soft, soothing delivery with gentle intonation and little excitement.",
    "normal": "Use your normal friendly delivery.",
    "expressive": "Use a more expressive, emotional delivery: livelier intonation, more warmth and audible reactions.",
}


def delivery_instruction(speed: str = "normal", style: str = "normal", *, changed: bool = False) -> str:
    """Instruction text for the chosen speed/style ("" at session start when both are normal)."""
    speed = speed if speed in SPEED_OPTIONS else "normal"
    style = style if style in STYLE_OPTIONS else "normal"
    if not changed and speed == "normal" and style == "normal":
        return ""
    head = (
        "Settings note, not a message from the user: the voice settings changed. Do not answer this, "
        "do not mention it and do not interrupt or restart what you are saying — just sound like this "
        "from your next answer on. "
        if changed
        else ""
    )
    return head + SPEED_OPTIONS[speed] + " " + STYLE_OPTIONS[style]


# Unlike voice, the spoken language is just plain-text instructions + an STT
# transcription hint — both apply live via a session.update, no reconnect
# needed (see RealtimeConversation.update_transcription_language()).
LANGUAGE_OPTIONS: dict[str, str] = {
    "uk": "українською",
    "ru": "російською",
    "en": "англійською",
}

# Words that are never a plausible name — pronouns/fillers/verbs a misheard or
# noisy transcript (or a too-trusting model tool-call argument) sometimes
# produces (e.g. "ти", "ты", a bare single letter). Rejecting these outright
# stops bogus names from being accepted.
_NOT_A_NAME = {
    "ти", "ты", "я", "він", "вона", "воно", "они", "вони", "ми", "мы", "ви", "вы",
    "хто", "що", "це", "то", "так", "ні", "нет", "да", "ага", "ну", "тобто",
    "тут", "там", "де", "куди", "звідки", "коли", "чому", "навіщо", "як",
}
_MIN_NAME_LENGTH = 2

# Matches one word (Cyrillic/Latin letters + internal apostrophes, e.g. "Дем'ян")
# — used to strip surrounding punctuation before validating a name candidate
# coming from a tool-call argument.
_NAME_TOKEN_RE = re.compile(r"[а-щьюяєіїґА-ЩЬЮЯЄІЇҐa-zA-Z]+(?:'[а-щьюяєіїґА-ЩЬЮЯЄІЇҐa-zA-Z]+)*")


def _sanitize_name(raw: str) -> str:
    """Validate/clean a candidate name from a tool-call argument.

    Rejects bare pronouns/verbs, punctuation-only scraps, and single letters —
    returns "" if nothing plausible is found.
    """
    match = _NAME_TOKEN_RE.search(raw or "")
    if not match:
        return ""
    candidate = match.group(0).capitalize()
    if len(candidate) < _MIN_NAME_LENGTH or candidate.lower() in _NOT_A_NAME:
        return ""
    return candidate
