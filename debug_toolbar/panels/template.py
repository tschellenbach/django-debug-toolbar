from os.path import normpath
from pprint import pformat

from django import http
from django.conf import settings
from django.template.context import get_standard_processors
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

if not hasattr(Template, '_render'): # Django < 1.2
    if Template.render != instrumented_test_render:
        Template.original_render = Template.render
        Template.render = instrumented_test_render
else:
    if Template._render != instrumented_test_render:
        Template.original_render = Template._render
        Template._render = instrumented_test_render

# -- testing jinja2 stuff
def new_get_template(func):
    def get_template(template_name):
        print 'get template: ', template_name
        return func(template_name)
    return get_template

def new_render_to_string(func):
    def render_to_string(template_name, dictionary=None, context_instance=None):
        print 'rendering to string'
        #print dictionary
        #print context_instance
        return func(template_name, dictionary, context_instance)
    return render_to_string

def new_render_to_resp(func):
    def render_to_response(template_name, dictionary=None,
                           context_instance=None, mimetype=None):
        #print 'rendering ', template_name
        #print dictionary
        #print context_instance
        template = func(template_name, dictionary, context_instance, mimetype)
        #print dir(template)
        #template_rendered.send(sender=template, template=template_name, context=dictionary)
    return render_to_response

def new_render(func):
    def render(self, context=None):
        template_rendered.send(sender=self, template=self,
                               context=context)
        template_string = func(self, context)
        print 'rendering template: ', self
        #print template
        #print context
        return template_string
    return render
        
        
try:
    import coffin
except ImportError:
    pass
else:
    print 'patching get_template'
    #coffin.template.loader.get_template = new_get_template(coffin.template.loader.get_template)
    #coffin.template.loader.render_to_string = new_render_to_string(coffin.template.loader.render_to_string)
    #coffin.shortcuts.render_to_response = new_render_to_resp(coffin.shortcuts.render_to_response)
    coffin.template.Template.render = new_render(coffin.template.Template.render)

# -- end testing jinja2 stuff 


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
    
    def __init__(self, *args, **kwargs):
        super(TemplateDebugPanel, self).__init__(*args, **kwargs)
        self.templates = []
        template_rendered.connect(self._store_template_info)
    
    def _store_template_info(self, sender, **kwargs):
        t = kwargs['template']

        """
        if not hasattr(t, 'name'):
            print 'unicode'
            raise(t)
        """
        print 'processing: ', 
        print t.name
        if t.name and t.name.startswith('debug_toolbar/'):
            print 'skipping template: ', t.name
            return  # skip templates that we are generating through the debug toolbar.
        context_data = kwargs['context']
        
        context_list = []
        for context_layer in context_data.dicts:
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
        print 'Added template to list'
        print kwargs['template']
    
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
        for template_data in self.templates:
            info = {}
            # Clean up some info about templates
            template = template_data.get('template', None)
            print template.name
            if not hasattr(template, 'origin'):
                template.origin_name = 'No origin'
            else:
                template.origin_name = template.origin.name
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
