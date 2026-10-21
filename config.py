"""Configuração persistente (config.json) e validação."""
import copy
import json
import os
import re

CONFIG_FILE = os.environ.get("MONITOR_CONFIG", "config.json")

DEFAULTS = {
    "router": {
        "host": "192.168.56.10",
        "port": 161,
        "version": "2c",          # "2c" ou "3"
        "community": "public",    # somente v2c
        "timeout": 2,
        "retries": 1,
        "v3": {
            "user": "",
            "auth_protocol": "SHA",   # NONE | MD5 | SHA
            "auth_key": "",
            "priv_protocol": "AES",   # NONE | DES | AES
            "priv_key": "",
        },
    },
    "interface": {"index": 1, "name": ""},
    "collection": {"interval": 5},
    "alerts": {
        "popup": True,
        "email": False,
        "email_to": "",
        "traffic_in_bps": 5_000_000,
        "traffic_out_bps": 5_000_000,
        "cpu_percent": 80,
        "cooldown_seconds": 300,
    },
    "smtp": {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "",
        "password": "",      # prefira a variável de ambiente SMTP_PASSWORD
        "use_tls": True,
        "sender": "",
    },
    "storage": {"db_path": "monitor.db"},
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path=None):
    path = path or CONFIG_FILE
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
    cfg = _merge(DEFAULTS, data)
    env_pw = os.environ.get("SMTP_PASSWORD")
    if env_pw:
        cfg["smtp"]["password"] = env_pw
    return cfg


def save(cfg, path=None):
    path = path or CONFIG_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def validate(cfg):
    """Retorna lista de mensagens de erro (vazia se válido)."""
    errs = []
    try:
        if float(cfg["collection"]["interval"]) <= 0:
            errs.append("Intervalo de coleta deve ser maior que zero.")
    except (TypeError, ValueError):
        errs.append("Intervalo de coleta inválido.")

    a = cfg["alerts"]
    if not (a["popup"] or a["email"]):
        errs.append("Selecione ao menos um tipo de alerta (pop-up e/ou e-mail).")
    if a["email"] or a["email_to"]:
        if not EMAIL_RE.match(str(a["email_to"]).strip()):
            errs.append("Endereço de e-mail inválido.")
    for key, label in (("traffic_in_bps", "Threshold de tráfego IN"),
                       ("traffic_out_bps", "Threshold de tráfego OUT")):
        try:
            if float(a[key]) <= 0:
                errs.append(f"{label} deve ser maior que zero.")
        except (TypeError, ValueError):
            errs.append(f"{label} inválido.")
    try:
        if not 0 < float(a["cpu_percent"]) <= 100:
            errs.append("Threshold de CPU deve estar entre 0 e 100.")
    except (TypeError, ValueError):
        errs.append("Threshold de CPU inválido.")
    try:
        if float(a["cooldown_seconds"]) < 0:
            errs.append("Período de espera dos alertas não pode ser negativo.")
    except (TypeError, ValueError):
        errs.append("Período de espera inválido.")

    r = cfg["router"]
    if not str(r["host"]).strip():
        errs.append("Endereço do roteador é obrigatório.")
    if r["version"] not in ("2c", "3"):
        errs.append("Versão SNMP deve ser 2c ou 3.")
    if r["version"] == "2c" and not str(r["community"]).strip():
        errs.append("Community SNMP é obrigatória no v2c.")
    if r["version"] == "3" and not str(r["v3"]["user"]).strip():
        errs.append("Usuário SNMPv3 é obrigatório.")
    try:
        if int(cfg["interface"]["index"]) <= 0:
            errs.append("Índice da interface deve ser positivo.")
    except (TypeError, ValueError):
        errs.append("Índice da interface inválido.")
    return errs
