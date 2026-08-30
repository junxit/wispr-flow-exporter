"""Local, incremental archive of Wispr Flow dictation, meetings and transcripts."""

__version__ = "0.3.1"

# Sent as the User-Agent by the cloud backend. Kept here so there is one source
# of truth rather than a copy in the client module that goes stale.
USER_AGENT = f"wispr-flow-exporter/{__version__}"
