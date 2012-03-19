from os.path import normpath
from pprint import pformat

from django import http
from django.conf import settings
from django.template.context import get_standard_processors
from django.template.loader import render_to_string
from django.test.signals import template_rendered
from django.utils.translation import ugettext_lazy as _
from django.db.models.query import QuerySet
from debug_toolbar.panels import DebugPanel
from debug_toolbar.utils.tracking.db import recording, SQLQueryTriggered

# Code taken and adapted from Simon Willison and Django Snippets:
# http://www.djangosnippets.org/snippets/766/

# Monkeypatch instrumented test renderer from django.test.utils - we could use
# django.test.utils.setup_test_environment for this but that would also set up
# e-mail interception, which we don't want
from django.test.utils import instrumented_test_render
from django.template import Template
from django.dispatch import Signal

get_template = Signal(providing_args=['template'])

if not hasattr(Template, '_render'): # Django < 1.2
    if Template.render != instrumented_test_render:
        Template.original_render = Template.render
        Template.render = instrumented_test_render
else:
    if Template._render != instrumented_test_render:
        Template.original_render = Template._render
        Template._render = instrumented_test_render


# Monkey patch coffin's render function to send template_rendered signal
def new_render(func):
    def render(self, context=None):
        template_rendered.send(sender=self, template=self,
            context=context)
        return func(self, context)
    return render

def track(f, name):
    def _track(env, filename, *a, **kw):
        ret = f(env, filename, *a, **kw)
        get_template.send(sender=name, filename=filename)
        return ret

    return _track

try:
    import jinja2
except ImportError:
    pass
else:
    jinja2.Template.render = new_render(jinja2.Template.render)
    jinja2.Environment.get_template = track(jinja2.Environment.get_template,
        'templates:jinja2')


# MONSTER monkey-patch
old_template_init = Template.__init__
def new_template_init(self, template_string, origin=None, name='<Unknown Template>'):
    old_template_init(self, template_string, origin, name)
    self.origin = origin
Template.__init__ = new_template_init


class TemplateDebugPanel(DebugPanel):
    """
    A panel that lists all templates used during processing of a response.
    """
    name = 'Template'
    template = 'debug_toolbar/panels/templates.html'
    has_content = True
    
    def jinjacontent(self):
        context = dict(
            template_calls = self.do_stat_call('get_total_calls'),
            template_time = self.do_stat_call('get_total_time'),
            template_calls_list = [(c['time'], c['args'][1], 'jinja2', c['stack']) for c in get_stats().get_calls('templates:jinja2')] + \
                    [(c['time'], c['args'][1], 'jinja', c['stack']) for c in get_stats().get_calls('templates:jinja')] + \
                    [(c['time'], c['args'][0].name, 'django', c['stack']) for c in get_stats().get_calls('templates:django')],
        )
        try:
            return render_to_string('debug_toolbar/panels/templates.html', context)
        except Exception, e:
            print e
            return repr(e)

    def __init__(self, *args, **kwargs):
        super(TemplateDebugPanel, self).__init__(*args, **kwargs)
        self.templates = []
        template_rendered.connect(self._store_template_info)
        get_template.connect(self._store_template_info)
        self.context = None
    
    def _store_template_info(self, sender, **kwargs):
        t = kwargs.get('template')
        if t:
            name = t.name
        else:
            name = kwargs['filename']

        if name and name.startswith('debug_toolbar/'):
            return  # skip templates that we are generating through the debug toolbar.

        context_data = kwargs.get('context')
        if context_data is None:
            context_data = self.context
        else:
            self.context = context_data
        
        #context_data can be either a dict or a context data object
        context_list = []
        context_data_dicts = [context_data or {}]
        if context_data and hasattr(context_data, 'dicts'):
            context_data_dicts = context_data.dicts
            
        for context_layer in context_data_dicts:
            temp_layer = {}
            if hasattr(context_layer, 'items'):
                for key, value in context_layer.items():
                    # Replace any request elements - they have a large
                    # unicode representation and the request data is
                    # already made available from the Request Vars panel.
                    if isinstance(value, http.HttpRequest):
                        temp_layer[key] = '<<request>>'
                    # Replace the debugging sql_queries element. The SQL
                    # data is already made available from the SQL panel.
                    elif key == 'sql_queries' and isinstance(value, list):
                        temp_layer[key] = '<<sql_queries>>'
                    # Replace LANGUAGES, which is available in i18n context processor
                    elif key == 'LANGUAGES' and isinstance(value, tuple):
                        temp_layer[key] = '<<languages>>'
                    # QuerySet would trigger the database: user can run the query from SQL Panel
                    elif isinstance(value, QuerySet):
                        model_name = "%s.%s" % (value.model._meta.app_label, value.model.__name__)
                        temp_layer[key] = '<<queryset of %s>>' % model_name
                    else:
                        try:
                            recording(False)
                            pformat(value)  # this MAY trigger a db query
                        except SQLQueryTriggered:
                            temp_layer[key] = '<<triggers database query>>'
                        except UnicodeEncodeError:
                            temp_layer[key] = ('UnicodeEncodeError when '
                                'parsing %r. Type: %s' % (key, type(value)))
                        else:
                            temp_layer[key] = value
                        finally:
                            recording(True)
            try:
                context_list.append(pformat(temp_layer))
            except UnicodeEncodeError:
                raise
                pass
        kwargs['context'] = context_list
        self.templates.append(kwargs)
    
    def nav_title(self):
        return _('Templates')
    
    def title(self):
        num_templates = len(self.templates)
        return _('Templates (%(num_templates)s rendered)') % {'num_templates': num_templates}
    
    def url(self):
        return ''
    
    def process_request(self, request):
        self.request = request
    
    def process_response(self, request, response):
        context_processors = dict(
            [
                ("%s.%s" % (k.__module__, k.__name__),
                    pformat(k(self.request))) for k in get_standard_processors()
            ]
        )
        template_context = []
        templates = dict()
        for template_data in self.templates:
            info = {}
            # Clean up some info about templates
            if 'template' in template_data:
                template = template_data['template']
            else:
                class template(object):
                    name = template_data['filename']

            if hasattr(template, 'origin') and hasattr(template.origin, 'name'):
                template.origin_name = template.origin.name
            else:
                template.origin_name = 'No origin'

            if template.name in templates:
                continue
            else:
                templates[template.name] = None

            info['template'] = template
            # Clean up context for better readability
            if getattr(settings, 'DEBUG_TOOLBAR_CONFIG', {}).get('SHOW_TEMPLATE_CONTEXT', True):
                context_list = template_data.get('context', [])
                info['context'] = '\n'.join(context_list)

            template_context.append(info)
        
        self.record_stats({
            'templates': template_context,
            'template_dirs': [normpath(x) for x in settings.TEMPLATE_DIRS],
            'context_processors': context_processors,
        })
