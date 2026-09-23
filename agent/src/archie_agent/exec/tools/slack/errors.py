"""Sanitized Slack tool errors."""

from archie_agent.exec.tools import ToolError


class SlackError(ToolError):
    """Base class for Slack failures."""


class SlackValidationError(SlackError):
    pass


class SlackPolicyError(SlackError):
    pass


class SlackConfigurationError(SlackError):
    pass


class SlackAuthenticationError(SlackError):
    pass


class SlackReauthorizationRequiredError(SlackAuthenticationError):
    def __init__(self, scopes: list[str]):
        self.scopes = tuple(scopes)
        super().__init__("Slack authorization is missing required capabilities")


class SlackNotFoundError(SlackError):
    pass


class SlackRateLimitError(SlackError):
    def __init__(self, method: str, retry_after_seconds: int | None):
        self.method = method
        self.retry_after_seconds = retry_after_seconds
        suffix = (
            f"; retry after {retry_after_seconds} seconds"
            if retry_after_seconds is not None
            else ""
        )
        super().__init__(f"Slack rate limit reached for {method}{suffix}")


class SlackResponseError(SlackError):
    pass


class SlackPaginationError(SlackResponseError):
    pass


class SlackDeadlineError(SlackError):
    pass


class SlackBoundedResultError(SlackResponseError):
    pass


class SlackMutationIndeterminateError(SlackError):
    pass


class SlackTransportError(SlackError):
    pass
