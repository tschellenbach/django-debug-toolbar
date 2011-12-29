import re
import uuid

from django.db.backends import BaseDatabaseWrapper
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _, ungettext_lazy as __
from django.conf import settings

from debug_toolbar.utils.compat.db import connections
from debug_toolbar.middleware import DebugToolbarMiddleware
from debug_toolbar.panels import DebugPanel
from debug_toolbar.utils import sqlparse
from debug_toolbar.utils.tracking.db import CursorWrapper
from debug_toolbar.utils.tracking import replace_call

FIELDS_RE = re.compile('^(.*SELECT<[^>]+>)(.+?)(<[^>]+>FROM.+)$', re.DOTALL)
COLLAPSED_FIELDS_HTML = '''
<span class="djDebugUncollapsed">%s &bull;&bull;&bull; %s</span>
<span class="djDebugCollapsed">%s</span>
'''.strip()

ANONYMIZE_QUERY_REPLACEMENTS = (
    (re.compile('\s+'), ' '),
    (re.compile('(\s+LIMIT|OFFSET)\s+\d+', re.IGNORECASE), r'\g<1> %d'),
    (re.compile('(\s+IN\s*)\([^)]+\)', re.IGNORECASE), r'\g<1>(%s)'),
    (re.compile('(SELECT )(.+?)( FROM)', re.IGNORECASE), r'\g<1>...\g<3>'),
)

def _get_setting(key, default=None):
    return getattr( settings, 'DEBUG_TOOLBAR_CONFIG', {}).get(key, default)

# Inject our tracking cursor
@replace_call(BaseDatabaseWrapper.cursor)
def cursor(func, self):
    result = func(self)

    djdt = DebugToolbarMiddleware.get_current()
    if not djdt:
        return result
    logger = djdt.get_panel(SQLDebugPanel)

    return CursorWrapper(result, self, logger=logger)

try:
    import pygments
    USE_PYGMENTS = True
except ImportError:
    USE_PYGMENTS = False

def get_isolation_level_display(engine, level):
    if engine == 'psycopg2':
        import psycopg2.extensions
        choices = {
            psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT: 'Autocommit',
            psycopg2.extensions.ISOLATION_LEVEL_READ_UNCOMMITTED:
                'Read uncommitted',
            psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED:
                'Read committed',
            psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ:
                'Repeatable read',
            psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE:
                'Serializable',
        }
    else:
        raise ValueError(engine)

    return choices.get(level)


def get_transaction_status_display(engine, level):
    if engine == 'psycopg2':
        import psycopg2.extensions
        choices = {
            psycopg2.extensions.TRANSACTION_STATUS_IDLE: 'Idle',
            psycopg2.extensions.TRANSACTION_STATUS_ACTIVE: 'Active',
            psycopg2.extensions.TRANSACTION_STATUS_INTRANS: 'In transaction',
            psycopg2.extensions.TRANSACTION_STATUS_INERROR: 'In error',
            psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN: 'Unknown',
        }
    else:
        raise ValueError(engine)

    return choices.get(level)


class SQLDebugPanel(DebugPanel):
    '''
    Panel that displays information about the SQL queries run while processing
    the request.
    '''
    name = 'SQL'
    template = 'debug_toolbar/panels/sql.html'
    has_content = True

    def __init__(self, *args, **kwargs):
        super(SQLDebugPanel, self).__init__(*args, **kwargs)
        self._offset = dict((k, len(connections[k].queries))
            for k in connections)
        self._sql_time = 0
        self._num_queries = 0
        self._queries = []
        self._databases = {}
        self._transaction_status = {}
        self._transaction_ids = {}
        self._seen = {}


    def get_transaction_id(self, alias):
        conn = connections[alias].connection
        if not conn:
            return None

        engine = conn.__class__.__module__.split('.', 1)[0]
        if engine == 'psycopg2':
            cur_status = conn.get_transaction_status()
        else:
            raise ValueError(engine)

        last_status = self._transaction_status.get(alias)
        self._transaction_status[alias] = cur_status

        if not cur_status:
            # No available state
            return None

        if cur_status != last_status:
            if cur_status:
                self._transaction_ids[alias] = uuid.uuid4().hex
            else:
                self._transaction_ids[alias] = None

        return self._transaction_ids[alias]

    def record(self, alias, **kwargs):
        self._queries.append((alias, kwargs))
        if alias not in self._databases:
            self._databases[alias] = {
                'time_spent': kwargs['duration'],
                'num_queries': 1,
            }
        else:
            self._databases[alias]['time_spent'] += kwargs['duration']
            self._databases[alias]['num_queries'] += 1
        self._sql_time += kwargs['duration']
        self._num_queries += 1

    def nav_title(self):
        return _('SQL')

    def nav_subtitle(self):
        # TODO l10n: use ngettext
        return "%d %s in %.2fms.\n%d unique" % (
            self._num_queries,
            (self._num_queries == 1) and 'query' or 'queries',
            self._sql_time,
            len(self._seen),
        )

    def title(self):
        count = len(self._databases)

        return __(
            'SQL Queries from %(count)d connection',
            'SQL Queries from %(count)d connections',
            count,
        ) % dict(count=count)

    def url(self):
        return ''

    def process_response(self, request, response):
        tables = set()
        if self._queries:
            width_ratio_tally = 0
            factor = int(256.0/(len(self._databases)*2.5))
            for n, db in enumerate(self._databases.itervalues()):
                rgb = [0, 0, 0]
                color = n % 3
                rgb[color] = 256 - n/3*factor
                nn = color
                # XXX: pretty sure this is horrible after so many aliases
                while rgb[color] < factor:
                    nc = min(256 - rgb[color], 256)
                    rgb[color] += nc
                    nn += 1
                    if nn > 2:
                        nn = 0
                    rgb[nn] = nc
                db['rgb_color'] = rgb

            trans_ids = {}
            trans_id = None
            i = 0
            for alias, query in self._queries:

                trans_id = query.get('trans_id')
                last_trans_id = trans_ids.get(alias)

                if trans_id != last_trans_id:
                    if last_trans_id:
                        self._queries[i-1][1]['ends_trans'] = True
                    trans_ids[alias] = trans_id
                    if trans_id:
                        query['starts_trans'] = True
                if trans_id:
                    query['in_trans'] = True

                query['alias'] = alias
                if 'iso_level' in query:
                    query['iso_level'] = get_isolation_level_display(
                        query['engine'], query['iso_level'])
                if 'trans_status' in query:
                    query['trans_status'] = get_transaction_status_display(
                        query['engine'], query['trans_status'])
                query['tables'] = set()
                query['sql'] = reformat_sql(query['sql'], query['tables'])
                tables |= query['tables']
                query['rgb_color'] = self._databases[alias]['rgb_color']
                try:
                    query['width_ratio'] = (query['duration'] /
                        self._sql_time) * 100
                    query['width_ratio_relative'] =  (100.0 *
                        query['width_ratio'] / (100.0 - width_ratio_tally))
                except ZeroDivisionError:
                    query['width_ratio'] = 0
                    query['width_ratio_relative'] = 0
                query['start_offset'] = width_ratio_tally
                query['end_offset'] = (query['width_ratio']
                    + query['start_offset'])
                width_ratio_tally += query['width_ratio']

                stacktrace = []
                for frame in query['stacktrace']:
                    params = map(
                        escape,
                        frame[0].rsplit('/', 1) + list(frame[1:]))
                    try:
                        stacktrace.append(u'<span class="path">{0}/</span>'
                            '<span class="file">{1}</span> in '
                            '<span class="func">{3}</span>('
                            '<span class="lineno">{2}</span>)\n  '
                            '<span class="code">{4}</span>'.format(*params)
                        )
                    except IndexError:
                        stacktrace.append(u'<span class="path">{0}/</span>'
                            '<span class="file">{1}</span> in '
                            '<span class="func">{3}</span>('
                            '<span class="lineno">{2}</span>)\n  '
                            '<span class="code">Couldnt find the code</span>'
                            .format(*params)
                        )
                        # This frame doesn't have the expected format, so
                        # skip it and move on to the next one
                        continue
                query['stacktrace'] = mark_safe('\n'.join(stacktrace))
                i += 1

            if trans_id:
                self._queries[i-1][1]['ends_trans'] = True

        # Should we check for duplicate queries?
        dupe_queries = None
        if _get_setting('SQL_DUPLICATES'):
            dupe_queries = self._get_dupe_queries()

        self.record_stats({
            'databases': sorted(
                self._databases.items(),
                key=lambda x: -x[1]['time_spent']
            ),
            'queries': [q for a, q in self._queries],
            'dupe_queries': dupe_queries,
            'sql_time': self._sql_time,
            'tables': tables,
        })

    @classmethod
    def _anonymize_query(cls, sql):
        for search, replace in ANONYMIZE_QUERY_REPLACEMENTS:
            sql = search.sub(replace, sql)
        return sql

    def _get_dupe_queries(self):
        ''' Returns information about duplicate queries.

            Builds the _seen dict, which describes duplicate queries:

            _seen = {'<some query>': {'time': 0.123,
                                       'queries': [q1, q2, q3]}
            }

            ... where time is the total time taken to execute all iterations
            of this query, and queries is a list of query objects that used
            this query.

            The original query object is modifed - a boolean 'duplicate'
            value is set, depending on whether or not this is a dupe query.
        '''
        # Should we be looking at sql or raw_sql?
        # If sql, the params are included when checking for dupes. If
        # raw_sql, params are ignored when checking for dupes.
        if _get_setting('SQL_DUPE_PARAMS'):
            # This is a tiny bit hacky. Only 'raw_sql' needs to be passed
            # to reformat_sql, so here's a lambda funcion that just
            # returns the query so we can use the same code for both
            # sql/raw_sql.
            func = lambda query: query
            sql_attr = 'sql'
        else:
            func = reformat_sql
            sql_attr = 'raw_sql'

        for alias, query in self._queries:
            sql = func(self._anonymize_query(query[sql_attr]))
            query['unique_hash'] = hash(sql)
            # Add this query to the list of occurrances for this query.
            data = self._seen.get(sql, {
                'time': 0,
                'queries': [],
                'unique_hash': hash(sql),
                'tables': query['tables'],
                'alias': query['alias'],
            })
            data['queries'].append(query)
            data['time'] += query['duration']
            self._seen[sql] = data
        return self._seen


class TableCollectionFilter(sqlparse.filters.Filter):
    def __init__(self, tables):
        self.tables = tables

    '''sqlparse filter to collect the tablenames'''
    def process(self, stack, stream):
        '''Process the token stream'''
        class States:
            UNKNOWN = 0 # default
            FROM = 1 # found from/join clause
            WHITESPACE = 2 # whitespace

        state = States.UNKNOWN
        for token_type, value in stream:
            if state is States.UNKNOWN:
                if(token_type in sqlparse.tokens.Keyword
                        and value.rsplit()[-1] in ('FROM', 'JOIN')):
                    state = States.FROM
            elif state is States.FROM:
                if token_type in sqlparse.tokens.Whitespace:
                    state = States.WHITESPACE
                else:
                    state = States.UNKNOWN
            elif state is States.WHITESPACE:
                if token_type in sqlparse.tokens.String.Symbol:
                    if value[0] == value[-1] == '"':
                        self.tables.add(value[1:-1])
                    else:
                        self.tables.add(value)
                state = States.UNKNOWN
            else:
                state = States.UNKNOWN

            yield token_type, value


class BoldKeywordFilter(sqlparse.filters.Filter):
    '''sqlparse filter to bold SQL keywords'''
    def process(self, stack, stream):
        '''Process the token stream'''
        for token_type, value in stream:
            is_keyword = token_type in sqlparse.tokens.Keyword
            if is_keyword:
                yield sqlparse.tokens.Text, '<strong>'
            yield token_type, escape(value)
            if is_keyword:
                yield sqlparse.tokens.Text, '</strong>'


def swap_fields(sql):
    match = FIELDS_RE.search(sql)

    strip_white = lambda s: ' '.join(s.replace('<br>', ' ').split())
    if match:
        from_, fields, to = match.groups()
        return COLLAPSED_FIELDS_HTML % (
            strip_white(from_),
            strip_white(to),
            sql,
        )
    else:
        return sql

def reformat_sql(sql, tables=None, expand=True):
    stack = sqlparse.engine.FilterStack()
    options = sqlparse.formatter.validate_options(dict(reindent=True))
    stack = sqlparse.formatter.build_filter_stack(stack, options)
    if tables is not None:
        stack.preprocess.append(TableCollectionFilter(tables))

    if not USE_PYGMENTS:
        stack.preprocess.append(BoldKeywordFilter()) # add our custom filter
    stack.postprocess.append(sqlparse.filters.SerializerUnicode()) # tokens -> strings
    sql = ''.join(stack.run(sql))

    if USE_PYGMENTS:
        from pygments import highlight
        from pygments.lexers import SqlLexer
        from pygments.formatters import HtmlFormatter
        sql = highlight(
            sql,
            SqlLexer(),
            HtmlFormatter(
                classprefix='djdt_',
                lineseparator='<br>',
            )
        )

    if expand:
        sql = swap_fields(sql).replace('...', '&bull;'*3)
    return sql

