"""
Preflight — быстрая проверка задачи на выполнимость ДО запуска агента.

Идея: если в инструкции упоминается файл, которого нет, или в данных нет
нужного столбца, или в математической задаче не хватает числа — незачем
тратить токены на попытки. Такие задачи помечаем skipped.

Каждая функция возвращает (feasible, reason, extra):
    feasible — True если задачу стоит пробовать
    reason   — почему решили, что невыполнима (для лога)
    extra    — доп. данные для агента (например, вывод первого прогона тестов)
"""
import re
from tools import run_bash

# ── Регулярки для извлечения сущностей из инструкции ─────────────

# Упоминания файлов: 'mylib.py', 'sales.csv', и т.п.
_FILE_REF = re.compile(
    r"'([\w./-]+\.(?:csv|tsv|txt|py|json|md|yaml|yml|xlsx?|log))'"
    r"|\b([\w./-]+\.(?:csv|tsv|txt|py|json|md|yaml|yml|xlsx?|log))\b"
)
# Упоминания каталогов: kb/, data/, src/, ...
_DIR_REF  = re.compile(
    r"'?((?:kb|data|src|tests?|lib|input|docs)/[\w./-]*)'?"
    r"|\bthe\s+directory\s+'?([\w./-]+/?)\'?"
)
# Имена собственные (два слова с большой буквы) — для search-задач.
_NAME_REF = re.compile(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b")
# Ожидаемые столбцы CSV: "columns A, B, C."
_COLS_REF = re.compile(r"columns?\s+([\w,\s]+?)(?:\.|\(|$)", re.I)
# Инструкции запустить тест: run 'python3 test_x.py'
_RUN_PY   = re.compile(r"run\s+'?python3\s+([\w./-]+\.py)'?", re.I)
# Инструкции исправить файл: fix the bug in 'foo.py'
_FIX_PY   = re.compile(r"fix\s+(?:the\s+)?(?:bugs?\s+in\s+)?'?([\w./-]+\.py)'?", re.I)
# Символьная переменная в math-задаче: "starts with X", "equals X"
_MATH_VAR = re.compile(r"\b(?:starts?\s+with|has|equals?|is)\s+([A-Z])\b(?!\w)")

# Файлы, которые агент создаёт сам — их отсутствие НЕ означает невыполнимость.
_OUTPUT_NAMES = {"answer.txt"}


def _is_output_file(name: str) -> bool:
    """Проверить, что это файл-ответ (не требуется как вход)."""
    return name.rsplit("/", 1)[-1].lower() in _OUTPUT_NAMES


def _files(instr: str) -> set[str]:
    """Извлечь из инструкции упоминания входных файлов (без answer.txt)."""
    out = set()
    for m in _FILE_REF.finditer(instr):
        f = m.group(1) or m.group(2)
        if f and not _is_output_file(f):
            out.add(f)
    return out


def _dirs(instr: str) -> set[str]:
    """Извлечь упоминания каталогов."""
    out = set()
    for m in _DIR_REF.finditer(instr):
        d = m.group(1) or m.group(2)
        if d:
            out.add(d.rstrip("/"))
    return out


def _exists(cwd, p):
    """Проверить, что путь существует (файл или каталог)."""
    return run_bash(cwd, f"test -e {p!r}", timeout=5)["exit"] == 0


def _find(cwd, name):
    """
    Найти файл по имени в подкаталогах (до глубины 4).
    Возвращает относительный путь или пустую строку.
    """
    base = name.rsplit("/", 1)[-1]
    r = run_bash(cwd, f"find . -maxdepth 4 -name {base!r} 2>/dev/null | head -1",
                 timeout=10)
    return r["stdout"].strip()


# ── SEARCH ──────────────────────────────────────────────────────
def preflight_search(cwd, instr):
    """
    Проверка для search-задач.

    Проверяем:
    1. Все упомянутые каталоги существуют и не пустые.
    2. Все упомянутые файлы существуют (или находятся по имени).
    3. Каждое "имя собственное" из инструкции реально встречается
       в каком-либо файле (grep -ril).
    """
    for d in _dirs(instr):
        if not _exists(cwd, d):
            return False, f"missing directory: {d}"
        if not run_bash(cwd, f"ls -1 {d!r} 2>/dev/null | head -1",
                        timeout=5)["stdout"].strip():
            return False, f"empty directory: {d}"

    for f in _files(instr):
        if not (_exists(cwd, f) or _find(cwd, f)):
            return False, f"missing file: {f}"

    for name in set(_NAME_REF.findall(instr)):
        # Сначала ищем полное имя, потом только первое слово (фамилию).
        if run_bash(cwd, f"grep -ril -- {name!r} . 2>/dev/null | head -1",
                    timeout=20)["stdout"].strip():
            continue
        first = name.split()[0]
        if run_bash(cwd, f"grep -ril -- {first!r} . 2>/dev/null | head -1",
                    timeout=20)["stdout"].strip():
            continue
        return False, f"entity not found: {name}"

    return True, "ok"


# ── DATA ANALYSIS ─────────────────────────────────────────────────
def preflight_data(cwd, instr):
    """
    Проверка для data_analysis-задач.

    Проверяем:
    1. Есть хотя бы один csv/tsv файл (из инструкции или в cwd).
    2. Файл читается и в нём есть строки данных.
    3. Если инструкция упоминает столбцы — они реально есть в заголовке.
    4. Если инструкция упоминает строковые значения в кавычках —
       хотя бы одно встречается в данных.
    """
    files = [f for f in _files(instr) if f.endswith((".csv", ".tsv"))]
    if not files:
        r = run_bash(cwd, "ls -1 *.csv 2>/dev/null | head -5", timeout=5)
        files = [x.strip() for x in r["stdout"].splitlines() if x.strip()]
        if not files:
            return False, "no csv/tsv files in cwd"

    for f in files:
        if not (_exists(cwd, f) or _find(cwd, f)):
            return False, f"missing data file: {f}"

        # Читаем заголовок.
        r = run_bash(cwd, f"head -1 {f!r}", timeout=5)
        if r["exit"] != 0 or not r["stdout"].strip():
            return False, f"empty or unreadable: {f}"

        # Проверка ожидаемых столбцов (если инструкция их перечисляет).
        cols_m = _COLS_REF.search(instr)
        if cols_m:
            header = {c.strip().lower()
                      for c in r["stdout"].strip().split(",") if c.strip()}
            expected = [c.strip().lower()
                        for c in cols_m.group(1).split(",") if c.strip()]
            missing = [c for c in expected if c not in header]
            if missing:
                return False, f"missing columns {missing} in {f}"

        # Должна быть хотя бы одна строка данных.
        r = run_bash(cwd, f"wc -l < {f!r}", timeout=5)
        try:
            n = int(r["stdout"].strip())
        except Exception:
            n = 0
        if n < 2:
            return False, f"{f} has no data rows"

        # Проверка строковых значений из инструкции.
        # Пропускаем значения с / или \.
        for val in re.findall(r"'([^']{2,40})'", instr):
            if any(c in val for c in "/\\"):
                continue
            r = run_bash(cwd, f"grep -c -- {val!r} {f!r} 2>/dev/null || true",
                         timeout=5)
            try:
                cnt = int(r["stdout"].strip() or "0")
            except Exception:
                cnt = 0
            if cnt == 0:
                return False, f"value {val!r} not found in {f}"

    return True, "ok"


# ── CODING ────────────────────────────────────────────────────────
# Признаки "сломано на уровне инфраструктуры": если функция не определена,
# её надо реализовать — иначе тесты не пройдут без правки кода.
_IMPORT_MISSING = re.compile(
    r"(ImportError|ModuleNotFoundError):\s*cannot import name\s+['\"](\w+)['\"]"
    r"|NameError:\s*name\s+['\"](\w+)['\"]\s+is not defined")
_ASSERT_ERR = re.compile(r"AssertionError")


def preflight_coding(cwd, instr):
    """
    Проверка для coding-задач.

    Что делаем:
    1. Находим test-файл (из инструкции или test_*.py).
    2. Находим src-файл (из инструкции "fix X").
    3. Проверяем, что они существуют.
    4. Если тесты уже зелёные — помечаем already_solved.
    5. Если тест падает с ImportError/NameError — задача пока невыполнима
       без правки кода (но мы её всё же попробуем в selection-режиме).

    Возвращает (feasible, reason, extra), где extra содержит:
        first_test_run — вывод первого прогона (передаём агенту)
        already_solved — True, если тесты уже проходят
    """
    extra = {}
    test_file = None
    m = _RUN_PY.search(instr)
    if m:
        test_file = m.group(1)
    if not test_file:
        r = run_bash(cwd, "ls -1 test_*.py 2>/dev/null | head -1", timeout=5)
        test_file = r["stdout"].strip() or None

    src_file = None
    m = _FIX_PY.search(instr)
    if m:
        src_file = m.group(1)

    if test_file and not (_exists(cwd, test_file) or _find(cwd, test_file)):
        return False, f"test file missing: {test_file}", extra
    if src_file and not (_exists(cwd, src_file) or _find(cwd, src_file)):
        return False, f"source file missing: {src_file}", extra

    # Нет теста — нечего проверять.
    if not test_file:
        return True, "no test file to run", extra

    # Запускаем тесты.
    r = run_bash(cwd, f"timeout 30 python3 {test_file!r} 2>&1 | head -150",
                 timeout=35)
    out = (r["stdout"] or "") + "\n" + (r["stderr"] or "")
    extra["first_test_run"] = out[-3000:]

    if _IMPORT_MISSING.search(out):
        return False, "required function/name missing in source", extra

    if r["exit"] == 0 and "OK" in out and not _ASSERT_ERR.search(out):
        # Тесты уже прошли без наших правок — задача простая.
        extra["already_solved"] = True
        return True, "tests already green", extra

    return True, "ok", extra


# ── MATH ──────────────────────────────────────────────────────────
def preflight_math(cwd, instr):
    """
    Проверка для math-задач.

    Отсеиваем явно невыполнимые формулировки:
    - символьная переменная там, где ожидается число;
    - неразрешённые placeholders вида <NAME>;
    - ссылки на предыдущую задачу;
    - "вычисли x из уравнения, потом используй" — классическая ловушка;
    - арифметика с "the number / the value" без конкретного числа.

    Также: если есть глагол вычисления, но нет ни одного числа —
    задача невыполнима.
    """
    m = _MATH_VAR.search(instr)
    if m:
        return False, f"symbolic variable {m.group(1)!r} where number expected"

    if re.search(r"<[A-Z][A-Z0-9_]+>|\{[A-Z][A-Z0-9_]+\}", instr):
        return False, "unresolved placeholder"

    if re.search(
        r"\b(previous task|above problem|earlier result|as before|"
        r"из предыдущ|как раньше)\b", instr, re.I):
        return False, "depends on previous task"

    # "value that makes X true, then use it" — невозможно без внешних данных.
    if re.search(r"\bvalue\s+that\s+makes\b", instr, re.I):
        return False, "derives value from equation without concrete number"
    if re.search(
        r"\b(multiplied by|plus|minus|divided by|times)\s+"
        r"the\s+(number|value|result)\b", instr, re.I):
        return False, "arithmetic on undefined 'the number/value'"

    if re.search(r"\b(the number|the value)\b", instr, re.I):
        if not re.search(r"\b\d+(?:\.\d+)?\b", instr):
            return False, "references a number not provided"

    has_verb = re.search(
        r"\b(compute|calculate|how many|solve|find the|определи|посчитай|найди)\b",
        instr, re.I)
    has_num  = re.search(r"\b\d+\b", instr)
    if has_verb and not has_num:
        return False, "math task without numeric inputs"

    return True, "ok"


# ── DISPATCH ──────────────────────────────────────────────────────
def preflight(task):
    """
    Точка входа: выбрать подходящую проверку по типу задачи.

    Возвращает (feasible, reason, extra):
        feasible — можно ли тратить бюджет на задачу
        reason   — короткое объяснение (в лог)
        extra    — доп. данные для агента
    """
    cwd   = task.get("_cwd") or task["_dir"]
    ttype = task.get("task_type", "unknown")
    instr = task.get("instruction", "")

    try:
        if ttype == "coding":
            return preflight_coding(cwd, instr)

        # Для search и unknown — search-проверка как дефолт.
        fn = {
            "search":        preflight_search,
            "data_analysis": preflight_data,
            "math":          preflight_math,
        }.get(ttype, preflight_search)
        ok, reason = fn(cwd, instr)
        return ok, reason, {}
    except Exception as e:
        # При внутренней ошибке preflight не блокируем задачу.
        return True, f"preflight error: {e}", {}