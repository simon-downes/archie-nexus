"""incident.io plugin test fixtures (D16: fakes are a top-level module, not a pytest plugin)."""

from incident_io_fakes import fake, incident_io_context_installed, policy  # noqa: F401
