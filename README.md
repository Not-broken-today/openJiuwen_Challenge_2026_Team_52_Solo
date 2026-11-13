# Team 52 (Solo) — Budget-Aware Code Agent Harness

Харнесс кодового агента на базе **openJiuwen**, который решает набор задач
в условиях фиксированного бюджета токенов: выбирает самые выгодные задачи,
распознаёт невыполнимые и решает выполнимые экономно.

---

## Содержание

- [Быстрый старт](#быстрый-старт)
- [Архитектура](#архитектура)
- [Как работает бюджет](#как-работает-бюджет)
- [Поиск невыполнимых задач](#поиск-невыполнимых-задач)
- [Оценка стоимости](#оценка-стоимости)
- [Выбор моделей](#выбор-моделей)
- [Формат ответа и sanity-check](#формат-ответа-и-sanity-check)
- [Переменные окружения](#переменные-окружения)
- [Сборка и запуск](#сборка-и-запуск)
- [Структура проекта](#структура-проекта)
- [Траектории и логи](#траектории-и-логи)
- [Ограничения и компромиссы](#ограничения-и-компромиссы)

---

## Быстрый старт

```bash
# 1. Сборка образа
docker build -t hack:latest .

# 2. Запуск (переменные LLM_BASE_URL / LLM_API_KEY передаются из окружения)
export TOKEN_BUDGET=100000
export MAX_RUNTIME=3300
export WORKERS=5
export TEAM=team_52_solo
export TASKS=/path/to/tasks

sudo -E docker run --rm --name "run-$TEAM" \
        --user 1000:1000 \
        --network hack-net \
        -e LLM_BASE_URL -e LLM_API_KEY \
        -e TOKEN_BUDGET -e MAX_RUNTIME -e WORKERS \
        -v "$TASKS:/data/tasks:rw" \
        --read-only \
        --tmpfs /tmp:rw,size=2g,uid=1000,gid=1000 \
        --tmpfs /app/logs:rw,size=64m,uid=1000,gid=1000 \
        -e HOME=/tmp \
        --cap-drop ALL --security-opt no-new-privileges \
        --cpus 8 --memory 16g --pids-limit 512 \
        --workdir /app \
        "hack:latest" --tasks-dir /data/tasks --workers "${WORKERS:-5}" \
        2>&1 | tee run.log
```


---

## Архитектура

```
run_agent.py          — точка входа: загрузка → preflight → очередь → воркеры
├── tasks.py          — чтение task.json, классификация типа, подсчёт токенов
├── classifier.py     — эвристический классификатор типа задачи по ключевым словам
├── preflight.py      — быстрые проверки выполнимости ДО запуска агента
├── estimator.py      — CostModel (EMA по типам) + FeasibilityModel (доля провалов)
├── agent.py          — цикл «промпт → <bash> → результат» на одну задачу
├── llm3.py           — обёртка openJiuwen: ретраи, учёт токенов, общий event loop
├── sanity.py         — валидация формата answer.txt перед засчитыванием
├── tools.py          — запуск bash с таймаутом и обрезкой вывода
└── file_log.py       — логирование с разным форматом по уровню
```

Поток данных:

```
task.json ──► load_tasks ──► preflight ──► PriorityQueue (value = reward·P(success)/cost)
                                               │
                                               ▼
                          WORKERS потоков ──► Agent.run() ──► answer.txt
                                               │
                                               ▼
                                    check_format + ensure_answer_at_root
```

---

## Как работает бюджет

Бюджет задаётся в **выходных токенах**; входные считаются с коэффициентом
`1/10`. Учёт ведётся в `run_agent._spent` (глобальный счётчик под
`_budget_lock`) и в `agent.cost_equiv` (стоимость одной задачи):

```
cost_equiv = out_tokens + in_tokens / 10
```

Токены рассуждений (`reasoning_tokens` в `completion_tokens_details`)
учитываются в `llm3._out_tokens` так:

```python
c = completion_tokens;  r = reasoning_tokens
return c + r if r > c else c
```

То есть reasoning прибавляется, только когда `reasoning_tokens` превышает
`completion_tokens` (у большинства провайдеров `completion_tokens` уже
включает reasoning, и тогда берётся `c`).

### Двухуровневый контроль

1. **Глобальный резерв** (`run_agent.try_reserve` / `settle`).
   Перед задачей резервируется прогноз `CostModel.predict`. Резерв
   снимается, реальный расход добавляется через `settle`. Резерв
   отклоняется, если `_spent + cost > 0.95 · TOKEN_BUDGET` (запас на
   «оборванные» запросы прокси).

2. **Локальный cap на задачу** (`Agent.cap`).
   `cap = min(CAP_ABS[type], TOKEN_BUDGET · MAX_COST_SHARE)`, где
   `MAX_COST_SHARE = 0.20` в обычном режиме и `0.50` в **selection-режиме**
   (≤5 задач — отборочный прогон). Абсолютные потолки:

   | тип | cap (токенов) |
   |---|---|
   | coding | 12 000 |
   | data_analysis | 3 500 |
   | math | 1 000 |
   | search | 2 000 |
   | unknown | 3 000 |

При достижении cap агент останавливается с `error="cap ... exceeded"`.

### Порядок выполнения

Задачи кладутся в `PriorityQueue` с ключом

```
value = reward · P(success) / max(1, predicted_cost)
```

`P(success)` — `FeasibilityModel.score` (пока наблюдений < 3 — `1.0`,
дальше `max(0.35, 1 − 0.65·fail_rate)`). Воркеры (до 5) берут задачи
параллельно и останавливаются при `time_left() ≤ 60s` или опустошении
очереди.

---

## Поиск невыполнимых задач

`preflight.py` до запуска агента делает дешёвые shell-проверки.
Диспатчер `preflight(task)` возвращает тройку `(feasible, reason, extra)`;
внутренние функции возвращают по-разному: `preflight_coding` —
`(feasible, reason, extra)`, а `preflight_search`, `preflight_data`,
`preflight_math` — только `(feasible, reason)`; диспатчер нормализует
результат к тройке. `extra` (например, вывод первого прогона тестов)
передаётся агенту в стартовый промпт.

### `preflight_search`

- Все упомянутые каталоги существуют и непусты. Учитываются только каталоги
  с префиксами из `_DIR_REF` (`kb/`, `data/`, `src/`, `tests/`, `lib/`,
  `input/`, `docs/`), а также конструкция `the directory '<path>'`. Прочие
  пути не проверяются.
- Все упомянутые файлы находятся (`test -e` или `find -maxdepth 4`).
- Каждое «имя собственное» встречается в файлах (`grep -ril`; при неудаче —
  поиск по первому слову).
- `answer.txt` из проверок исключён — его создаёт агент.

### `preflight_data`

- Есть хотя бы один `.csv`/`.tsv` (из инструкции или `ls *.csv`).
- Заголовок читается, `wc -l` по файлу ≥ 2 (т.е. заголовок + хотя бы одна
  строка данных).
- Если инструкция перечисляет `columns A, B, C` — все они есть в заголовке.
- Строковые значения из инструкции (`'value'`, кроме путей с `/`) реально
  встречаются в данных (`grep -c`).

### `preflight_coding`

- Находит `test_mylib.py` (из `run 'python3 test_mylib.py'` или `ls`).
- Находит `src`-файл из `fix ... 'mylib.py'`, проверяет существование.
- Запускает тесты один раз (`timeout 30 python3 test_file`).
- Если в выводе `ImportError: cannot import name 'X'` или
  `NameError: name 'X' is not defined` — функция не реализована, задача
  невыполнима без правки исходников (reason
  `required function/name missing in source`). В selection-режиме всё равно
  пробуем.
- Если тесты уже зелёные **и** в выводе есть `OK` и нет `AssertionError` —
  `extra["already_solved"]=True`, агент сразу пишет `DONE` и не тратит
  токены.

### `preflight_math`

Отсеивает классические невыполнимые формулировки:

- символьная переменная там, где нужно число (`starts with X`);
- `<PLACEHOLDER>` / `{PLACEHOLDER}`;
- ссылка на предыдущую задачу (`previous task`, `as before`);
- `value that makes ... true` без конкретного числа;
- арифметика с `the number / the value` без числа в тексте
  (`(multiplied by|plus|minus|divided by|times) the (number|value|result)`);
- упоминание `the number` / `the value` при полном отсутствии цифр в
  инструкции (reason `references a number not provided`);
- глагол «вычисли» без единого числа в инструкции.

### Обработка ошибок

Любое исключение внутри preflight возвращает `(True, "preflight error: ...", {})` —
лучше попробовать, чем случайно заблокировать выполнимую задачу.

### Selection-режим

Если в наборе **≤5 задач** (отборочный прогон из регламента), результаты
preflight игнорируются как блокирующие: помечаем `attempt` и идём решать.
Причина: на отборе важнее решить обе задачи, чем сэкономить.

---

## Оценка стоимости

`CostModel` — экспоненциальное сглаживание (α = 0.3) с априорными
средними `PRIOR_COST = {math:600, search:1200, data_analysis:1600,
coding:4000, unknown:2000}`. Прогноз масштабируется длиной инструкции:

```
scale = clamp(instr_tokens / 400, 0.5, 2.0)
predicted = max(200, prior · (0.6 + 0.4·scale))
```

`update()` вызывается только для решённых задач — неуспехи не задирают
оценку. `FeasibilityModel` хранит `{seen, failed}` по типу и возвращает
`P(success)`.

---

## Выбор моделей

| тип | модель |
|---|---|
| coding | `qwen3.8-27b` (с рассуждениями — качество важнее) |
| data_analysis / math / search / unknown | `qwen3.6-35b-a3b-noreason` (дешевле) |
| selection-режим (≤5 задач) | всегда `qwen3.8-27b` (`_force_model`) |

`llm3.LLM1` кеширует объекты `Model` по `(name, temperature)`, ограничивает
параллельность семафором `max_inflight=WORKERS`, делает до 4 попыток
(3 ретрая) с экспоненциальной паузой при `429/RateLimit` и использует один
общий asyncio-loop в фоновом потоке.

### Лимиты шагов и recon

`agent.py` ограничивает число шагов на задачу:

| тип | max_steps |
|---|---|
| coding | 15 |
| data_analysis | 8 |
| math | 5 |
| search | 8 |
| unknown | 8 |

Перед первым шагом выполняется recon-команда (кроме `math`, где она `None`):
`pwd && ls -la` плюс `find`/`head` в зависимости от типа. Её вывод попадает
в стартовый user-промпт.

---

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `LLM_BASE_URL` | — (обязательна) | адрес прокси |
| `LLM_API_KEY` | — (обязательна) | ключ прокси |
| `TASKS` | `/data/tasks` | каталог с задачами |
| `TOKEN_BUDGET` | `100000` | общий бюджет в выходных токенах |
| `MAX_RUNTIME` | `3300` | потолок по времени, сек (< 1 ч) |
| `WORKERS` | `3` | число параллельных агентов (1..5) |

`run_agent.py` также принимает `--tasks-dir` и `--workers` — флаги
перекрывают переменные окружения.

---

## Сборка и запуск

### Dockerfile (ключевые моменты)

- базовый образ и версия `openjiuwen==0.1.19` зафиксированы;
- образ уже содержит `ENTRYPOINT ["python", "/app/run_agent.py"]` — при
  запуске достаточно передать аргументы (`--tasks-dir`, `--workers`);
  `--entrypoint python` не нужен;
- требования «образ < 4 ГБ, репозиторий < 100 МБ» в Dockerfile не обеспечены
  автоматически — нет `.dockerignore`, нет очистки слоёв. Это дисциплина
  сборки, а не свойство кода.

### Отладочный запуск

```bash
export TOKEN_BUDGET=100000
export MAX_RUNTIME=3300
export WORKERS=5

sudo -E docker run --rm --name "run-$TEAM" \
        --user 1000:1000 \
        --network hack-net \
        -e LLM_BASE_URL -e LLM_API_KEY \
        -e TOKEN_BUDGET -e MAX_RUNTIME -e WORKERS \
        -v "$TASKS:/data/tasks:rw" \
        --read-only \
        --tmpfs /tmp:rw,size=2g,uid=1000,gid=1000 \
        --tmpfs /app/logs:rw,size=64m,uid=1000,gid=1000 \
        -e HOME=/tmp \
        --cap-drop ALL --security-opt no-new-privileges \
        --cpus 8 --memory 16g --pids-limit 512 \
        --workdir /app \
        "hack:latest" --tasks-dir /data/tasks --workers "${WORKERS:-5}" \
        2>&1 | tee run.log
```

---

## Структура проекта

```
.
├── Dockerfile
├── requirements.txt          # openjiuwen==0.1.19
├── README.md
├── run_agent.py              # entrypoint
├── agent.py                  # цикл агента
├── tasks.py                  # загрузка задач
├── classifier.py             # тип задачи по ключевым словам
├── preflight.py              # проверки выполнимости
├── estimator.py              # CostModel + FeasibilityModel
├── llm3.py                   # обёртка openJiuwen
├── sanity.py                 # валидация answer.txt
├── tools.py                  # run_bash
└── file_log.py               # логирование
```

---

## Траектории и логи

- Каждый шаг агента хранится в `Agent.history` (пары role/content) и
  `Agent.bash_log` (команда + exit code). Сериализация в `answer.trace.json`
  в текущей версии не реализована — если нужна, её надо добавить отдельно
  (хук в `process_task` после `agent.run()`).
- INFO-лог: `task_id | type | reward | SOLVED/GIVE_UP/failed | cost | spent/budget`.
- WARNING/ERROR-лог содержит `filename/funcName/lineno` — удобно искать
  источник ошибки.
- Болтливые библиотеки (`httpx`, `httpcore`, `openai`, `urllib3`)
  приглушены до WARNING.

Пример строки лога:

```
12:34:56 - c0d3 | coding | r=42.0 | SOLVED | cost=1870 | spent=23410/100000
```

---