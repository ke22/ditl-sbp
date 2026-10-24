"""evidence-answer-kind-open-coding: what an evidence answer can support.

A respondent who says 「可以。」 to 「CP值是OK的？」 affirmed the interviewer's
proposition; a respondent who says 「不會」 to 「會不會影響購買？」 answered the
question without giving a reason. Neither is the respondent's own judgment.
The kind is read from the answer and its `question_context` only, computed on
read and never written back to evidence. The rules decide only clear cases;
anything else is `unconfirmed`.
"""
from __future__ import annotations

import re

SUBSTANTIVE = "substantive_statement"
BRIEF_AGREEMENT = "brief_agreement"
BRIEF_ANSWER = "brief_answer"
PROCEDURAL = "procedural"
UNCONFIRMED = "unconfirmed"
KINDS = (SUBSTANTIVE, BRIEF_AGREEMENT, BRIEF_ANSWER, PROCEDURAL, UNCONFIRMED)

# Removed longest-first, negatives before affirmatives (不會 before 會).
NEGATIVE_WORDS = ("不太會", "不太会", "不會", "不会", "沒有", "没有", "不是", "不要", "不用", "不好",
                  "還好", "还好", "沒", "没", "不")
AFFIRMATIVE_WORDS = ("對對對", "对对对", "對對", "对对", "沒錯", "没错", "可以", "是的", "對", "对",
                     "嗯", "恩", "是", "好", "OK", "ok", "Ok", "有", "會", "会", "要")
_PARTICLES = "的啊吧呀喔哦欸耶啦呢嘛哈"
_STRIP_RE = re.compile(r"[\s　，。、．,.!！?？~～…:：;；「」『』\"'（）()\-—]")

_PROCEDURAL_RE = re.compile(r"錄音|录音|錄影|录影|受訪|受访|訪談|访谈|打擾|打扰|耽誤|耽误|問你幾個|问你几个|拍照")
_TAG_RE = re.compile(r"(對不對|对不对|是不是|對吧|对吧|是吧)$")
_TWO_SIDED_RE = re.compile(r"還是|还是|或是|是否|([一-鿿])[不沒没]\1")
_QUESTION_END_RE = re.compile(r"(嗎|吗|\?|？)$")


def _bare_polarity(text: str) -> str | None:
    """'negative' / 'affirmative' when the answer is only short answer words,
    'empty' when it is only particles or punctuation, else None (the respondent
    said something more)."""
    rest = _STRIP_RE.sub("", text)
    negative = False
    for word in NEGATIVE_WORDS:
        if word in rest:
            negative = True
            rest = rest.replace(word, "")
    affirmative = False
    for word in AFFIRMATIVE_WORDS:
        if word in rest:
            affirmative = True
            rest = rest.replace(word, "")
    if rest.translate({ord(ch): None for ch in _PARTICLES}):
        return None
    if negative:
        return "negative"
    return "affirmative" if affirmative else "empty"


def classify(evidence: dict) -> str:
    """One of KINDS for one evidence item; see the module docstring."""
    text = str(evidence.get("text") or "")
    polarity = _bare_polarity(text)
    if polarity is None:
        return SUBSTANTIVE
    if polarity == "empty":
        return UNCONFIRMED
    question = re.sub(r"\s", "", str(evidence.get("question_context") or ""))
    if not question:
        return UNCONFIRMED
    if _PROCEDURAL_RE.search(question):
        return PROCEDURAL
    core = question.rstrip("?？。.!！~～")
    if _TAG_RE.search(core):
        return UNCONFIRMED
    two_sided = bool(_TWO_SIDED_RE.search(question))
    asks = bool(_QUESTION_END_RE.search(question))
    if polarity == "negative":
        return BRIEF_ANSWER if asks or two_sided else UNCONFIRMED
    if two_sided:
        return BRIEF_ANSWER
    return BRIEF_AGREEMENT if asks else UNCONFIRMED


def label(evidence: list) -> dict:
    """Evidence id -> answer kind, for every item that has an id."""
    return {e["id"]: classify(e) for e in evidence if isinstance(e, dict) and e.get("id")}
