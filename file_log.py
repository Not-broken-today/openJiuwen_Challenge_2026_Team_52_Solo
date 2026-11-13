"""
Настройка логирования: разные форматы для INFO и DEBUG/WARNING/ERROR.
"""
import logging


class LevelSpecificFormatter(logging.Formatter):
    """Формат зависит от уровня записи."""

    FORMATS = {
        logging.DEBUG:    "%(asctime)s - %(filename)s/%(funcName)s/%(lineno)d  - [%(levelname)s]: %(message)s",
        logging.INFO:     "%(asctime)s - %(message)s",
        logging.WARNING:  "%(asctime)s - %(filename)s/%(funcName)s/%(lineno)d  - [%(levelname)s]: %(message)s",
        logging.ERROR:    "%(asctime)s - %(filename)s/%(funcName)s/%(lineno)d  - [%(levelname)s]: %(message)s",
        logging.CRITICAL: "%(asctime)s - %(filename)s/%(funcName)s/%(lineno)d - [%(levelname)s]: %(message)s",
    }

    def __init__(self, fmt=None, datefmt=None, style='%'):
        super().__init__(fmt=fmt, datefmt=datefmt, style=style)
        self.datefmt = datefmt

    def format(self, record):
        fmt = self.FORMATS.get(record.levelno, self.FORMATS[logging.INFO])
        return logging.Formatter(fmt=fmt, datefmt=self.datefmt).format(record)


logging.basicConfig(level=logging.INFO, datefmt='%H:%M:%S')
root = logging.getLogger()
for h in root.handlers:
    old = h.formatter.datefmt if h.formatter else '%H:%M:%S'
    h.setFormatter(LevelSpecificFormatter(datefmt=old))