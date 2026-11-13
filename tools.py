"""
Инструменты для выполнения shell-команд внутри задач.
"""

import subprocess

# Ограничение размера вывода.
MAX_STDOUT = 4000
MAX_STDERR = 1500


def run_bash(cwd: str, command: str, timeout: int = 120) -> dict:
    """
    Выполнить bash-команду в каталоге `cwd`.

    Возвращает dict:
        exit   — код возврата (124 при таймауте, 1 при прочих ошибках)
        stdout — последние MAX_STDOUT символов stdout
        stderr — последние MAX_STDERR символов stderr

    Используем `bash -lc`, чтобы работали алиасы и пайпы.
    """
    try:
        r = subprocess.run(["bash", "-lc", command], cwd=cwd,
                           capture_output=True, text=True, timeout=timeout)

        return {"exit": r.returncode,
                "stdout": (r.stdout or "")[-MAX_STDOUT:],
                "stderr": (r.stderr or "")[-MAX_STDERR:]}

    except subprocess.TimeoutExpired:
        # Команда не уложилась в timeout.
        return {"exit": 124, "stdout": "", "stderr": f"timeout {timeout}s"}

    except Exception as e:
        # Иная любая ошибка
        return {"exit": 1, "stdout": "", "stderr": f"exec error: {e}"}