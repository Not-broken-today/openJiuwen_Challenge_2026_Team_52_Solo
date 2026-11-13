"""
Агент, который решает одну задачу.

Работает по простой схеме:
1. Сначала делает recon (ls/find/head), чтобы "увидеть" файлы задачи.
2. Вызывает модель, разбирает ответ:
   - <bash>...</bash> — выполнить команду;
   - <finish/>        — задача готова (answer.txt уже записан);
   - <give_up/>       — задача невыполнима.
3. Повторяет до max_steps или пока не кончится cap по токенам.

Особенности:
- Модель выбирается по типу задачи (coding → qwen3.8-27b, остальное — noreason).
- Для coding после <finish/> проверяем, что последний прогон тестов был успешным.
  Если нет — просим агента доработать.
- Защита от зацикливания: если последние 3 команды совпадают с предыдущими
  3 — останавливаемся (для coding).
"""

import os
import re

from tools import run_bash
from sanity import check_format

# Системный промпт — объясняет агенту протокол.
PROTOCOL = """You are a tool-using agent. You have ONE tool: bash.

To run a command, output exactly:
<bash>
your command here
</bash>

The command runs with cwd set to the directory where task files live.
Do NOT cd. There is NO network access.

When the task is FULLY complete (answer.txt already written correctly),
output exactly: <finish/>

When the task is IMPOSSIBLE — a required file, function, column, or number
is missing and cannot be created — output exactly: <give_up/>

RULES:
- Work only inside the current directory.
- Write answer.txt via bash before <finish/>.
- If recon shows a required file is MISSING, do not invent it — <give_up/>.
- math: one 'python3 -c "print(...)"' is enough. ALWAYS use '//' for integer
  division, NEVER 'round()' or 'int(x/y)'. Examples:
    print((a + b) * c)
    print(n - ships * days + recv * amt)
    print(sum(nums) // len(nums))
- data: 'head/'wc -l'/'python3 -c' with csv module.
- search: 'ls', 'grep -ri', 'cat'. Answers are short strings.
- coding: run the tests, read code, fix, re-run until green, then DONE.
- Minimise turns. Do not repeat identical commands.
"""

# Парсеры ответа модели.
BASH_RE    = re.compile(r"<bash>\s*\n?(.*?)\n?\s*</bash>", re.S)
FINISH_RE  = re.compile(r"<finish\s*/>")
GIVE_UP_RE = re.compile(r"<give_up\s*/>")
TEST_CMD   = re.compile(r"\b(test_[\w.]+\.py|pytest|unittest)\b")

# Какая модель для какого типа задач.
# Coding берем модель с рассуждениями для качества, остальное — noreason (дешевле).
MODEL_BY_TYPE = {
    "coding":        "qwen3.8-27b",
    "data_analysis": "qwen3.6-35b-a3b-noreason",
    "math":          "qwen3.6-35b-a3b-noreason",
    "search":        "qwen3.6-35b-a3b-noreason",
    "unknown":       "qwen3.6-35b-a3b-noreason",
}

# Лимит шагов на задачу
STEPS_BY_TYPE = {"coding": 15, "data_analysis": 8, "math": 5,
                 "search": 8, "unknown": 8}

# Recon-команды: что показать агенту перед первым шагом.
RECON_BY_TYPE = {
    "coding":        "pwd && ls -la && find . -maxdepth 3 -name '*.py' 2>/dev/null | head -20",
    "data_analysis": "pwd && ls -la && (ls -1 *.csv 2>/dev/null; head -1 *.csv 2>/dev/null)",
    "search":        "pwd && ls -la && (find . -maxdepth 3 -type f 2>/dev/null | head -40)",
    "math":          None,
    "unknown":       "pwd && ls -la && find . -maxdepth 3 -type f 2>/dev/null | head -30",
}


def _is_test_cmd(cmd: str) -> bool:
    """Похожа ли команда на запуск тестов."""
    return bool(TEST_CMD.search(cmd))


class Agent:
    """
    Один агент = одна задача. Между задачами не переиспользуется.
    """

    def __init__(self, llm, task, cap=5000, time_left_fn=None, preflight_extra=None):
        self.llm = llm
        self.task = task
        self.cwd = task.get("_cwd") or task["_dir"]
        self.cap = cap                                  # лимит токенов на задачу
        self.time_left_fn = time_left_fn or (lambda: 999999)
        self.preflight_extra = preflight_extra or {}

        self.history: list[tuple[str, str]] = []        # [(role, content), ...]
        self.bash_log: list[tuple[str, int]] = []       # [(cmd, exit), ...]
        self.in_tok = 0
        self.out_tok = 0
        self.finished = False
        self.gave_up = False
        self.error: str | None = None
        self._recon = ""

    def _run_recon(self, ttype):
        """
        Выполнить recon-команду и сохранить вывод — он попадёт в первый
        user-message, чтобы агент сразу видел структуру каталога.
        """
        cmd = RECON_BY_TYPE.get(ttype)
        if not cmd:
            return
        r = run_bash(self.cwd, cmd, timeout=30)
        self._recon = f"exit={r['exit']}\nstdout:\n{r['stdout']}\nstderr:\n{r['stderr']}"

    def _messages(self):
        """Собрать полный список messages: system + user + накопленная история."""
        parts = [f"TASK:\n{self.task.get('instruction', '')}"]
        if self._recon:
            parts.append(f"\nRecon (cwd={self.cwd}):\n{self._recon}")
        fr = self.preflight_extra.get("first_test_run")
        if fr:
            parts.append(f"\nPreflight already ran the tests. Output:\n{fr}")
        parts.append("\nNow solve the task, using <bash> commands.")
        return [("system", PROTOCOL), ("user", "\n".join(parts))] + self.history

    def _call(self, model, temperature):
        """Один вызов LLM с учётом потраченных токенов."""
        r = self.llm.chat(self._messages(), model_name=model, temperature=temperature)
        self.in_tok += r.prompt_tokens
        self.out_tok += r.completion_tokens
        return r.content

    def _read_answer(self) -> str:
        """Прочитать текущий answer.txt (пустая строка если файла нет)."""
        p = os.path.join(self.cwd, "answer.txt")
        if not os.path.exists(p):
            return ""
        try:
            return open(p, encoding="utf-8").read()
        except Exception:
            return ""

    def _tests_green(self) -> bool:
        """Последний запуск тестов был успешным?"""
        tests = [(c, e) for c, e in self.bash_log if _is_test_cmd(c)]
        return bool(tests) and tests[-1][1] == 0

    def _stuck(self) -> bool:
        """Агент повторяет одни и те же команды? (3 последние = предыдущим 3)"""
        if len(self.bash_log) < 6:
            return False
        return tuple(self.bash_log[-3:]) == tuple(self.bash_log[-6:-3])

    def run(self) -> bool:
        """
        Основной цикл. Возвращает True, если задача решена (finished=True).
        """
        ttype     = self.task.get("task_type", "unknown")
        model     = self.task.get("_force_model") or MODEL_BY_TYPE.get(
            ttype, MODEL_BY_TYPE["unknown"])
        max_steps = STEPS_BY_TYPE.get(ttype, 8)

        self._run_recon(ttype)

        # Если preflight уже увидел зелёные тесты — просто пишем DONE и выходим.
        if self.preflight_extra.get("already_solved"):
            if run_bash(self.cwd, "echo DONE > answer.txt", timeout=5)["exit"] == 0:
                self.finished = True
                return True

        for _ in range(max_steps):
            # Проверки на выход из цикла — чтобы не тратить токены впустую.
            if self.time_left_fn() < 20:
                self.error = "wall_clock_deadline"; break
            if self.cost_equiv > self.cap:
                self.error = f"cap {self.cap:.0f} exceeded"; break
            if ttype == "coding" and self._stuck():
                self.error = "stuck_loop"; break

            try:
                out = self._call(model, 0.1)
            except Exception as e:
                self.error = f"llm error: {e}"; break

            self.history.append(("assistant", out))

            # Сначала проверяем спец-маркеры, потом обычные команды.
            if GIVE_UP_RE.search(out):
                self.gave_up = True
                self.error = "agent_give_up"
                break

            if FINISH_RE.search(out):
                # Проверяем answer.txt и (для coding) что тесты зелёные.
                ans = self._read_answer()
                ok, why = check_format(self.task, ans)
                if ttype == "coding" and not self._tests_green():
                    ok, why = False, "coding tests not green"
                if ok:
                    self.finished = True
                    break
                # Не приняли — просим агента исправить.
                self.history.append(("user",
                    f"answer.txt rejected: {why}. Fix and emit <finish/> again."))
                continue

            # Обычный шаг: ищем один или несколько блоков <bash>...</bash>.
            cmds = BASH_RE.findall(out)
            if not cmds:
                self.history.append(("user",
                    "No <bash> block. Use <bash>...</bash> or <give_up/>."))
                continue

            results = []
            for cmd in cmds:
                cmd_s = cmd.strip()
                r = run_bash(self.cwd, cmd_s, timeout=120)
                self.bash_log.append((cmd_s, r["exit"]))
                results.append(
                    f"exit={r['exit']}\nstdout:\n{r['stdout']}\nstderr:\n{r['stderr']}")
            self.history.append(("user", "RESULTS:\n" + "\n---\n".join(results)))

        # Авто-DONE для coding: тесты прошли, но модель забыла <finish/>.
        if (ttype == "coding" and not self.finished and not self.gave_up
                and self._tests_green()):
            if run_bash(self.cwd, "echo DONE > answer.txt", timeout=5)["exit"] == 0:
                self.finished = True
                self.error = (self.error or "") + " | auto_done"

        return self.finished

    @property
    def cost_equiv(self) -> float:
        """Стоимость в эквиваленте выходных токенов (входные делим на 10)."""
        return self.out_tok + self.in_tok / 10.0
