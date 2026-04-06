from __future__ import annotations

import logging
import logging.config


class SimpleFormatter(logging.Formatter):
    default_time_format = "%Y-%m-%d %H:%M:%S"
    default_msec_format = None


def configure_logging() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "simple": {
                    "()": "app.logging_config.SimpleFormatter",
                    "format": "%(asctime)s %(levelname)s %(message)s",
                }
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "simple",
                }
            },
            "root": {
                "level": "INFO",
                "handlers": ["default"],
            },
            "loggers": {
                "ghostreplay.http": {
                    "level": "INFO",
                    "propagate": True,
                },
                "uvicorn": {
                    "level": "INFO",
                    "handlers": ["default"],
                    "propagate": False,
                },
                "uvicorn.error": {
                    "level": "INFO",
                    "handlers": ["default"],
                    "propagate": False,
                },
                "uvicorn.access": {
                    "level": "WARNING",
                    "handlers": [],
                    "propagate": False,
                },
            },
        }
    )
