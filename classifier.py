"""
Классификатор задач по типу — используется, если task.json не задаёт тип.

Простое сопоставление по ключевым словам.
"""

import re

# Порядок важен: первое совпадение побеждает.
_TYPE_HINTS = [
    ("coding", re.compile(
        r"\b(test|unittest|pytest|fix|bug|refactor|implement|mylib|function|class)\b",
        re.I)),
    ("data_analysis", re.compile(
        r"\b(csv|pandas|dataframe|column|row|units|sales|mean|sum|count|group|median)\b",
        re.I)),
    ("math", re.compile(
        r"\b(compute|calculate|solve|how many|integral|equation|derivative|round)\b",
        re.I)),
    ("search", re.compile(
        r"\b(find|grep|knowledge base|kb/|in which|what year|which city|look up|who|where)\b",
        re.I)),
]


def infer_type(instruction: str, declared: str) -> str:
    """
    Вернуть тип задачи.
    Если организаторы указали тип явно (не "unknown") — доверяем.
    Иначе гадаем по словам в инструкции. Если ничего не нашли — "unknown".
    """
    if declared and declared != "unknown":
        return declared
    for tt, rx in _TYPE_HINTS:
        if rx.search(instruction or ""):
            return tt
    return "unknown"