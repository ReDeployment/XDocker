"""Local, bounded secret checks. Findings contain locations and rules, never values."""
from pathlib import PurePosixPath
import re

VERSION = 'committed-content-v2'
TEMP_SUFFIXES = ('.tmp', '.temp', '.swp', '.swo', '.bak', '.log', '.dump', '.sqlite', '.sqlite3', '.db', '.p12', '.pfx', '.key', '.zip', '.gz', '.tar', '.7z')
TEMP_DIRS = {'node_modules', '__pycache__', '.venv', 'venv', '.ssh', '.cache', '.nuxt', '.output', 'coverage'}
TOKEN_PATTERNS = [
    ('private-key', re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')),
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b')),
    ('aws-access-key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('slack-token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b')),
    ('api-token', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b')),
    ('credential-url', re.compile(r'\b(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/:@]+:([^\s/@]{8,})@')),
]
ASSIGNMENT = re.compile(r'''(?im)(?:^|[,{;])\s*(?:export\s+)?["']?(?P<key>[\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)[\w.-]*)["']?\s*[:=]\s*(?:["']([^\r\n"']{8,256})["']|([A-Za-z0-9_+/=.!@#$%-]{12,256})(?=\s*(?:#|$)))''')
PLACEHOLDER = re.compile(r'(?i)^(?:\$\{[^}]+\}|<[^>]+>|(?:fixture|example|dummy|fake|test)[_-].*|change[-_]?me.*|replace[-_].*|your[-_].*|\*+|x+|\.\.\.)$')
ASSETS = {'.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8', '.jpeg': b'\xff\xd8', '.gif': b'GIF8', '.ico': b'\x00\x00\x01\x00', '.woff': b'wOFF', '.woff2': b'wOF2', '.ttf': b'\x00\x01\x00\x00', '.otf': b'OTTO'}


def path_rules(path):
    p = PurePosixPath(path); name = p.name.lower(); parts = [v.lower() for v in p.parts]
    template = name.endswith(('.example', '.sample', '.template', '.dist'))
    rules = []
    if any(v in TEMP_DIRS for v in parts) or name.endswith(TEMP_SUFFIXES) or name.endswith('~'):
        rules.append('temporary-or-private-file')
    if name.startswith('.env') and not template:
        rules.append('environment-file')
    if name in {'id_rsa', 'id_ed25519', 'credentials', '.netrc', '.npmrc', '.pypirc'}:
        rules.append('credential-file')
    return rules


def content_findings(path, body):
    """Only fixed high-signal patterns; this is not proof that a commit is secret-free."""
    findings = [{'rule': rule, 'line': None} for rule in path_rules(path)]
    if body.startswith(b'version https://git-lfs.github.com/spec/v1'):
        findings.append({'rule':'external-lfs-object','line':None})
    try:
        text = body.decode('utf-16') if body.startswith((b'\xff\xfe', b'\xfe\xff')) else body.decode('utf-8')
        if '\0' in text:raise UnicodeError()
    except UnicodeError:
        suffix = PurePosixPath(path).suffix.lower()
        approved = suffix in ASSETS and body.startswith(ASSETS[suffix])
        approved |= suffix == '.webp' and body.startswith(b'RIFF') and body[8:12] == b'WEBP'
        if not approved:findings.append({'rule': 'unscannable-binary', 'line': None})
        # Known media still gets ASCII token/private-key checks.
        text = body.decode('utf-8', 'replace')
    for rule, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            if rule == 'credential-url' and PLACEHOLDER.fullmatch(match.group(1)):continue
            findings.append({'rule': rule, 'line': text.count('\n', 0, match.start()) + 1})
            break
    for match in ASSIGNMENT.finditer(text):
        value = match.group(2) or match.group(3)
        # Token/key fields containing ordinary explanatory prose are common in UIs.
        # Password assignments and authorization-shaped values remain suspicious.
        if any(c.isspace() for c in value) and not re.search(r'password|passwd',match.group('key'),re.I) and not re.match(r'(?i)^(?:bearer|basic)\s+\S{8,}$',value):continue
        if not PLACEHOLDER.fullmatch(value):
            findings.append({'rule': 'literal-credential', 'line': text.count('\n', 0, match.start()) + 1})
            break
    return findings


def safe_label(value):
    for _,pattern in TOKEN_PATTERNS:value=pattern.sub('[已隐藏]',value)
    return value
