"""水下干预服务向 API 和 CLI 暴露的稳定错误。"""


class InterventionError(RuntimeError):
    code = "intervention_error"
    status = 400


class NotFound(InterventionError):
    code = "not_found"
    status = 404


class Conflict(InterventionError):
    code = "conflict"
    status = 409


class Forbidden(InterventionError):
    code = "forbidden"
    status = 403


class InvalidState(InterventionError):
    code = "invalid_state"
    status = 409


class ValidationFailed(InterventionError):
    code = "validation_failed"
    status = 422
