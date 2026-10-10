"""How much care a question deserves, decided before it is sent.

Three answers, each changing only what the vendor is asked for — never what the
student may ask, what they are charged per token, or which model answers:

* ``LIGHT`` — a greeting, a thank-you, a goodbye. Sent without the follow-up
  instruction: three suggested questions after "thanks!" are noise, and the
  instruction is input tokens the reply does not need.
* ``DEEP`` — maths, science calculations and code: where a wrong answer is
  both likeliest and costliest. Reasoning models are given room to think
  harder; sampling models are made near-deterministic, without the repetition
  penalties that punish a digit or a variable name for appearing twice.
* ``STANDARD`` — everything else, treated exactly as before.

Deliberately a handful of patterns rather than a model call: it runs on every
message, must cost nothing and add no latency, and a miss only means the
default treatment — never a refusal.
"""
from __future__ import annotations

import re

LIGHT = "light"
STANDARD = "standard"
DEEP = "deep"

#: What a reasoning model is allowed to spend on a DEEP question, thinking
#: included. Below this, "medium" effort risks thinking through the whole
#: budget and returning nothing, so the request stays at "low".
DEEP_REASONING_BUDGET = 4096

#: Sampling temperature for a DEEP question on a model that samples. Maths and
#: code have one right answer; creativity there is just variance.
DEEP_TEMPERATURE = 0.2

# -- light --------------------------------------------------------------------
_LIGHT = re.compile(
    r"^(?:"
    r"hi+|hey+|hello+|hiya|yo|howdy|greetings|good (?:morning|afternoon|evening|night|day)|"
    r"thanks?(?: you)?(?: (?:so|very) much| a lot)?|thx|ty|tysm|many thanks|"
    r"ok(?:ay)?|got it|cool|nice|great|awesome|perfect|"
    r"bye|goodbye|see (?:you|ya)(?: later)?|cheers|"
    # Khmer: hello (casual and formal), thank you (and "very much").
    r"សួស្តី|ជំរាបសួរ|អរគុណ(?:ច្រើន)?"
    r")(?: (?:there|again|bro|sir|teacher|everyone|all|so much))?$"
)
_LIGHT_STRIP = re.compile(r"[\s!?.,;:~…'\"()*_\-☀-➿\U0001F000-\U0001FAFF]+")

# -- deep: code ----------------------------------------------------------------
_CODE_SYNTAX = re.compile(
    r"```"
    r"|\bdef \w+\s*\("
    r"|\bfunction\s*\w*\s*\("
    r"|=>"
    r"|#include\b"
    r"|\bpublic\s+(?:static\s+)?(?:void|class|int|String)\b"
    r"|console\.log|System\.out"
    r"|\bprintf?\s*\("
    r"|^\s*(?:import|from)\s+[\w.]+"
    r"|\bSELECT\b[\s\S]{1,200}?\bFROM\b"
    r"|Traceback \(most recent"
    r"|\b\w+(?:Error|Exception)\b"
    r"|[{};]\s*$",
    re.MULTILINE,
)
_CODE_WORDS = re.compile(
    r"\b(?:code|coding|program(?:me)?|script|algorithm|debug(?:ging)?|bugs?|"
    r"compil(?:e|er|ing)|syntax|regex|sql|pseudocode)\b",
    re.IGNORECASE,
)

# -- deep: maths and science -----------------------------------------------------
_MATH_SYMBOLS = re.compile(
    r"[√∫∑∏π≤≥≠±÷×²³ⁿ∞∆Δθ]"
    r"|\d\s*[+*/^=×÷]\s*[\d(]"          # 3+4, 12/5, 2^8, 7 = 7
    r"|\d\s+-\s+\d"                      # 9 - 4 (not 2-3, not a date)
    r"|\b\d*[a-z]\s*\^\s*\d"             # x^2, 3x^2
    r"|\b[a-z]\s*[+=]\s*-?\d"            # x = 5, x+3
    r"|\d\s*%"                           # 15%
    r"|\\(?:frac|sqrt|int|sum)\b",       # pasted LaTeX
    re.IGNORECASE,
)
_MATH_WORDS = re.compile(
    r"\b(?:solve|calculate|compute|evaluate|simplify|factori[sz]e|differentiate|derivative|"
    r"integrate|integral|prove|proof|equations?|inequalit(?:y|ies)|probability|percentage|"
    r"quadratic|polynomial|logarithm|trigonometry|sine|cosine|matri(?:x|ces)|vectors?|"
    r"velocity|acceleration|momentum|molarity|stoichiometry|"
    r"find the (?:value|area|volume|perimeter|length|angle|speed|mass|force|roots?))\b",
    re.IGNORECASE,
)


def classify(text: str) -> str:
    """``LIGHT``, ``STANDARD`` or ``DEEP`` for the student's message."""
    text = (text or "").strip()
    if not text:
        return STANDARD
    if len(text) <= 60:
        bare = _LIGHT_STRIP.sub(" ", text.lower()).strip()
        if bare and _LIGHT.match(bare):
            return LIGHT
    if _CODE_SYNTAX.search(text) or _CODE_WORDS.search(text):
        return DEEP
    if _MATH_SYMBOLS.search(text) or _MATH_WORDS.search(text):
        return DEEP
    return STANDARD
