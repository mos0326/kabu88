import pytest

from kabu.guard import scrub, violations

FORBIDDEN = [
    "来週には反発しそうです。",
    "短期的に上昇する可能性が高い。",
    "目標株価は3,500円とします。",
    "4,000円を目指す展開。",
    "今が買い時です。",
    "この銘柄は買うべきだ。",
    "数週間で急騰が見込まれる。",
    "損切りラインは2,800円。",
]
ALLOWED = [
    "信用買い残が平均出来高の12日分あり、上値が重くなりやすい需給。",
    "RSIは78で短期的に買われすぎの水準。",
    "短期的な下落リスクとして決算発表がある。",
    "データセンター投資は今後数年で拡大する見通しと会社は説明している。",
    "株価は1年で+120%上昇しており、期待の織り込みが進んでいる。",
    "2027年3月期の会社予想は営業増益20%。",
]


@pytest.mark.parametrize("text", FORBIDDEN)
def test_forbidden_detected(text):
    assert violations(text)


@pytest.mark.parametrize("text", ALLOWED)
def test_allowed_kept(text):
    assert not violations(text)


def test_scrub_removes_only_offending_sentences():
    obj = {"code": "6857", "summary": "需要は拡大中。来週は上昇しそう。リスクは為替。", "items": ["今が買い時", "PBRは高い"]}
    cleaned, log = scrub(obj)
    assert cleaned == {"code": "6857", "summary": "需要は拡大中。リスクは為替。", "items": ["PBRは高い"]}
    assert [x["field"] for x in log] == ["summary", "items[0]"]


def test_scrub_marks_fully_removed_text():
    cleaned, _ = scrub({"note": "目標株価は5,000円。"})
    assert cleaned["note"].startswith("（短期予測")
