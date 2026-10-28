class ApiError(Exception):
    def __init__(self, code, message, status=409, details=None):
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def fail(code, message, status=409, details=None):
    raise ApiError(code, message, status, details)
