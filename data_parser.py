from collections import OrderedDict
import functools
from io import BytesIO
import io
import struct
import binascii
import copy

from utilities import DotDict


#
## Packet Helpers
#

class NotFound:
    pass


class StructCacher:
    def __init__(self):
        self.cache = {}
        self.set_count = 0
        self.retrieve_count = 0

    def get_key(self, string, *args, **kwargs):
        return hash(string)

    def retrieve(self, cls, string, *args, **kwargs):
        key = self.get_key(string)
        try:
            c = self.cache[cls.__name__][key]
            self.retrieve_count += 1
            return c
        except KeyError:
            return None

    def set(self, cls, result, string):
        key = self.get_key(string)
        self.set_key(cls.__name__, key, result)

    def set_key(self, cls, key, result):
        self.set_count += 1
        self.cache[cls][key] = result


cacher = StructCacher()


def composed(*decs):
    def deco(f):
        for dec in reversed(decs):
            f = dec(f)
        return f

    return deco


def make_hash(o):
    """
    Makes a hash from a dictionary, list, tuple or set to any level, that
    contains only other hashable types (including any lists, tuples, sets, and
    dictionaries).
    """

    if isinstance(o, (set, tuple, list)):
        return tuple([make_hash(e) for e in o])
    elif not isinstance(o, dict):
        return hash(o)

    new_o = copy.deepcopy(o)
    for k, v in new_o.items():
        new_o[k] = make_hash(v)

    return hash(tuple(frozenset(sorted(new_o.items()))))


class OrderedDotDict(OrderedDict, DotDict):
    def __hash__(self):
        return make_hash(self)


cm = composed(classmethod, functools.lru_cache())


class MetaStruct(type):
    @classmethod
    def __prepare__(mcs, name, bases):
        return OrderedDict({'_struct_fields': [], '_cache': {}})

    def __new__(mcs, name, bases, clsdict):
        for key, value in clsdict.items():
            if isinstance(value, mcs):
                clsdict['_struct_fields'].append((key, value))
        c = type.__new__(mcs, name, bases, clsdict)
        cacher.cache[c.__name__] = {}
        return c


class Struct(metaclass=MetaStruct):
    @classmethod
    def parse(cls, string, ctx=None):
        if not isinstance(string, io.BufferedReader):
            if not isinstance(string, BytesIO):
                if isinstance(string, str):
                    string = bytes(string, encoding="utf-8")
                string = BytesIO(string)
            string = io.BufferedReader(string)

        # FIXME: Stream caching appears to be causing a parsing issue. Disabling
        #  for now...
        # d = string.peek()
        # big_enough = len(d) > 1
        # if big_enough:
        #     _c = cacher.retrieve(cls, d)
        #     if _c is not None:
        #         return _c

        if ctx is None:
            ctx = {}
        res = cls.parse_stream(string, ctx)
        # if big_enough:
        #     cacher.set(cls, res, d)
        return res

    @classmethod
    def parse_stream(cls, stream, ctx=None):
        if cls._struct_fields:
            for name, struct in cls._struct_fields:
                try:
                    ctx[name] = struct.parse(stream, ctx=ctx)
                except:
                    print("Context at time of failure:", ctx)
                    raise
            res = ctx
        else:
            res = cls._parse(stream, ctx=ctx)

        return res

    @classmethod
    def build(cls, obj, res=None, ctx=None):
        if res is None:
            res = b''
        if ctx is None:
            ctx = {}
        if cls._struct_fields:
            for name, struct in cls._struct_fields:
                try:
                    if name in obj:
                        res += struct.build(obj[name], ctx=ctx)
                    else:
                        res += struct.build(None, ctx=ctx)
                except:
                    print("Context at time of failure:", ctx)
                    raise
        else:
            res = cls._build(obj, ctx=ctx)
        return res

    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        raise NotImplementedError

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        raise NotImplementedError


class VLQ(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict) -> int:
        value = 0
        while True:
            try:
                tmp = ord(stream.read(1))
                value = (value << 7) | (tmp & 0x7f)
                if tmp & 0x80 == 0:
                    break
            except TypeError:  # If the stream is empty.
                break
        return value

    @classmethod
    def _build(cls, obj, ctx):
        result = bytearray()
        value = int(obj)
        if obj == 0:
            result = bytearray(b'\x00')
        else:
            while value > 0:
                byte = value & 0x7f
                value >>= 7
                if value != 0:
                    byte |= 0x80
                result.insert(0, byte)
            if len(result) > 1:
                result[0] |= 0x80
                result[-1] ^= 0x80
        return bytes(result)


class SignedVLQ(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        v = VLQ.parse(stream, ctx)
        if (v & 1) == 0x00:
            return v >> 1
        else:
            return -((v >> 1) + 1)

    @classmethod
    def _build(cls, obj, ctx):
        value = abs(obj * 2)
        if obj < 0:
            value -= 1
        return VLQ.build(value, ctx)


class UBInt16(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return struct.unpack(">H", stream.read(2))[0]

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return struct.pack(">H", obj)


class SBInt16(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return struct.unpack(">h", stream.read(2))[0]

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return struct.pack(">h", obj)


class UBInt32(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return struct.unpack(">L", stream.read(4))[0]

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return struct.pack(">L", obj)


class SBInt32(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return struct.unpack(">l", stream.read(4))[0]

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return struct.pack(">l", obj)


class BFloat32(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return struct.unpack(">f", stream.read(4))[0]

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return struct.pack(">f", obj)


class StarByteArray(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        length = VLQ.parse(stream, ctx)
        return stream.read(length)

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return VLQ.build(len(obj), ctx) + obj


class StarString(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        data = StarByteArray.parse(stream, ctx)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return StarByteArray.build(obj.encode("utf-8"), ctx)


class Byte(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return int.from_bytes(stream.read(1), byteorder="big", signed=False)

    @classmethod
    def _build(cls, obj: int, ctx: OrderedDotDict):
        return obj.to_bytes(1, byteorder="big", signed=False)


class Flag(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return bool(stream.read(1))

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return struct.pack(">?", obj)


class BDouble(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return struct.unpack(">d", stream.read(8))

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        return struct.pack(">d", obj)


class UUID(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        return binascii.hexlify(stream.read(16))

    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        res = b''
        if obj:
            res += Flag.build(True)
            res += obj
        else:
            res += Flag.build(False)
        return res


class VariantVariant(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        l = VLQ.parse(stream, ctx)
        return [Variant.parse(stream, ctx) for _ in range(l)]


class DictVariant(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        l = VLQ.parse(stream, ctx)
        c = {}
        for _ in range(l):
            key = StarString.parse(stream, ctx)
            value = Variant.parse(stream, ctx)
            if isinstance(value, bytes):
                try:
                    value = value.decode('utf-8')
                except UnicodeDecodeError:
                    pass
            c[key] = value
        return c


class Variant(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        x = Byte.parse(stream, ctx)
        if x == 1:
            return None
        elif x == 2:
            return BDouble.parse(stream, ctx)
        elif x == 3:
            return Flag.parse(stream, ctx)
        elif x == 4:
            return SignedVLQ.parse(stream, ctx)
        elif x == 5:
            return StarString.parse(stream, ctx)
        elif x == 6:
            return VariantVariant.parse(stream, ctx)
        elif x == 7:
            return DictVariant.parse(stream, ctx)


class StringSet(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        l = VLQ.parse(stream, ctx)
        c = []
        for _ in range(l):
            value = StarString.parse(stream, ctx)
            if isinstance(value, bytes):
                try:
                    value = value.decode('utf-8')
                except UnicodeDecodeError:
                    pass
            c.append(value)
        return c


class WorldChunks(Struct):
    @classmethod
    def _parse(cls, stream: BytesIO, ctx: OrderedDict):
        l = VLQ.parse(stream, ctx)
        d = {}
        c = []
        n = 0
        for _ in range(l):
            # value1 = StarByteArray.parse(stream, ctx)
            # sep = Byte.parse(stream, ctx)
            # value2 = StarByteArray.parse(stream, ctx)
            # c.append((value1, value2))
            v1 = VLQ.parse(stream, ctx)
            c1 = stream.read(v1)
            sep = Byte.parse(stream, ctx)
            v2 = VLQ.parse(stream, ctx)
            c2 = stream.read(v2)
            c.append((n, v1, c1, sep, v2, c2))
            n += 1
        d['length'] = l
        d['content'] = c
        return d


class GreedyArray(Struct):
    @classmethod
    def parse_stream(cls, stream, ctx=None):
        bcls = cls.mro()[0]
        res = []
        _l = -1
        try:
            while True:
                l = len(stream.peek())
                if l == 0 or _l == l:
                    break
                res.append(super().parse(stream, ctx))
                _l = l
        finally:
            return res


class SpawnCoordinates(Struct):
    x = BFloat32
    y = BFloat32

#
## Packet implementations
#

class ProtocolRequest(Struct):
    """packet type: 9"""
    client_build = UBInt32


class ProtocolResponse(Struct):
    """packet type 0"""
    server_response = Byte


class ClientConnect(Struct):
    """packet type: 10"""
    asset_digest = StarByteArray
    uuid = UUID
    name = StarString
    species = StarString
    shipdata = WorldChunks
    ship_level = UBInt32
    max_fuel = UBInt32
    # Junk means, I don't know what this value represents... <_<
    junk1 = UBInt32
    ship_upgrades = StringSet
    intro_complete = Byte
    account = StarString


class ConnectSuccess(Struct):
    """packet type: 2"""
    client_id = VLQ
    server_uuid = UUID
    planet_orbital_levels = SBInt32
    satellite_orbital_levels = SBInt32
    chunk_size = SBInt32
    xy_min = SBInt32
    xy_max = SBInt32
    z_min = SBInt32
    z_max = SBInt32


class ConnectFailure(Struct):
    """packet type: 3"""
    reason = StarString


class ClientDisconnectRequest(Struct):
    """packet type: 11"""
    request = Byte


class ServerDisconnect(Struct):
    """packet type: 11"""
    reason = StarString


class ChatReceived(Struct):
    """packet type: 5"""
    mode = Byte
    channel = StarString
    client_id = UBInt16
    name = StarString
    message = StarString


class PlayerWarp(Struct):
    """packet type: 13"""
    warp_type = Byte
    everything_else = StarString


class ChatSent(Struct):
    """packet type: 15"""
    message = StarString
    send_mode = Byte


class WorldStart(Struct):
    """packet type: 18"""
    template_data = Variant
    sky_data = StarByteArray
    weather_data = StarByteArray
    spawn = SpawnCoordinates
    respawn_in_world = Flag
    #dungeonid = StarString
    world_properties = Variant
    client_id = UBInt16
    local_interpolation = Flag


class WorldStop(Struct):
    """packet type: 19"""
    reason = StarString


class GiveItem(Struct):
    """packet type: 26"""
    name = StarString
    count = VLQ
    variant_type = Byte
    description = StarString


class BasePacket(Struct):
    @classmethod
    def _build(cls, obj, ctx: OrderedDotDict):
        res = b''
        res += Byte.build(obj['id'], ctx)
        v = len(obj['data'])
        if 'compressed' in ctx and ctx['compressed']:
            v = -abs(v)
        res += SignedVLQ.build(v)
        if not isinstance(obj['data'], bytes):
            obj['data'] = bytes(obj['data'].encode("utf-8"))
        res += obj['data']
        return res
