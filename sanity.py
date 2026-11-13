"""
Проверка формата ответа перед тем, как считать задачу решённой.

Отлавливаем отловить явные проблемы: пустой ответ, 
слишком длинный, markdown-обёртку, отказ модели,
несоответствие ожидаемому типу (int / float / DONE).
"""
import re

_INT     = re.compile(r"^-?\d+$")
_FLOAT   = re.compile(r"^-?\d+(\.\d+)?$")
_FENCE   = re.compile(r"^```[\s\S]*```$", re.S)
_REFUSAL = re.compile(
    r"^(i cannot|i don'?t know|i'?m sorry|unable to|не могу|"
    r"error|failed|unknown|n/a)\b", re.I)


def check_format(task: dict, answer: str) -> tuple[bool, str]:
    """
    Проверить, что 'answer' похож на правильный ответ для 'task'.

    Возвращает (ok, reason):
        ok     — True если формат приемлем
        reason — человекочитаемая причина отказа (для лога и фидбека агенту)
    """
    s = (answer or "").strip()

    # Базовые проверки на пустоту и мусор.
    if not s:                            return False, "empty"
    if len(s) > 500:                     return False, f"too long ({len(s)} chars)"
    if _REFUSAL.search(s):               return False, f"refusal-like: {s[:50]!r}"
    if _FENCE.match(s):                  return False, "raw markdown fence"

    # Проверка под конкретный тип (из поля check в task.json).
    kind = (task.get("check") or "").lower()
    if kind == "exact_int"   and not _INT.match(s):    return False, f"expected int, got {s[:30]!r}"
    if kind == "exact_float" and not _FLOAT.match(s):  return False, f"expected float, got {s[:30]!r}"
    if kind == "exact_str"   and len(s) > 200:         return False, f"exact_str too long ({len(s)} chars)"
    if kind == "unittest"    and s.upper() != "DONE":  return False, f"expected DONE, got {s[:30]!r}"
    return True, "ok"