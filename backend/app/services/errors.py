"""Business errors, mapped to the same outcome codes as every MCP tool
(app/policy/engine.py) so the agent, the REST API and the alerting all
speak the same language."""

from app.policy.engine import ACCESS_DENIED, NOT_FOUND, SERVICE_UNAVAILABLE, VALIDATION_ERROR, tool_error


class ServiceError(Exception):
    def __init__(self, code: str, message: str, *, concealed: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        # Same message whether the resource is missing or forbidden (no
        # existence oracle) - see DOCUMENT_NOT_FOUND_OR_DENIED.
        self.concealed = concealed

    def as_tool_error(self) -> dict:
        return tool_error(self.code, self.message, concealed=self.concealed)

    @property
    def http_status(self) -> int:
        return {ACCESS_DENIED: 403, NOT_FOUND: 404, VALIDATION_ERROR: 422, SERVICE_UNAVAILABLE: 503}.get(self.code, 400)


def denied(message: str) -> ServiceError:
    return ServiceError(ACCESS_DENIED, message)


def not_found(message: str, *, concealed: bool = False) -> ServiceError:
    return ServiceError(NOT_FOUND, message, concealed=concealed)


def invalid(message: str) -> ServiceError:
    return ServiceError(VALIDATION_ERROR, message)
