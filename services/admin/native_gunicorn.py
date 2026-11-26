"""Gunicorn master configuration. Application modules load in workers after fork."""
# Gunicorn 26 otherwise opens a shared per-user control socket outside this state.
# Older supported development installations ignore unknown config settings.
control_socket_disable=True
