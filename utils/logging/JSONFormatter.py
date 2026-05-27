import logging, json, traceback


class JsonFormatter(logging.Formatter):
    def format(self, record):

        log_data= {
            "time":  f"{self.formatTime(record, self.datefmt)}.{int(record.msecs):03d}",
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "pathname": record.pathname,
            "funcName": record.funcName,
            "line": record.lineno,
            "logger": record.name
        }

        if record.exc_info:
            log_data["trace"] = "".join(traceback.format_exception(*record.exc_info))
            log_data["trace_type"] = type(record.exc_info[1]).__name__ if record.exc_info and record.exc_info[1] else "N/A"

        fields = getattr(record, "fields_name", [])


        for key in fields:
            log_data[key] = getattr(record, key, 'N/A')

        # extra_details = {k: v for k, v in record.__dict__.items() if k not in logging.LogRecord.__dict__}
        #
        # for key, value in extra_details.items():
        #     log_data[key] = value

        return json.dumps(log_data)