import logging

import pytest

from packages.platform.worker_logging import protect_broker_logs


@pytest.mark.parametrize(
    "message,code",
    [
        ("consumer: Connection to broker lost. %s", "TASK_BROKER_CONNECTION_LOST"),
        ("consumer: Cannot connect to %s", "TASK_BROKER_UNAVAILABLE"),
    ],
)
def test_broker_logger_filters_secret_and_exception_before_formatting(message, code):
    logger = logging.Logger("fixture")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    protect_broker_logs(logger)
    protect_broker_logs(logger)
    assert len(handler.filters) == 1
    record = logging.LogRecord(
        "celery.worker.consumer.consumer",
        logging.WARNING,
        "private/path",
        1,
        message,
        ("redis://:private-secret@host/0",),
        (RuntimeError, RuntimeError("private-exception"), None),
    )
    assert handler.filter(record)
    output = handler.format(record)
    assert output == code
    assert "private" not in output and record.exc_info is None
