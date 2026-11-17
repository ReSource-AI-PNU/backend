"""한국어 근거 검색의 어휘 순위: BM25F(필드 가중) + 한국어 낱말 나누기 + 분야 동의어.

    index = LexicalIndex(evidence_items)
    index.rank("필름을 받으려면 업체에 어떤 허가 품목이 있어야 하나") -> [(근거 ID, 점수), ...]

- 낱말 나누기(terms): 영문·숫자 낱말, "제25조"·"제16조의13" 같은 조 번호, 한글 덩어리(끝의 조사와
  어미를 뗀 것), 한글 덩어리의 글자 2-gram. 띄어쓰기가 달라도("공동처리"·"공동 처리") 2-gram이 맞는다.
- 필드(BM25F, Robertson·Zaragoza 2009): 근거 이름(label)과 원문(excerpt), 출처(source), 근거 ID를
  따로 길이 정규화하고 가중해 합친 뒤 한 번 포화시킨다. 법령은 조 제목이 label에 있다.
- 질의 넓히기: 담당자가 쓰는 말과 법령이 쓰는 말을 잇는 작은 동의어 표(SYNONYMS). 넓힌 말은 원래
  말보다 낮은 무게로 더한다. 표는 개발 질의(dev)만 보고 만들었고 시험 질의는 보지 않았다.
- 점수가 같으면 근거 ID 순서다. 난수와 시각을 쓰지 않는다.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

K1 = 1.2
# (필드, 무게, 길이 정규화 b). 조 제목과 근거 이름이 원문보다 무겁다
FIELDS: tuple[tuple[str, float, float], ...] = (
    ("label", 3.0, 0.3),
    ("excerpt", 1.0, 0.75),
    ("source", 0.5, 0.3),
    ("id", 1.0, 0.3),
)
# 질의 낱말의 종류별 무게. 2-gram은 낱말과 겹쳐 세지므로 낮게 둔다
WORD_WEIGHT = 1.0
GRAM_WEIGHT = 0.6
EXPANSION_WEIGHT = 0.8
MAX_EXCERPT = 6000  # 아주 긴 조(부칙, 별표)는 앞부분만 본다

_WORD = re.compile(r"제\d+조(?:의\d+)?|[0-9a-z]+|[가-힣]+")
# 한글 낱말 끝에서 떼는 조사·어미(긴 것부터)
_ENDINGS = tuple(
    sorted(
        (
            "에서는",
            "으로는",
            "에게서",
            "이라도",
            "까지는",
            "부터는",
            "에서",
            "으로",
            "에게",
            "께서",
            "까지",
            "부터",
            "보다",
            "처럼",
            "이나",
            "이며",
            "하고",
            "에는",
            "와는",
            "과는",
            "하려면",
            "하려는",
            "하면",
            "해야",
            "하는",
            "되는",
            "되나",
            "하나",
            "인가",
            "인지",
            "려면",
            "으면",
            "면",
            "은",
            "는",
            "이",
            "가",
            "을",
            "를",
            "의",
            "에",
            "로",
            "와",
            "과",
            "도",
            "만",
            "나",
        ),
        key=len,
        reverse=True,
    )
)

# 담당자의 말 → 법령·근거의 말(synonyms.ko.json). 한 방향으로만 넓힌다
SYNONYMS_FILE = Path(__file__).with_name("synonyms.ko.json")


def load_synonyms(path: Path = SYNONYMS_FILE) -> dict[str, tuple[str, ...]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): tuple(str(x) for x in v) for k, v in data["synonyms"].items()}


SYNONYMS: dict[str, tuple[str, ...]] = load_synonyms()


def _plain(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).lower()


# 한 글자 낱말에서도 뗄 수 있는 조사(받침 있는 낱말 뒤의 조사와 두 글자 조사). "법에서" → "법",
# "뜻은" → "뜻". "이·가·과"는 두 글자 낱말의 끝 글자와 헷갈려("사이", "결과") 떼지 않는다
_SHORT_OK = ("에서", "으로", "에게", "에는", "은", "는", "을", "를")
# 묻는 말과 서술 어미. 질의에서 빼도 뜻이 남는다
STOPWORDS = frozenset(
    (
        "무엇",
        "무엇인",
        "무엇인가",
        "어떤",
        "어떻게",
        "어디",
        "언제",
        "얼마",
        "얼마인",
        "얼마인가",
        "하나",
        "인가",
        "되나",
        "있나",
        "있어야",
        "해야",
        "하는",
        "필요한",
        "필요한가",
        "되",
        "할",
        "수",
        "있는",
        "때",
        "때도",
        "말하",
        "당하",
    )
)


def stem(word: str) -> str:
    """한글 낱말 끝의 조사·어미를 하나 뗀다. 남는 것이 두 글자보다 짧으면 _SHORT_OK만 뗀다."""
    for ending in _ENDINGS:
        if not word.endswith(ending) or len(word) == len(ending):
            continue
        rest = len(word) - len(ending)
        if rest >= 2 or ending in _SHORT_OK:
            return word[: -len(ending)]
    return word


def _raw_words(text: Any) -> list[str]:
    return _WORD.findall(_plain(text))


def _is_article(token: str) -> bool:
    return bool(re.fullmatch(r"제\d+조(?:의\d+)?", token))


def words(text: Any) -> list[str]:
    """낱말(조사를 뗀 한글 덩어리, 영문·숫자 낱말, 조 번호)."""
    out = []
    for token in _raw_words(text):
        if token[0] >= "가" and not _is_article(token):
            token = stem(token)
        out.append(token)
    return out


def grams(word: str) -> list[str]:
    if not re.fullmatch(r"[가-힣]+", word) or len(word) < 2:
        return []
    return [word[i : i + 2] for i in range(len(word) - 1)]


def terms(text: Any) -> list[str]:
    """문서 쪽 낱말: 낱말과 한글 2-gram(앞에 #를 붙여 낱말과 나눈다)."""
    out: list[str] = []
    for word in words(text):
        out.append(word)
        out.extend("#" + g for g in grams(word))
    return out


def query_terms(query: str, *, expand: bool = True) -> dict[str, float]:
    """질의 낱말과 무게. 넓힌 말은 EXPANSION_WEIGHT를 곱한다. 같은 낱말은 큰 무게 하나만."""
    weights: dict[str, float] = {}

    def add(term: str, weight: float) -> None:
        weights[term] = max(weights.get(term, 0.0), weight)

    raw = _raw_words(query)
    found = [w if _is_article(w) else stem(w) for w in raw]
    for word in found:
        if word in STOPWORDS:
            continue
        add(word, WORD_WEIGHT)
        for gram in grams(word):
            add("#" + gram, GRAM_WEIGHT)
    if expand:
        # 동의어는 조사를 떼기 전 낱말에서도 찾는다(고유 이름의 끝 글자 "로"는 조사가 아니다)
        for word in {*raw, *found}:
            for key, more in SYNONYMS.items():
                if key == word or (len(key) >= 2 and key in word):
                    for extra in more:
                        for term in words(extra):
                            add(term, WORD_WEIGHT * EXPANSION_WEIGHT)
                            for gram in grams(term):
                                add("#" + gram, GRAM_WEIGHT * EXPANSION_WEIGHT)
    return weights


def _field_text(item: Any, name: str) -> str:
    value = item.get(name) if isinstance(item, dict) else getattr(item, name, "")
    text = str(value or "")
    return text[:MAX_EXCERPT] if name == "excerpt" else text


def _doc_key(item: Any) -> tuple:
    texts = tuple(_field_text(item, name) for name, _, _ in FIELDS)
    version = item.get("version") if isinstance(item, dict) else getattr(item, "version", None)
    return (version, *texts)


# 근거 하나의 필드별 낱말 수. 같은 근거(같은 판, 같은 글)는 다시 나누지 않는다
_DOC_CACHE: dict[tuple, list[Counter[str]]] = {}
_DOC_CACHE_LIMIT = 50_000
# 같은 근거 목록이면 색인을 다시 만들지 않는다(최근 것 몇 개만)
_INDEX_CACHE: dict[tuple, LexicalIndex] = {}
_INDEX_CACHE_LIMIT = 8


def _doc_terms(item: Any, key: tuple) -> list[Counter[str]]:
    found = _DOC_CACHE.get(key)
    if found is None:
        if len(_DOC_CACHE) >= _DOC_CACHE_LIMIT:
            _DOC_CACHE.clear()
        found = [Counter(terms(text)) for text in key[1:]]
        _DOC_CACHE[key] = found
    return found


class LexicalIndex:
    """근거 목록 위의 BM25F 색인. 만든 뒤에는 바뀌지 않는다."""

    def __init__(self, items: Iterable[Any]) -> None:
        self.ids: list[str] = []
        self.fields: list[list[Counter[str]]] = []
        self.postings: dict[str, list[int]] = {}
        lengths = [0.0] * len(FIELDS)
        doc_lengths: list[list[int]] = []
        for position, item in enumerate(items):
            evidence_id = item["id"] if isinstance(item, dict) else item.id
            per = _doc_terms(item, _doc_key(item))
            self.ids.append(str(evidence_id))
            self.fields.append(per)
            sizes = [sum(counts.values()) for counts in per]
            doc_lengths.append(sizes)
            for i, size in enumerate(sizes):
                lengths[i] += size
            for term in set().union(*per):
                self.postings.setdefault(term, []).append(position)
        docs = len(self.ids)
        self.size = docs
        self.average = [max(total / docs, 1.0) if docs else 1.0 for total in lengths]
        self.norms = [
            [
                1 - b + b * (size / self.average[i])
                for i, ((_, _, b), size) in enumerate(zip(FIELDS, sizes, strict=True))
            ]
            for sizes in doc_lengths
        ]
        self.idf = {
            term: math.log(1 + (docs - len(found) + 0.5) / (len(found) + 0.5))
            for term, found in self.postings.items()
        }

    @classmethod
    def cached(cls, items: Sequence[Any]) -> LexicalIndex:
        """같은 근거 목록(ID와 판과 글이 같다)이면 앞서 만든 색인을 다시 쓴다."""
        key = tuple(
            ((item["id"] if isinstance(item, dict) else item.id), _doc_key(item)) for item in items
        )
        found = _INDEX_CACHE.get(key)
        if found is None:
            if len(_INDEX_CACHE) >= _INDEX_CACHE_LIMIT:
                _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
            found = _INDEX_CACHE[key] = cls(items)
        return found

    def score(self, position: int, weights: dict[str, float]) -> float:
        per, norms = self.fields[position], self.norms[position]
        total = 0.0
        for term, weight in weights.items():
            idf = self.idf.get(term)
            if idf is None:
                continue
            tf = sum(
                boost * per[i][term] / norms[i]
                for i, (_, boost, _) in enumerate(FIELDS)
                if per[i][term]
            )
            if tf:
                total += weight * idf * tf / (K1 + tf)
        return total

    def ceiling(self, weights: dict[str, float]) -> float:
        """질의 낱말이 모두 아주 많이 나오는 문서의 점수(포화의 끝). 점수를 0~1로 맞출 때 쓴다."""
        return sum(w * self.idf.get(t, 0.0) for t, w in weights.items())

    def rank(self, query: str, *, expand: bool = True) -> list[tuple[str, float]]:
        """점수는 질의마다 ceiling으로 나눈 0~1 값이다(순위는 BM25F 그대로). 질의끼리 견줄 수
        있어 '답 없음' 문턱값을 하나로 정할 수 있다."""
        weights = query_terms(query, expand=expand)
        top = self.ceiling(weights)
        if not weights or top <= 0:
            return []
        candidates = sorted({p for term in weights for p in self.postings.get(term, ())})
        scored = []
        for position in candidates:
            value = round(self.score(position, weights) / top, 6)
            if value > 0:
                scored.append((-value, self.ids[position]))
        scored.sort()
        return [(evidence_id, -negative) for negative, evidence_id in scored]


def rrf(rankings: Sequence[Sequence[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion(Cormack et al. 2009). 순위 목록들을 하나로 합친다."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, evidence_id in enumerate(ranking, 1):
            scores[evidence_id] = scores.get(evidence_id, 0.0) + 1.0 / (k + position)
    return sorted(((e, round(s, 9)) for e, s in scores.items()), key=lambda p: (-p[1], p[0]))
