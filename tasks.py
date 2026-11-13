
"""
Чтение задач из папки, которую дали организаторы.

Для каждой задачи вытаскиваем task.json, разбираемся, где её рабочий каталог,
и прикидываем, сколько в инструкции токенов. Ещё пытаемся понять тип задачи,
если в файле не указан явно.
"""

import os
import json
from collections import defaultdict

from file_log import logging
from classifier import infer_type

log = logging.getLogger()


def count_tokens(t: str) -> int:
    """
    Грубая оценка числа токенов в строке.
    Считаем что 3 символа примерно составляет 1 токен
    (Рассматривался и другой вариант 1 слово = 1 токен).
    """
    return max(1, len(t or "") // 3)


def _read_json(p: str):
    """Прочитать JSON-файл. Если битый или его нет — вернуть None."""
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"failed to read {p}: {e}")
        return None


def load_tasks(dir_path: str):
    """
    Обойти каталог dir_path и загрузить все задачи.

    Возвращает два параметра:
        groups — задачи, сгруппированные по типу (dict task_type -> [task, ...])
        types  — просто множество всех типов (для лога)

    К каждой задаче добавляем несколько служебных полей:
        _dir          — полный путь до папки задачи
        _cwd          — где агенту работать (workspace/ если есть, иначе корень)
        _instr_tokens — сколько токенов в инструкции (по грубой оценке)
        task_type     — перезаписываем результатом классификатора
    """
    groups = defaultdict(list)
    types = set()
    total = 0

    # Идём по подкаталогам — каждая задача это отдельная папка.
    for name in sorted(os.listdir(dir_path)):
        full = os.path.join(dir_path, name)
        if not os.path.isdir(full):
            continue

        task = _read_json(os.path.join(full, "task.json"))
        if not task:
            # Не читаемый task.json — пропускаем.
            continue

        instr    = task.get("instruction", "")
        declared = task.get("task_type", "unknown")
        # Если тип не указан — пробуем угадать по словам в инструкции.
        inferred = infer_type(instr, declared)
        if inferred != declared:
            log.info(f"{task.get('task_id')}: type {declared!r} -> {inferred!r}")

        # Указываем где агенту работать: по умолчанию workspace/, иначе корень.
        ws = os.path.join(full, "workspace")
        task["_dir"]          = full
        task["_cwd"]          = ws if os.path.isdir(ws) else full
        task["_instr_tokens"] = count_tokens(instr)
        task["task_type"]     = inferred

        groups[inferred].append(task)
        types.add(inferred)
        total += 1

    log.info(f"{total} tasks loaded. Types: {types}")
    return dict(groups), types