"""
Две маленькие статистические модели для принятия решений:

CostModel        — сколько токенов потребует задача.
FeasibilityModel — насколько вероятно, что задача решаема.

Обе — это скользящие средние / счётчики.
"""

from collections import defaultdict

# Априорные средние стоимости по типам задач (в токенах эквивалента).
# Калибровка «на глаз»; модель быстро адаптируется EMA по факту.
PRIOR_COST = {
    "math": 600, "search": 1200, "data_analysis": 1600,
    "coding": 4000, "unknown": 2000,
}


class CostModel:
    """
    Оценка стоимости задачи.

    predict() даёт оценку в токенах, update() обновляет EMA по факту.
    Дополнительно масштабируем по длине инструкции (длинная инструкция =>
    вероятно более сложная задача => больше шагов).
    """
    EMA_ALPHA = 0.3     # скорость адаптации: быстро, но без скачков
    REF_INSTR = 400.0   # «типичная» длина инструкции в токенах


    def __init__(self):
        self.ema = {}   # task_type -> текущая оценка


    def predict(self, task_type: str, instr_tokens: int) -> float:
        """Предсказать стоимость для задачи."""
        base = self.ema.get(task_type, PRIOR_COST.get(task_type, 2000.0))
        # Масштаб 0.5–2.0: короткие задачи дешевле, длинные — дороже.
        scale = max(0.5, min(2.0, instr_tokens / self.REF_INSTR))
        return max(200.0, base * (0.6 + 0.4 * scale))


    def update(self, task_type: str, actual_cost: float, solved: bool):
        """
        Обновить EMA по факту.
        Неуспешные задачи не учитываем — чтобы не задирать оценку из-за
        провальных попыток.
        """
        if not solved:
            return
        prev = self.ema.get(task_type, PRIOR_COST.get(task_type, 2000.0))
        self.ema[task_type] = (1 - self.EMA_ALPHA) * prev + self.EMA_ALPHA * actual_cost


class FeasibilityModel:
    """
    Оценка вероятности, что задача решаема.

    Хранит счётчики {seen, failed} по типу задач. Пока наблюдений мало —
    возвращает 1.0 (доверяем). Дальше — пропорционально снижаем.
    Нижняя граница 0.35 — даже «плохие» типы пробуем, если reward высокий.
    """
    
    MIN_OBS = 3   # сколько наблюдений нужно, прежде чем доверять статистике


    def __init__(self):
        self.stats = defaultdict(lambda: {"seen": 0, "failed": 0})


    def score(self, task: dict) -> float:
        """Вернуть вероятность успеха для задачи (0.35..1.0)."""
        s = self.stats[task.get("task_type", "unknown")]
        if s["seen"] < self.MIN_OBS:
            return 1.0
        return max(0.35, 1.0 - 0.65 * (s["failed"] / s["seen"]))


    def record(self, task_type: str, failed: bool):
        """Зафиксировать исход попытки."""
        self.stats[task_type]["seen"] += 1
        if failed:
            self.stats[task_type]["failed"] += 1