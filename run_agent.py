"""
Главный скрипт. Тут собирается всё вместе:

1. Читаем переменные окружения (TASKS, LLM_BASE_URL, LLM_API_KEY).
2. Загружаем задачи из папки.
3. Гоняем preflight — отсеиваем заведомо невыполнимые.
4. Строим очередь по "ценности" задачи: reward * P(успех) / стоимость.
   Первыми решаем самые выгодные.
5. Запускаем несколько воркеров параллельно, каждый берёт задачу из очереди.
6. После каждой задачи answer.txt гарантированно лежит в папке задачи.

Бюджет токенов:
- Один общий счётчик _spent на всё.
- Перед задачей "резервируем" оценку стоимости (try_reserve).
- После — корректируем по факту (settle).
- STOP_AT=0.95 — стоп при 95% бюджета, чтобы точно уложиться и
  не получить оборванные запросы от прокси.
"""
import os
import sys
import time
import argparse
import logging as _logging
import threading
from queue import PriorityQueue, Empty
from concurrent.futures import ThreadPoolExecutor, as_completed

from file_log import logging
from tasks import load_tasks
from llm3 import LLM1
from agent import Agent
from estimator import CostModel, FeasibilityModel
from preflight import preflight
from sanity import check_format

# Приглушаем болтливые библиотеки — иначе логи решений тонут в их спаме.
for name in ("httpx", "httpcore", "openai", "urllib3"):
    _logging.getLogger(name).setLevel(_logging.WARNING)

log = logging.getLogger()

# ── Аргументы и переменные окружения ────────────────────────────
parser = argparse.ArgumentParser("agent")
parser.add_argument("--tasks-dir", default=os.getenv("TASKS", "/data/tasks"))
parser.add_argument("--workers", type=int, default=int(os.getenv("WORKERS", "3")))
args = parser.parse_args()

BASE_URL     = os.getenv("LLM_BASE_URL", "")
API_KEY      = os.getenv("LLM_API_KEY", "")
TASKS_DIR    = args.tasks_dir
# ВАЖНО: если TOKEN_BUDGET не передан — считаем 100000 (см. README).
TOKEN_BUDGET = float(os.getenv("TOKEN_BUDGET", "100000"))
MAX_RUNTIME  = float(os.getenv("MAX_RUNTIME", "3300"))   # <1ч, запас на финиш

WORKERS        = max(1, min(args.workers, 5))     # регламент: не больше 5
MAX_COST_SHARE = 0.20                             # макс доля бюджета на задачу
STOP_AT        = 0.95                             # стоп при 95% бюджета

# Абсолютные потолки на одну задачу.
# Чтобы одна "дорогая" задача не съела весь бюджет.
CAP_ABS = {
    "coding":        12000,
    "data_analysis": 3500,
    "math":          1000,
    "search":        2000,
    "unknown":       3000,
}

_START = time.monotonic()


def time_left() -> float:
    """Сколько секунд осталось до MAX_RUNTIME."""
    return MAX_RUNTIME - (time.monotonic() - _START)


def check_envs():
    """Проверить, что обязательные переменные заданы. Иначе — выходим."""
    if not BASE_URL: log.critical("LLM_BASE_URL missing"); sys.exit(1)
    if not API_KEY:  log.critical("LLM_API_KEY missing");  sys.exit(1)
    if not os.path.isdir(TASKS_DIR):
        log.critical(f"tasks dir missing: {TASKS_DIR}"); sys.exit(1)


# ── Учёт бюджета (потокобезопасно) ──────────────────────────────
_budget_lock = threading.Lock()
_spent = 0.0


def try_reserve(cost: float) -> bool:
    """
    Попробовать "зарезервировать" cost токенов под задачу.
    Если после резерва выйдем за STOP_AT * BUDGET — отказ.
    """
    global _spent
    with _budget_lock:
        if _spent + cost > TOKEN_BUDGET * STOP_AT:
            return False
        _spent += cost
        return True


def settle(reserved: float, actual: float):
    """Скорректировать счётчик: снять резерв, добавить реальный расход."""
    global _spent
    with _budget_lock:
        _spent += actual - reserved


# ── Сохранение ответов ──────────────────────────────────────────
def _read(p: str) -> str:
    """Прочитать файл; вернуть "" при любой ошибке."""
    try:
        return open(p, encoding="utf-8").read()
    except Exception:
        return ""


def ensure_answer_at_root(task):
    """
    Гарантировать, что answer.txt лежит в корне папки задачи (_dir).

    Регламент требует answer.txt именно в папке задачи. Агент может
    работать в workspace/ — тогда копируем оттуда. Если answer.txt
    нет вообще — создаём пустой, чтобы папка соответствовала требованиям.
    """
    root = task["_dir"]
    cwd  = task.get("_cwd") or root
    dst  = os.path.join(root, "answer.txt")
    src  = os.path.join(cwd, "answer.txt")
    try:
        if cwd != root and os.path.exists(src):
            with open(src, "rb") as f:
                data = f.read()
            with open(dst, "wb") as f:
                f.write(data)
        elif not os.path.exists(dst):
            with open(dst, "w", encoding="utf-8"):
                pass
    except Exception as e:
        log.warning(f"ensure answer.txt failed: {e}")


def process_task(llm, task, cost_model, feas_model, selection_mode=False):
    """
    Решить одну задачу: зарезервировать бюджет, запустить агента,
    проверить формат, гарантировать answer.txt в папке задачи.
    """
    ttype    = task.get("task_type", "unknown")
    reserved = cost_model.predict(ttype, task["_instr_tokens"])
    # В selection-режиме (2 задачи) разрешаем тратить больше на одну задачу.
    cap_share = 0.50 if selection_mode else MAX_COST_SHARE
    cap      = min(CAP_ABS.get(ttype, 3000), TOKEN_BUDGET * cap_share)

    # В selection-режиме всегда берём самую сильную модель — важнее решить.
    if selection_mode:
        task = dict(task); task["_force_model"] = "qwen3.8-27b"

    if not try_reserve(reserved):
        log.warning(f"{task['task_id']}: budget exhausted, skip")
        ensure_answer_at_root(task)
        return

    agent = Agent(llm, task, cap=cap, time_left_fn=time_left,
                  preflight_extra=task.get("_preflight_extra", {}))
    try:
        agent.run()
    except Exception as e:
        agent.error = f"crashed: {e}"
        log.error(f"{task['task_id']}: {e}")

    settle(reserved, agent.cost_equiv)
    cost_model.update(ttype, agent.cost_equiv, solved=agent.finished)

    ensure_answer_at_root(task)

    # Финальная проверка формата ответа.
    ans_path = os.path.join(task["_dir"], "answer.txt")
    if agent.finished and os.path.exists(ans_path):
        ok, why = check_format(task, _read(ans_path))
        if not ok:
            agent.finished = False
            agent.error = (agent.error or "") + f" | sanity: {why}"

    # Если задачу не решили и в answer.txt мусор — очищаем,
    # чтобы проверяющий не принял его за ответ.
    if not agent.finished and os.path.exists(ans_path):
        ok, _ = check_format(task, _read(ans_path))
        if not ok:
            try:
                with open(ans_path, "w", encoding="utf-8"): pass
            except Exception: pass

    feas_model.record(ttype, not agent.finished)
    log.info(f"{task['task_id']} | {ttype} | r={task.get('reward')} | "
             f"{'SOLVED' if agent.finished else ('GIVE_UP' if agent.gave_up else 'failed')} "
             f"| cost={agent.cost_equiv:.0f} | spent={_spent:.0f}/{TOKEN_BUDGET:.0f}"
             + (f" | {agent.error}" if agent.error else ""))


def worker_loop(llm, pq, cost_model, feas_model, selection_mode):
    """
    Один воркер: в цикле достаёт задачи из очереди и решает,
    пока они не кончатся или не выйдет время.
    """
    while time_left() > 60:
        try:
            _, _, task = pq.get_nowait()
        except Empty:
            return
        try:
            process_task(llm, task, cost_model, feas_model, selection_mode)
        except Exception as e:
            log.error(f"worker: {e}")
        finally:
            pq.task_done()


def main():
    """Точка входа: загрузка → preflight → очередь → воркеры → финал."""
    check_envs()
    log.info(f"budget={TOKEN_BUDGET} workers={WORKERS} runtime={MAX_RUNTIME}s")

    groups, _ = load_tasks(TASKS_DIR)
    all_tasks = [t for lst in groups.values() for t in lst]

    # Если задач мало (2 штуки) — это отборочный прогон. Тогда preflight
    # не доверяем и пробуем всё: лучше потратить токены, чем провалить отбор.
    is_selection = len(all_tasks) <= 5
    if is_selection:
        log.warning(f"SELECTION MODE: {len(all_tasks)} tasks")

    cost_model = CostModel()
    feas_model = FeasibilityModel()

    pq = PriorityQueue()
    seq = 0; skipped = 0

    for t in all_tasks:
        ok, reason, extra = preflight(t)
        if not ok:
            if is_selection:
                log.warning(f"{t['task_id']}: preflight={reason!r}, but SELECTION → attempt")
            else:
                log.warning(f"{t['task_id']}: SKIP ({reason})")
                ensure_answer_at_root(t)
                skipped += 1
                continue

        t["_preflight_extra"] = extra
        est = cost_model.predict(t["task_type"], t["_instr_tokens"])
        p = feas_model.score(t)
        rw = float(t.get("reward", 0) or 0)
        # Ценность = reward * P(успех) / стоимость.
        # Чем выше — тем раньше задача попадёт в работу.
        t["_value"] = (rw * p) / max(1.0, est)
        seq += 1
        pq.put((-t["_value"], seq, t))   # минус — PriorityQueue достаёт минимум

    log.info(f"queue: {len(all_tasks) - skipped} tasks (skipped {skipped})")
    if pq.empty():
        return

    llm = LLM1(BASE_URL, API_KEY, tlimit=int(TOKEN_BUDGET), max_inflight=WORKERS)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(worker_loop, llm, pq, cost_model, feas_model, is_selection)
                   for _ in range(WORKERS)]
        for f in as_completed(futures):
            f.result()

    # На всякий случай убеждаемся, что answer.txt есть во всех задачах.
    for t in all_tasks:
        ensure_answer_at_root(t)


if __name__ == "__main__":
    t0 = time.monotonic()
    try:
        main()
    except KeyboardInterrupt:
        log.warning("interrupted")
    finally:
        dt = time.monotonic() - t0
        m, s = divmod(dt, 60)
        log.info(f"done in {int(m)}m {s:.1f}s, spent={_spent:.0f}/{TOKEN_BUDGET:.0f}")