__all__ = ('VERSION',)
try:
    distribution = __import__('pkg_resources').get_distribution('django-debug-toolbar')
    VERSION = distribution.version
except Exception, e:
    VERSION = 'unknown'
