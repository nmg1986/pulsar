import socket
from functools import partial, reduce

from pulsar import TooManyConnections
from pulsar.utils.pep import get_event_loop, new_event_loop, itervalues
from pulsar.utils.sockets import get_transport_type, create_socket
from pulsar.async.defer import is_failure

from .protocols import ProtocolConsumer, EventHandler, Producer
from .transport import create_transport, LOGGER

__all__ = ['ConnectionPool', 'Client', 'Request', 'SingleClient']

    
class Request(object):
    '''A :class:`Client` request class is an hashable object used to select
the appropiate :class:`ConnectionPool` for the client request.'''
    def __init__(self, address, timeout=0):
        self.address = address
        self.timeout = timeout
        
    @property
    def key(self):
        return (self.address, self.timeout)
    
    def encode(self):
        raise NotImplementedError
    
    
class ConnectionPool(Producer):
    '''A :class:`Producer` of of active connections for client
protocols. It maintains a live set of connections.

.. attribute:: address

    Address to connect to
    '''    
    def __init__(self, request, **params):
        params['timeout'] = request.timeout
        super(ConnectionPool, self).__init__(**params)
        self._address = request.address
        self._available_connections = set()
    
    @property
    def address(self):
        return self._address
    
    @property
    def available_connections(self):
        return len(self._available_connections)
        
    def release_connection(self, connection, response=None):
        '''Releases the *connection* back to the pool. This function remove
the *connection* from the set of concurrent connections and add it to the set
of available connections.

:parameter connection: The connection to release
:parameter response: Optional :class:`ProtocolConsumer` which consumed the
    connection.
'''
        self._concurrent_connections.discard(connection)
        if connection.producer.can_reuse_connection(connection, response):
            self._available_connections.add(connection)
        self._remove_self(connection.producer)
        
    def get_or_create_connection(self, client):
        "Get or create a new connection for *client*"
        try:
            closed = True
            while closed:
                connection = self._available_connections.pop()
                closed = connection.is_stale()
                if closed and not connection.closed:
                    connection.transport.close()
        except KeyError:
            connection = None
        else:
            # we have a connection, lets added it to the concurrent set
            self._concurrent_connections.add(connection)
        if connection is None:
            # build the new connection
            connection = self.new_connection(self.address,
                                             client.consumer_factory,
                                             producer=client)
            # Bind the post request event to the release connection function
            connection.bind_event('post_request', self._release_response)
            # Bind the connection_lost to connection to handle dangling connections
            connection.bind_event('connection_lost',
                                  partial(self._try_reconnect, connection))
            #IMPORTANT: create client transport an connect to endpoint
            transport = create_transport(connection, address=connection.address,
                                         event_loop=client.get_event_loop())
            return transport.connect(connection.address)
        else:
            return connection
        
    ############################################################################
    ##    INTERNALS
    def _try_reconnect(self, connection, exc):
        # handle Read Exception on the transport
        if is_failure(exc) and exc.is_instance((socket.timeout, socket.error)):
            # Have we been here before?
            consumer = connection.current_consumer
            if consumer is None:
                # No consumer, The address was probably wrong. Connection Refused
                return
            client = connection.producer
            received = getattr(consumer, '_received_count', -1)
            # The connection has processed request before and the consumer
            # has never received data. If the client allows it, try to
            # reconnect, it was probably a stale connection.
            retries = consumer.can_reconnect(client.max_reconnect, exc)
            if retries:
                connection._current_consumer = None
                lag = retries - 1
                if lag:
                    lag = client.reconnect_time_lag(lag)
                    LOGGER.debug('Try to reconnect in %s seconds', lag)
                    loop = get_event_loop()
                    loop.call_later(lag, self._reconnect, client, consumer)
                else:
                    LOGGER.debug('Try to reconnect')
                    self._reconnect(client, consumer)
                    
    def _reconnect(self, client, consumer):
        # get a new connection
        conn = self.get_or_create_connection(client)
        # Start the response without firing the events
        conn.set_consumer(consumer)
        consumer.new_request(consumer.current_request)
                
    def _remove_connection(self, connection, exc=None):
        super(ConnectionPool, self)._remove_connection(connection, exc)
        self._available_connections.discard(connection)
        self._remove_self(connection.producer)
    
    def _release_response(self, response):
        #proxy to release_connection
        if getattr(response, 'release_connection', True):
            self.release_connection(response.connection, response)

    def _remove_self(self, client):
        if not self._available_connections and not self._concurrent_connections:
            client.remove_pool(self)


class Client(EventHandler):
    '''A client for a remote server which handles one or more
:class:`ConnectionPool` of asynchronous connections.

.. attribute:: force_sync

    Force a synchronous client, that is a client which has it
    own :class:`EventLoop` and blocks until a response is available.
    
    Default: `False`
'''
    max_reconnect = 1
    '''Can reconnect on socket error.'''
    connection_pools = None
    '''Dictionar of :class:`ConnectionPool`. If initialized at class level it
will remain as a class attribute, otherwise it will be an instance attribute.'''
    connection_pool = ConnectionPool
    '''Factory of :class:`ConnectionPool`.'''
    consumer_factory = None
    '''A factory of :class:`ProtocolConsumer` for sending and consuming data.'''
    connection_factory = None
    '''A factory of :class:`Connection`.'''
    client_version = ''
    '''An optional version for this client'''
    timeout = 0
    '''Optional timeout in seconds for idle connections.'''
    max_connections = 0
    '''Maximum number of concurrent connections.'''
    reconnecting_gap = 2
    '''Reconnecting gap in seconds.'''
    
    
    ONE_TIME_EVENTS = ('finish',)
    MANY_TIMES_EVENTS = ('connection_made', 'pre_request','post_request',
                         'connection_lost')
    
    def __init__(self, max_connections=None, timeout=None, client_version=None,
                 trust_env=True, consumer_factory=None, max_reconnect=None,
                 force_sync=False, **params):
        super(Client, self).__init__()
        self.trust_env = trust_env
        self.client_version = client_version or self.client_version
        self.timeout = timeout if timeout is not None else self.timeout
        if consumer_factory:
            self.consumer_factory = consumer_factory
        self.max_connections = max_connections or self.max_connections or 2**31
        if self.connection_pools is None:
            self.connection_pools = {}
        if max_reconnect:
            self.max_reconnect = max_reconnect
        self.force_sync = force_sync
        self._event_loop = None
        self.setup(**params)
    
    def setup(self, **params):
        '''Setup the client. By default it does nothing.'''
    
    def __str__(self):
        return self.__repr__()
    
    def __repr__(self):
        return self.__class__.__name__
    
    @property
    def concurrent_connections(self):
        return reduce(lambda x,y: x + y, (p.concurrent_connections for p in\
                                          itervalues(self.connection_pools)), 0)
    
    @property
    def available_connections(self):
        return reduce(lambda x,y: x + y, (p.available_connections for p in\
                                          itervalues(self.connection_pools)), 0)
        
    def get_event_loop(self):
        if self._event_loop:
            return self._event_loop
        elif self.force_sync:
            self._event_loop = new_event_loop(iothreadloop=False)
            return self._event_loop
        else:
            return get_event_loop()
    
    def hash(self, address, timeout, request):
        return hash((address, timeout))
    
    def request(self, *args, **params):
        '''Abstract method for creating a request to send to the server.
**Must be implemented by subclasses**. The method should return a
:class:`ProtocolConsumer` via invoking the :meth:`response` method. '''
        raise NotImplementedError
    
    def response(self, request):
        '''Once a *request* object has been constructed, the :meth:`request`
method can invoke this method to build the protocol consumer and
start the response.

:parameter request: A custom :class:`Request` for the :class:`Client`.
:rtype: An :class:`ProtocolConsumer` obtained form
    :attr:`consumer_factory`.
'''
        conn = self.get_connection(request)
        # build the protocol consumer
        consumer = self.consumer_factory(conn)
        # start the request
        consumer.new_request(request)
        event_loop = self.get_event_loop()
        if self.force_sync:
            event_loop.run_until_complete(consumer.on_finished)
        return consumer
    
    def get_connection(self, request):
        '''Get a suitable :class:`Connection` for *request*.'''
        pool = self.connection_pools.get(request.key)
        if pool is None:
            pool = self.connection_pool(
                                    request,
                                    max_connections=self.max_connections,
                                    connection_factory=self.connection_factory)
            self.connection_pools[request.key] = pool
        return pool.get_or_create_connection(self)
        
    def update_parameters(self, parameter_list, params):
        '''Update *param* with attributes of this :class:`Client` defined
in :attr:`request_parameters` tuple.'''
        for name in parameter_list:
            if name not in params:
                params[name] = getattr(self, name)
        return params
        
    def close_connections(self, async=True):
        for p in self.connection_pools.values():
            p.close_connections(async=async)
            
    def close(self, async=True):
        self.close_connections(async)
        self.fire_event('finish')
        
    def abort(self):
        self.close(async=False)

    def can_reuse_connection(self, connection, response):
        '''Invoked by the :meth:`ConnectionPool.release_connection`, it checks
whether the *connection* can be reused in the future or it must be disposed.

:param connection: the :class:`Connection` to check.
:param response: the :class:`ProtocolConsumer` which last consumed the incoming
    data from the connection (it can be ``None``).
:return: ``True`` or ``False``.
'''
        return True
    
    def reconnect_time_lag(self, lag):
        lag = self.reconnect_time_lag*(math.log(lag) + 1)
        return round(lag, 1)
    
    def remove_pool(self, pool):
        key = None
        for key, p in self.connection_pools.items():
            if pool is p:
                break
        if key:
            self.connection_pools.pop(key)
            
    def upgrade(self, connection, protocol_factory):
        '''Upgrade an existing connection with a new protocol factory.
Return the upgraded connection only if the :attr:`Connection.current_consumer`
is available.'''
        protocol = connection.current_consumer
        if protocol:
            protocol.release_connection = False
            protocol.finished()
            connection.upgrade(protocol_factory)
            return connection
             
        
class SingleClient(Client):
    '''A :class:`Client` which handle one connection only.'''
    def __init__(self, address, **kwargs):
        super(SingleClient, self).__init__(**kwargs)
        self.address = address
        self._consumer = None
    
    def response(self, request):
        if self._consumer is None:
            self._consumer = super(SingleClient, self).response(request)
        else:
            self._consumer.new_request(request)
        return self._consumer