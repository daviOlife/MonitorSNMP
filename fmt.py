"""Funções de formatação."""
from datetime import datetime

UNITS = [(1e9, "Gbps"), (1e6, "Mbps"), (1e3, "Kbps"), (1.0, "bps")]


def fmt_bps(v):
    if v is None:
        return "—"
    for scale, name in UNITS:
        if abs(v) >= scale:
            return f"{v / scale:.2f} {name}"
    return f"{v:.0f} bps"


def pick_scale(max_value):
    """Escolhe (fator, unidade) para o eixo Y dos gráficos de tráfego."""
    for scale, name in UNITS:
        if max_value >= scale:
            return scale, name
    return 1.0, "bps"


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M:%S")
