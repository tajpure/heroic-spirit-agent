"""Expected failures surfaced at stable application boundaries."""


class HSAError(Exception):
    """Base error for the HSA think tank."""


class CatalogError(HSAError):
    """A catalog entry is absent or invalid."""


class BackendUnavailable(HSAError):
    """The configured Hermes runtime cannot be started."""


class BackendError(HSAError):
    """A Hermes invocation failed."""


class StructuredOutputError(HSAError):
    """An HSA response failed its structured contract."""


class BudgetExceeded(HSAError):
    """A run exhausted its configured invocation budget."""


class ProtocolError(HSAError):
    """A protocol cannot safely continue."""
