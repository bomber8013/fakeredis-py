import inspect
import logging
import queue
import threading
import time
import uuid
import warnings
import weakref
from collections import defaultdict
from typing import Dict

import redis

from fakeredis._fakesocket import FakeSocket
from fakeredis._helpers import (Database, FakeSelector)
from . import _msgs as msgs

LOGGER = logging.getLogger('fakeredis')


class FakeServer:
    _servers_map: Dict[str, 'FakeServer'] = dict()

    def __init__(self, version=7):
        self.lock = threading.Lock()
        self.dbs = defaultdict(lambda: Database(self.lock))
        # Maps channel/pattern to weak set of sockets
        self.subscribers = defaultdict(weakref.WeakSet)
        self.psubscribers = defaultdict(weakref.WeakSet)
        self.lastsave = int(time.time())
        self.connected = True
        # List of weakrefs to sockets that are being closed lazily
        self.closed_sockets = []
        self.version = version

    @staticmethod
    def get_server(key, version: int):
        return FakeServer._servers_map.setdefault(key, FakeServer(version=version))


class FakeBaseConnectionMixin:
    def __init__(self, *args, **kwargs):
        self.client_name = None
        self._sock = None
        self._selector = None
        self._server = kwargs.pop('server', None)
        path = kwargs.pop('path', None)
        version = kwargs.pop('version', 7)
        connected = kwargs.pop('connected', True)
        if self._server is None:
            if path:
                self.server_key = path
            else:
                host, port = kwargs.get('host'), kwargs.get('port')
                self.server_key = uuid.uuid4().hex if host is None or port is None else f'{host}:{port}'
            self.server_key += f'v{version}'
            self._server = FakeServer.get_server(self.server_key, version=version)
            self._server.connected = connected
        super().__init__(*args, **kwargs)


class FakeConnection(FakeBaseConnectionMixin, redis.Connection):

    def connect(self):
        super().connect()
        # The selector is set in redis.Connection.connect() after _connect() is called
        self._selector = FakeSelector(self._sock)

    def _connect(self):
        if not self._server.connected:
            raise redis.ConnectionError(msgs.CONNECTION_ERROR_MSG)
        return FakeSocket(self._server, db=self.db)

    def can_read(self, timeout=0):
        if not self._server.connected:
            return True
        if not self._sock:
            self.connect()
        # We use check_can_read rather than can_read, because on redis-py<3.2,
        # FakeSelector inherits from a stub BaseSelector which doesn't
        # implement can_read. Normally can_read provides retries on EINTR,
        # but that's not necessary for the implementation of
        # FakeSelector.check_can_read.
        return self._selector.check_can_read(timeout)

    def _decode(self, response):
        if isinstance(response, list):
            return [self._decode(item) for item in response]
        elif isinstance(response, bytes):
            return self.encoder.decode(response, )
        else:
            return response

    def read_response(self, **kwargs):
        if not self._server.connected:
            try:
                response = self._sock.responses.get_nowait()
            except queue.Empty:
                if kwargs.get('disconnect_on_error', True):
                    self.disconnect()
                raise redis.ConnectionError(msgs.CONNECTION_ERROR_MSG)
        else:
            response = self._sock.responses.get()
        if isinstance(response, redis.ResponseError):
            raise response
        if kwargs.get('disable_decoding', False):
            return response
        else:
            return self._decode(response)

    def repr_pieces(self):
        pieces = [
            ('server', self._server),
            ('db', self.db)
        ]
        if self.client_name:
            pieces.append(('client_name', self.client_name))
        return pieces

    def __str__(self):
        return self.server_key


class FakeRedisMixin:
    def __init__(self, *args, server=None, connected=True, version=7, **kwargs):
        # Interpret the positional and keyword arguments according to the
        # version of redis in use.
        parameters = inspect.signature(redis.Redis.__init__).parameters
        parameter_names = list(parameters.keys())
        default_args = parameters.values()
        ignore_default_param_values = {'host', 'port', 'db'}
        kwds = {p.name: p.default
                for p in default_args
                if (p.default != inspect.Parameter.empty
                    and p.name not in ignore_default_param_values)}
        kwds.update(kwargs)
        if not kwds.get('connection_pool', None):
            charset = kwds.get('charset', None)
            errors = kwds.get('errors', None)
            # Adapted from redis-py
            if charset is not None:
                warnings.warn(DeprecationWarning(
                    '"charset" is deprecated. Use "encoding" instead'))
                kwds['encoding'] = charset
            if errors is not None:
                warnings.warn(DeprecationWarning(
                    '"errors" is deprecated. Use "encoding_errors" instead'))
                kwds['encoding_errors'] = errors
            conn_pool_args = {
                'host',
                'port',
                'db',
                # Ignoring because AUTH is not implemented
                # 'username',
                # 'password',
                'socket_timeout',
                'encoding',
                'encoding_errors',
                'decode_responses',
                'retry_on_timeout',
                'max_connections',
                'health_check_interval',
                'client_name',
            }
            connection_kwargs = {
                'connection_class': FakeConnection,
                'server': server,
                'version': version,
            }
            connection_kwargs.update({arg: kwds[arg] for arg in conn_pool_args if arg in kwds})
            kwds['connection_pool'] = redis.connection.ConnectionPool(**connection_kwargs)
        kwds.pop('server', None)
        kwds.pop('connected', None)
        kwds.pop('version', None)
        parameter_names_to_cut = parameter_names[1:len(args) + 1]
        for param in parameter_names_to_cut:
            kwds.pop(param, None)
        super().__init__(*args, **kwds)

    @classmethod
    def from_url(cls, *args, **kwargs):
        pool = redis.ConnectionPool.from_url(*args, **kwargs)
        # Now override how it creates connections
        pool.connection_class = FakeConnection
        # Using username and password fails since AUTH is not implemented.
        # https://github.com/cunla/fakeredis-py/issues/9
        pool.connection_kwargs.pop('username', None)
        pool.connection_kwargs.pop('password', None)
        return cls(connection_pool=pool)


class FakeStrictRedis(FakeRedisMixin, redis.StrictRedis):
    pass


class FakeRedis(FakeRedisMixin, redis.Redis):
    pass


# RQ
# Configuration to pretend there is a Redis service available.
# Set up the connection before RQ Django reads the settings.
# The connection must be the same because in fakeredis connections
# do not share the state. Therefore, we define a singleton object to reuse it.
class FakeRedisConnSingleton:
    """Singleton FakeRedis connection."""

    def __init__(self):
        self.conn = None

    def __call__(self, _, strict):
        if not self.conn:
            self.conn = FakeStrictRedis() if strict else FakeRedis()
        return self.conn
