"""Cliente SNMP (pysnmp) e cliente simulado para testes sem roteador.

Compatível com:
  * pysnmp 7.x  -> API assíncrona (pysnmp.hlapi.v3arch.asyncio: get_cmd / walk_cmd)
  * pysnmp 4.x-6.x -> API síncrona (pysnmp.hlapi: getCmd / nextCmd)
"""
import asyncio
import math
import random
import time

# ---------------------------------------------------------------- OIDs (MIB-II / HOST-RESOURCES)
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"                 # SNMPv2-MIB::sysUpTime
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"                 # IF-MIB::ifDescr
OID_IF_IN_32 = "1.3.6.1.2.1.2.2.1.10"                # IF-MIB::ifInOctets   (Counter32)
OID_IF_OUT_32 = "1.3.6.1.2.1.2.2.1.16"               # IF-MIB::ifOutOctets  (Counter32)
OID_IF_IN_64 = "1.3.6.1.2.1.31.1.1.1.6"              # IF-MIB::ifHCInOctets (Counter64)
OID_IF_OUT_64 = "1.3.6.1.2.1.31.1.1.1.10"            # IF-MIB::ifHCOutOctets(Counter64)
OID_CPU_LOAD = "1.3.6.1.2.1.25.3.3.1.2"              # HOST-RESOURCES-MIB::hrProcessorLoad


class SnmpError(Exception):
    """Falha de consulta (timeout, autenticação, rede...)."""


class OidUnsupported(SnmpError):
    """OID inexistente no equipamento (noSuchObject/noSuchInstance)."""


# ---------------------------------------------------------------- pysnmp real
try:  # pysnmp 7.x
    from pysnmp.hlapi.v3arch.asyncio import (
        SnmpEngine, CommunityData, UsmUserData, UdpTransportTarget, ContextData,
        ObjectType, ObjectIdentity, get_cmd, walk_cmd,
        usmNoAuthProtocol, usmHMACMD5AuthProtocol, usmHMACSHAAuthProtocol,
        usmNoPrivProtocol, usmDESPrivProtocol, usmAesCfb128Protocol,
    )
    _MODE = "async"
except ImportError:
    try:  # pysnmp 4.x - 6.x
        from pysnmp.hlapi import (
            SnmpEngine, CommunityData, UsmUserData, UdpTransportTarget, ContextData,
            ObjectType, ObjectIdentity, getCmd, nextCmd,
            usmNoAuthProtocol, usmHMACMD5AuthProtocol, usmHMACSHAAuthProtocol,
            usmNoPrivProtocol, usmDESPrivProtocol, usmAesCfb128Protocol,
        )
        _MODE = "sync"
    except ImportError:
        _MODE = None

_AUTH = {"NONE": "usmNoAuthProtocol", "MD5": "usmHMACMD5AuthProtocol",
         "SHA": "usmHMACSHAAuthProtocol"}
_PRIV = {"NONE": "usmNoPrivProtocol", "DES": "usmDESPrivProtocol",
         "AES": "usmAesCfb128Protocol"}


def pysnmp_available():
    return _MODE is not None


def _is_missing(value):
    return type(value).__name__ in ("NoSuchObject", "NoSuchInstance", "EndOfMibView")


class SnmpClient:
    def __init__(self, router_cfg):
        if _MODE is None:
            raise SnmpError("Biblioteca pysnmp não instalada (pip install pysnmp).")
        self.cfg = router_cfg
        self.engine = SnmpEngine()
        self._loop = asyncio.new_event_loop() if _MODE == "async" else None
        self._target = None
        self._auth = self._build_auth()

    # ------------------------------------------------------------ montagem
    def _build_auth(self):
        c = self.cfg
        if c["version"] == "3":
            v3 = c["v3"]
            g = globals()
            auth_p = g[_AUTH.get(v3["auth_protocol"].upper(), "usmNoAuthProtocol")]
            priv_p = g[_PRIV.get(v3["priv_protocol"].upper(), "usmNoPrivProtocol")]
            kwargs = {}
            if v3["auth_key"]:
                kwargs.update(authKey=v3["auth_key"], authProtocol=auth_p)
            if v3["priv_key"]:
                kwargs.update(privKey=v3["priv_key"], privProtocol=priv_p)
            return UsmUserData(v3["user"], **kwargs)
        return CommunityData(c["community"], mpModel=1)  # mpModel=1 -> v2c

    def _get_target(self):
        if self._target is None:
            addr = (self.cfg["host"], int(self.cfg["port"]))
            kw = dict(timeout=float(self.cfg["timeout"]), retries=int(self.cfg["retries"]))
            if _MODE == "async":
                self._target = self._loop.run_until_complete(
                    UdpTransportTarget.create(addr, **kw))
            else:
                self._target = UdpTransportTarget(addr, **kw)
        return self._target

    @staticmethod
    def _check(err_ind, err_stat, err_idx, var_binds):
        if err_ind:
            raise SnmpError(f"Falha SNMP: {err_ind}")
        if err_stat:
            where = var_binds[int(err_idx) - 1][0] if err_idx and var_binds else "?"
            raise SnmpError(f"Erro SNMP: {err_stat.prettyPrint()} em {where}")

    # ------------------------------------------------------------ operações
    def get(self, oids):
        """GET de vários OIDs. Retorna {oid: valor}. Levanta SnmpError/OidUnsupported."""
        try:
            target = self._get_target()
            objs = [ObjectType(ObjectIdentity(o)) for o in oids]
            if _MODE == "async":
                res = self._loop.run_until_complete(
                    get_cmd(self.engine, self._auth, target, ContextData(), *objs))
            else:
                res = next(getCmd(self.engine, self._auth, target, ContextData(), *objs))
            err_ind, err_stat, err_idx, var_binds = res
            self._check(err_ind, err_stat, err_idx, var_binds)
        except SnmpError:
            raise
        except Exception as exc:  # erros de transporte/DNS/etc.
            raise SnmpError(f"Falha de comunicação SNMP: {exc}") from exc
        out = {}
        for name, value in var_binds:
            if _is_missing(value):
                raise OidUnsupported(f"OID não suportado pelo equipamento: {name}")
            out[str(name)] = value
        return out

    def get_int(self, oid):
        v = self.get([oid])
        return int(next(iter(v.values())))

    def walk(self, base_oid):
        """Retorna lista [(oid, valor)] abaixo de base_oid."""
        results = []
        try:
            target = self._get_target()
            obj = ObjectType(ObjectIdentity(base_oid))
            if _MODE == "async":
                async def _run():
                    async for err_ind, err_stat, err_idx, vbs in walk_cmd(
                            self.engine, self._auth, target, ContextData(), obj,
                            lexicographicMode=False):
                        self._check(err_ind, err_stat, err_idx, vbs)
                        for n, v in vbs:
                            if not _is_missing(v):
                                results.append((str(n), v))
                self._loop.run_until_complete(_run())
            else:
                for err_ind, err_stat, err_idx, vbs in nextCmd(
                        self.engine, self._auth, target, ContextData(), obj,
                        lexicographicMode=False):
                    self._check(err_ind, err_stat, err_idx, vbs)
                    for n, v in vbs:
                        results.append((str(n), v))
        except SnmpError:
            raise
        except Exception as exc:
            raise SnmpError(f"Falha de comunicação SNMP: {exc}") from exc
        if not results:
            raise OidUnsupported(f"OID não suportado pelo equipamento: {base_oid}")
        return results

    # ------------------------------------------------------------ alto nível
    def list_interfaces(self):
        """[(ifIndex, ifDescr)] via ifDescr."""
        out = []
        for oid, val in self.walk(OID_IF_DESCR):
            out.append((int(oid.rsplit(".", 1)[1]), str(val)))
        return sorted(out)

    def read_cpu(self):
        """Média do hrProcessorLoad de todos os núcleos (%)."""
        vals = [int(v) for _, v in self.walk(OID_CPU_LOAD)]
        return sum(vals) / len(vals)

    def close(self):
        try:
            if _MODE == "async" and self._loop:
                self._loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------- cliente simulado
class MockSnmpClient:
    """Simula um roteador (contadores 64 bits, CPU variável) para demonstração/testes."""

    def __init__(self, router_cfg=None):
        self._t0 = time.monotonic()
        self._in = 0
        self._out = 0
        self._last = self._t0
        self.fail = False         # use para simular perda de conectividade

    def _advance(self):
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        wave = 1 + 0.8 * math.sin((now - self._t0) / 15.0)
        self._in += int(dt * (400_000 * wave + random.randint(0, 60_000)))   # bytes
        self._out += int(dt * (150_000 * wave + random.randint(0, 40_000)))

    def get(self, oids):
        if self.fail:
            raise SnmpError("Timeout: sem resposta do equipamento (simulado)")
        self._advance()
        up = int((time.monotonic() - self._t0) * 100)
        table = {OID_SYS_UPTIME: up}
        for o in oids:
            if o.startswith(OID_IF_IN_64):
                table[o] = self._in
            elif o.startswith(OID_IF_OUT_64):
                table[o] = self._out
            elif o.startswith(OID_IF_IN_32) or o.startswith(OID_IF_OUT_32):
                raise OidUnsupported(f"OID não suportado (simulado): {o}")
        return {o: table[o] for o in oids if o in table}

    def get_int(self, oid):
        return int(next(iter(self.get([oid]).values())))

    def list_interfaces(self):
        if self.fail:
            raise SnmpError("Timeout: sem resposta do equipamento (simulado)")
        return [(1, "ether1"), (2, "ether2"), (3, "lo")]

    def read_cpu(self):
        if self.fail:
            raise SnmpError("Timeout: sem resposta do equipamento (simulado)")
        base = 35 + 30 * math.sin((time.monotonic() - self._t0) / 20.0)
        return max(0.0, min(100.0, base + random.uniform(-5, 5)))

    def close(self):
        pass
