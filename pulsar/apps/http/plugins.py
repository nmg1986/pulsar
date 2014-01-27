import sys
from functools import partial
from collections import namedtuple
from copy import copy

from pulsar import Future
from pulsar.apps.ws import WebSocketProtocol, WS
from pulsar.utils.websocket import frame_parser
from pulsar.utils.internet import is_tls
from pulsar.utils.httpurl import (REDIRECT_CODES, urlparse, urljoin,
                                  requote_uri, SimpleCookie)

from pulsar import PulsarException


class request_again(namedtuple('request_again', 'method url params')):

    @property
    def status_code(self):
        return -1

    @property
    def headers(self):
        return ()


class TooManyRedirects(PulsarException):

    def __init__(self, response):
        self.response = response


class WebSocketClient(WebSocketProtocol):
    status_code = 101

    @property
    def _request(self):
        return self.handshake._request

    @property
    def headers(self):
        return self.handshake.headers

    def __getattr__(self, name):
        if not name.startswith('__'):
            return getattr(self.handshake, name)
        else:
            raise AttributeError("'%s' object has no attribute '%s'" %
                                 (self.__class__.__name__, name))


def handle_redirect(response, exc=None):
    if not exc and (response.status_code in REDIRECT_CODES and
                    'location' in response.headers and
                    response._request.allow_redirects):
        # put at the end of the pile
        response.bind_event('post_request', _do_redirect)


def _do_redirect(response, exc=None):
    request = response.request
    client = request.client
    # done with current response
    url = response.headers.get('location')
    # Handle redirection without scheme (see: RFC 1808 Section 4)
    if url.startswith('//'):
        parsed_rurl = urlparse(request.full_url)
        url = '%s:%s' % (parsed_rurl.scheme, url)
    # Facilitate non-RFC2616-compliant 'location' headers
    # (e.g. '/path/to/resource' instead of
    # 'http://domain.tld/path/to/resource')
    if not urlparse(url).netloc:
        url = urljoin(request.full_url,
                      # Compliant with RFC3986, we percent
                      # encode the url.
                      requote_uri(url))
    history = request.history
    if history and len(history) >= request.max_redirects:
        raise TooManyRedirects(response)
    #
    params = request.inp_params.copy()
    params['history'] = copy(history) if history else []
    params['history'].append(response)
    if response.status_code == 303:
        method = 'GET'
        params.pop('data', None)
        params.pop('files', None)
    else:
        method = request.method
    return request_again(method, url, params)


def handle_cookies(response, exc=None):
    '''Handle response cookies.
    '''
    headers = response.headers
    request = response.request
    client = request.client
    response._cookies = c = SimpleCookie()
    if 'set-cookie' in headers or 'set-cookie2' in headers:
        for cookie in (headers.get('set-cookie2'),
                       headers.get('set-cookie')):
            if cookie:
                c.load(cookie)
        if client.store_cookies:
            client.cookies.extract_cookies(response, request)
    return response


def handle_100(response, exc=None):
    '''Handle Except: 100-continue.

    This is a pre_request hook which checks if the request headers
    have the ``Expect: 100-continue`` value. If so add a ``on_headers``
    callback to handle the response from the server.
    '''
    if not exc:
        request = response.request
        if (request.headers.has('expect', '100-continue') and
                response.status_code == 100):
            response.bind_event('on_headers', _write_body)


def _write_body(response, exc=None):
    if response.status_code == 100:
        response.request.new_parser()
        if response.request.body:
            response.transport.write(response.request.body)


def handle_101(response, exc=None):
    '''Websocket upgrade as ``on_headers`` event.'''

    if not exc and response.status_code == 101:
        connection = response.connection
        request = response._request
        handler = request.websocket_handler
        parser = frame_parser(kind=1)
        if not handler:
            handler = WS()
        connection.upgrade(partial(WebSocketClient, response, handler, parser))
        response.finished()


class Tunneling:
    '''A pre request callback for handling proxy tunneling.

    If Tunnelling is required, it writes the CONNECT headers and abort
    the writing of the actual request until headers from the proxy server
    are received.
    '''
    def __call__(self, response, exc=None):
        # the pre_request handler
        request = response._request
        if request:
            tunnel = request._tunnel
            if tunnel:
                if getattr(request, '_apply_tunnel', False):
                    # if transport is not SSL already
                    if not is_tls(response.transport.get_extra_info('socket')):
                        response._request = tunnel
                        response.bind_event('on_headers', self.on_headers)
                else:
                    # Append self again as pre_request
                    request._apply_tunnel = True
                    response.bind_event('pre_request', self)

    def on_headers(self, response, exc=None):
        '''Called back once the headers have arrived.'''
        if response.status_code == 200:
            connection = response._connection
            response.bind_event('post_request', self._tunnel_consumer)
            return response.finished()

    def _tunnel_consumer(self, response, exc=None):
        request = response._request.request
        connection = response._connection
        loop = connection._loop
        d = Future(loop)
        loop.remove_reader(connection.transport.sock.fileno())
        # Wraps the socket at the next iteration loop. Important!
        loop.call_later(1, self.switch_to_ssl, connection, request, d)
        return d

    def switch_to_ssl(self, connection, request, d):
        '''Wrap the transport for SSL communication.'''
        try:
            loop = connection._loop
            transport = connection.transport
            sock = transport.sock
            sslt = SocketStreamSslTransport(loop, sock, transport.protocol,
                                            request._ssl, server_side=False,
                                            server_hostname=request._netloc)
            connection._transport = sslt
            # silence connection made since it will be called again when the
            # ssl handshake occurs. This is just to avoid unwanted logging.
            connection.silence_event('connection_made')
            connection._processed -= 1
            connection.producer._requests_processed -= 1
            response = connection._build_consumer()
            response.start(request)
            response.on_finished.chain(d)
        except Exception:
            d.callback(sys.exc_info())
