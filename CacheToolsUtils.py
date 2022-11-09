"""
CacheTools Utilities Risika Version

This code is public domain.
"""
import datetime
import decimal
import sys
from typing import Any, Callable, Union, MutableMapping as MutMap

import cachetools
import json

import pkg_resources as pkg  # type: ignore

__version__ = pkg.require("CacheToolsUtils")[0].version

import logging

log = logging.getLogger(__name__)


#
# UTILS
#

_NO_DEFAULT = object()


class MutMapMix:
    """Convenient MutableMapping Mixin, forward to _cache."""

    def __contains__(self, key):
        return self._cache.__contains__(key)

    def __getitem__(self, key):
        return self._cache.__getitem__(key)

    def __setitem__(self, key, val):
        return self._cache.__setitem__(key, val)

    def __delitem__(self, key):
        return self._cache.__delitem__(key)

    def __len__(self):
        return self._cache.__len__()

    def __iter__(self):
        return self._cache.__iter__()


class KeyMutMapMix(MutMapMix):
    """Convenient MutableMapping Mixin with a key filter, forward to _cache."""

    def __contains__(self, key):
        return self._cache.__contains__(self._key(key))

    def __getitem__(self, key):
        return self._cache.__getitem__(self._key(key))

    def __setitem__(self, key, val):
        return self._cache.__setitem__(self._key(key), val)

    def __delitem__(self, key):
        return self._cache.__delitem__(self._key(key))


#
# CACHETOOLS EXTENSIONS
#


class PrefixedCache(KeyMutMapMix, MutMap):
    """Cache class to add a key prefix."""

    def __init__(self, cache: MutMap, prefix: Union[str, bytes] = ""):
        self._prefix = prefix
        self._cache = cache

    def _key(self, key):
        return self._prefix + str(key)


class StatsCache(MutMapMix, MutMap):
    """Cache class to keep stats."""

    def __init__(self, cache: MutMap):
        self._cache = cache
        self.reset()

    def hits(self):
        return float(self._hits) / float(max(self._reads, 1))

    def reset(self):
        self._reads, self._writes, self._dels, self._hits = 0, 0, 0, 0

    def __getitem__(self, key):
        self._reads += 1
        res = self._cache.__getitem__(key)
        self._hits += 1
        return res

    def __setitem__(self, key, val):
        self._writes += 1
        return self._cache.__setitem__(key, val)

    def __delitem__(self, key):
        self._dels += 1
        return self._cache.__delitem__(key)

    def clear(self):
        return self._cache.clear()


class TwoLevelCache(MutMapMix, MutMap):
    """Two-Level Cache class for CacheTools."""

    def __init__(self, cache: MutMap, cache2: MutMap):
        self._cache = cache
        self._cache2 = cache2

    def __getitem__(self, key):
        try:
            return self._cache.__getitem__(key)
        except KeyError:
            val = self._cache2.__getitem__(key)
            self._cache.__setitem__(key, val)
            return val

    def __setitem__(self, key, val):
        self._cache2.__setitem__(key, val)
        return self._cache.__setitem__(key, val)

    def __delitem__(self, key):
        try:
            self._cache2.__delitem__(key)
        except KeyError:
            pass
        return self._cache.__delitem__(key)

    def clear(self):
        return self._cache.clear()


# short generator type name
MapGen = Callable[[MutMap, str], MutMap]


def cacheMethods(cache: MutMap, obj: Any, gen: MapGen = PrefixedCache, **funs):
    """Cache some object methods."""
    for fun, prefix in funs.items():
        assert hasattr(obj, fun), f"cannot cache missing method {fun} on {obj}"
        f = getattr(obj, fun)
        while hasattr(f, "__wrapped__"):
            f = f.__wrapped__
        setattr(obj, fun, cachetools.cached(cache=gen(cache, prefix))(f))


def cacheFunctions(
    cache: MutMap, globs: MutMap[str, Any], gen: MapGen = PrefixedCache, **funs
):
    """Cache some global functions."""
    for fun, prefix in funs.items():
        assert fun in globs, "caching functions: {fun} not found"
        f = globs[fun]
        while hasattr(f, "__wrapped__"):
            f = f.__wrapped__
        globs[fun] = cachetools.cached(cache=gen(cache, prefix))(f)


#
# MEMCACHED
#


class JsonSerde:
    """JSON serialize/deserialize for MemCached."""

    # keep strings, else json
    def serialize(self, key, value):
        if isinstance(value, str):
            return value.encode("utf-8"), 1
        else:
            return json.dumps(value).encode("utf-8"), 2

    # reverse previous serialization
    def deserialize(self, key, value, flag):
        if flag == 1:
            return value.decode("utf-8")
        elif flag == 2:
            return json.loads(value.decode("utf-8"))
        else:
            raise Exception("Unknown serialization format")


class MemCached(KeyMutMapMix, MutMap):
    """MemCached-compatible wrapper class for cachetools with key encoding."""

    def __init__(self, cache):
        self._cache = cache

    # memcached keys are constrained bytes, we need some encoding…
    # short (250 bytes) ASCII without control chars nor spaces
    # we do not use hashing which might be costly and induce collisions
    def _key(self, key):
        import base64

        return base64.b64encode(str(key).encode("utf-8"))

    def __len__(self):
        return self._cache.stats()[b"curr_items"]

    def stats(self):
        return self._cache.stats()

    def clear(self):  # pragma: no cover
        return self._cache.flush_all()

    def hits(self):
        """Return overall cache hit rate."""
        stats = self._cache.stats()
        return float(stats[b"get_hits"]) / max(stats[b"cmd_get"], 1)


class PrefixedMemCached(MemCached):
    """MemCached-compatible wrapper class for cachetools with a key prefix."""

    def __init__(self, cache, prefix: str = ""):
        super().__init__(cache=cache)
        self._prefix = bytes(prefix, "utf-8")

    def _key(self, key):
        import base64

        return self._prefix + base64.b64encode(str(key).encode("utf-8"))


class StatsMemCached(MemCached):
    """Cache MemCached-compatible class with stats."""

    pass


#
# REDIS
#

class RedisJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime.datetime):
            ARGS = ('year', 'month', 'day', 'hour', 'minute',
                     'second', 'microsecond')
            return {'__type__': 'datetime.datetime',
                    'args': [getattr(obj, a) for a in ARGS]}
        elif isinstance(obj, datetime.date):
            ARGS = ('year', 'month', 'day')
            return {'__type__': 'datetime.date',
                    'args': [getattr(obj, a) for a in ARGS]}
        elif isinstance(obj, datetime.time):
            ARGS = ('hour', 'minute', 'second', 'microsecond')
            return {'__type__': 'datetime.time',
                    'args': [getattr(obj, a) for a in ARGS]}
        elif isinstance(obj, datetime.timedelta):
            ARGS = ('days', 'seconds', 'microseconds')
            return {'__type__': 'datetime.timedelta',
                    'args': [getattr(obj, a) for a in ARGS]}
        elif isinstance(obj, decimal.Decimal):
            return {'__type__': 'decimal.Decimal',
                    'args': [str(obj),]}
        else:
            return super().default(obj)


class RedisJSONDecoder(json.JSONDecoder):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, object_hook=self.object_hook,
                         **kwargs)

    def object_hook(self, d):
        if '__type__' not in d:
            return d
        o = sys.modules[__name__]
        for e in d['__type__'].split('.'):
            o = getattr(o, e)
        args, kwargs = d.get('args', ()), d.get('kwargs', {})
        return o(*args, **kwargs)


class RedisCache(MutMap):
    """Redis wrapper for cachetools."""

    def __init__(self, cache, ttl=600):
        self._cache = cache
        self._ttl = ttl
        self._circuit_breaker_until = None

    def clear(self):  # pragma: no cover
        return self._cache.flushdb()

    def _serialize(self, s):
        return json.dumps(s, cls=RedisJSONEncoder)

    def _deserialize(self, s):
        return json.loads(s, cls=RedisJSONDecoder)

    def _key(self, key):
        return json.dumps(key)

    def _circuit_breaker_on(self):
        if self._circuit_breaker_until and self._circuit_breaker_until > datetime.datetime.now():
            return True
        else:
            self._circuit_breaker_until = None
            print("Disabling REDIS circuit breaker")
            return False

    def _set_circuit_breaker_on(self):
        print("Enabling REDIS circuit breaker")
        self._circuit_breaker_until = datetime.datetime.now() + datetime.timedelta(minutes=1)

    def __getitem__(self, index):
        if self._circuit_breaker_on():
            raise KeyError()
        try:
            val = self._cache.get(self._key(index))
        except Exception:
            self._set_circuit_breaker_on()
            raise KeyError()

        if val:
            return self._deserialize(val)
        else:
            raise KeyError()

    def __setitem__(self, index, value):
        if self._circuit_breaker_on():
            return
        try:
            return self._cache.set(self._key(index), self._serialize(value), ex=self._ttl)
        except Exception:
            self._set_circuit_breaker_on()
            return

    def __delitem__(self, index):
        if self._circuit_breaker_on():
            return 0
        try:
            return self._cache.delete(self._key(index))
        except Exception:
            self._set_circuit_breaker_on()
            return

    def __len__(self):
        if self._circuit_breaker_on():
            return 0
        try:
            return self._cache.dbsize()
        except Exception:
            self._set_circuit_breaker_on()
            return 0

    def __iter__(self):
        raise Exception("not implemented yet")

    def info(self, *args, **kwargs):
        return self._cache.info(*args, **kwargs)

    def dbsize(self, *args, **kwargs):
        return self._cache.dbsize(*args, **kwargs)

    # also forward Redis set/get/delete
    def set(self, index, value, **kwargs):
        if self._circuit_breaker_on():
            return
        if "ex" not in kwargs:  # pragma: no cover
            kwargs["ex"] = self._ttl
        try:
            return self._cache.set(self._key(index), self._serialize(value), **kwargs)
        except Exception:
            self._set_circuit_breaker_on()
            return

    def get(self, index, default=None):
        return self[index]

    def delete(self, index):
        del self[index]

    # stats
    def hits(self):
        stats = self.info(section="stats")
        return float(stats["keyspace_hits"]) / (
            stats["keyspace_hits"] + stats["keyspace_misses"]
        )


class PrefixedRedisCache(RedisCache):
    """Prefixed Redis wrapper class for cachetools."""

    def __init__(self, cache, prefix: str = "", ttl=600):
        super().__init__(cache, ttl)
        self._prefix = prefix

    def _key(self, key):
        # or self._cache._key(prefix + key)?
        return self._prefix + str(key)


class StatsRedisCache(PrefixedRedisCache):
    """TTL-ed Redis wrapper class for cachetools."""

    pass
