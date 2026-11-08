"""One-way, request-local credential scanning for model-visible JSON values.

Only explicit credential labels and recognizable token formats are matched. Bare
unlabelled secrets and image contents require the caller to keep them local.
"""
import ast
import io
import json
import re
import tokenize


class CredentialRedactionError(RuntimeError):
    """Local processing failure; callers stop the request without endpoint penalties."""

    def __init__(self):
        super().__init__('API Pool credential redaction unavailable')


# Label boundaries accept service-prefixed environment variables (e.g. DB_PASSWORD).
_LABEL = (r'(?:[A-Za-z][A-Za-z0-9]*[_-])*'
          r'(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|client[_ -]?secret|'
          r'access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|session[_ -]?token|'
          r'password|passwd|pwd|pass|secret|token|username|user[_ -]?name|user|account|login|'
          r'authorization|cookie|session|session[_ -]?id|api密钥|接口密钥|访问令牌|刷新令牌|'
          r'用户名|用户账号|登录账号|账号|账户|密码|口令|密钥|令牌)')
_KEY = re.compile(r'^(?:' + _LABEL + r')$', re.IGNORECASE)
_VALUE = (r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
          r'(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\[REDACTED_[A-Z_]+\]|[^\s,;，；。}\]\)\"\'])+')
_ASSIGN = re.compile(
    r'(?<![\w$])(?P<label>' + _LABEL + r')(?P<labelquote>[\"\']?)'
    r'(?P<sep>\s*(?:[:=：]|为|是)\s*)'
    r'(?P<value>' + _VALUE + r')', re.IGNORECASE)
_CLI = re.compile(r'(?<!\w)--(?P<label>' + _LABEL + r')(?:=|\s+)'
                  r'(?P<value>' + _VALUE + r')', re.IGNORECASE)
_AUTH = re.compile(r'\b(?P<scheme>Bearer|Basic)\s+(?P<value>[A-Za-z0-9_./+~=-]+)', re.IGNORECASE)
# Inline Chinese labels carry the value by whitespace only: 「账号 svc 密码 pw」.
# Only ASCII-looking values qualify, so ordinary prose (「密码 忘记了」) stays readable.
_CN_ASSIGN = re.compile(
    r'(?<![\w$])(?P<label>用户名|用户账号|登录账号|账号|账户|密码|口令|密钥|令牌)'
    r'(?P<sep>[ \t\u3000]+)'
    r'(?P<value>[A-Za-z0-9][A-Za-z0-9._@!+#%^&*?~:/-]{2,})')
_CN_VALUE_TAIL = re.compile(r'[.,;:!?。)）]+$')

_URL = re.compile(r'(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<user>[^\s/:@]+):(?P<password>[^\s/@]+)@', re.IGNORECASE)
_COOKIE = re.compile(r'(?P<prefix>\b(?:set-cookie|cookie)[\"\']?\s*[:=]\s*)'
                     r'(?P<value>"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\r\n]+)', re.IGNORECASE)
_TOKEN = re.compile(
    r'(?<![\w-])(?:sk-(?:proj-|ant-api\d*-)?[A-Za-z0-9_-]{16,}|'
    r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|'
    r'AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|'
    r'xox[baprs]-[A-Za-z0-9-]{16,}|wbt_[A-Za-z0-9_-]{20,})\b')
_JWT = re.compile(r'(?<![\w-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![\w-])')
_PRIVATE_KEY = re.compile(r'-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----')
_REFERENCE = re.compile(
    r'\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)|'
    r'\[REDACTED_[A-Z_]+\]')
_PYTHON_LOOKUP = re.compile(
    r'(?<![\w.])(?:os\s*\.\s*(?:getenv\s*\(|environ\s*(?:\[|\.\s*get\s*\())|'
    r'(?:dotenv\s*\.\s*)?dotenv_values\s*\()')
_YAML_BLOCK = re.compile(
    r'^(?P<indent>[ \t]*)(?P<sequence>-[ \t]+)?(?P<quote>[\"\']?)'
    r'(?P<label>' + _LABEL + r')(?P=quote)[ \t]*:[ \t]*'
    r'(?P<marker>[|>](?:[1-9][+-]?|[+-][1-9]?)?)[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)',
    re.IGNORECASE | re.MULTILINE)
_JSON_START = re.compile(r'[\[{]')
_AUTH_PREFIX = re.compile(r'(?:Bearer|Basic)\s+', re.IGNORECASE)
_COOKIE_PAIR = re.compile(r'(?P<name>[^=;\s]+)=(?P<value>[^;]*)')
_CURL_USER = re.compile(r'(?<!\S)(?:-u|--user)\s+(?P<value>' + _VALUE + r')')
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')
_BINARY_FIELDS = {
    'image': {'source'}, 'image_url': {'image_url'}, 'input_image': {'image_url', 'file_id'},
    'input_audio': {'input_audio'}, 'audio': {'data'}, 'file': {'file'},
    'input_file': {'file_id', 'file_data'}, 'document': {'source'}, 'redacted_thinking': {'data'},
}
_TEXT_KEYS = {'text', 'content', 'system', 'instructions', 'thinking', 'reasoning',
              'reasoning_content', 'reasoning_text', 'refusal', 'description', 'output', 'summary'}
_CONTAINERS = {'messages', 'contents', 'parts', 'systemInstruction', 'system_instruction',
               'tool_calls', 'function_call', 'function', 'functionCall', 'functionResponse',
               'tools', 'functions', 'functionDeclarations', 'reasoning_details',
               'generationConfig', 'response_format', 'json_schema', 'format'}
_SCHEMA_KEYS = {'parameters', 'input_schema', 'parametersJsonSchema', 'responseSchema', 'schema'}


def _kind(label):
    key = re.sub(r'[ _-]', '', label).lower()
    if key == 'pass' or any(s in key for s in ('password', 'passwd', 'pwd', '密码', '口令')):
        return 'PASSWORD'
    if key == 'user' or key.endswith('user') or any(s in key for s in ('username', 'account', 'login', '用户名', '账号', '账户')):
        return 'ACCOUNT'
    if 'cookie' in key or 'session' in key:
        return 'COOKIE'
    if any(s in key for s in ('key', 'secret', '密钥')):
        return 'API_KEY'
    return 'TOKEN'


def _python_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _python_name(node.value)
        return base + '.' + node.attr if base else ''
    return ''


def _lookup_parts(node):
    """Return the key/default AST nodes of a supported local environment lookup."""
    if isinstance(node, ast.Subscript):
        base = node.value
        if _python_name(base) == 'os.environ':
            return node.slice, None
        if (isinstance(base, ast.Call) and _python_name(base.func) in ('dotenv_values', 'dotenv.dotenv_values')
                and len(base.args) == 1 and not base.keywords
                and isinstance(base.args[0], ast.Constant) and isinstance(base.args[0].value, str)
                and base.args[0].value.endswith('.env')):
            return node.slice, None
    if (isinstance(node, ast.Call) and _python_name(node.func) in ('os.getenv', 'os.environ.get')
            and 1 <= len(node.args) <= 2 and len(node.keywords) <= 1
            and all(k.arg == 'default' for k in node.keywords)
            and not (len(node.args) == 2 and node.keywords)):
        default = node.args[1] if len(node.args) == 2 else (node.keywords[0].value if node.keywords else None)
        return node.args[0], default
    return None


def _safe_default(node):
    if node is None or (isinstance(node, ast.Constant) and node.value in ('', None)):
        return True
    if isinstance(node, (ast.Name, ast.Attribute)) and _python_name(node):
        return True
    parts = _lookup_parts(node)
    return parts is not None and _safe_lookup_key(parts[0]) and _safe_default(parts[1])


def _safe_lookup_key(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and (re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', node.value)
                 or _REFERENCE.fullmatch(node.value)))


def _extended_expression(source):
    """Parse the longest expression prefix, stopping at its enclosing delimiter."""
    rows = source.splitlines(keepends=True)
    offsets = [0]
    for row in rows:
        offsets.append(offsets[-1] + len(row))
    depth = 0
    best = None
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if depth == 0 and (token.type in (tokenize.NEWLINE, tokenize.ENDMARKER, tokenize.COMMENT)
                               or token.string in (',', ';', ':', ')', ']', '}')):
                break
            if token.type == tokenize.OP:
                if token.string in ('(', '[', '{'):
                    depth += 1
                elif token.string in (')', ']', '}'):
                    depth -= 1
            if depth == 0:
                end = offsets[token.end[0] - 1] + token.end[1]
                try:
                    node = ast.parse(source[:end], mode='eval').body
                except (SyntaxError, ValueError):
                    continue
                best = end, node
    except (tokenize.TokenError, IndentationError):
        pass
    return best


def _literal_nodes(node, credential_context=False):
    parts = _lookup_parts(node)
    if parts is not None and _safe_lookup_key(parts[0]):
        assert isinstance(parts[0], ast.Constant) and isinstance(parts[0].value, str)
        default = parts[1]
        sensitive = credential_context or bool(_KEY.fullmatch(parts[0].value))
        if default is not None:
            if sensitive and not _safe_default(default):
                yield default
            elif not sensitive:
                yield from _literal_nodes(default)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        if (not re.fullmatch(r'\[REDACTED_[A-Z_]+\]', node.value)
                and (credential_context or _TOKEN.search(node.value) or _JWT.search(node.value)
                     or _PRIVATE_KEY.search(node.value) or _ASSIGN.search(node.value))):
            yield node
        return
    for child in ast.iter_child_nodes(node):
        yield from _literal_nodes(child, credential_context)


def _python_lookups(text):
    """Find complete local expressions with tokenizer boundaries and AST validation.

    Parsing only; values are never evaluated and environment files stay local.
    Unsafe defaults become quoted placeholders within the executable expression.
    """
    cursor = 0
    for match in _PYTHON_LOOKUP.finditer(text):
        if match.start() < cursor:
            continue
        source = text[match.start():]
        lines = source.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        depth = 0
        end = None
        dotenv = source.startswith(('dotenv_values', 'dotenv'))
        groups = 0
        try:
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type != tokenize.OP:
                    continue
                if token.string in ('(', '['):
                    depth += 1
                elif token.string in (')', ']'):
                    depth -= 1
                    if depth == 0:
                        groups += 1
                        if not dotenv or groups == 2:
                            end = offsets[token.end[0] - 1] + token.end[1]
                            break
        except (tokenize.TokenError, IndentationError):
            continue
        if end is None:
            continue
        expression = source[:end]
        try:
            node = ast.parse(expression, mode='eval').body
        except (SyntaxError, ValueError):
            continue
        parts = _lookup_parts(node)
        if parts is None or not _safe_lookup_key(parts[0]):
            continue
        key, _ = parts
        # A credential assignment can concatenate a local lookup and a literal.
        # Extend its AST boundary before protecting it, then replace its literals.
        assignment = next((m for m in _ASSIGN.finditer(text[:match.end()])
                           if m.start('value') == match.start()), None)
        extended = _extended_expression(source) if assignment and source[end:].lstrip().startswith('+') else None
        if extended is not None:
            end, node = extended
            expression = source[:end]
        literals = list(_literal_nodes(node, credential_context=assignment is not None))
        clean = expression.encode()
        rows = clean.splitlines(keepends=True)
        edits = []
        for literal in literals:
            assert isinstance(key, ast.Constant)
            assert literal.end_lineno is not None and literal.end_col_offset is not None
            # AST columns count UTF-8 bytes; slice bytes for non-ASCII source too.
            start = sum(map(len, rows[:literal.lineno - 1])) + literal.col_offset
            stop = sum(map(len, rows[:literal.end_lineno - 1])) + literal.end_col_offset
            edits.append((start, stop, repr('[REDACTED_' + _kind(key.value) + ']').encode()))
        for start, stop, replacement in sorted(edits, reverse=True):
            clean = clean[:start] + replacement + clean[stop:]
        cursor = match.start() + end
        yield match.start(), cursor, clean.decode()


def _is_reference(value):
    if _TOKEN.search(value) or _JWT.search(value):
        return False
    if _REFERENCE.fullmatch(value):
        return True
    return any(start == 0 and end == len(value) and clean == value
               for start, end, clean in _python_lookups(value))


def _replacement(value, kind):
    if not value or _is_reference(value):
        return value
    # A composed environment expression remains executable (e.g. "Bearer $TOKEN").
    auth = _AUTH_PREFIX.match(value)
    if auth and _is_reference(value[auth.end():]):
        return value
    return '[REDACTED_' + kind + ']'


def _label_value(match, cli=False):
    value = match['value']
    quote = value[0] if value[:1] in ('"', "'") else ''
    inner = value[1:-1] if quote else value
    if cli and match['label'].lower() == 'user' and ':' in inner:
        return _curl_user(match)
    if not cli and match['label'].lower() in ('cookie', 'set-cookie'):
        return match[0]  # The whole header was processed by _cookie_value.
    if (_is_reference(inner) or (match['label'].lower() == 'authorization'
                                         and inner.strip().lower() in ('bearer', 'basic'))):
        return match[0]
    # Python/JS variable assignments and keyword arguments are references, not values.
    # Uppercase .env assignments, CLI literals and colon-labelled data are explicit values.
    if (not cli and not quote and '=' in match['sep'] and match['label'].isascii()
            and not match['label'].isupper() and _IDENTIFIER.fullmatch(inner)):
        prefix = match.string[:match.start()].rstrip()
        if (inner == match['label'] or '_' in inner or inner.isupper() or '.' in inner
                or inner in ('value', 'secret', 'password', 'token', 'None', 'null')
                or (match['sep'] != '=' and ' ' in match['sep'])
                or prefix.endswith(('(', ','))):
            return match[0]
    replacement = quote + _replacement(inner, _kind(match['label'])) + quote
    return match[0][:match.start('value') - match.start()] + replacement


def _protected_sub(pattern, replace, text):
    references = [(start, end) for start, end, _ in _python_lookups(text)]

    def apply(match):
        for start, end in references:
            # A local expression may exceed a regex value's partial match. Protect
            # only that expression, or a match entirely contained in its span.
            if start <= match.start() and end >= match.end():
                return match[0]
            if ('value' in match.re.groupindex and match.start('value') == start
                    and match.end() <= end):
                return match[0]
        return replace(match) if callable(replace) else replace
    return pattern.sub(apply, text)


def _cookie_value(match):
    value = match['value']
    quote = value[0] if value[:1] in ('"', "'") else ''
    inner = value[1:-1] if quote else value
    if _is_reference(inner):
        return match[0]
    clean = _COOKIE_PAIR.sub(lambda pair: pair['name'] + '='
                             + _replacement(pair['value'].strip(), 'COOKIE'), inner)
    if not _COOKIE_PAIR.search(inner):
        clean = _replacement(inner, 'COOKIE')
    return match['prefix'] + quote + clean + quote


def _curl_user(match):
    value = match['value']
    quote = value[0] if value[:1] in ('"', "'") else ''
    inner = value[1:-1] if quote else value
    clean = ':'.join(_replacement(part, kind) for part, kind in
                     zip(inner.split(':', 1), ('ACCOUNT', 'PASSWORD')))
    return match[0][:match.start('value') - match.start()] + quote + clean + quote


def _cn_label_value(match):
    raw = match['value']
    tail = _CN_VALUE_TAIL.search(raw)
    value, suffix = (raw[:tail.start()], tail[0]) if tail else (raw, '')
    if len(value) < 3 or _is_reference(value):
        return match[0]
    return match[0][:match.start('value') - match.start()] + _replacement(value, _kind(match['label'])) + suffix


def _scan_text(text):
    for start, end, clean in reversed(list(_python_lookups(text))):
        text = text[:start] + clean + text[end:]
    text = _PRIVATE_KEY.sub('[REDACTED_PRIVATE_KEY]', text)
    text = _URL.sub(lambda m: m['scheme'] + _replacement(m['user'], 'ACCOUNT') + ':'
                    + _replacement(m['password'], 'PASSWORD') + '@', text)
    text = _protected_sub(_COOKIE, _cookie_value, text)
    text = _CURL_USER.sub(_curl_user, text)
    text = _protected_sub(_AUTH, lambda m: m['scheme'] + ' ' + _replacement(m['value'], 'TOKEN'), text)
    text = _protected_sub(_CLI, lambda m: _label_value(m, cli=True), text)
    text = _protected_sub(_ASSIGN, _label_value, text)
    text = _protected_sub(_CN_ASSIGN, _cn_label_value, text)
    text = _TOKEN.sub('[REDACTED_API_KEY]', text)
    return _JWT.sub('[REDACTED_TOKEN]', text)


def _yaml_blocks(text):
    cursor = 0
    for match in _YAML_BLOCK.finditer(text):
        if match.start() < cursor:
            continue
        base = len(match['indent']) + len(match['sequence'] or '')
        width = re.search(r'[1-9]', match['marker'])
        required = base + int(width[0]) if width else None
        end = match.end()
        first_indent = None
        for line in text[end:].splitlines(keepends=True):
            if line.strip():
                indent = len(line) - len(line.lstrip(' \t'))
                if indent <= base or (required is not None and indent < required):
                    break
                if required is None:
                    required = indent
                if first_indent is None:
                    first_indent = line[:indent]
            end += len(line)
        clean = text[match.start():end]
        if first_indent is not None:
            ending = '\r\n' if text[:end].endswith('\r\n') else ('\n' if text[:end].endswith('\n') else '')
            clean = match[0] + first_indent + '[REDACTED_' + _kind(match['label']) + ']' + ending
        cursor = end
        yield match.start(), end, clean


def redact_text(text):
    """Protect complete local lookups and YAML blocks before embedded JSON scanning."""
    pieces = list(_yaml_blocks(text))
    references = [(start, end) for start, end, _ in _python_lookups(text)]
    cursor = 0
    decoder = json.JSONDecoder()
    while cursor < len(text):
        start_match = _JSON_START.search(text, cursor)
        if start_match is None:
            break
        start = start_match.start()
        protected_end = next((end for begin, end in references if begin <= start < end), None)
        protected_end = protected_end or next((end for begin, end, _ in pieces if begin <= start < end), None)
        if protected_end is not None:
            cursor = protected_end
            continue
        try:
            parsed, end = decoder.raw_decode(text, start)
        except ValueError:
            # Consume a character; leave scanning until we have a full text span.
            cursor = start + 1
            continue
        clean = _data(parsed)
        pieces.append((start, end, text[start:end] if clean == parsed else json.dumps(clean, ensure_ascii=False)))
        cursor = end
    output = []
    cursor = 0
    for start, end, value in sorted(pieces):
        output.extend((_scan_text(text[cursor:start]), value))
        cursor = end
    output.append(_scan_text(text[cursor:]))
    return ''.join(output)


def _data(value, label=None):
    """Tool arguments/results and metadata have data keys, not protocol identifiers."""
    if isinstance(value, dict):
        return {k: _data(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_data(v, label) for v in value]
    if label and _KEY.fullmatch(label) and isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return _replacement(str(value), _kind(label))
    return redact_text(value) if isinstance(value, str) else value


def _text(value):
    if isinstance(value, str):
        # Embedded JSON stays valid; its keys are never rewritten.
        if value.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                clean = _data(parsed)
                return value if clean == parsed else json.dumps(clean, ensure_ascii=False)
        return redact_text(value)
    if isinstance(value, dict):
        return _data(value)
    if isinstance(value, list):
        return [_protocol(item, content_block=True) for item in value]
    return _protocol(value)


def _arguments(value):
    if not isinstance(value, str):
        return _data(value)
    try:
        parsed = json.loads(value)
    except ValueError:
        return redact_text(value)
    clean = _data(parsed)
    return value if clean == parsed else json.dumps(clean, ensure_ascii=False)


def _schema(value):
    if isinstance(value, list):
        return [_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key, item in value.items():
        if key == 'description' and isinstance(item, str):
            result[key] = redact_text(item)
        elif key in ('properties', '$defs', 'definitions') and isinstance(item, dict):
            result[key] = {name: _schema(spec) for name, spec in item.items()}
        elif key in ('items', 'anyOf', 'allOf', 'oneOf', 'additionalProperties'):
            result[key] = _schema(item)
    return result


def _protocol(value, content_block: bool | str = False):
    if isinstance(value, list):
        return [_protocol(item, content_block=content_block) for item in value]
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return value
    tag = value.get('type')
    if content_block:
        binary_fields = _BINARY_FIELDS.get(tag, set()) if isinstance(tag, str) else set()
        if binary_fields.intersection(value):
            binary_keys = binary_fields | {'type', 'id', 'detail', 'filename', 'mime_type', 'cache_control'}
            return {k: v if k in binary_keys else _data(v, k) for k, v in value.items()}
        gemini_part = content_block == 'gemini' and tag is None and any(k in value for k in (
            'inlineData', 'inline_data', 'fileData', 'file_data', 'functionCall', 'functionResponse', 'text'))
        if not gemini_part and (not isinstance(tag, str) or tag not in {
                'text', 'input_text', 'output_text', 'thinking', 'reasoning', 'tool_use', 'tool_result'}):
            return _data(value)
    result = dict(value)
    for key, item in value.items():
        if key in _SCHEMA_KEYS:
            result[key] = _schema(item)
        elif key == 'metadata':
            result[key] = _data(item)
        elif key in ('arguments', 'args'):
            result[key] = _arguments(item)
        elif key == 'input':
            result[key] = _data(item) if value.get('type') == 'tool_use' else _protocol(item)
        elif key == 'response' and 'name' in value:
            result[key] = _data(item)  # Gemini functionResponse
        elif ((key == 'content' and value.get('role') == 'tool')
              or (key == 'output' and tag == 'function_call_output')):
            result[key] = _data(item)
        elif key == 'text' and isinstance(item, dict) and 'format' in item:
            result[key] = _protocol(item)
        elif key in _TEXT_KEYS:
            result[key] = _text(item)
        elif key in _CONTAINERS:
            result[key] = _protocol(item, content_block='gemini' if key == 'parts' else False)
    return result


def redact_body(body: dict) -> dict:
    """Return a transformed request tree, preserving protocol and binary fields."""
    if not isinstance(body, dict):
        raise TypeError('model request must be an object')
    result = _protocol(body)
    assert isinstance(result, dict)
    return result
