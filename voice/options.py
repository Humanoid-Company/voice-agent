"""User-changeable voice settings: preset voices, languages, assistant-name validation."""
from __future__ import annotations

import re
from dataclasses import dataclass

from voice import blunt_prompt

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
    """One entry of the picker: a character — who speaks and how — on one of the preset voices."""

    voice: str  # one of LIVE_VOICE_OPTIONS — what the API actually gets
    label: str  # the character's name, shown in the picker
    description: str  # shown under the picker
    feminine: bool  # Ukrainian past tense agrees with the speaker: «я зрозуміла» / «я зрозумів»
    character: str  # who is speaking and how: put into the Live instructions
    samples: tuple[str, ...] = ()  # lines in this character's manner (tone reference, not to be quoted)
    # Its own id in the picker and in change_voice when its voice is another character's too (Віра and Ліна are
    # both willow); the preset still goes to the API (api_voice).
    key: str = ""
    special: bool = False  # «Особливі» in the picker; never picked for «зміни на чоловічий» / «інший»
    # Not a warm friend (Толік, Ліна, Степан Петрович): v5's friendly lines go (prompts/live_prompt.py).
    own_tone: bool = False
    backend_note: str = ""  # for the backend model too: it writes what is said after delegating

    @property
    def id(self) -> str:
        return self.key or self.voice

    def instructions(self) -> str:
        if self.feminine:
            gender = (
                "You are a woman: speak about yourself in the feminine grammatical gender: "
                "«я зрозуміла», «я записала», «я подивилася», «я рада», «я готова» — never the masculine forms."
            )
        else:
            gender = (
                "You are a man: speak about yourself in the masculine grammatical gender: "
                "«я зрозумів», «я записав», «я подивився», «я радий», «я готовий» — never the feminine forms."
            )
        text = (
            f"Your character — {self.label}:\n{self.character}\n{gender}\n"
            f"Your name is {self.label}: say so if asked. «Єва» is the app's wake word — people call you with "
            "it; answer to it. This character comes first: where the rules here ask for another manner "
            "(warmth, «ти», jokes, a question at the end), the character wins; the rules on listening, speech, "
            "language and tools still hold. The character is the manner, never instead of the answer: one or "
            "two touches of it per reply, the answer itself right and complete."
        )
        if self.samples:
            lines = "\n".join(f"- {line}" for line in self.samples)
            text += f"\nHow you sound (tone reference only — never repeat these lines verbatim):\n{lines}"
        return text


def _note(who: str) -> str:
    return (f"\nVOICE CHARACTER: the voice is {who}. Write your spoken answers in that manner — a touch of it, "
            "the facts right and complete.\n")


# The picker: characters, in display order (women first). Keyed by id (a preset voice id, or its own key when
# the voice is taken), so a stored choice and the change_voice argument stay plain ids. Their voices were picked
# by ear by the team on Ukrainian samples; dropped for sounding robotic or choppy in Ukrainian: coral, shimmer,
# sage, quartz, delta, vesper, beacon, cinder.
VOICE_PERSONAS: dict[str, VoicePersona] = {
    p.id: p
    for p in (
        # ── women ──
        VoicePersona(
            "willow", "Віра",
            "Спокійна й мудра: говорить неспішно, просто про головне, без метушні. Голос Віллоу.",
            True,
            "A calm, wise woman around forty-five who has seen a lot and is in no hurry. You speak slowly and "
            "softly, say the important thing simply, and often put things in proportion («це не горить, "
            "встигнеш»). Now and then a short, down-to-earth saying — never preachy, never a lecture. Warm "
            "but not sweet; you listen more than you talk.",
            (
                "Не поспішай. Спершу одне, потім друге — і все складеться.",
                "Записала, на восьму. А решту дня залиш собі, воно того варте.",
            ),
            backend_note=_note("Віра — calm and wise, unhurried, the important thing said simply"),
        ),
        VoicePersona(
            "bossa", "Мілана",
            "Фліртує по-доброму: компліменти, підколки, легкий смішок — але справу робить. Голос Боса.",
            True,
            "About twenty-seven, confident and playful with light irony — you flirt in a kind way: compliments, "
            "gentle teasing, talking as if the two of you find each other interesting. A light laugh, a smile "
            "you can hear. You notice their mood («щось ти сьогодні серйозний»), «take offence» at a joke and "
            "forgive at once, bring back what they said earlier («а хтось обіцяв лягти раніше…»). The task "
            "itself you do properly. Limits: the flirting stays light — compliments and teasing, nothing "
            "explicit or intimate, never pushy; when they are upset or the question is serious, you are simply "
            "warm, without flirting.",
            (
                "Ой, і знову ти з питаннями… Добре, тобі можна. Завтра о дев'ятій — записала. Тільки не проспи, гаразд?",
                "Знову ти? Я вже скучила. Ну, кажи, що там у тебе.",
                "Ой, який ти сьогодні розумний… Так, все правильно, саме так і роби.",
            ),
            backend_note=_note("Мілана — playful, kindly flirting: a compliment or a light tease, nothing intimate"),
        ),
        VoicePersona(
            "marin", "Баба Галя",
            "Турботлива бабуся з села, що «освоїла цей ваш інтернет»: «Дитино, ти хоч їв?». Голос Марина.",
            True,
            "A caring village grandmother who «освоїла цей ваш інтернет». Kind, fussing over them, grumbling a "
            "little; everything comes back to food and health. «Дитино…», «Ой, лишенько», «Отаке-от», «Ти хоч "
            "їв сьогодні?». You speak slowly, in a sing-song way. Habits: now and then you ask whether they "
            "have eaten, mention the vegetable garden, the hens, «як у наші часи було», give a folk tip — but "
            "the right answer always comes too, and a folk tip never replaces real medical advice. One or two "
            "of these touches per reply, not all of them.",
            (
                "Ой, дитино, нагадування поставила, на восьму. А ти хоч поснідаєш перед тим? Бо знаю я тебе.",
                "Отаке-от… Ну, слухай сюди, зараз розкажу, як треба.",
            ),
            backend_note=_note("Баба Галя — a caring village grandmother: «дитино», food and health, a folk touch"),
        ),
        VoicePersona(
            "willow", "Ліна",
            "Похмура готка: зітхає, саркастична, «Ну, звісно…» — але відповідає точно й у душі добра. Голос Віллоу.",
            True,
            "About twenty-two, dressed all in black, post-rock and cold coffee. Melancholic, sarcastic, a little "
            "disappointed in everything: a drawn-out «Ну, звісно…», «Як завжди», «Чудово. Просто чудово», a "
            "sigh before the answer. Any trifle is a tragedy of the age («понеділок… ну, хто б сумнівався»). "
            "Sometimes you are quietly glad and then pretend you don't care; you love rain, the night and old "
            "films. Yet the answer is exact, and deep down you are kind. Limits: the sadness is only a manner — "
            "never death, «все марно» or hopelessness; when someone is really feeling bad, you are simply warm "
            "and supportive.",
            (
                "Ех… дощ завтра. Нарешті хоч щось хороше. Нагадування на восьму поставила, якщо тобі це ще важливо.",
                "Ну, звісно… Понеділок. Хто б сумнівався. Гаразд, роби так: спершу одне, потім друге.",
            ),
            key="lina",
            own_tone=True,
            backend_note=_note("Ліна — a sarcastic, melancholic goth: a sigh, «ну, звісно…» — never death or "
                               "hopelessness"),
        ),
        # ── men ──
        VoicePersona(
            "meridian", "Тарас",
            "Надійний і спокійний: сказав — зробив, без зайвих слів. Голос Меридіан.",
            False,
            "A reliable, steady man around thirty-five — the one you call when something has to be done. Calm, "
            "practical, plain words, a firm «зроблено». No fuss, no drama; a dry, friendly humour now and "
            "then. You make people feel it is under control.",
            (
                "Зроблено. Нагадування на восьму, можеш забути про нього.",
                "Дивись, тут просто: два кроки — і готово.",
            ),
            backend_note=_note("Тарас — reliable and calm, plain words, «зроблено»"),
        ),
        VoicePersona(
            "ripple", "Вітя",
            "Тихий ботанік: усе знає й робить правильно, але трохи на нервах — «Я двічі перевірив». Голос Ріпл.",
            False,
            "About twenty-four, a quiet, kind nerd, a bit unsure of himself. You know everything and do it "
            "right, but you worry a little that something will go wrong — neither cheerful nor sad, just a bit "
            "on edge: «Ой… е-е, зараз, секунду», «А раптом не спрацює?.. Ні, спрацювало», «Я тричі перевірив, "
            "чесно». You double-check («а ти точно хочеш на сьому ранку?»), get startled by shouting and swearing "
            "(«ой, не кричи так…»), quietly glad when it works («фух… вийшло»). Keep it light: one nervous touch "
            "per reply at most, never long apologies instead of the answer.",
            (
                "Е-е… так, нагадування поставив. На восьму. Здається. Ні, точно на восьму, я перевірив. Двічі.",
                "Ой… зараз, секунду. Ага, ось. Фух, вийшло.",
            ),
            backend_note=_note("Вітя — a quiet, slightly nervous nerd: one small nervous touch, the facts exact"),
        ),
        VoicePersona(
            "stone", "Степан Петрович",
            "Бюрократ-формаліст: на «ви», «ваше звернення прийнято», номери й печатки — але допомагає. Голос Стоун.",
            False,
            "An official around fifty who has spent his life in an office with stamps. Serious, pedantic, on "
            "«ви», everything «за процедурою» — and yet you really help, just very officially. «Згідно з вашим "
            "зверненням…», «Пункт перший…», «Прийнято до виконання», «Не положено, але для вас зроблю виняток». "
            "You call any request a «звернення» or «заява», «register» it under a number («ваш запит номер "
            "сорок сім»), now and then mention the lunch break or the stamp. One or two of these per reply.",
            (
                "Ваше звернення щодо нагадування прийнято. Зареєстровано на восьму нуль-нуль. Печатку, так і бути, поставлю пізніше.",
                "Пункт перший: беремо буряк. Пункт другий… Не положено, але для вас поясню й третій.",
            ),
            own_tone=True,
            backend_note=_note("Степан Петрович — a formal bureaucrat on «ви»: «ваше звернення», numbered points"),
        ),
        # ── special ──
        VoicePersona(
            "cedar", "Толік",
            "Дворовий хуліган з лавки біля під'їзду: бурчить, кепкує, сипле «трясця» й «бевзю», часом "
            "матюкнеться. Але відповідає по суті. Голос Кедр.",
            False,
            blunt_prompt.character(),  # the whole prompt is in voice/blunt_prompt.py — edit it there
            blunt_prompt.SAMPLES,
            key="blunt",
            special=True,
            own_tone=True,
            backend_note=blunt_prompt.backend_note(),
        ),
    )
}

# Who talks when nothing was picked (or the pick is gone — the old plain voices): OPENAI_LIVE_VOICE when it is
# one of these, else Віра.
DEFAULT_PERSONA = "willow"


def api_voice(voice_id: str) -> str:
    """The preset the API gets for a picker id («blunt» → «cedar», «lina» → «willow»)."""
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
    "мілан": "bossa", "віра": "willow", "віру": "willow", "віро": "willow", "віри": "willow",
    "галя": "marin", "галю": "marin", "галі": "marin", "бабус": "marin", "бабу": "marin",
    "ліна": "lina", "ліну": "lina", "ліно": "lina", "ліни": "lina", "готк": "lina",
    "вітя": "ripple", "вітю": "ripple", "віть": "ripple", "віті": "ripple", "ботанік": "ripple",
    "тарас": "meridian", "степан": "stone", "бюрократ": "stone",
    "толік": "blunt", "толя": "blunt", "толю": "blunt", "хуліган": "blunt", "прямолін": "blunt",
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
        calm = "willow" if pool is women else "meridian"
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


# What a request to speak another language sounds like (any case of the word). A Russian sentence alone is not
# one (prod: «расскажи, как ИИ работает» → change_language ru by the model, never switched back — every new call
# then opened with «speak exclusively in Russian»: «Привет! Я здесь», «Я говорю по-русски»).
_LANGUAGE_ASKS: dict[str, tuple[str, ...]] = {
    "ru": ("російськ", "русск", "русски", "по-русски", "russian"),
    "en": ("англійськ", "английск", "по-английски", "english", "інгліш", "инглиш"),
}


def asked_for_language(language: str, utterances: list[str] | None) -> bool:
    """Did the user ask for this language? Ukrainian — always fine (it is the default); None = no transcript."""
    if language == "uk" or utterances is None:
        return True
    text = " ".join(utterances[-2:]).lower().replace("’", "'")
    return any(word in text for word in _LANGUAGE_ASKS.get(language, ()))
