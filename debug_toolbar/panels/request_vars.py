import inspect
from django.utils.translation import ugettext_lazy as _

from debug_toolbar.panels import DebugPanel
from debug_toolbar.utils import get_name_from_obj
from django.core.urlresolvers import resolve
from framework.utils import urlresolvers

class RequestVarsDebugPanel(DebugPanel):
    """
    A panel to display request variables (POST/GET, session, cookies).
    """
    name = 'RequestVars'
    template = 'debug_toolbar/panels/request_vars.html'
    has_content = True
    
    def __init__(self, *args, **kwargs):
        DebugPanel.__init__(self, *args, **kwargs)
        self.view_func = None
        self.view_args = None
        self.view_kwargs = None
    
    def nav_title(self):
        return _('Request Vars')
    
    def title(self):
        return _('Request Vars')
    
    def url(self):
        return ''
    
    def process_request(self, request):
        self.request = request
    
    def process_view(self, request, view_func, view_args, view_kwargs):
        self.view_func = view_func
        self.view_args = view_args
        self.view_kwargs = view_kwargs

    def get_view(self, request):
        out = []
        if self.request.path.startswith('/dev'):
            '''Strip /dev'''
            path = self.request.path[4:]
        else:
            path = self.request.path

        out.append(('path', path))
        out.append(('full_path', self.request.get_full_path()))

        try:
            name, namespace = urlresolvers.resolve_to_name(path)
            function, args, kwargs = resolve(path)
        except urlresolvers.Resolver404:
            name, namespace = None, None
            function, args, kwargs = None, (), {}

        out.append(('view name', name))
        if function:
            function = getattr(function, 'wrapped_func', function)
            filename = inspect.getfile(function)
            line_no = inspect.getsourcelines(function)[-1]
            out.append(('view func', '%s on line %d in %s' % (
                function.__name__, line_no, filename)))

        if name:
            full_name = name
            if namespace:
                full_name = namespace + ':' + str(full_name)
            
            url_parts = ['{% url', full_name]

            if args:
                url_parts += map(unicode, args)

            if kwargs:
                for k, v in kwargs.iteritems():
                    if isinstance(v, unicode):
                        v = v.encode('utf-8', 'replace')
                    url_parts.append('%s=%r' % (k, v))

            url_parts.append('%}')
            out.append(('view url ', ' '.join(url_parts)))
        
        else:
            out.append(('view url ', name))

        return out
    
    def process_response(self, request, response):
        self.record_stats({
            'get': [(k, self.request.GET.getlist(k)) for k in self.request.GET],
            'post': [(k, self.request.POST.getlist(k)) for k in self.request.POST],
            'cookies': [(k, self.request.COOKIES.get(k)) for k in self.request.COOKIES],
        })
        
        if hasattr(self, 'view_func'):
            if self.view_func is not None:
                name = get_name_from_obj(self.view_func)
            else:
                name = '<no view>'
            
            self.record_stats({
                'view': self.get_view(request),
                'view_func': name,
                'view_args': self.view_args,
                'view_kwargs': self.view_kwargs
            })
        
        if hasattr(self.request, 'session'):
            self.record_stats({
                'session': [(k, self.request.session.get(k)) for k in self.request.session.iterkeys()]
            })
