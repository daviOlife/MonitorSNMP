"""Avaliação de thresholds, pop-up (via fila) e e-mail (SMTP)."""
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage

from fmt import fmt_bps, fmt_ts

METRICS = {
    # chave           (rótulo,          campo da amostra, chave do limite,    é tráfego)
    "traffic_in":  ("Tráfego IN",  "in_bps",  "traffic_in_bps",  True),
    "traffic_out": ("Tráfego OUT", "out_bps", "traffic_out_bps", True),
    "cpu":         ("CPU",         "cpu",     "cpu_percent",     False),
}


def _fmt_value(is_traffic, v):
    return fmt_bps(v) if is_traffic else f"{v:.1f} %"


def build_message(metric_key, value, limit, ts):
    label, _, _, is_traffic = METRICS[metric_key]
    return (f"Métrica: {label}\n"
            f"Valor observado: {_fmt_value(is_traffic, value)}\n"
            f"Limite configurado: {_fmt_value(is_traffic, limit)}\n"
            f"Horário: {fmt_ts(ts)}")


def send_email(smtp_cfg, to_addr, subject, body):
    """Envia e-mail; levanta exceção em caso de falha (tratada por quem chama)."""
    if not smtp_cfg["host"] or not smtp_cfg["sender"] and not smtp_cfg["user"]:
        raise RuntimeError("SMTP não configurado (servidor/remetente).")
    msg = EmailMessage()
    msg["From"] = smtp_cfg["sender"] or smtp_cfg["user"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    host, port = smtp_cfg["host"], int(smtp_cfg["port"])
    ctx = ssl.create_default_context()
    try:
        if port == 465:     # SSL implícito
            conn = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
        else:               # 587 (STARTTLS) ou 25
            conn = smtplib.SMTP(host, port, timeout=15)
        with conn as s:
            if port != 465 and smtp_cfg["use_tls"]:
                s.starttls(context=ctx)
            if smtp_cfg["user"]:
                s.login(smtp_cfg["user"], smtp_cfg["password"])
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError("Usuário ou senha recusados. No Gmail use uma SENHA DE APP "
                           f"(16 caracteres). Detalhe: {exc.smtp_code}") from exc
    except smtplib.SMTPServerDisconnected as exc:
        raise RuntimeError(f"O servidor {host}:{port} fechou a conexão. Verifique se a porta "
                           "não está bloqueada (firewall/antivírus/rede) ou tente a porta 465.") from exc
    except OSError as exc:
        raise RuntimeError(f"Não foi possível conectar em {host}:{port}: {exc}") from exc


class AlertManager:
    """
    Estratégia anti-repetição (por métrica, IN e OUT são independentes):
      * notifica na transição NORMAL -> EXCEDIDO;
      * enquanto permanecer excedido, só notifica de novo após `cooldown_seconds`;
      * ao voltar ao normal o estado é rearmado (próxima ultrapassagem alerta de imediato).
    """

    def __init__(self, config, storage, events):
        self.config = config
        self.storage = storage
        self.events = events          # queue.Queue consumida pela GUI
        self._state = {k: {"exceeded": False, "last": 0.0} for k in METRICS}

    def reset_state(self):
        for st in self._state.values():
            st.update(exceeded=False, last=0.0)

    def evaluate(self, sample, now=None):
        """Avalia uma amostra {ts,in_bps,out_bps,cpu}. Retorna lista de alertas disparados."""
        a = self.config["alerts"]
        now = now if now is not None else time.time()
        fired = []
        for key, (label, field, limit_key, is_traffic) in METRICS.items():
            value = sample.get(field)
            if value is None:
                continue
            limit = float(a[limit_key])
            st = self._state[key]
            if value > limit:
                due = (not st["exceeded"]) or (now - st["last"] >= float(a["cooldown_seconds"]))
                if due:
                    st["exceeded"] = True
                    st["last"] = now
                    fired.append(self._notify(key, value, limit, sample["ts"]))
            else:
                st["exceeded"] = False
        return fired

    def _notify(self, key, value, limit, ts):
        a = self.config["alerts"]
        label = METRICS[key][0]
        body = build_message(key, value, limit, ts)
        channels = []
        if a["popup"]:
            channels.append("popup")
            self.events.put(("popup", {"title": f"ALERTA: {label}", "body": body}))
        if a["email"]:
            channels.append("email")
            threading.Thread(target=self._email_worker,
                             args=(key, value, limit, ts, body), daemon=True).start()
        self.storage.add_alert(key, value, limit, "+".join(channels) or "nenhum", "disparado", ts)
        return {"metric": key, "value": value, "limit": limit, "channels": channels}

    def _email_worker(self, key, value, limit, ts, body):
        label = METRICS[key][0]
        try:
            send_email(self.config["smtp"], self.config["alerts"]["email_to"],
                       f"[Monitor SNMP] Alerta: {label}", body)
            self.events.put(("info", f"E-mail de alerta enviado ({label})."))
        except Exception as exc:  # nunca derruba a aplicação
            msg = f"Falha ao enviar e-mail de alerta ({label}): {exc}"
            self.storage.add_error(msg)
            self.events.put(("error", msg))
