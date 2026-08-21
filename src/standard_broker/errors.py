"""Errors raised at the canonical broker boundary."""


class BrokerError(RuntimeError):
    """Base error for provider-neutral broker boundary failures."""


class PaperBoundaryError(BrokerError):
    """Raised when a local Paper adapter would cross its safety boundary."""


class BrokerCapabilityError(BrokerError):
    """Raised when a Broker cannot provide the requested canonical operation."""

    def __init__(self, port: str, operation: str, detail: str = "") -> None:
        self.reason_code = "capability_gap"
        self.port = port
        self.operation = operation
        message = f"{self.reason_code}: {port}.{operation} is not supported"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
