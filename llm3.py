"""
Обёртка над openJiuwen для работы с моделями через прокси организаторов.
"""
import asyncio
import inspect
import threading
import time
from dataclasses import dataclass

from openjiuwen.core.foundation.llm import (
    Model, ModelClientConfig, ModelRequestConfig,
    SystemMessage, UserMessage,
)
try:
    from openjiuwen.core.foundation.llm import AssistantMessage
except ImportError:
    AssistantMessage = UserMessage


@dataclass
class LLMResponse:
    """Что возвращает один вызов LLM."""
    content: str              # текст ответа
    prompt_tokens: int        # входные токены
    completion_tokens: int    # выходные (вместе с reasoning)


# Один общий event loop на всё приложение. Создавать новый на каждый
# запрос дорого и ломает внутренние клиенты openjiuwen.
_loop = None
_loop_lock = threading.Lock()


def _get_loop():
    """Лениво создать общий loop и запустить его в фоновом потоке."""
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop


def _run(coro):
    """Выполнить корутину на общем loop и дождаться результата (blocking)."""
    return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result()


def _out_tokens(usage) -> int:
    """
    Достать число выходных токенов из usage.
    """
    c = int(getattr(usage, "completion_tokens", 0) or 0)
    r = 0
    det = getattr(usage, "completion_tokens_details", None)
    if det is not None:
        r = int(getattr(det, "reasoning_tokens", 0) or 0)
    return c + r if r > c else c


class LLM1:
    """
    Клиент LLM с ограничением параллельности и учётом токенов.
    """

    def __init__(self, base_url, api_key, tlimit=100000, max_inflight=3):
        self.tlimit = tlimit           # оставлено на будущее, сейчас не используется
        self.tcurrent = 0              # накопленный счётчик (для дебага)
        self.client_config = ModelClientConfig(
            client_provider="OpenAI",
            api_base=base_url, api_key=api_key, verify_ssl=False,
        )
        self._cache = {}               # (model, temperature) -> Model
        self._sema = threading.Semaphore(max_inflight)

    def _model(self, name, temperature):
        """Получить (или создать и закешировать) объект Model."""
        key = (name, round(temperature, 2))
        if key not in self._cache:
            self._cache[key] = Model(
                model_client_config=self.client_config,
                model_config=ModelRequestConfig(model=name, temperature=temperature),
            )
        return self._cache[key]

    @staticmethod
    def _build(messages):
        """
        Превратить наши [(role, content), ...] в объекты сообщений openJiuwen.
        Роли: system, user, assistant.
        """
        out = []
        for role, content in messages:
            if role == "system":
                out.append(SystemMessage(content=content))
            elif role == "user":
                out.append(UserMessage(content=content))
            else:
                out.append(AssistantMessage(content=content))
        return out

    def _invoke(self, model, objs, max_retries=4):
        """
        Вызвать модель с ретраями.
        """
        async_fn = None
        for name in ("ainvoke", "invoke_async", "async_invoke"):
            fn = getattr(model, name, None)
            if callable(fn):
                async_fn = fn
                break

        last = None
        for attempt in range(max_retries):
            try:
                with self._sema:
                    if async_fn is not None:
                        return _run(async_fn(messages=objs))
                    res = model.invoke(messages=objs)
                    if inspect.iscoroutine(res):
                        return _run(res)
                    return res
            except Exception as e:
                last = e
                msg = str(e)
                if "429" in msg or "RateLimit" in msg or "Too Many" in msg:
                    # Экспоненциальная пауза: 0.5, 1.5, 4.5, 15 секунд.
                    time.sleep(min(15.0, 2 ** attempt + 0.5))
                    continue
                if "Event loop is closed" in msg:
                    time.sleep(0.5)
                    continue
                raise
        raise RuntimeError(f"LLM failed after {max_retries}: {last}")

    def chat(self, messages, model_name, temperature=0.1):
        """
        Главный метод: отправить messages в model_name, получить ответ.

        Возвращает LLMResponse(content, prompt_tokens, completion_tokens).
        """
        objs = self._build(messages)
        model = self._model(model_name, temperature)
        res = self._invoke(model, objs)

        content = getattr(res, "content", "") or ""
        if isinstance(content, list):
            content = "".join(map(str, content))
        content = str(content).strip()

        # Достаём usage. Если его нет — грубо прикидываем по длине текста.
        usage = getattr(res, "usage", None)
        if usage is not None:
            p = int(getattr(usage, "prompt_tokens", 0)
                    or getattr(usage, "input_tokens", 0) or 0)
            c = _out_tokens(usage)
        else:
            text_in = "".join(m for _, m in messages)
            p, c = len(text_in) // 3, len(content) // 3

        # Учёт в эквиваленте выходных токенов (входные делим на 10).
        self.tcurrent += c + p // 10
        return LLMResponse(content=content, prompt_tokens=p, completion_tokens=c)