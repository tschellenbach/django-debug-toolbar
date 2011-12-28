import time
import inspect

from django.core import cache
from django.core.cache.backends.base import BaseCache
from django.utils.translation import ugettext_lazy as _, ungettext_lazy as __
from debug_toolbar.panels import DebugPanel


class CacheStatTracker(BaseCache):
    '''A small class used to track cache calls.'''

    def __init__(self, cache):
        self.cache = cache
        self.reset()

    def reset(self):
        self.stats = dict(
            calls = [],
            hits = 0,
            misses = 0,
            sets = 0,
            gets = 0,
            get_many = 0,
            deletes = 0,
            total_time = 0,
        )

    def track(self, key, value):
        self.stats[key] += value

    def _get_func_info(self):
        ''' Some attempts to get stack info fail, so try/except so we can set
            a message if this happens.
        '''
        try:
            stack = inspect.stack()[2]
        except IndexError:
            return ('Unable to get stack info',)
        else:
            return (stack[1], stack[2], stack[3], stack[4])

    def get(self, key, default=None):
        t = time.time()
        value = self.cache.get(key, default)
        this_time = time.time() - t
        self.track('total_time', this_time * 1000)
        if value is None:
            self.track('misses', 1)
        else:
            self.track('hits', 1)
        self.track('gets', 1)
        self.track('calls', [(this_time, 'get', (key,), self._get_func_info())])
        return value

    def set(self, key, value, timeout=None):
        t = time.time()
        self.cache.set(key, value, timeout)
        this_time = time.time() - t
        self.track('total_time', this_time * 1000)
        self.track('sets', 1)
        self.track('calls', [(this_time, 'set', (key, value, timeout),
            self._get_func_info())])

    def delete(self, key):
        t = time.time()
        self.cache.delete(key)
        this_time = time.time() - t
        self.track('total_time', this_time * 1000)
        self.track('deletes', 1)
        self.track('calls', [(this_time, 'delete', (key,),
            self._get_func_info())])

    def get_many(self, keys):
        t = time.time()
        results = self.cache.get_many(keys)
        this_time = time.time() - t
        self.track('total_time', this_time * 1000)
        self.track('get_many', 1)
        for key, value in results.iteritems():
            if value is None:
                self.track('misses', 1)
            else:
                self.track('hits', 1)
        self.track('calls', [(this_time, 'get_many', (keys,),
            self._get_func_info())])
        return results


class CacheDebugPanel(DebugPanel):
    '''
    Panel that displays the cache statistics.
    '''
    name = 'Cache'
    template = 'debug_toolbar/panels/cache.html'
    has_content = True

    def __init__(self, *args, **kwargs):
        super(CacheDebugPanel, self).__init__(*args, **kwargs)
  # This is hackish but to prevent threading issues is somewhat needed
        if isinstance(cache.cache, CacheStatTracker):
            cache.cache.reset()
            self.cache = cache.cache
        else:
            self.cache = CacheStatTracker(cache.cache)
            cache.cache = self.cache

    def nav_title(self):
        return _('Cache')

    def nav_subtitle(self):
        calls = len(self.cache.stats['calls'])
        return __(
            '%(calls)d calls, %(time).2fms, %(hitpct).1f%% hit',
            '%(calls)d calls, %(time).2fms, %(hitpct).1f%% hit',
            calls,
        ) % dict(
            calls=calls,
            time=self.cache.stats['total_time'],
            hitpct=100. * self.cache.stats['hits'] / (
                self.cache.stats['hits'] + self.cache.stats['misses']),
        )

    def title(self):
        return _('Cache Usage')

    def url(self):
        return ''

    def process_response(self, request, response):
        self.record_stats({
            'stats': self.cache.stats,
        })

