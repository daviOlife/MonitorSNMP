"""Monitor SNMP de roteador - interface gráfica (tkinter + matplotlib).

Uso:
    python app.py            # roteador real (config.json)
    python app.py --mock     # roteador simulado (demonstração/testes)
"""
import argparse
import queue
import threading
import tkinter as tk
from collections import deque
from datetime import datetime, timedelta
from tkinter import messagebox, ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import config as cfgmod
import snmp_client as sc
from alerts import AlertManager, send_email
from collector import Collector
from fmt import fmt_bps, fmt_ts, pick_scale
from storage import Storage

MAX_POINTS_LIVE = 300       # janela de pontos na tela em tempo real
MAX_POINTS_HIST = 5000      # acima disso o histórico é decimado só para exibição
DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M")


def parse_dt(text):
    for f in DT_FORMATS:
        try:
            return datetime.strptime(text.strip(), f)
        except ValueError:
            pass
    raise ValueError("Data/hora inválida. Use AAAA-MM-DD HH:MM:SS ou DD/MM/AAAA HH:MM.")


def style_axes(ax, title, ylabel):
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("Horário")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))


def plot_traffic(ax, times, ins, outs, title):
    ax.clear()
    vals = [v for v in list(ins) + list(outs) if v == v and v is not None]
    scale, unit = pick_scale(max(vals) if vals else 0)
    nan = float("nan")
    y_in = [(v / scale) if v is not None else nan for v in ins]
    y_out = [(v / scale) if v is not None else nan for v in outs]
    ax.plot(times, y_in, label="IN", color="tab:blue", linewidth=1.6)
    ax.plot(times, y_out, label="OUT", color="tab:orange", linewidth=1.6)
    style_axes(ax, title, f"Tráfego ({unit})")
    ax.legend(loc="upper left")


def plot_cpu(ax, times, cpus, title):
    ax.clear()
    nan = float("nan")
    ax.plot(times, [c if c is not None else nan for c in cpus],
            label="CPU", color="tab:green", linewidth=1.6)
    style_axes(ax, title, "CPU (%)")
    ax.set_ylim(0, 100)
    ax.legend(loc="upper left")


class App(tk.Tk):
    def __init__(self, mock=False):
        super().__init__()
        self.title("Monitor SNMP de Roteador" + (" [SIMULADO]" if mock else ""))
        self.geometry("1100x760")
        self.mock = mock
        self.cfg = cfgmod.load()
        self.events = queue.Queue()
        self.storage = Storage(self.cfg["storage"]["db_path"])
        self.alerts = AlertManager(self.cfg, self.storage, self.events)
        self.collector = Collector(self.cfg, self.storage, self.alerts, self.events, mock=mock)

        self.t = deque(maxlen=MAX_POINTS_LIVE)
        self.v_in = deque(maxlen=MAX_POINTS_LIVE)
        self.v_out = deque(maxlen=MAX_POINTS_LIVE)
        self.v_cpu = deque(maxlen=MAX_POINTS_LIVE)

        if not mock and not sc.pysnmp_available():
            messagebox.showwarning(
                "pysnmp ausente",
                "A biblioteca pysnmp não está instalada.\n"
                "Instale com: pip install pysnmp\n(ou use --mock para simulação).")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.tab_live = ttk.Frame(nb)
        self.tab_hist = ttk.Frame(nb)
        self.tab_cfg = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_live, text="Tempo real")
        nb.add(self.tab_hist, text="Histórico")
        nb.add(self.tab_cfg, text="Configurações")
        nb.add(self.tab_log, text="Eventos")
        self._build_live()
        self._build_hist()
        self._build_cfg()
        self._build_log()
        nb.bind("<<NotebookTabChanged>>", lambda e: self._refresh_log())

        self.status = tk.StringVar(value="Parado.")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", side="bottom")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(500, self._poll_events)

    # ================================================================ TEMPO REAL
    def _build_live(self):
        bar = ttk.Frame(self.tab_live)
        bar.pack(fill="x", padx=8, pady=6)
        self.btn_start = ttk.Button(bar, text="▶ Iniciar", command=self.on_start)
        self.btn_pause = ttk.Button(bar, text="⏸ Pausar", command=self.on_pause, state="disabled")
        self.btn_reset = ttk.Button(bar, text="⟲ Resetar", command=self.on_reset)
        for b in (self.btn_start, self.btn_pause, self.btn_reset):
            b.pack(side="left", padx=4)
        self.lbl_now = tk.StringVar(value="IN: —   OUT: —   CPU: —")
        ttk.Label(bar, textvariable=self.lbl_now, font=("TkDefaultFont", 10, "bold")).pack(
            side="right", padx=8)

        self.fig_live = Figure(figsize=(10, 6), dpi=100)
        self.ax_tr = self.fig_live.add_subplot(211)
        self.ax_cpu = self.fig_live.add_subplot(212)
        self.fig_live.subplots_adjust(hspace=0.55, left=0.09, right=0.97, top=0.93, bottom=0.1)
        self.canvas_live = FigureCanvasTkAgg(self.fig_live, master=self.tab_live)
        self.canvas_live.get_tk_widget().pack(fill="both", expand=True)
        self._redraw_live()

    def _redraw_live(self):
        times = list(self.t)
        plot_traffic(self.ax_tr, times, list(self.v_in), list(self.v_out),
                     "Tráfego da interface (tempo real)")
        plot_cpu(self.ax_cpu, times, list(self.v_cpu), "Utilização da CPU (tempo real)")
        if not times:
            for ax in (self.ax_tr, self.ax_cpu):
                ax.text(0.5, 0.5, "Aguardando coleta (clique em Iniciar)",
                        transform=ax.transAxes, ha="center", va="center", alpha=0.6)
        self.canvas_live.draw_idle()

    def on_start(self):
        errs = cfgmod.validate(self.cfg)
        if errs:
            messagebox.showerror("Configuração inválida", "\n".join(errs))
            return
        self.collector.start()          # idempotente: não cria coleta duplicada
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal")
        self.status.set("Coletando...")

    def on_pause(self):
        self.collector.pause()
        self.btn_start.config(state="normal", text="▶ Retomar")
        self.btn_pause.config(state="disabled")
        self.status.set("Pausado (dados preservados).")

    def on_reset(self):
        """Limpa somente a visualização da sessão; o histórico em disco NÃO é apagado."""
        self.t.clear(); self.v_in.clear(); self.v_out.clear(); self.v_cpu.clear()
        self.alerts.reset_state()
        self.lbl_now.set("IN: —   OUT: —   CPU: —")
        self._redraw_live()
        self.status.set("Visualização resetada (histórico mantido no banco).")

    # ================================================================ HISTÓRICO
    def _build_hist(self):
        bar = ttk.Frame(self.tab_hist)
        bar.pack(fill="x", padx=8, pady=6)
        now = datetime.now()
        self.h_start = tk.StringVar(value=(now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
        self.h_end = tk.StringVar(value=now.strftime("%Y-%m-%d %H:%M:%S"))
        ttk.Label(bar, text="Início:").pack(side="left")
        ttk.Entry(bar, textvariable=self.h_start, width=20).pack(side="left", padx=4)
        ttk.Label(bar, text="Fim:").pack(side="left")
        ttk.Entry(bar, textvariable=self.h_end, width=20).pack(side="left", padx=4)
        ttk.Button(bar, text="Consultar", command=self.on_query).pack(side="left", padx=6)
        ttk.Button(bar, text="Última hora", command=lambda: self._quick(timedelta(hours=1))).pack(side="left")
        ttk.Button(bar, text="Hoje", command=self._today).pack(side="left", padx=4)
        self.h_info = tk.StringVar(value="Informe o período (AAAA-MM-DD HH:MM:SS) e clique em Consultar.")
        ttk.Label(self.tab_hist, textvariable=self.h_info).pack(fill="x", padx=8)

        self.fig_hist = Figure(figsize=(10, 6), dpi=100)
        self.hx_tr = self.fig_hist.add_subplot(211)
        self.hx_cpu = self.fig_hist.add_subplot(212)
        self.fig_hist.subplots_adjust(hspace=0.6, left=0.09, right=0.97, top=0.93, bottom=0.12)
        self.canvas_hist = FigureCanvasTkAgg(self.fig_hist, master=self.tab_hist)
        self.canvas_hist.get_tk_widget().pack(fill="both", expand=True)

    def _quick(self, delta):
        now = datetime.now()
        self.h_start.set((now - delta).strftime("%Y-%m-%d %H:%M:%S"))
        self.h_end.set(now.strftime("%Y-%m-%d %H:%M:%S"))
        self.on_query()

    def _today(self):
        now = datetime.now()
        self.h_start.set(now.strftime("%Y-%m-%d 00:00:00"))
        self.h_end.set(now.strftime("%Y-%m-%d %H:%M:%S"))
        self.on_query()

    def on_query(self):
        try:
            d0, d1 = parse_dt(self.h_start.get()), parse_dt(self.h_end.get())
        except ValueError as exc:
            messagebox.showerror("Período inválido", str(exc)); return
        if d1 <= d0:
            messagebox.showerror("Período inválido", "O fim deve ser posterior ao início."); return
        rows = self.storage.query(d0.timestamp(), d1.timestamp())
        period = f"{d0:%d/%m/%Y %H:%M:%S} → {d1:%d/%m/%Y %H:%M:%S}"
        if not rows:
            self.h_info.set(f"Período: {period} | Nenhum dado encontrado neste período.")
            for ax, name in ((self.hx_tr, "Tráfego histórico"), (self.hx_cpu, "CPU histórica")):
                ax.clear(); style_axes(ax, name, "")
                ax.text(0.5, 0.5, "Sem dados no período selecionado",
                        transform=ax.transAxes, ha="center", va="center", alpha=0.7)
            self.canvas_hist.draw_idle(); return
        total = len(rows)
        if total > MAX_POINTS_HIST:
            rows = rows[:: -(-total // MAX_POINTS_HIST)]
        times = [datetime.fromtimestamp(r[0]) for r in rows]
        plot_traffic(self.hx_tr, times, [r[1] for r in rows], [r[2] for r in rows],
                     "Tráfego histórico IN/OUT")
        plot_cpu(self.hx_cpu, times, [r[3] for r in rows], "Utilização histórica da CPU")
        span = (d1 - d0).total_seconds()
        fmt = "%H:%M:%S" if span <= 86400 else "%d/%m %H:%M"
        for ax in (self.hx_tr, self.hx_cpu):
            ax.set_xlim(d0, d1)
            ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
        self.h_info.set(
            f"Período: {period} | {total} amostras"
            f"{' (exibindo '+str(len(rows))+', decimado)' if len(rows) != total else ''} | "
            f"primeira: {fmt_ts(rows[0][0])} | última: {fmt_ts(rows[-1][0])}")
        self.canvas_hist.draw_idle()

    # ================================================================ CONFIGURAÇÕES
    def _build_cfg(self):
        f = ttk.Frame(self.tab_cfg, padding=12)
        f.pack(fill="both", expand=True)
        c = self.cfg
        self.vars = {}

        def row(r, label, key, value, width=28, show=None):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
            v = tk.StringVar(value=str(value))
            ttk.Entry(f, textvariable=v, width=width, show=show).grid(row=r, column=1, sticky="w", pady=2)
            self.vars[key] = v

        ttk.Label(f, text="Coleta e alertas", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        row(1, "Intervalo de coleta (s):", "interval", c["collection"]["interval"])
        row(2, "Threshold tráfego IN (bps):", "t_in", c["alerts"]["traffic_in_bps"])
        row(3, "Threshold tráfego OUT (bps):", "t_out", c["alerts"]["traffic_out_bps"])
        row(4, "Threshold CPU (%):", "t_cpu", c["alerts"]["cpu_percent"])
        row(5, "Espera entre alertas repetidos (s):", "cooldown", c["alerts"]["cooldown_seconds"])
        self.v_popup = tk.BooleanVar(value=c["alerts"]["popup"])
        self.v_email = tk.BooleanVar(value=c["alerts"]["email"])
        ttk.Checkbutton(f, text="Alerta por pop-up", variable=self.v_popup).grid(row=6, column=0, sticky="w")
        ttk.Checkbutton(f, text="Alerta por e-mail", variable=self.v_email).grid(row=6, column=1, sticky="w")
        row(7, "E-mail de destino:", "email_to", c["alerts"]["email_to"])
        row(8, "SMTP servidor:", "smtp_host", c["smtp"]["host"])
        row(9, "SMTP porta:", "smtp_port", c["smtp"]["port"], width=8)
        row(10, "SMTP usuário:", "smtp_user", c["smtp"]["user"])
        row(11, "SMTP senha:", "smtp_pw", c["smtp"]["password"], show="•")
        row(12, "Remetente:", "smtp_sender", c["smtp"]["sender"])

        ttk.Label(f, text="Equipamento / SNMP (avançado)", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=2, columnspan=2, sticky="w", padx=(30, 0))

        def row2(r, label, key, value, width=24, show=None):
            ttk.Label(f, text=label).grid(row=r, column=2, sticky="w", padx=(30, 4), pady=2)
            v = tk.StringVar(value=str(value))
            ttk.Entry(f, textvariable=v, width=width, show=show).grid(row=r, column=3, sticky="w", pady=2)
            self.vars[key] = v

        r = c["router"]
        row2(1, "IP do roteador:", "host", r["host"])
        row2(2, "Porta:", "port", r["port"], width=8)
        ttk.Label(f, text="Versão SNMP:").grid(row=3, column=2, sticky="w", padx=(30, 4))
        self.v_ver = tk.StringVar(value=r["version"])
        ttk.Combobox(f, textvariable=self.v_ver, values=("2c", "3"), width=6, state="readonly").grid(
            row=3, column=3, sticky="w")
        row2(4, "Community (v2c):", "community", r["community"], show="•")
        row2(5, "Usuário (v3):", "v3_user", r["v3"]["user"])
        row2(6, "Auth (NONE/MD5/SHA):", "v3_authp", r["v3"]["auth_protocol"], width=10)
        row2(7, "Chave auth (v3):", "v3_authk", r["v3"]["auth_key"], show="•")
        row2(8, "Priv (NONE/DES/AES):", "v3_privp", r["v3"]["priv_protocol"], width=10)
        row2(9, "Chave priv (v3):", "v3_privk", r["v3"]["priv_key"], show="•")
        row2(10, "Timeout (s):", "timeout", r["timeout"], width=8)
        row2(11, "Tentativas:", "retries", r["retries"], width=8)
        ttk.Label(f, text="Interface:").grid(row=12, column=2, sticky="w", padx=(30, 4))
        self.v_if = tk.StringVar(value=self._if_label(c["interface"]["index"], c["interface"]["name"]))
        self.cb_if = ttk.Combobox(f, textvariable=self.v_if, width=26)
        self.cb_if.grid(row=12, column=3, sticky="w")
        ttk.Button(f, text="Buscar interfaces", command=self.on_list_if).grid(row=13, column=3, sticky="w", pady=2)

        btns = ttk.Frame(f)
        btns.grid(row=14, column=0, columnspan=4, sticky="w", pady=14)
        ttk.Button(btns, text="Salvar", command=self.on_save).pack(side="left", padx=4)
        ttk.Button(btns, text="Testar SNMP", command=self.on_test_snmp).pack(side="left", padx=4)
        ttk.Button(btns, text="Testar e-mail", command=self.on_test_email).pack(side="left", padx=4)

    @staticmethod
    def _if_label(idx, name):
        return f"{idx} - {name}" if name else str(idx)

    def _collect_cfg(self):
        """Lê o formulário e devolve (novo_cfg, erros)."""
        import copy
        n = copy.deepcopy(self.cfg)
        v = {k: x.get().strip() for k, x in self.vars.items()}
        errs = []

        def num(key, label, cast=float):
            try:
                return cast(v[key])
            except ValueError:
                errs.append(f"{label}: valor numérico inválido.")
                return 0
        n["collection"]["interval"] = num("interval", "Intervalo")
        n["alerts"].update(
            popup=self.v_popup.get(), email=self.v_email.get(), email_to=v["email_to"],
            traffic_in_bps=num("t_in", "Threshold IN"), traffic_out_bps=num("t_out", "Threshold OUT"),
            cpu_percent=num("t_cpu", "Threshold CPU"), cooldown_seconds=num("cooldown", "Espera"))
        n["smtp"].update(host=v["smtp_host"], port=num("smtp_port", "Porta SMTP", int),
                         user=v["smtp_user"], password=v["smtp_pw"], sender=v["smtp_sender"])
        n["router"].update(host=v["host"], port=num("port", "Porta", int), version=self.v_ver.get(),
                           community=v["community"], timeout=num("timeout", "Timeout"),
                           retries=num("retries", "Tentativas", int))
        n["router"]["v3"].update(user=v["v3_user"], auth_protocol=v["v3_authp"].upper(),
                                 auth_key=v["v3_authk"], priv_protocol=v["v3_privp"].upper(),
                                 priv_key=v["v3_privk"])
        label = self.v_if.get().strip()
        head = label.split(" - ", 1)
        try:
            n["interface"]["index"] = int(head[0])
            n["interface"]["name"] = head[1] if len(head) > 1 else ""
        except ValueError:
            errs.append("Interface: informe o índice (ex.: 1 ou '1 - ether1').")
        return n, errs

    def on_save(self):
        new, errs = self._collect_cfg()
        errs += cfgmod.validate(new) if not errs else []
        if errs:
            messagebox.showerror("Não foi possível salvar", "\n".join(errs)); return
        self.cfg.clear(); self.cfg.update(new)    # objeto compartilhado com collector/alerts
        cfgmod.save(self.cfg)
        self.collector.config_changed()
        self.status.set("Configurações salvas.")
        messagebox.showinfo("Configurações", "Configurações salvas com sucesso.")

    def _bg(self, fn, done):
        def run():
            try:
                res, err = fn(), None
            except Exception as exc:
                res, err = None, exc
            self.after(0, lambda: done(res, err))
        threading.Thread(target=run, daemon=True).start()

    def _client_from_form(self):
        new, errs = self._collect_cfg()
        if errs:
            raise ValueError("\n".join(errs))
        return sc.MockSnmpClient() if self.mock else sc.SnmpClient(new["router"]), new

    def on_list_if(self):
        def work():
            client, _ = self._client_from_form()
            try:
                return client.list_interfaces()
            finally:
                client.close()

        def done(res, err):
            if err:
                messagebox.showerror("Falha SNMP", str(err)); return
            self.cb_if["values"] = [self._if_label(i, n) for i, n in res]
            self.status.set(f"{len(res)} interfaces encontradas.")
        self.status.set("Consultando interfaces...")
        self._bg(work, done)

    def on_test_snmp(self):
        def work():
            client, new = self._client_from_form()
            try:
                idx = new["interface"]["index"]
                cpu = client.read_cpu()
                try:
                    client.get_int(f"{sc.OID_IF_IN_64}.{idx}")
                    ctr = "64 bits (ifHCInOctets)"
                except sc.OidUnsupported:
                    client.get_int(f"{sc.OID_IF_IN_32}.{idx}")
                    ctr = "32 bits (ifInOctets)"
                return f"Conexão OK.\nCPU atual: {cpu:.1f}%\nContadores: {ctr}"
            finally:
                client.close()

        def done(res, err):
            (messagebox.showerror("Falha SNMP", str(err)) if err
             else messagebox.showinfo("Teste SNMP", res))
        self._bg(work, done)

    def on_test_email(self):
        new, errs = self._collect_cfg()
        if errs or not cfgmod.EMAIL_RE.match(new["alerts"]["email_to"]):
            messagebox.showerror("E-mail", "\n".join(errs) or "Endereço de e-mail inválido."); return

        def work():
            send_email(new["smtp"], new["alerts"]["email_to"], "[Monitor SNMP] Teste",
                       "Mensagem de teste do Monitor SNMP.")

        def done(res, err):
            (messagebox.showerror("Falha no e-mail", str(err)) if err
             else messagebox.showinfo("E-mail", "E-mail de teste enviado."))
        self._bg(work, done)

    # ================================================================ EVENTOS
    def _build_log(self):
        cols = ("hora", "tipo", "detalhe")
        self.tree = ttk.Treeview(self.tab_log, columns=cols, show="headings")
        for c, w in zip(cols, (160, 70, 800)):
            self.tree.heading(c, text=c.capitalize()); self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6, pady=6)

    def _refresh_log(self):
        self.tree.delete(*self.tree.get_children())
        for ts, kind, text in self.storage.recent_events():
            self.tree.insert("", "end", values=(fmt_ts(ts), kind, text))

    # ================================================================ LOOP DA GUI
    def _poll_events(self):
        changed = False
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "sample":
                    self.t.append(datetime.fromtimestamp(data["ts"]))
                    self.v_in.append(data["in_bps"]); self.v_out.append(data["out_bps"])
                    self.v_cpu.append(data["cpu"])
                    cpu = f"{data['cpu']:.1f}%" if data["cpu"] is not None else "—"
                    self.lbl_now.set(f"IN: {fmt_bps(data['in_bps'])}   OUT: {fmt_bps(data['out_bps'])}   CPU: {cpu}")
                    changed = True
                elif kind == "popup":
                    self._popup(data["title"], data["body"])
                elif kind == "error":
                    self.status.set("⚠ " + data)
                elif kind == "info":
                    self.status.set(data)
        except queue.Empty:
            pass
        if changed:
            self._redraw_live()
        self.after(500, self._poll_events)

    def _popup(self, title, body):
        w = tk.Toplevel(self)
        w.title(title)
        w.attributes("-topmost", True)
        ttk.Label(w, text=title, font=("TkDefaultFont", 11, "bold"), foreground="#b00020").pack(padx=16, pady=(14, 4))
        ttk.Label(w, text=body, justify="left").pack(padx=16, pady=4)
        ttk.Button(w, text="OK", command=w.destroy).pack(pady=12)

    def _on_close(self):
        self.collector.stop()
        self.storage.close()
        self.destroy()


def main():
    ap = argparse.ArgumentParser(description="Monitor SNMP de roteador")
    ap.add_argument("--mock", action="store_true", help="usa roteador simulado (sem SNMP real)")
    args = ap.parse_args()
    App(mock=args.mock).mainloop()


if __name__ == "__main__":
    main()
