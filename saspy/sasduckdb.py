#
# Copyright SAS Institute
#
#  Licensed under the Apache License, Version 2.0 (the License);
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""
Engine for SASsession.sas_to_duckdb(): submit a string of SAS code where every
PROC SQL / PROC FEDSQL block is executed by DuckDB instead of SAS, with results
returned to SAS. Non-SQL SAS segments run in SAS unchanged, in original order.

Two transports:
  'csv'  - Python owns the DuckDB connection; data moves through CSV files on
           a tmpfs directory (/dev/shm) in both directions.
  'jdbc' - SAS/ACCESS Interface to JDBC owns a DuckDB *file* database inside
           the SAS session's JVM; SQL blocks are rewritten into native PROC SQL
           explicit pass-through. Requires the DuckDB JDBC driver plus the
           org.saspy.duckdb.DuckDBSASDriver shim (source in saspy/java/duckdbsas)
           installed under the SAS deployment's JDBC DataDrivers directory.

Known limitations (both transports unless noted):
  * Only the STDIO (local, shared-filesystem) access method is supported.
  * SQL produced by macro expansion is not seen; a %if around a PROC SQL block
    does not make the reroute conditional; PROC SQL inside a %MACRO definition
    raises an error.
  * Semicolons inside SQL '--' line comments break statement splitting; use
    /* */ comments instead.
  * Embedded CR/LF in character data are replaced with spaces in transfers.
  * SQL must be DuckDB dialect. Statements other than CREATE TABLE ... AS
    SELECT, SELECT ... INTO :macrovar, and bare SELECT raise an error.
  * Double-quoted strings are converted to single-quoted literals (SAS
    semantics); use SAS name literals ('my col'n) for quoted identifiers.
  * jdbc transport: the DuckDB database is a file owned by the SAS JVM; a live
    Python duckdb connection cannot be used (DuckDB allows only one read-write
    process per database file). TIME values lose sub-second precision. TIMETZ
    result columns trip a fetch bug in the DuckDB JDBC driver - cast them
    (::time) in your SQL; the csv transport normalizes them automatically.
"""

import logging
import os
import re
import shutil
import tempfile
import time
from collections import namedtuple

from saspy.sasexceptions import (SASDuckDBError, SASDuckDBNotSupportedError,
                                 SASDuckDBMacroError, SASDuckDBExecutionError,
                                 SASDuckDBTransferError, SASDuckDBSASCodeError)

logger = logging.getLogger('saspy')


# ---------------------------------------------------------------------------
# statement lexer
# ---------------------------------------------------------------------------

Stmt = namedtuple('Stmt', ['start', 'end', 'text', 'words'])
# start/end: character offsets into the original source (end is one past the
# terminating ';' when present). text: verbatim slice. words: first up-to-3
# word tokens, lowercased, comments skipped ('*' kept as a token when a
# statement starts with a star comment).

_DATALINES_WORDS = frozenset(('datalines', 'cards', 'lines', 'parmcards'))
_DATALINES4_WORDS = frozenset(('datalines4', 'cards4', 'lines4', 'parmcards4'))

_WORD_RE = re.compile(r'[%]?[A-Za-z_][A-Za-z_0-9]*|\*|;')


def _stmt_words(text, maxwords=3):
    """First up-to-maxwords word tokens of a statement, lowercased, skipping
    /* */ comments. A leading '*' is returned as a token."""
    words = []
    i = 0
    n = len(text)
    while i < n and len(words) < maxwords:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        m = _WORD_RE.match(text, i)
        if not m:
            break
        tok = m.group(0)
        if tok == ';':
            break
        words.append(tok.lower())
        if tok == '*':
            break
        i = m.end()
    return tuple(words)


def _iter_statements(code):
    """Split SAS source into statements with a quote/comment/datalines-aware
    single-pass scan. Yields Stmt tuples covering all non-whitespace source;
    datalines data is included in the datalines statement's span."""
    n = len(code)
    i = 0
    stmt_start = 0
    state = 'CODE'
    seen_token = False      # non-comment content seen in current statement
    pending_datalines = None  # set to '1' or '4' after a datalines-style stmt

    def make(end):
        text = code[stmt_start:end]
        return Stmt(stmt_start, end, text, _stmt_words(text))

    while i < n:
        if pending_datalines is not None:
            # consume raw data lines until the terminator line
            four = pending_datalines == '4'
            pending_datalines = None
            while i < n:
                eol = code.find('\n', i)
                if eol < 0:
                    eol = n
                line = code[i:eol]
                if four:
                    done = line.startswith(';;;;')
                else:
                    done = line.lstrip()[:1] == ';'
                i = min(eol + 1, n)
                if done:
                    break
            # raw data: empty words so it can never look like a statement
            yield Stmt(stmt_start, i, code[stmt_start:i], ())
            stmt_start = i
            seen_token = False
            continue

        c = code[i]

        if state == 'CODE':
            if c == "'":
                state = 'SQUOTE'
                seen_token = True
            elif c == '"':
                state = 'DQUOTE'
                seen_token = True
            elif c == '/' and code.startswith('/*', i):
                j = code.find('*/', i + 2)
                i = n if j < 0 else j + 1  # +1 then +1 below
            elif c == '*' and not seen_token:
                state = 'STARCOMMENT'
                seen_token = True
            elif c == '%' and not seen_token and code.startswith('%*', i):
                state = 'STARCOMMENT'
                seen_token = True
                i += 1
            elif c == ';':
                stmt = make(i + 1)
                yield stmt
                w0 = stmt.words[0] if stmt.words else ''
                if w0 in _DATALINES_WORDS:
                    pending_datalines = '1'
                elif w0 in _DATALINES4_WORDS:
                    pending_datalines = '4'
                stmt_start = i + 1
                seen_token = False
            elif not c.isspace():
                seen_token = True
        elif state == 'SQUOTE':
            if c == "'":
                if code.startswith("''", i):
                    i += 1
                else:
                    state = 'CODE'
        elif state == 'DQUOTE':
            if c == '"':
                if code.startswith('""', i):
                    i += 1
                else:
                    state = 'CODE'
        elif state == 'STARCOMMENT':
            if c == ';':
                yield make(i + 1)
                stmt_start = i + 1
                seen_token = False
                state = 'CODE'
        i += 1

    if stmt_start < n and code[stmt_start:].strip():
        yield make(n)


# ---------------------------------------------------------------------------
# segmentation
# ---------------------------------------------------------------------------

class Segment(object):
    """One run of the submitted program: either passthrough SAS code or a
    PROC SQL / PROC FEDSQL block destined for DuckDB."""
    def __init__(self, kind, text, start, end, dialect=None, proc_options='',
                 stmts=None):
        self.kind = kind            # 'sas' | 'sql'
        self.text = text            # verbatim source slice
        self.start = start
        self.end = end
        self.dialect = dialect      # 'sql' | 'fedsql' (sql segments only)
        self.proc_options = proc_options
        self.stmts = stmts or []    # body Stmt list (sql segments only)

    def __repr__(self):
        return 'Segment({}, {}..{}, {!r})'.format(self.kind, self.start,
                                                  self.end, self.text[:40])


_SQL_BLOCK_ENDERS = frozenset(('proc', 'data', 'endsas', '%macro'))


def segment_code(code):
    """Split a SAS program into ordered 'sas' and 'sql' Segments.

    A sql segment starts at a PROC SQL/FEDSQL statement and ends inclusively
    at its QUIT; or exclusively at the next step boundary (proc/data/%macro/
    endsas) or end of source, matching SAS's implicit-quit behavior.
    """
    segments = []
    macro_depth = 0
    sas_start = 0           # start offset of the current sas segment
    cur_sql = None          # [proc_stmt, dialect, options, body_stmts]

    def flush_sas(upto):
        if code[sas_start:upto].strip():
            segments.append(Segment('sas', code[sas_start:upto], sas_start, upto))

    def close_sql(end_offset):
        proc_stmt, dialect, opts, body = cur_sql
        segments.append(Segment('sql', code[proc_stmt.start:end_offset],
                                proc_stmt.start, end_offset, dialect=dialect,
                                proc_options=opts, stmts=body))

    for stmt in _iter_statements(code):
        w = stmt.words
        if cur_sql is None:
            if len(w) >= 2 and w[0] == 'proc' and w[1] in ('sql', 'fedsql'):
                if macro_depth > 0:
                    raise SASDuckDBNotSupportedError(
                        'PROC {} inside a %MACRO definition cannot be rerouted '
                        'to DuckDB. Expand the macro or move the SQL out of '
                        'it.'.format(w[1].upper()))
                flush_sas(stmt.start)
                opts = _proc_options_text(stmt.text)
                cur_sql = [stmt, w[1], opts, []]
            else:
                if w[:1] == ('%macro',):
                    macro_depth += 1
                elif w[:1] == ('%mend',):
                    macro_depth = max(0, macro_depth - 1)
        else:
            if w[:1] == ('quit',):
                close_sql(stmt.end)
                cur_sql = None
                sas_start = stmt.end
            elif w[:1] and (w[0] in _SQL_BLOCK_ENDERS):
                close_sql(stmt.start)
                cur_sql = None
                sas_start = stmt.start
                # reprocess this statement as sas-segment content
                if len(w) >= 2 and w[0] == 'proc' and w[1] in ('sql', 'fedsql'):
                    if macro_depth > 0:
                        raise SASDuckDBNotSupportedError(
                            'PROC {} inside a %MACRO definition cannot be '
                            'rerouted to DuckDB.'.format(w[1].upper()))
                    opts = _proc_options_text(stmt.text)
                    cur_sql = [stmt, w[1], opts, []]
                elif w[0] == '%macro':
                    macro_depth += 1
            else:
                cur_sql[3].append(stmt)

    if cur_sql is not None:
        close_sql(len(code))
    else:
        flush_sas(len(code))
    return segments


def _proc_options_text(proc_stmt_text):
    """Text after 'proc sql|fedsql' up to the terminating ';'."""
    m = re.match(r'\s*proc\s+(?:sql|fedsql)\b(.*?);?\s*$',
                 proc_stmt_text, re.IGNORECASE | re.DOTALL)
    return (m.group(1).strip() if m else '')


# ---------------------------------------------------------------------------
# PROC statement options
# ---------------------------------------------------------------------------

_PROCOPT_IGNORED = frozenset(('stimer', 'nostimer', 'feedback', 'nofeedback',
                              'errorstop', 'noerrorstop', '_method', '_tree',
                              'sortmsg', 'nosortmsg', 'warnrecurs', 'nowarnrecurs',
                              'threads', 'nothreads', 'double', 'nodouble',
                              'flow', 'noflow', 'number', 'nonumber'))
_PROCOPT_ERROR = frozenset(('outobs', 'inobs', 'undo_policy', 'exec', 'noexec',
                            'loops', 'reduceput', 'reduceputobs', 'reduceputvalues'))


def parse_proc_options(opts_text, block=None):
    """Return {'noprint': bool, 'ignored': [..]}; raise on options that change
    semantics in ways we can't honor."""
    out = {'noprint': False, 'ignored': []}
    for tok in re.split(r'\s+', opts_text.strip()):
        if not tok:
            continue
        key = tok.split('=', 1)[0].lower()
        if key == 'noprint':
            out['noprint'] = True
        elif key == 'print':
            out['noprint'] = False
        elif key in _PROCOPT_IGNORED:
            out['ignored'].append(tok)
        elif key in _PROCOPT_ERROR:
            raise SASDuckDBNotSupportedError(
                "PROC SQL option '{}' is not supported when rerouting to "
                "DuckDB.".format(tok), block=block)
        else:
            raise SASDuckDBNotSupportedError(
                "Unrecognized PROC SQL option '{}' (not supported when "
                "rerouting to DuckDB).".format(tok), block=block)
    return out


# ---------------------------------------------------------------------------
# lightweight SQL token scanner (comment/quote aware)
# ---------------------------------------------------------------------------

Token = namedtuple('Token', ['kind', 'value', 'start', 'end'])
# kind: 'word' | 'str' (single-quoted) | 'dstr' (double-quoted) | 'colon-var'
#       | 'punct' (single char)

_TOK_WORD_RE = re.compile(r'[%&]?[A-Za-z_][A-Za-z_0-9$]*')
_TOK_NUM_RE = re.compile(r'\d[\w.]*')


def _scan_quoted(text, i, q):
    """Return end offset (one past closing quote) of a quoted literal starting
    at text[i] == q, honoring doubled-quote escapes."""
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == q:
            if j + 1 < n and text[j + 1] == q:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _sql_tokens(text):
    """Tokenize a SQL statement: words, quoted strings, :names, single punct
    chars. Skips whitespace and /* */ comments. '--' comments are NOT handled
    (documented limitation)."""
    i = 0
    n = len(text)
    toks = []
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        if text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in '\'"':
            j = _scan_quoted(text, i, c)
            toks.append(Token('str' if c == "'" else 'dstr', text[i:j], i, j))
            # name-literal / date-const suffix is picked up as a word token
            i = j
            continue
        if c == ':':
            m = _TOK_WORD_RE.match(text, i + 1)
            if m:
                toks.append(Token('colon-var', m.group(0), i, m.end()))
                i = m.end()
                continue
        m = _TOK_WORD_RE.match(text, i)
        if m:
            toks.append(Token('word', m.group(0), i, m.end()))
            i = m.end()
            continue
        m = _TOK_NUM_RE.match(text, i)
        if m:
            toks.append(Token('word', m.group(0), i, m.end()))
            i = m.end()
            continue
        toks.append(Token('punct', c, i, i + 1))
        i += 1
    return toks


# ---------------------------------------------------------------------------
# statement classification
# ---------------------------------------------------------------------------

class ClassifiedStmt(object):
    """A classified statement from inside a PROC SQL/FEDSQL block."""
    def __init__(self, kind, text, select_sql=None, libref='', table='',
                 into_specs=None, keyword=''):
        self.kind = kind              # 'ctas'|'select_into'|'select'|'ignore'
                                      # |'sas_passthrough'
        self.text = text
        self.select_sql = select_sql  # SQL to hand to DuckDB (INTO stripped)
        self.libref = libref          # ctas target
        self.table = table
        self.into_specs = into_specs or []  # [(name, sep_or_None, trimmed)]
        self.keyword = keyword


_SELECT_STARTERS = frozenset(('select', 'with', 'from', 'values'))

_UNSUPPORTED_LEADS = ('insert', 'update', 'delete', 'alter', 'drop', 'describe',
                      'validate', 'reset', 'options', 'connect', 'execute',
                      'disconnect', 'title', 'footnote')


def classify_sql_statement(stmt, block=None, snum=None):
    """Classify one statement of a SQL block. Raises SASDuckDBNotSupportedError
    for anything DuckDB rerouting cannot honor."""
    text = stmt.text
    body = text.rstrip()
    if body.endswith(';'):
        body = body[:-1]
    toks = _sql_tokens(body)
    if not toks:
        return ClassifiedStmt('ignore', text)
    w0 = toks[0].value.lower() if toks[0].kind == 'word' else ''

    if toks[0].kind == 'punct' and toks[0].value == '*':
        return ClassifiedStmt('ignore', text)
    if w0 in ('quit', 'run'):
        return ClassifiedStmt('ignore', text, keyword=w0)
    if w0 in ('%let', '%put'):
        return ClassifiedStmt('sas_passthrough', text, keyword=w0)

    if w0 == 'create':
        return _classify_create(body, toks, text, block, snum)

    if w0 in _SELECT_STARTERS:
        into_specs, stripped = _extract_into_clause(body, toks, block, snum)
        if into_specs:
            return ClassifiedStmt('select_into', text, select_sql=stripped,
                                  into_specs=into_specs)
        return ClassifiedStmt('select', text, select_sql=body)

    snippet = ' '.join(body.split())[:120]
    raise SASDuckDBNotSupportedError(
        "Statement '{}' is not supported in a DuckDB-rerouted SQL block: "
        "{!r}".format(w0 or toks[0].value, snippet), block=block, stmt=snum)


def _classify_create(body, toks, text, block, snum):
    words = [t.value.lower() if t.kind == 'word' else t.value for t in toks]
    if len(toks) < 2:
        raise SASDuckDBNotSupportedError('Malformed CREATE statement.',
                                         block=block, stmt=snum)
    if words[1] in ('view', 'index', 'unique'):
        raise SASDuckDBNotSupportedError(
            'CREATE {} is not supported in a DuckDB-rerouted SQL block.'
            .format(words[1].upper()), block=block, stmt=snum)
    if words[1] != 'table':
        raise SASDuckDBNotSupportedError(
            "CREATE {} is not supported.".format(words[1].upper()),
            block=block, stmt=snum)

    # target: word | word.word  (name literals / dataset options unsupported)
    idx = 2
    if idx >= len(toks) or toks[idx].kind != 'word':
        raise SASDuckDBNotSupportedError(
            'CREATE TABLE target must be a plain [libref.]name (name literals '
            'and dataset options are not supported).', block=block, stmt=snum)
    part1 = toks[idx].value
    idx += 1
    libref, table = '', part1
    if idx < len(toks) and toks[idx].kind == 'punct' and toks[idx].value == '.':
        idx += 1
        if idx >= len(toks) or toks[idx].kind != 'word':
            raise SASDuckDBNotSupportedError('Malformed CREATE TABLE target.',
                                             block=block, stmt=snum)
        libref, table = part1, toks[idx].value
        idx += 1
    if idx < len(toks) and toks[idx].kind == 'punct' and toks[idx].value == '(':
        raise SASDuckDBNotSupportedError(
            'CREATE TABLE with dataset options or a column-definition list is '
            'not supported; use CREATE TABLE ... AS SELECT.', block=block,
            stmt=snum)
    if idx >= len(toks) or not (toks[idx].kind == 'word'
                                and toks[idx].value.lower() == 'as'):
        raise SASDuckDBNotSupportedError(
            'Only CREATE TABLE ... AS SELECT is supported (LIKE/column '
            'definitions are not).', block=block, stmt=snum)
    idx += 1
    if idx >= len(toks):
        raise SASDuckDBNotSupportedError('CREATE TABLE ... AS requires a '
                                         'query.', block=block, stmt=snum)
    lead = toks[idx].value.lower() if toks[idx].kind == 'word' else toks[idx].value
    if not (lead in _SELECT_STARTERS or lead == '('):
        raise SASDuckDBNotSupportedError(
            'CREATE TABLE ... AS must be followed by a SELECT-style query.',
            block=block, stmt=snum)
    select_sql = body[toks[idx].start:]
    return ClassifiedStmt('ctas', text, select_sql=select_sql,
                          libref=libref, table=table)


def _extract_into_clause(body, toks, block, snum):
    """Find a top-level INTO clause; return (into_specs, sql_without_into).
    into_specs: [(macrovar, separator_or_None, trimmed_bool)]."""
    depth = 0
    into_i = None
    for i, t in enumerate(toks):
        if t.kind == 'punct':
            if t.value == '(':
                depth += 1
            elif t.value == ')':
                depth -= 1
        elif t.kind == 'word' and depth == 0:
            v = t.value.lower()
            if v == 'into':
                into_i = i
                break
            if v == 'from':
                break
    if into_i is None:
        return [], body

    specs = []
    i = into_i + 1
    end_i = None
    while i < len(toks):
        t = toks[i]
        if t.kind == 'colon-var':
            name = t.value
            sep = None
            trimmed = False
            i += 1
            # range form :a1-:a99 is unsupported
            if i < len(toks) and toks[i].kind == 'punct' and toks[i].value == '-':
                raise SASDuckDBNotSupportedError(
                    'INTO :var1-:varN macro variable ranges are not supported.',
                    block=block, stmt=snum)
            while i < len(toks) and toks[i].kind == 'word' and \
                    toks[i].value.lower() in ('separated', 'trimmed', 'notrim'):
                v = toks[i].value.lower()
                if v == 'separated':
                    if (i + 2 < len(toks)
                            and toks[i + 1].value.lower() == 'by'
                            and toks[i + 2].kind in ('str', 'dstr')):
                        sep = _unquote(toks[i + 2].value)
                        i += 3
                    else:
                        raise SASDuckDBNotSupportedError(
                            "Malformed SEPARATED BY in INTO clause.",
                            block=block, stmt=snum)
                elif v == 'trimmed':
                    trimmed = True
                    i += 1
                else:
                    raise SASDuckDBNotSupportedError(
                        'NOTRIM in INTO clause is not supported.',
                        block=block, stmt=snum)
            specs.append((name, sep, trimmed))
            if i < len(toks) and toks[i].kind == 'punct' and toks[i].value == ',':
                i += 1
                continue
            end_i = i
            break
        raise SASDuckDBNotSupportedError(
            'Unsupported INTO clause element {!r}.'.format(t.value),
            block=block, stmt=snum)
    if end_i is None:
        end_i = i

    start_off = toks[into_i].start
    end_off = toks[end_i].start if end_i < len(toks) else len(body)
    stripped = (body[:start_off] + body[end_off:]).strip()
    return specs, stripped


def _unquote(qstr):
    q = qstr[0]
    return qstr[1:-1].replace(q + q, q)


# ---------------------------------------------------------------------------
# SQL rewriting for DuckDB
# ---------------------------------------------------------------------------

_MACRO_REF_RE = re.compile(r'(&+)(\w+)(\.?)')

_SAS_DATE_FMTS = ('%d%b%Y', '%d%b%y')
_SAS_DT_FMTS = ('%d%b%Y:%H:%M:%S.%f', '%d%b%Y:%H:%M:%S', '%d%b%Y:%H:%M')
_SAS_TM_FMTS = ('%H:%M:%S.%f', '%H:%M:%S', '%H:%M')


def _convert_sas_literal(raw, suffix, block=None, snum=None):
    """Convert a SAS quoted constant ('...'d / '...'dt / '...'t) to a DuckDB
    literal, or a name literal ('x y'n) to a quoted identifier."""
    import datetime as _dt
    val = _unquote(raw)
    sfx = suffix.lower()
    if sfx == 'n':
        return '"' + val.replace('"', '""') + '"'
    if sfx == 'd':
        for f in _SAS_DATE_FMTS:
            try:
                d = _dt.datetime.strptime(val.strip(), f)
                if f.endswith('%y'):
                    raise ValueError('ambiguous 2-digit year')
                return "DATE '{}'".format(d.strftime('%Y-%m-%d'))
            except ValueError:
                continue
    elif sfx == 'dt':
        for f in _SAS_DT_FMTS:
            try:
                d = _dt.datetime.strptime(val.strip(), f)
                return "TIMESTAMP '{}'".format(d.strftime('%Y-%m-%d %H:%M:%S.%f'))
            except ValueError:
                continue
    elif sfx == 't':
        for f in _SAS_TM_FMTS:
            try:
                d = _dt.datetime.strptime(val.strip(), f)
                return "TIME '{}'".format(d.strftime('%H:%M:%S.%f'))
            except ValueError:
                continue
    raise SASDuckDBNotSupportedError(
        'Cannot convert SAS constant {}{} to a DuckDB literal.'.format(
            raw, suffix), block=block, stmt=snum)


def rewrite_sql(text, resolver=None, block=None, snum=None):
    """Rewrite one SQL statement for DuckDB:

    1. resolve &macrovar references (bare code and double-quoted strings only,
       matching SAS semantics) via resolver(name) -> str; resolver=None skips
       resolution (jdbc transport lets SAS resolve natively)
    2. convert "..." string literals to '...' (in SAS SQL, "..." is a string
       unless suffixed with n; in DuckDB it is an identifier)
    3. convert name literals '...'n / "..."n to "..." identifiers
    4. convert '...'d / '...'dt / '...'t constants to DATE/TIMESTAMP/TIME
    5. drop the SAS 'calculated' keyword
    """
    out = []
    i = 0
    n = len(text)

    def resolve_run(s):
        if resolver is None:
            return s
        def _sub(m):
            amps, name, dot = m.groups()
            if len(amps) > 1:
                raise SASDuckDBMacroError(
                    'Indirect macro references ({}{}) are not supported.'
                    .format(amps, name), block=block, stmt=snum)
            return resolver(name)
        return _MACRO_REF_RE.sub(_sub, s)

    plain_start = 0
    while i < n:
        c = text[i]
        if text.startswith('/*', i):
            out.append(resolve_run(text[plain_start:i]))
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(text[i:j])   # comments copied verbatim, no resolution
            i = j
            plain_start = i
            continue
        if c in '\'"':
            out.append(resolve_run(text[plain_start:i]))
            j = _scan_quoted(text, i, c)
            raw = text[i:j]
            m = re.match(r'(dt|[dtn])\b', text[j:j + 2], re.IGNORECASE)
            if m:
                sfx = m.group(0)
                if c == '"' and sfx.lower() != 'n':
                    raw = "'" + _unquote(raw).replace("'", "''") + "'"
                out.append(_convert_sas_literal(raw, sfx, block, snum))
                i = j + len(sfx)
            elif c == '"':
                inner = resolve_run(_unquote(raw))
                out.append("'" + inner.replace("'", "''") + "'")
                i = j
            else:
                out.append(raw)
                i = j
            plain_start = i
            continue
        i += 1
    out.append(resolve_run(text[plain_start:]))

    result = ''.join(out)
    result = re.sub(r'\bcalculated\s+', '', result, flags=re.IGNORECASE)
    return result


# ---------------------------------------------------------------------------
# table discovery (which tables does a query reference?)
# ---------------------------------------------------------------------------

_PLAIN_NAME_RE = re.compile(r'^[A-Za-z_]\w*$')


def find_table_refs(parser_con, sql, block=None, snum=None):
    """Return an ordered list of (schema, table) BASE_TABLE references in sql,
    minus CTE names, using DuckDB's own parser (json_serialize_sql). Table
    functions (read_parquet/read_csv/...) never appear as BASE_TABLE refs.
    Raises SASDuckDBExecutionError if DuckDB cannot parse the statement."""
    import json as _json
    js = parser_con.execute('select json_serialize_sql(?)', [sql]).fetchone()[0]
    ast = _json.loads(js)
    if ast.get('error'):
        raise SASDuckDBExecutionError(
            'DuckDB could not parse rerouted SQL ({}: {} at position {}).'
            .format(ast.get('error_type'), ast.get('error_message'),
                    ast.get('position')), block=block, stmt=snum)

    refs = []
    ctes = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get('type') == 'BASE_TABLE':
                key = (node.get('schema_name') or '', node.get('table_name') or '')
                if key not in refs:
                    refs.append(key)
            cm = node.get('cte_map')
            if isinstance(cm, dict):
                for ent in cm.get('map') or []:
                    if isinstance(ent, dict) and ent.get('key'):
                        ctes.add(ent['key'].lower())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(ast)
    return [(s, t) for (s, t) in refs if not (s == '' and t.lower() in ctes)]


# ---------------------------------------------------------------------------
# SAS-side schema probe and CSV export (the "ship" path)
# ---------------------------------------------------------------------------

SasColumn = namedtuple('SasColumn', ['name', 'ctype', 'length', 'fmtname'])
# ctype: 'char' | 'num' | 'date' | 'time' | 'datetime'


def _sas_name_ref(libref, table):
    """lib.'table'n reference usable in SAS code (libref may be '')."""
    tbl = "'" + table.replace("'", "''") + "'n"
    return (libref + '.' + tbl) if libref else tbl


def probe_sas_table_schema(sas, libref, table, block=None):
    """One submit: read variable names/types/formats of a SAS dataset via the
    open()/varname()/vartype()/varfmt() functions, echoed to the log with
    markers. Returns ordered [SasColumn]."""
    ref = _sas_name_ref(libref or 'work', table)
    code = (
        "data _null_;\n"
        "  dsid = open(\"{}\");\n"
        "  if dsid = 0 then do; put 'SPDDB_OPENFAIL='; stop; end;\n"
        "  nv = attrn(dsid, 'NVARS');\n"
        "  put 'SPDDB_NVARS=' nv;\n"
        "  length _n $32 _f $49;\n"
        "  do i = 1 to nv;\n"
        "    _n = varname(dsid, i); _t = vartype(dsid, i);\n"
        "    _l = varlen(dsid, i);  _f = varfmt(dsid, i);\n"
        "    put 'SPDDB_V=' _t +(-1) '|' _l +(-1) '|' _f +(-1) '|' _n;\n"
        "  end;\n"
        "  put 'SPDDB_DONE=';\n"
        "  rc = close(dsid);\n"
        "run;\n").format(ref.replace('"', '""'))
    ll = sas._io.submit(code, results='text')
    log = ll['LOG']
    # match markers at line start only: SAS echoes the submitted source into
    # the log, so a substring test would match the echoed PUT statements
    lines = log.splitlines()
    openfail = any(l.startswith('SPDDB_OPENFAIL=') for l in lines)
    done = any(l.startswith('SPDDB_DONE=') for l in lines)
    if openfail or not done:
        raise SASDuckDBTransferError(
            'Could not open SAS dataset {} to read its schema.\n{}'.format(
                _sas_name_ref(libref, table), log[-800:]), block=block)

    cols = []
    for line in lines:
        if not line.startswith('SPDDB_V='):
            continue
        body = line[len('SPDDB_V='):]
        vtype, vlen, vfmt, vname = body.split('|', 3)
        vtype = vtype.strip().upper()
        fmtname = re.sub(r'[\d.]+$', '', vfmt.strip().upper())
        if vtype == 'C':
            ctype = 'char'
        elif fmtname and fmtname in sas.sas_date_fmts:
            ctype = 'date'
        elif fmtname and fmtname in sas.sas_time_fmts:
            ctype = 'time'
        elif fmtname and fmtname in sas.sas_datetime_fmts:
            ctype = 'datetime'
        else:
            ctype = 'num'
        cols.append(SasColumn(vname, ctype, int(vlen.strip()), fmtname))
    return cols


_DUCK_TYPE_FOR = {'char': 'VARCHAR', 'num': 'DOUBLE', 'date': 'DATE',
                  'time': 'TIME', 'datetime': 'TIMESTAMP'}


def build_export_code(libref, table, cols, csv_path, viewname='_spddb_v'):
    """SAS code exporting a dataset to CSV with ISO date/time formats and
    CR/LF scrubbed from character values."""
    ref = _sas_name_ref(libref or 'work', table)
    fmt_groups = {'date': [], 'datetime': [], 'time': [], 'num': []}
    scrubs = []
    for c in cols:
        nl = "'" + c.name.replace("'", "''") + "'n"
        if c.ctype == 'char':
            scrubs.append("  {} = translate({}, '  ', '0A0D'x);\n".format(nl, nl))
        else:
            fmt_groups[c.ctype].append(nl)
    fmts = ''
    for names, f in ((fmt_groups['date'], 'E8601DA10.'),
                     (fmt_groups['datetime'], 'E8601DT26.6'),
                     (fmt_groups['time'], 'E8601TM15.6'),
                     (fmt_groups['num'], 'BEST32.')):
        if names:
            fmts += '  format {} {};\n'.format(' '.join(names), f)
    code = ("options missing=' ';\n"
            "data work.{vw} / view=work.{vw};\n"
            "  set {ref};\n"
            "{fmts}{scrubs}"
            "run;\n"
            "proc export data=work.{vw} outfile=\"{csv}\" dbms=csv replace; run;\n"
            "proc delete data=work.{vw}(memtype=view); run;\n"
            "options missing='.';\n").format(vw=viewname, ref=ref, fmts=fmts,
                                             scrubs=''.join(scrubs),
                                             csv=csv_path.replace('"', '""'))
    return code


def build_read_csv_sql(csv_path, cols, encoding):
    """DuckDB read_csv(...) call with explicit column names/types in dataset
    order (read_csv validates them positionally against the header)."""
    coldefs = ', '.join("'{}': '{}'".format(c.name.replace("'", "''"),
                                            _DUCK_TYPE_FOR[c.ctype])
                        for c in cols)
    return ("read_csv('{p}', header=true, nullstr='', columns={{{cd}}}, "
            "dateformat='%Y-%m-%d', timestampformat='%Y-%m-%dT%H:%M:%S.%f', "
            "encoding='{enc}')").format(p=csv_path.replace("'", "''"),
                                        cd=coldefs, enc=encoding)


# ---------------------------------------------------------------------------
# fast DuckDB -> SAS loader (csv transport)
# ---------------------------------------------------------------------------

_UNSUPPORTED_DUCK_TYPES = ('LIST', 'STRUCT', 'MAP', 'UNION', 'BLOB', 'BIT',
                           'INTERVAL', 'ARRAY')
_PRECISION_LOSS_TYPES = ('BIGINT', 'HUGEINT', 'UBIGINT', 'UHUGEINT')

SAS_MAX_CHAR = 32767


_CHAR_BASES = ('VARCHAR', 'CHAR', 'UUID', 'ENUM')


def _is_char_dtype(dtype):
    return any(dtype.startswith(b) for b in _CHAR_BASES)


def _upper_dict(d):
    return {k.upper(): v for k, v in d.items()} if d else {}


def _build_load_codegen(cols, char_len, tmpdir_or_none, libref, table,
                        outfmts=None, labels=None, outdsopts=None,
                        datetimes=None, annotate=None, block=None, snum=None,
                        csv_path=None):
    """Per-column codegen shared by the load and codegen-only paths.

    Returns (sel_parts, data_step_code). cols: [(name, DTYPE)]; char_len:
    {name: bytes}; csv_path: real path, or a placeholder for codegen-only.
    """
    fmt_upper = _upper_dict(outfmts)
    lab_upper = _upper_dict(labels)
    dts_upper = _upper_dict(datetimes)

    sel_parts = []
    lengths = []
    formats = []
    label_lines = []
    inputs = []
    for name, dtype in cols:
        qn = '"' + name.replace('"', '""') + '"'
        nl = "'" + name.replace("'", "''") + "'n"
        up = name.upper()
        base = dtype.split('(')[0]
        default_fmt = None
        if base in _UNSUPPORTED_DUCK_TYPES or dtype.endswith('[]'):
            raise SASDuckDBNotSupportedError(
                'Result column "{}" has DuckDB type {} which cannot be '
                'loaded into SAS. Cast it in your SQL.'.format(name, dtype),
                block=block, stmt=snum)
        if base in _PRECISION_LOSS_TYPES and annotate:
            annotate('warning: column "{}" ({}) may lose precision in SAS '
                     'numeric (double)'.format(name, dtype))
        if base == 'DECIMAL':
            m = re.match(r'DECIMAL\((\d+)', dtype)
            if m and int(m.group(1)) > 15 and annotate:
                annotate('warning: column "{}" ({}) may lose precision in '
                         'SAS numeric (double)'.format(name, dtype))
        as_date = (base.startswith('TIMESTAMP') or base == 'DATETIME') and \
            str(dts_upper.get(up, '')).lower() == 'date'
        if name in char_len:
            sel_parts.append(
                "replace(replace({q}::varchar, chr(13), ' '), chr(10), ' ')"
                ' as {q}'.format(q=qn))
            lengths.append('{} ${}'.format(nl, char_len[name]))
            inputs.append('{} : ${}.'.format(nl, char_len[name]))
        elif base == 'BOOLEAN':
            sel_parts.append('{q}::int as {q}'.format(q=qn))
            lengths.append('{} 8'.format(nl))
            inputs.append(nl)
        elif base == 'DATE' or as_date:
            sel_parts.append('{q}::date as {q}'.format(q=qn)
                             if as_date else qn)
            lengths.append('{} 8'.format(nl))
            default_fmt = 'E8601DA.'
            inputs.append('{} : ??yymmdd10.'.format(nl))
        elif base.startswith('TIMESTAMP') or base == 'DATETIME':
            # covers TIMESTAMP, DATETIME, TIMESTAMP_NS/_MS/_S, and
            # TIMESTAMP WITH TIME ZONE - all normalized to microsecond
            # timezone-naive timestamps for the CSV
            if base in ('TIMESTAMP', 'DATETIME'):
                sel_parts.append(qn)
            else:
                if annotate:
                    if 'ZONE' in base or 'TZ' in base:
                        annotate('note: column "{}" converted to '
                                 'timezone-naive timestamp'.format(name))
                    elif base == 'TIMESTAMP_NS':
                        annotate('note: column "{}" truncated from nano- '
                                 'to microsecond precision'.format(name))
                sel_parts.append('{q}::timestamp as {q}'.format(q=qn))
            lengths.append('{} 8'.format(nl))
            default_fmt = 'E8601DT26.6'
            inputs.append('{} : ??e8601dt26.6'.format(nl))
        elif base.startswith('TIME'):
            # covers TIME and TIME WITH TIME ZONE
            if base == 'TIME':
                sel_parts.append(qn)
            else:
                if annotate:
                    annotate('note: column "{}" converted to timezone-'
                             'naive time'.format(name))
                sel_parts.append('{q}::time as {q}'.format(q=qn))
            lengths.append('{} 8'.format(nl))
            default_fmt = 'E8601TM15.6'
            inputs.append('{} : ??e8601tm15.6'.format(nl))
        else:
            sel_parts.append(qn)
            lengths.append('{} 8'.format(nl))
            inputs.append(nl)

        fmt = fmt_upper.get(up, default_fmt)
        if fmt:
            formats.append('{} {}'.format(nl, fmt))
        if up in lab_upper:
            label_lines.append('  label {} = "{}";\n'.format(
                nl, str(lab_upper[up]).replace('"', '""')))

    lrecl = max(1048576, sum(char_len.values()) + 64 * len(cols))
    target = _sas_name_ref(libref, table)
    if outdsopts:
        target += '({})'.format(' '.join(
            '{}={}'.format(k, v) for k, v in outdsopts.items()))
    code = 'data {};\n'.format(target)
    code += '  length {};\n'.format(' '.join(lengths))
    if formats:
        code += '  format {};\n'.format(' '.join(formats))
    code += ''.join(label_lines)
    code += ("  infile '{p}' dlm=',' dsd firstobs=2 missover lrecl={l} "
             "encoding='utf-8' termstr=LF;\n").format(
                 p=(csv_path or '<csv written by DuckDB COPY>')
                 .replace("'", "''"), l=lrecl)
    code += '  input {};\nrun;\n'.format(' '.join(inputs))
    return sel_parts, code


def _validate_char_len(name, n, block=None, snum=None):
    if n > SAS_MAX_CHAR:
        raise SASDuckDBTransferError(
            'Column "{}" has values longer than the SAS maximum of '
            '{} bytes.'.format(name, SAS_MAX_CHAR), block=block, stmt=snum)
    return max(1, int(n))


def duckdb_relation_to_sas_table(sas, con, select_sql, libref, table, tmpdir,
                                 annotate=None, tempkeep=False, keep_as=None,
                                 outfmts=None, labels=None, outdsopts=None,
                                 char_lengths=None, datetimes=None,
                                 codegen_only=False, block=None, snum=None):
    """Materialize select_sql once in DuckDB, COPY it to a tmpfs CSV, and load
    it into SAS with a generated DATA step. Returns the row count.

    keep_as: (schema, name) to keep the materialized result in the connection
    (used so later SQL blocks can reference a CTAS result without re-shipping);
    None materializes to a session-temp table dropped afterward.

    Optional per-column overrides (dicts keyed by column name, case
    insensitive): outfmts (SAS format replacing the default), labels,
    outdsopts (dataset options on the created table), char_lengths (explicit
    byte lengths - covered columns are excluded from the strlen scan),
    datetimes ({col: 'date'} loads a TIMESTAMP column as a SAS date).

    codegen_only=True generates and returns the DATA step code WITHOUT
    executing anything: schema comes from a bind-only DESCRIBE, char lengths
    from char_lengths or a 1024 placeholder (teach_me_SAS support).
    """
    import duckdb as _duckdb

    chr_upper = _upper_dict(char_lengths)

    if codegen_only:
        try:
            schema_rows = con.execute('describe ({})'.format(select_sql)
                                      ).fetchall()
        except _duckdb.Error as e:
            raise SASDuckDBExecutionError(
                'DuckDB failed parsing SQL: {}'.format(e),
                block=block, stmt=snum) from e
        cols = [(r[0], r[1].upper()) for r in schema_rows]
        char_len = {}
        for n, t in cols:
            if _is_char_dtype(t):
                given = chr_upper.get(n.upper())
                char_len[n] = (_validate_char_len(n, given, block, snum)
                               if given else 1024)
        _sel, code = _build_load_codegen(
            cols, char_len, None, libref, table, outfmts=outfmts,
            labels=labels, outdsopts=outdsopts, datetimes=datetimes,
            block=block, snum=snum, csv_path=None)
        note = ('/* teach_me_SAS: generated without executing the query; '
                'unlisted char lengths default to 1024 (computed from the '
                'data on a real run) */\n')
        return note + code

    if keep_as:
        schema, dname = keep_as
        if schema:
            con.execute('create schema if not exists "{}"'.format(
                schema.replace('"', '""')))
            qual = '"{}"."{}"'.format(schema.replace('"', '""'),
                                      dname.replace('"', '""'))
        else:
            qual = '"{}"'.format(dname.replace('"', '""'))
        create = 'create or replace table {} as {}'.format(qual, select_sql)
    else:
        qual = '"_spddb_result"'
        create = ('create or replace temp table {} as {}'
                  .format(qual, select_sql))

    t0 = time.time()
    try:
        con.execute(create)
    except _duckdb.Error as e:
        raise SASDuckDBExecutionError(
            'DuckDB failed executing rerouted SQL: {}'.format(e),
            block=block, stmt=snum) from e

    csv_path = None
    try:
        nrows = con.execute('select count(*) from {}'.format(qual)).fetchone()[0]
        schema_rows = con.execute('describe {}'.format(qual)).fetchall()
        cols = [(r[0], r[1].upper()) for r in schema_rows]

        # char lengths in BYTES (strlen); explicit char_lengths skip the
        # scan for their columns, and the scan is skipped entirely when all
        # char columns are covered
        char_cols = [n for n, t in cols if _is_char_dtype(t)]
        char_len = {}
        unmapped = []
        for n in char_cols:
            given = chr_upper.get(n.upper())
            if given:
                char_len[n] = _validate_char_len(n, given, block, snum)
            else:
                unmapped.append(n)
        if unmapped and nrows:
            aggs = ', '.join('max(strlen("{}"::varchar))'
                             .format(n.replace('"', '""')) for n in unmapped)
            vals = con.execute('select {} from {}'.format(aggs, qual)).fetchone()
            for n, v in zip(unmapped, vals):
                char_len[n] = _validate_char_len(n, int(v or 1), block, snum)
        for n in char_cols:
            char_len.setdefault(n, 1)

        csv_path = os.path.join(tmpdir, '_spddb_{}_{}.csv'.format(
            os.getpid(), abs(hash((libref, table, snum))) % 99991))
        sel_parts, code = _build_load_codegen(
            cols, char_len, tmpdir, libref, table, outfmts=outfmts,
            labels=labels, outdsopts=outdsopts, datetimes=datetimes,
            annotate=annotate, block=block, snum=snum, csv_path=csv_path)

        con.execute(
            "copy (select {sel} from {q}) to '{p}' (format csv, header true, "
            "nullstr '', dateformat '%Y-%m-%d', "
            "timestampformat '%Y-%m-%dT%H:%M:%S.%f')".format(
                sel=', '.join(sel_parts), q=qual,
                p=csv_path.replace("'", "''")))
        t_copy = time.time()

        ll = sas._io.submit(code, results='text')
        if sas._io._checkLogForError(ll['LOG']):
            raise SASDuckDBTransferError(
                'SAS logged an ERROR loading the DuckDB result into {}:\n{}'
                .format(_sas_name_ref(libref, table), ll['LOG'][-1500:]),
                block=block, stmt=snum)
        if annotate:
            annotate('loaded result -> SAS {} ({} rows, copy {:.2f}s, '
                     'load {:.2f}s)'.format(
                         (libref + '.' if libref else 'WORK.') + table, nrows,
                         t_copy - t0, time.time() - t_copy))
        return nrows
    finally:
        if not keep_as:
            try:
                con.execute('drop table if exists "_spddb_result"')
            except Exception:
                pass
        try:
            if not tempkeep and csv_path and os.path.exists(csv_path):
                os.remove(csv_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------

_ENC_MAP = {'utf-8': 'utf-8', 'utf8': 'utf-8', 'latin1': 'latin-1',
            'wlatin1': 'latin-1', 'latin9': 'latin-1'}

_JDBC_DEFAULT_CLASSPATH = \
    '/usr/local/SASHome/AccessClients/9.4/DataDrivers/jdbc/duckdb'


class SasToDuckDB(object):
    """Executes a segmented SAS program, rerouting SQL blocks to DuckDB.

    Not part of the public API; use SASsession.sas_to_duckdb().
    """

    def __init__(self, sas, con=None, transport='csv', duckdb_file=None,
                 jdbc_classpath=None, jdbc_text_len=1024, results='text',
                 tempdir=None, tempkeep=False, keep_duckdb_objects=False,
                 render_limit=1000, stop_on_sas_error=True, echo_sql=True):
        self.sas = sas
        self.con = con
        self.transport = transport
        self.duckdb_file = duckdb_file
        self.jdbc_classpath = jdbc_classpath or _JDBC_DEFAULT_CLASSPATH
        self.jdbc_text_len = jdbc_text_len
        self.results = (results or sas.results or 'text').lower()
        if self.results == 'pandas':
            self.results = 'html'
        self.tempkeep = tempkeep
        self.keep_duckdb_objects = keep_duckdb_objects
        self.render_limit = render_limit
        self.stop_on_sas_error = stop_on_sas_error
        self.echo_sql = echo_sql

        self._tempdir_arg = tempdir
        self.tmpdir = None
        self._own_tmpdir = False
        self._log = []             # ordered LOG pieces (SAS logs + DUCKDB: lines)
        self._lst = []             # ordered LST pieces
        self._macro_cache = {}
        self._shipped = set()      # (schema_lower, table_lower) present in duckdb
        self._created = []         # duckdb objects we created (for cleanup)
        self._jdbc_db_own = False
        self._encoding = None
        self._parser_con = None    # in-memory duckdb used only for parsing

    # -- infrastructure ----------------------------------------------------

    def _annotate(self, msg):
        line = 'DUCKDB: ' + msg
        self._log.append(line + '\n')

    def _submit(self, code, results='text'):
        ll = self.sas._io.submit(code, results=results)
        return ll

    def _sas_encoding(self):
        if self._encoding:
            return self._encoding
        ll = self._submit('%put SPDDBENC=&sysencoding;')
        enc = ''
        for line in ll['LOG'].splitlines():
            if line.startswith('SPDDBENC=') and '&' not in line:
                enc = line.split('=', 1)[1].strip().lower()
        codec = _ENC_MAP.get(enc)
        if codec is None:
            logger.warning('SAS session encoding %r is not one sas_to_duckdb '
                           'knows how to hand to DuckDB; assuming utf-8. '
                           'Non-ASCII characters may be corrupted.', enc)
            codec = 'utf-8'
        if codec != 'utf-8':
            self._annotate('SAS session encoding is {}; CSVs shipped to DuckDB '
                           'are read as {}'.format(enc, codec))
        self._encoding = codec
        return codec

    def _resolver(self, name):
        key = name.lower()
        if key in self._macro_cache:
            return self._macro_cache[key]
        if not self.sas.symexist(name):
            raise SASDuckDBMacroError(
                "Macro variable &{} is not defined.".format(name))
        val = self.sas.symget(name, outtype='str')
        self._macro_cache[key] = val
        return val

    def _invalidate_sas_state(self, macro_only=False):
        self._macro_cache.clear()
        if not macro_only:
            self._shipped.clear()

    # -- planning (teach_me_SAS) --------------------------------------------

    def nosub_plan(self, code):
        segments = segment_code(code)
        plan = []
        bnum = 0
        for seg in segments:
            if seg.kind == 'sas':
                plan.append('/* --- SAS segment (runs in SAS unchanged) --- */')
                plan.append(seg.text.strip('\n'))
            else:
                bnum += 1
                plan.append('/* --- PROC {} block {} -> DuckDB ({} transport)'
                            '{} --- */'.format(seg.dialect.upper(), bnum,
                                               self.transport,
                                               '; options: ' + seg.proc_options
                                               if seg.proc_options else ''))
                for k, stmt in enumerate(seg.stmts, 1):
                    c = classify_sql_statement(stmt, block=bnum, snum=k)
                    note = {'ctas': 'CREATE TABLE {} <- DuckDB result'.format(
                                (c.libref + '.' if c.libref else 'WORK.') + c.table),
                            'select_into': 'SELECT INTO {} via DuckDB'.format(
                                ', '.join(':' + s[0] for s in c.into_specs)),
                            'select': 'SELECT rendered from DuckDB',
                            'sas_passthrough': 'macro statement runs in SAS',
                            'ignore': 'ignored'}[c.kind]
                    plan.append('/* stmt {}: {} */'.format(k, note))
                    plan.append(stmt.text.strip('\n'))
        return {'LOG': '\n'.join(plan) + '\n', 'LST': ''}

    # -- main loop -----------------------------------------------------------

    def run(self, code):
        import duckdb as _duckdb

        segments = segment_code(code)
        if self.sas.nosub:
            return self.nosub_plan(code)

        base = self._tempdir_arg or ('/dev/shm' if os.path.isdir('/dev/shm')
                                     else tempfile.gettempdir())
        self.tmpdir = tempfile.mkdtemp(prefix='saspy_ddb_', dir=base)
        self._own_tmpdir = True

        if self.transport == 'csv':
            if self.con is None:
                self.con = _duckdb.connect()
                self._own_con = True
            else:
                self._own_con = False
            self._parser_con = self.con
        else:
            self._parser_con = _duckdb.connect()   # parser only, in-memory
            if self.duckdb_file is None:
                self.duckdb_file = os.path.join(self.tmpdir, 'sas_to_duckdb.db')
                self._jdbc_db_own = True

        err = None
        try:
            bnum = 0
            for seg in segments:
                if seg.kind == 'sas':
                    self._run_sas_segment(seg)
                else:
                    bnum += 1
                    if self.transport == 'csv':
                        self._run_sql_block_csv(seg, bnum)
                    else:
                        self._run_sql_block_jdbc(seg, bnum)
            return {'LOG': ''.join(self._log), 'LST': ''.join(self._lst)}
        except SASDuckDBError as e:
            e.partial = {'LOG': ''.join(self._log), 'LST': ''.join(self._lst)}
            raise
        finally:
            self._cleanup()

    def _cleanup(self):
        if self.transport == 'csv' and self.con is not None:
            if not self.keep_duckdb_objects:
                for kind, qual in reversed(self._created):
                    try:
                        self.con.execute('drop {} if exists {}'.format(kind, qual))
                    except Exception:
                        pass
            if getattr(self, '_own_con', False):
                try:
                    self.con.close()
                except Exception:
                    pass
        if self._parser_con is not None and self._parser_con is not self.con:
            try:
                self._parser_con.close()
            except Exception:
                pass
        if self._jdbc_db_own and self.duckdb_file and not self.tempkeep:
            for f in (self.duckdb_file, self.duckdb_file + '.wal'):
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except Exception:
                    pass
        if self._own_tmpdir and self.tmpdir and not self.tempkeep:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- SAS segments ----------------------------------------------------------

    def _run_sas_segment(self, seg):
        res = 'html' if self.results == 'html' else 'text'
        t0 = time.time()
        ll = self._submit(seg.text, results=res)
        self._log.append(ll['LOG'])
        if ll['LST']:
            self._lst.append(ll['LST'])
        self._invalidate_sas_state()
        if self.sas._io._checkLogForError(ll['LOG']):
            self.sas.check_error_log = True
            if self.stop_on_sas_error:
                raise SASDuckDBSASCodeError(
                    'A SAS segment logged an ERROR (stop_on_sas_error=True):\n'
                    + ll['LOG'][-1500:])
            self._annotate('warning: ERROR in SAS segment log; continuing '
                           '(stop_on_sas_error=False)')

    # -- shipping ---------------------------------------------------------------

    def _resolve_target_schema(self, libref):
        """DuckDB schema name for a SAS libref. Unqualified names use DuckDB's
        default (main) schema, because unqualified references in the user's
        SQL resolve there."""
        return libref.lower() if libref else ''

    def _ship_missing_tables(self, sql, bnum, snum, jdbc_pieces=None):
        """Find table refs in sql; ship any that are SAS datasets not yet in
        DuckDB. For jdbc transport, append 'execute (...) by ddb' pieces to
        jdbc_pieces instead of importing via the Python connection."""
        refs = find_table_refs(self._parser_con, sql, block=bnum, snum=snum)
        for schema, tbl in refs:
            key = ((schema or 'main').lower(), tbl.lower())
            if key in self._shipped:
                continue
            if self.transport == 'csv' and not schema:
                # unqualified: present in duckdb already? (user tables, temps)
                try:
                    self.con.execute('select 1 from "{}" limit 0'
                                     .format(tbl.replace('"', '""')))
                    continue
                except Exception:
                    pass
            elif self.transport == 'csv':
                try:
                    self.con.execute('select 1 from "{}"."{}" limit 0'.format(
                        schema.replace('"', '""'), tbl.replace('"', '""')))
                    continue
                except Exception:
                    pass
            if not _PLAIN_NAME_RE.match(tbl) or \
               (schema and not _PLAIN_NAME_RE.match(schema)):
                continue    # let DuckDB report it if it truly doesn't resolve
            libref = schema or ''
            if not self.sas.exist(tbl, libref):
                continue    # not a SAS table; DuckDB will error if unresolvable
            self._ship_table(libref, tbl, schema, bnum, jdbc_pieces)

    def _ship_table(self, libref, tbl, schema, bnum, jdbc_pieces=None):
        t0 = time.time()
        cols = probe_sas_table_schema(self.sas, libref, tbl, block=bnum)
        csv_path = os.path.join(self.tmpdir, 'ship_{}_{}.csv'.format(
            (libref or 'work').lower(), tbl.lower()))
        code = build_export_code(libref, tbl, cols, csv_path)
        ll = self._submit(code)
        if self.sas._io._checkLogForError(ll['LOG']):
            raise SASDuckDBTransferError(
                'SAS logged an ERROR exporting {} for DuckDB:\n{}'.format(
                    _sas_name_ref(libref, tbl), ll['LOG'][-1500:]), block=bnum)
        rc_sql = build_read_csv_sql(csv_path, cols, self._sas_encoding())
        target_schema = schema.lower() if schema else ''
        if target_schema:
            qual = '"{}"."{}"'.format(target_schema, tbl.lower())
        else:
            qual = '"{}"'.format(tbl.lower())   # DuckDB main schema
        if self.transport == 'csv':
            if target_schema:
                self.con.execute('create schema if not exists "{}"'
                                 .format(target_schema))
            self.con.execute('create or replace table {} as select * from {}'
                             .format(qual, rc_sql))
            nrows = self.con.execute('select count(*) from {}'
                                     .format(qual)).fetchone()[0]
            self._created.append(('table', qual))
            if not self.tempkeep:
                try:
                    os.remove(csv_path)
                except OSError:
                    pass
            self._annotate('shipped SAS table {} -> duckdb {} ({} rows, '
                           '{:.2f}s)'.format(
                               (libref or 'WORK').upper() + '.' + tbl.upper(),
                               qual, nrows, time.time() - t0))
        else:
            if target_schema:
                jdbc_pieces.append('execute (create schema if not exists "{}") '
                                   'by ddb;'.format(target_schema))
            jdbc_pieces.append('execute (create or replace table {} as '
                               'select * from {}) by ddb;'.format(qual, rc_sql))
            self._annotate('shipping SAS table {} -> duckdb {} (export '
                           '{:.2f}s; import runs in-block)'.format(
                               (libref or 'WORK').upper() + '.' + tbl.upper(),
                               qual, time.time() - t0))
        self._shipped.add(((schema or 'main').lower(), tbl.lower()))

    # -- csv transport: SQL blocks -------------------------------------------

    def _run_sql_block_csv(self, seg, bnum):
        opts = parse_proc_options(seg.proc_options, block=bnum)
        for o in opts['ignored']:
            self._annotate('[block {}] PROC option {} ignored'.format(bnum, o))
        if seg.dialect == 'fedsql':
            self._annotate('[block {}] PROC FEDSQL rerouted like PROC SQL'
                           .format(bnum))
        for k, stmt in enumerate(seg.stmts, 1):
            c = classify_sql_statement(stmt, block=bnum, snum=k)
            if c.kind == 'ignore':
                continue
            if c.kind == 'sas_passthrough':
                ll = self._submit(stmt.text.strip() + '\n')
                self._log.append(ll['LOG'])
                self._invalidate_sas_state(macro_only=True)
                continue
            sql = rewrite_sql(c.select_sql, resolver=self._resolver,
                              block=bnum, snum=k)
            if self.echo_sql:
                self._annotate('[block {}, stmt {}] {}'.format(
                    bnum, k, ' '.join(sql.split())[:500]))
            self._ship_missing_tables(sql, bnum, k)
            if c.kind == 'ctas':
                schema = self._resolve_target_schema(c.libref)
                if c.libref and c.libref.lower() not in \
                        [l.lower() for l in self.sas.assigned_librefs()]:
                    raise SASDuckDBNotSupportedError(
                        "CREATE TABLE target libref '{}' is not assigned in "
                        'the SAS session.'.format(c.libref), block=bnum, stmt=k)
                nrows = duckdb_relation_to_sas_table(
                    self.sas, self.con, sql, c.libref, c.table, self.tmpdir,
                    annotate=self._annotate, tempkeep=self.tempkeep,
                    keep_as=(schema, c.table.lower()), block=bnum, snum=k)
                if schema:
                    qual = '"{}"."{}"'.format(schema, c.table.lower())
                else:
                    qual = '"{}"'.format(c.table.lower())
                self._created.append(('table', qual))
                self._shipped.add((schema or 'main', c.table.lower()))
                self._set_sqlmacros(nrows)
            elif c.kind == 'select_into':
                self._run_select_into_csv(c, sql, bnum, k)
            else:
                self._run_bare_select_csv(sql, opts, bnum, k)

    def _run_select_into_csv(self, c, sql, bnum, snum):
        import duckdb as _duckdb
        t0 = time.time()
        try:
            cur = self.con.execute(sql)
            rows = cur.fetchall()
        except _duckdb.Error as e:
            raise SASDuckDBExecutionError(
                'DuckDB failed executing rerouted SQL: {}'.format(e),
                block=bnum, stmt=snum) from e
        ncols = len(cur.description or [])
        if len(c.into_specs) > ncols:
            raise SASDuckDBNotSupportedError(
                'INTO lists {} macro variables but the query returns only {} '
                'columns.'.format(len(c.into_specs), ncols),
                block=bnum, stmt=snum)

        def fmt(v):
            if v is None:
                return ''
            return str(v)

        lets = []
        if rows:
            for i, (name, sep, trimmed) in enumerate(c.into_specs):
                if sep is not None:
                    val = sep.join(fmt(r[i]) for r in rows)
                else:
                    # always trimmed semantics (SAS would pad numerics to
                    # BEST8; we keep full precision - documented difference)
                    val = fmt(rows[0][i]).strip()
                lets.append((name, val))
        else:
            self._annotate('[block {}, stmt {}] query returned 0 rows; INTO '
                           'macro variables left unchanged'.format(bnum, snum))
        for name, val in lets:
            self.sas.symput(name, val)
        self._set_sqlmacros(len(rows))
        self._invalidate_sas_state(macro_only=True)
        self._annotate('[block {}, stmt {}] INTO set {} ({} rows, {:.2f}s)'
                       .format(bnum, snum,
                               ', '.join(':' + n for n, _v in lets) or 'nothing',
                               len(rows), time.time() - t0))

    def _run_bare_select_csv(self, sql, opts, bnum, snum):
        import duckdb as _duckdb
        if opts['noprint']:
            # SAS still executes a SELECT under NOPRINT (errors surface,
            # SQLOBS is set); only the output is suppressed. Run the real
            # query and fetch its first row - a count(*) wrapper would let
            # DuckDB prune the SELECT list and skip runtime errors in
            # projected expressions. Stopping after the first row also
            # matches native SAS, which sets SQLOBS=1 for a destination-less
            # NOPRINT select.
            t0 = time.time()
            try:
                row = self.con.sql(sql).fetchone()
            except _duckdb.Error as e:
                raise SASDuckDBExecutionError(
                    'DuckDB failed executing rerouted SQL: {}'.format(e),
                    block=bnum, stmt=snum) from e
            self._set_sqlmacros(1 if row is not None else 0)
            self._annotate('[block {}, stmt {}] SELECT executed, output '
                           'suppressed (noprint; result {}, {:.2f}s)'
                           .format(bnum, snum,
                                   'empty' if row is None else 'non-empty',
                                   time.time() - t0))
            return
        t0 = time.time()
        try:
            df = self.con.execute('select * from ({}) limit {}'.format(
                sql, self.render_limit + 1)).df()
        except _duckdb.Error as e:
            raise SASDuckDBExecutionError(
                'DuckDB failed executing rerouted SQL: {}'.format(e),
                block=bnum, stmt=snum) from e
        truncated = len(df) > self.render_limit
        if truncated:
            df = df.iloc[:self.render_limit]
            nrows = self.con.execute('select count(*) from ({}) _spddb_q'
                                     .format(sql)).fetchone()[0]
        else:
            nrows = len(df)
        self._set_sqlmacros(nrows)
        if self.results == 'html':
            out = df.to_html(index=False)
        else:
            out = df.to_string(index=False) + '\n'
        if truncated:
            out += ('\n-- display truncated at {} rows; use CREATE TABLE ... '
                    'AS to materialize the full result --\n'
                    .format(self.render_limit))
        self._lst.append(out)
        self._annotate('[block {}, stmt {}] SELECT rendered ({} row{}{}, '
                       '{:.2f}s)'.format(bnum, snum, len(df),
                                         '' if len(df) == 1 else 's',
                                         '+' if truncated else '',
                                         time.time() - t0))

    def _set_sqlmacros(self, nrows):
        self._submit('%let SQLOBS={}; %let SQLRC=0;\n'.format(int(nrows)))

    # -- jdbc transport: SQL blocks --------------------------------------------

    def _jdbc_connect_stmt(self):
        url = 'jdbc:duckdbsas:{};sas_text_len={}'.format(
            self.duckdb_file, self.jdbc_text_len)
        return ('connect to jdbc as ddb (driverclass='
                '"org.saspy.duckdb.DuckDBSASDriver" url="{}" classpath="{}");'
                .format(url, self.jdbc_classpath))

    def _run_sql_block_jdbc(self, seg, bnum):
        opts = parse_proc_options(seg.proc_options, block=bnum)
        for o in opts['ignored']:
            self._annotate('[block {}] PROC option {} ignored'.format(bnum, o))
        pieces = ['proc sql{};'.format(' noprint' if opts['noprint'] else ''),
                  self._jdbc_connect_stmt()]
        body = []
        had_data_stmt = False
        for k, stmt in enumerate(seg.stmts, 1):
            c = classify_sql_statement(stmt, block=bnum, snum=k)
            if c.kind == 'ignore':
                continue
            if c.kind == 'sas_passthrough':
                body.append(stmt.text.strip())
                # macro values may change; drop cache so later stmts re-resolve
                self._invalidate_sas_state(macro_only=True)
                continue
            sql = rewrite_sql(c.select_sql, resolver=self._resolver,
                              block=bnum, snum=k)
            if self.echo_sql:
                self._annotate('[block {}, stmt {}] {}'.format(
                    bnum, k, ' '.join(sql.split())[:500]))
            self._ship_missing_tables(sql, bnum, k, jdbc_pieces=body)
            passthru = sql
            if c.kind == 'ctas':
                if c.libref and c.libref.lower() not in \
                        [l.lower() for l in self.sas.assigned_librefs()]:
                    raise SASDuckDBNotSupportedError(
                        "CREATE TABLE target libref '{}' is not assigned in "
                        'the SAS session.'.format(c.libref), block=bnum, stmt=k)
                # materialize in DuckDB first (so later statements/blocks can
                # reference it), then pull the materialized table into SAS
                schema = self._resolve_target_schema(c.libref)
                if schema:
                    dqual = '"{}"."{}"'.format(schema, c.table.lower())
                    body.append('execute (create schema if not exists "{}") '
                                'by ddb;'.format(schema))
                else:
                    dqual = '"{}"'.format(c.table.lower())
                body.append('execute (create or replace table {} as {}) '
                            'by ddb;'.format(dqual, passthru))
                tgt = (c.libref + '.' if c.libref else 'work.') + c.table
                body.append('create table {} as select * from connection to '
                            'ddb (select * from {});'.format(tgt, dqual))
                body.append('%let _spddb_obs = &sqlobs;')
                had_data_stmt = True
                self._shipped.add((schema or 'main', c.table.lower()))
            elif c.kind == 'select_into':
                into = ', '.join(
                    ':{}{}{}'.format(n, " separated by '{}'".format(
                        s.replace("'", "''")) if s is not None else '',
                        ' trimmed' if t else '')
                    for n, s, t in c.into_specs)
                body.append('select * into {} from connection to ddb ({});'
                            .format(into, passthru))
                body.append('%let _spddb_obs = &sqlobs;')
                had_data_stmt = True
            else:
                if opts['noprint']:
                    self._annotate('[block {}, stmt {}] SELECT runs under '
                                   'noprint'.format(bnum, k))
                body.append('select * from connection to ddb ({});'
                            .format(passthru))
                body.append('%let _spddb_obs = &sqlobs;')
                had_data_stmt = True
        pieces.extend(body)
        pieces.append('disconnect from ddb;')
        pieces.append('quit;')
        if had_data_stmt:
            # execute/disconnect statements reset SQLOBS; restore the value
            # captured right after the last data-producing statement
            pieces.append('%let SQLOBS = &_spddb_obs; %let SQLRC = 0;')
        code = '\n'.join(pieces) + '\n'
        res = 'html' if self.results == 'html' else 'text'
        t0 = time.time()
        ll = self._submit(code, results=res)
        self._log.append(ll['LOG'])
        if ll['LST']:
            self._lst.append(ll['LST'])
        self._invalidate_sas_state(macro_only=True)
        if self.sas._io._checkLogForError(ll['LOG']):
            raise SASDuckDBExecutionError(
                'ERROR in rerouted PROC SQL pass-through block {} (see log '
                'excerpt):\n{}'.format(bnum, ll['LOG'][-2000:]), block=bnum)
        self._annotate('[block {}] pass-through block completed in {:.2f}s'
                       .format(bnum, time.time() - t0))
