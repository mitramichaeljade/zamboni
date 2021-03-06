import hashlib
import json
import logging
import os
import subprocess

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage as storage
from django.http import (HttpResponse, HttpResponseNotFound,
                         HttpResponseServerError)
from django.template import RequestContext
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt, requires_csrf_token
from django.views.decorators.http import etag

import jingo
import jingo_minify
from django_statsd.clients import statsd
from django_statsd.views import record as django_statsd_record
from jingo import render_to_string

from amo.context_processors import get_collect_timings
from amo.decorators import post_required
from amo.helpers import absolutify, media
from amo.urlresolvers import reverse
from amo.utils import urlparams

from mkt.carriers import get_carrier


log = logging.getLogger('z.mkt.site')


# This can be called when CsrfViewMiddleware.process_view has not run,
# therefore needs @requires_csrf_token in case the template needs
# {% csrf_token %}.
@requires_csrf_token
def handler403(request):
    # NOTE: The mkt.api uses Tastypie which has its own mechanism for
    # triggering 403s. If we ever end up calling PermissionDenied there, we'll
    # need something here similar to the 404s and 500s.
    #
    # TODO: Bug 793241 for different 403 templates at different URL paths.
    return jingo.render(request, 'site/403.html', status=403)


def handler404(request):
    if request.path_info.startswith('/api/'):
        # Pass over to API handler404 view if API was targeted.
        return HttpResponseNotFound()
    else:
        return jingo.render(request, 'site/404.html', status=404)


def handler500(request):
    if request.path_info.startswith('/api/'):
        # Pass over to API handler500 view if API was targeted.
        return HttpResponseServerError()
    else:
        return jingo.render(request, 'site/500.html', status=500)


def csrf_failure(request, reason=''):
    return jingo.render(request, 'site/403.html',
                        {'because_csrf': 'CSRF' in reason}, status=403)


def manifest(request):
    ctx = RequestContext(request)
    data = {
        'name': getattr(settings, 'WEBAPP_MANIFEST_NAME',
                        'Firefox Marketplace'),
        'description': 'The Firefox Marketplace',
        'developer': {
            'name': 'Mozilla',
            'url': 'http://mozilla.org',
        },
        'icons': {
            # Using the default addon image until we get a marketplace logo.
            '128': media(ctx, 'img/mkt/logos/128.png'),
            '64': media(ctx, 'img/mkt/logos/64.png'),
            '32': media(ctx, 'img/mkt/logos/32.png'),
        },
        'activities': {
            'marketplace-app': {'href': '/'},
            'marketplace-app-rating': {'href': '/'},
            'marketplace-category': {'href': '/'},
            'marketplace-search': {'href': '/'},
        },
        'orientation': ['portrait-primary']
    }
    if settings.USE_APPCACHE:
        data['appcache_path'] = reverse('django_appcache.manifest')
    if get_carrier():
        data['launch_path'] = urlparams('/', carrier=get_carrier())

    manifest_content = json.dumps(data)
    manifest_etag = hashlib.md5(manifest_content).hexdigest()

    @etag(lambda r: manifest_etag)
    def _inner_view(request):
        response = HttpResponse(manifest_content,
                                mimetype='application/x-web-app-manifest+json')
        return response

    return _inner_view(request)


def get_package_path(signed=True):
    path = os.path.join(settings.MEDIA_ROOT, 'packaged-apps',
                        settings.PACKAGED_ZIP)
    if signed:
        split = path.rsplit('.', 1)
        split.insert(-1, 'signed')
        path = '.'.join(split)
    return path


def get_package_info(signed=True):
    """
    Returns the local path to the packaged `.zip` and a hash of
    the package's last-modified time.
    """
    path = get_package_path(signed=signed)
    etag = hashlib.md5(str(os.stat(path).st_mtime)).hexdigest()
    return path, etag


def package_zip(request):
    """
    Serves the packaged `.zip` with an `ETag` header based on
    the package's last-modified time.
    """
    package_path, package_etag = get_package_info()

    cache_key = 'package_zip:%s' % package_etag
    package_content = cache.get(cache_key)
    if package_content is None:
        with storage.open(package_path) as fd:
            package_content = fd.read()
        cache.set(cache_key, package_content)

    @etag(lambda r: package_etag)
    def _inner_view(request):
        return HttpResponse(package_content, mimetype='application/zip')

    return _inner_view(request)


def package_minifest(request):
    """Serves the mini manifest ("minifest") for the packaged `.zip`."""
    package_path, package_etag = get_package_info()

    manifest_content = json.dumps({
        'description': 'Firefox Marketplace',
        'developer': {
            'name': 'Mozilla',
            'url': 'http://mozilla.org',
        },
        'icons': {
            '64': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADwAAAA8CAYAAAA6/NlyAAAAGXRFWHRTb2Z0d2FyZQBBZG9iZSBJbWFnZVJlYWR5ccllPAAAA2hpVFh0WE1MOmNvbS5hZG9iZS54bXAAAAAAADw/eHBhY2tldCBiZWdpbj0i77u/IiBpZD0iVzVNME1wQ2VoaUh6cmVTek5UY3prYzlkIj8+IDx4OnhtcG1ldGEgeG1sbnM6eD0iYWRvYmU6bnM6bWV0YS8iIHg6eG1wdGs9IkFkb2JlIFhNUCBDb3JlIDUuMy1jMDExIDY2LjE0NTY2MSwgMjAxMi8wMi8wNi0xNDo1NjoyNyAgICAgICAgIj4gPHJkZjpSREYgeG1sbnM6cmRmPSJodHRwOi8vd3d3LnczLm9yZy8xOTk5LzAyLzIyLXJkZi1zeW50YXgtbnMjIj4gPHJkZjpEZXNjcmlwdGlvbiByZGY6YWJvdXQ9IiIgeG1sbnM6eG1wTU09Imh0dHA6Ly9ucy5hZG9iZS5jb20veGFwLzEuMC9tbS8iIHhtbG5zOnN0UmVmPSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvc1R5cGUvUmVzb3VyY2VSZWYjIiB4bWxuczp4bXA9Imh0dHA6Ly9ucy5hZG9iZS5jb20veGFwLzEuMC8iIHhtcE1NOk9yaWdpbmFsRG9jdW1lbnRJRD0ieG1wLmRpZDowNDgwMTE3NDA3MjA2ODExODIyQUI1QzMyMDQyNjY5NSIgeG1wTU06RG9jdW1lbnRJRD0ieG1wLmRpZDo2MDE5RjlGQTIyQjgxMUUyQkUyM0JCNEZBMkI4QTY1RSIgeG1wTU06SW5zdGFuY2VJRD0ieG1wLmlpZDo2MDE5RjlGOTIyQjgxMUUyQkUyM0JCNEZBMkI4QTY1RSIgeG1wOkNyZWF0b3JUb29sPSJBZG9iZSBQaG90b3Nob3AgQ1M2IChNYWNpbnRvc2gpIj4gPHhtcE1NOkRlcml2ZWRGcm9tIHN0UmVmOmluc3RhbmNlSUQ9InhtcC5paWQ6RkI3RjExNzQwNzIwNjgxMTgyMkFBQUUyODlFQjEzQUMiIHN0UmVmOmRvY3VtZW50SUQ9InhtcC5kaWQ6MDQ4MDExNzQwNzIwNjgxMTgyMkFCNUMzMjA0MjY2OTUiLz4gPC9yZGY6RGVzY3Jp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',
        },
        'messages': [
            {'mobilenetwork': '/index.html'}
        ],
        'name': getattr(settings, 'WEBAPP_MANIFEST_NAME', 'Marketplace'),
        'size': storage.size(package_path),
        'package_path': absolutify(reverse('package.zip')),
        'permissions': {
            'mobilenetwork': {'description': 'To detect mobile carrier and regional information.'}
        },
        #'release_notes': '',
        'type': 'privileged',
        'version': '0.0.4',
    })

    manifest_etag = hashlib.md5(manifest_content).hexdigest()

    @etag(lambda r: manifest_etag)
    def _inner_view(request):
        response = HttpResponse(manifest_content,
                                mimetype='application/x-web-app-manifest+json')
        return response

    return _inner_view(request)


def robots(request):
    """Generate a `robots.txt`."""
    template = jingo.render(request, 'site/robots.txt')
    return HttpResponse(template, mimetype='text/plain')


@csrf_exempt
@post_required
def record(request):
    # The rate limiting is done up on the client, but if things go wrong
    # we can just turn the percentage down to zero.
    if get_collect_timings():
        return django_statsd_record(request)
    raise PermissionDenied


# Cache this for an hour so that newly deployed changes are available within
# an hour. This will be served from the CDN which mimics these headers.
@cache_page(60 * 60)
def mozmarket_js(request):
    vendor_js = []
    for lib, path in (('receiptverifier',
                       'receiptverifier/receiptverifier.js'),):
        if lib in settings.MOZMARKET_VENDOR_EXCLUDE:
            continue
        with open(os.path.join(settings.ROOT,
                               'vendor', 'js', path), 'r') as fp:
            vendor_js.append((lib, fp.read()))
    js = render_to_string(request, 'site/mozmarket.js',
                          {'vendor_js': vendor_js})
    if settings.MINIFY_MOZMARKET:
        js = minify_js(js)
    return HttpResponse(js, content_type='text/javascript')


@statsd.timer('mkt.mozmarket.minify')
def minify_js(js):
    if settings.UGLIFY_BIN:
        log.info('minifying JS with uglify')
        return _minify_js_with_uglify(js)
    else:
        # The YUI fallback here is important
        # because YUI compressor is bundled with jingo
        # minify and therefore doesn't require any deps.
        log.info('minifying JS with YUI')
        return _minify_js_with_yui(js)


def _minify_js_with_uglify(js):
    sp = _open_pipe([settings.UGLIFY_BIN])
    js, err = sp.communicate(js)
    if sp.returncode != 0:
        raise ValueError('Compressing JS with uglify failed; error: %s'
                         % err.strip())
    return js


def _minify_js_with_yui(js):
    jar = os.path.join(os.path.dirname(jingo_minify.__file__), 'bin',
                       'yuicompressor-2.4.7.jar')
    if not os.path.exists(jar):
        raise ValueError('Could not find YUI compressor; tried %r' % jar)
    sp = _open_pipe([settings.JAVA_BIN, '-jar', jar, '--type', 'js',
                     '--charset', 'utf8'])
    js, err = sp.communicate(js)
    if sp.returncode != 0:
        raise ValueError('Compressing JS with YUI failed; error: %s'
                         % err.strip())
    return js


def _open_pipe(cmd):
    return subprocess.Popen(cmd,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)


def fireplace(request):
    return jingo.render(request, 'site/fireplace.html')
