"""Coleta periódica em thread única (impede coletas duplicadas) e cálculo de bps."""
import threading
import time

import snmp_client as sc

MAX_PLAUSIBLE_BPS = 400e9   # acima disso (400 Gbps) a taxa é tratada como inválida


def compute_rate(prev_bytes, curr_bytes, dt, bits64=True, max_bps=MAX_PLAUSIBLE_BPS):
    """
    bps = (Δbytes * 8) / Δt, com Δt = intervalo REAL entre leituras.
    Retorna None se a taxa não for confiável:
      * dt <= 0;
      * contador diminuiu: em Counter32 trata-se como wrap (soma 2^32); em Counter64
        considera reinicialização e descarta a amostra;
      * resultado absurdo (> max_bps).
    """
    if prev_bytes is None or dt is None or dt <= 0:
        return None
    delta = curr_bytes - prev_bytes
    if delta < 0:
        if bits64:
            return None            # reset de contador / reboot
        delta += 2 ** 32           # wrap de Counter32
    bps = delta * 8 / dt
    if bps < 0 or bps > max_bps:
        return None
    return bps


class Collector:
    def __init__(self, config, storage, alerts, events, mock=False):
        self.config = config
        self.storage = storage
        self.alerts = alerts
        self.events = events
        self.mock = mock
        self._thread = None
        self._running = threading.Event()   # set = coletando
        self._stop = threading.Event()
        self._cfg_version = 0
        self._seen_version = -1
        self._client = None
        self._prev = None                   # (monotonic, in_bytes, out_bytes, uptime)
        self._use_hc = True
        self._last_error = None

    # ------------------------------------------------------------ controle
    @property
    def running(self):
        return self._running.is_set()

    def start(self):
        """Inicia ou retoma. Chamadas repetidas não criam coletas extras."""
        if self._running.is_set():
            return
        self._prev = None               # nova linha de base após iniciar/retomar
        self._running.set()
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="collector", daemon=True)
            self._thread.start()

    def pause(self):
        self._running.clear()

    def stop(self):
        self._running.clear()
        self._stop.set()

    def config_changed(self):
        """Chamar após salvar configurações: recria o cliente SNMP e a linha de base."""
        self._cfg_version += 1

    # ------------------------------------------------------------ loop
    def _loop(self):
        while not self._stop.is_set():
            if not self._running.wait(0.3):
                continue
            t0 = time.monotonic()
            try:
                self._cycle()
            except Exception as exc:   # rede de segurança: a thread nunca morre
                self._report_error(f"Erro inesperado na coleta: {exc}")
            interval = max(0.2, float(self.config["collection"]["interval"]))
            remaining = interval - (time.monotonic() - t0)
            end = time.monotonic() + max(0.0, remaining)
            while time.monotonic() < end and self._running.is_set() and not self._stop.is_set():
                time.sleep(min(0.2, end - time.monotonic()))

    def _ensure_client(self):
        if self._client is not None and self._seen_version == self._cfg_version:
            return
        if self._client is not None:
            self._client.close()
        self._client = (sc.MockSnmpClient() if self.mock
                        else sc.SnmpClient(self.config["router"]))
        self._seen_version = self._cfg_version
        self._prev = None               # mudou equipamento/interface: descarta baseline
        self._use_hc = True

    def _report_error(self, msg):
        if msg != self._last_error:     # evita inundar o log com a mesma falha
            self._last_error = msg
            self.storage.add_error(msg)
            self.events.put(("error", msg))

    def _cycle(self):
        try:
            self._ensure_client()
        except sc.SnmpError as exc:
            self._report_error(str(exc))
            return
        idx = int(self.config["interface"]["index"])
        client = self._client
        in_b = out_b = uptime = cpu = None
        problems = []

        # ---- tráfego (preferência: contadores 64 bits; fallback 32 bits)
        try:
            in_b, out_b, uptime = self._read_counters(client, idx)
        except sc.SnmpError as exc:
            problems.append(f"Tráfego: {exc}")
            self._prev = None
        # ---- CPU
        try:
            cpu = client.read_cpu()
        except sc.SnmpError as exc:
            problems.append(f"CPU: {exc}")

        in_bps = out_bps = None
        if in_b is not None:
            now_m = time.monotonic()
            if self._prev is not None:
                p_t, p_in, p_out, p_up = self._prev
                if uptime is not None and p_up is not None and uptime < p_up:
                    self.events.put(("info", "Reinício do equipamento detectado; baseline reiniciado."))
                else:
                    dt = now_m - p_t
                    in_bps = compute_rate(p_in, in_b, dt, self._use_hc)
                    out_bps = compute_rate(p_out, out_b, dt, self._use_hc)
            self._prev = (now_m, in_b, out_b, uptime)

        if problems:
            self._report_error(" | ".join(problems))
        elif self._last_error is not None:
            self._last_error = None
            self.events.put(("info", "Comunicação SNMP restabelecida."))

        if in_b is None and cpu is None:
            return                      # nada coletado neste ciclo
        sample = {"ts": time.time(), "in_bps": in_bps, "out_bps": out_bps, "cpu": cpu}
        self.storage.add_sample(**sample)
        self.events.put(("sample", sample))
        self.alerts.evaluate(sample)

    def _read_counters(self, client, idx):
        if self._use_hc:
            try:
                r = client.get([f"{sc.OID_IF_IN_64}.{idx}", f"{sc.OID_IF_OUT_64}.{idx}",
                                sc.OID_SYS_UPTIME])
                return self._unpack(r, sc.OID_IF_IN_64, sc.OID_IF_OUT_64, idx)
            except sc.OidUnsupported:
                self._use_hc = False
                self._prev = None
                self.events.put(("info", "ifHCInOctets não suportado; usando contadores de 32 bits."))
        r = client.get([f"{sc.OID_IF_IN_32}.{idx}", f"{sc.OID_IF_OUT_32}.{idx}",
                        sc.OID_SYS_UPTIME])
        return self._unpack(r, sc.OID_IF_IN_32, sc.OID_IF_OUT_32, idx)

    @staticmethod
    def _unpack(result, in_base, out_base, idx):
        vals = {k.lstrip("."): v for k, v in result.items()}
        try:
            in_b = int(vals[f"{in_base}.{idx}"])
            out_b = int(vals[f"{out_base}.{idx}"])
        except KeyError as exc:
            raise sc.SnmpError(f"Resposta SNMP incompleta: {exc}") from exc
        up = vals.get(sc.OID_SYS_UPTIME)
        return in_b, out_b, (int(up) if up is not None else None)
