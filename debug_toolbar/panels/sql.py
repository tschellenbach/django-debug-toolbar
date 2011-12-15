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
from debug_toolbar.utils import Counter


# Inject our tracking cursor
@replace_call(BaseDatabaseWrapper.cursor)
def cursor(func, self):
    result = func(self)
    
    djdt = DebugToolbarMiddleware.get_current()
    if not djdt:
        return result
    logger = djdt.get_panel(SQLDebugPanel)
    
    return CursorWrapper(result, self, logger=logger)


def get_isolation_level_display(engine, level):
    if engine == 'psycopg2':
        import psycopg2.extensions
        choices = {
            psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT: 'Autocommit',
            psycopg2.extensions.ISOLATION_LEVEL_READ_UNCOMMITTED: 'Read uncommitted',
            psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED: 'Read committed',
            psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ: 'Repeatable read',
            psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE: 'Serializable',
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
    """
    Panel that displays information about the SQL queries run while processing
    the request.
    """
    name = 'SQL'
    template = 'debug_toolbar/panels/sql.html'
    has_content = True
    
    def __init__(self, *args, **kwargs):
        super(SQLDebugPanel, self).__init__(*args, **kwargs)
        self._offset = dict((k, len(connections[k].queries)) for k in connections)
        self._sql_time = 0
        self._num_queries = 0
        self._queries = []
        self._databases = {}
        self._transaction_status = {}
        self._transaction_ids = {}
    
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
        
        return __('SQL Queries from %(count)d connection', 'SQL Queries from %(count)d connections', count) % dict(
            count=count,
        )
    
    def url(self):
        return ''
    
    def process_response(self, request, response):
        if self._queries:
            width_ratio_tally = 0
            colors = [
                (256, 0, 0), # red
                (0, 256, 0), # blue
                (0, 0, 256), # green
            ]
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
                    query['iso_level'] = get_isolation_level_display(query['engine'], query['iso_level'])
                if 'trans_status' in query:
                    query['trans_status'] = get_transaction_status_display(query['engine'], query['trans_status'])
                query['sql'] = reformat_sql(query['sql'])
                query['rgb_color'] = self._databases[alias]['rgb_color']
                try:
                    query['width_ratio'] = (query['duration'] / self._sql_time) * 100
                    query['width_ratio_relative'] =  100.0 * query['width_ratio'] / (100.0 - width_ratio_tally)
                except ZeroDivisionError:
                    query['width_ratio'] = 0
                    query['width_ratio_relative'] = 0
                query['start_offset'] = width_ratio_tally
                query['end_offset'] = query['width_ratio'] + query['start_offset']
                width_ratio_tally += query['width_ratio']
                
                stacktrace = []
                for frame in query['stacktrace']:
                    params = map(escape, frame[0].rsplit('/', 1) + list(frame[1:]))
                    try:
                        stacktrace.append(u'<span class="path">{0}/</span><span class="file">{1}</span> in <span class="func">{3}</span>(<span class="lineno">{2}</span>)\n  <span class="code">{4}</span>'.format(*params))
                    except IndexError:
                        # This frame doesn't have the expected format, so skip it and move on to the next one
                        continue
                query['stacktrace'] = mark_safe('\n'.join(stacktrace))
                i += 1
            
            if trans_id:
                self._queries[i-1][1]['ends_trans'] = True
        
        # Should we check for duplicate queries?
        if hasattr(settings, 'DEBUG_TOOLBAR_CONFIG'):
            if settings.DEBUG_TOOLBAR_CONFIG.get('SQL_DUPLICATES', False):
                dupe_queries = self._get_dupe_queries()
            else:
                dupe_queries = None
        
        self.record_stats({
            'databases': sorted(self._databases.items(), key=lambda x: -x[1]['time_spent']),
            'queries': [q for a, q in self._queries],
            'dupe_queries': dupe_queries,
            'sql_time': self._sql_time,
        })
      
    def _get_dupe_queries(self):
        """ Returns information about duplicate queries.
        
            Builds the _seen dict, which describes duplicate queries:
            
            _seen = {'<some query>': {'time': 0.123,
                                       'queries': [q1, q2, q3]}
            }
            
            ... where time is the total time taken to execute all iterations of
            this query, and queries is a list of query objects that used this
            query.
            
            The original query object is modifed - a boolean 'duplicate'
            value is set, depending on whether or not this is a dupe query.
        """      
        self._seen = {}

        if hasattr(settings, 'DEBUG_TOOLBAR_CONFIG'):
            # Should we be looking at sql or raw_sql?
            # If sql, the params are included when checking for dupes. If
            # raw_sql, params are ignored when checking for dupes.
            inc_params = settings.DEBUG_TOOLBAR_CONFIG.get('SQL_DUPE_PARAMS', False)
            if inc_params:
                # This is a tiny bit hacky. Only 'raw_sql' needs to be passed
                # to reformat_sql, so here's a lambda funcion that just returns
                # the query so we can use the same code for both sql/raw_sql.
                func = lambda query: query
                sql_attr = 'sql'
            else: 
                func = reformat_sql
                sql_attr = 'raw_sql'

        # Fill the counter with the queries, so we can use it to easily
        # count occurances while processing the query list. This list comp
        # populates the counter with formated SQL (either sql or raw_sql).
        c = Counter([func(q[sql_attr]) for a, q in self._queries])

        for alias, query in self._queries:
            sql = func(query[sql_attr])
            # Is there more than one occurrance of this query?
            query['duplicate'] = (c[sql] > 1)
            # Add this query to the list of occurrances for this query.
            data = self._seen.get(sql, {'time': 0, 'queries': []})
            data['queries'].append(query)
            data['time'] += query['duration']
            self._seen[sql] = data
        return self._seen


class BoldKeywordFilter(sqlparse.filters.Filter):
    """sqlparse filter to bold SQL keywords"""
    def process(self, stack, stream):
        """Process the token stream"""
        for token_type, value in stream:
            is_keyword = token_type in sqlparse.tokens.Keyword
            if is_keyword:
                yield sqlparse.tokens.Text, '<strong>'
            yield token_type, escape(value)
            if is_keyword:
                yield sqlparse.tokens.Text, '</strong>'


def swap_fields(sql):
    return re.sub('SELECT</strong> (.*) <strong>FROM', 'SELECT</strong> <a class="djDebugUncollapsed djDebugToggle" href="#">&bull;&bull;&bull;</a> ' +
        '<a class="djDebugCollapsed djDebugToggle" href="#">\g<1></a> <strong>FROM', sql)


def reformat_sql(sql):
    stack = sqlparse.engine.FilterStack()
    stack.preprocess.append(BoldKeywordFilter()) # add our custom filter
    stack.postprocess.append(sqlparse.filters.SerializerUnicode()) # tokens -> strings
    return swap_fields(''.join(stack.run(sql)))
