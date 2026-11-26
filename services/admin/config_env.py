"""Read private XDocker environment files as data."""
import re
import shlex


def env_values(path):
    """Read simple assignment values, never evaluate shell or execute commands."""
    result = {}
    if not path.is_file():
        return result
    for line in path.read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$', line)
        if match:
            try:
                parts = shlex.split(match[2], comments=True)
                if len(parts) <= 1:
                    result[match[1]] = parts[0] if parts else ''
            except ValueError:
                continue
    return result
