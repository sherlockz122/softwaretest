"""Keep broker reconnect diagnostics useful without URLs or library tracebacks."""

import logging

from celery.signals import after_setup_logger, after_setup_task_logger


class BrokerLogFilter(logging.Filter):
    def filter(self, record):
        if record.name.startswith("celery.worker.consumer") and record.levelno >= logging.WARNING:
            message = record.getMessage()
            code = (
                "TASK_BROKER_CONNECTION_LOST"
                if "Connection to broker lost" in message
                else "TASK_BROKER_UNAVAILABLE"
                if "Cannot connect" in message
                else "TASK_BROKER_WARNING"
            )
            record.msg, record.args = code, ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


@after_setup_logger.connect(weak=False)
@after_setup_task_logger.connect(weak=False)
def protect_broker_logs(logger, **kwargs):
    for handler in logger.handlers:
        if not any(isinstance(item, BrokerLogFilter) for item in handler.filters):
            handler.addFilter(BrokerLogFilter())
