"""Testes da lógica (sem GUI e sem roteador): python -m unittest discover -s tests"""
import os
import queue
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config as cfgmod
from alerts import AlertManager
from collector import Collector, compute_rate
from storage import Storage


class RateTests(unittest.TestCase):
    def test_basico(self):
        self.assertEqual(compute_rate(1000, 2250, 2.0), 5000.0)   # 1250 B*8/2s

    def test_primeira_leitura(self):
        self.assertIsNone(compute_rate(None, 100, 5))

    def test_intervalo_invalido(self):
        self.assertIsNone(compute_rate(0, 100, 0))
        self.assertIsNone(compute_rate(0, 100, -1))

    def test_reset_contador64(self):
        self.assertIsNone(compute_rate(10_000, 50, 5, bits64=True))

    def test_wrap_contador32(self):
        # 2^32 - 1000 -> 3000 : delta = 4000 bytes
        self.assertEqual(compute_rate(2 ** 32 - 1000, 3000, 1.0, bits64=False), 32000.0)

    def test_valor_absurdo(self):
        self.assertIsNone(compute_rate(0, 10 ** 15, 1.0))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfgmod.load("/nonexistent.json")

    def test_padrao_valido(self):
        self.assertEqual(cfgmod.validate(self.cfg), [])

    def test_intervalo(self):
        for bad in (0, -3, "abc"):
            self.cfg["collection"]["interval"] = bad
            self.assertTrue(cfgmod.validate(self.cfg))

    def test_email(self):
        self.cfg["alerts"]["email"] = True
        self.cfg["alerts"]["email_to"] = "invalido"
        self.assertTrue(any("e-mail" in e.lower() for e in cfgmod.validate(self.cfg)))
        self.cfg["alerts"]["email_to"] = "a@b.com"
        self.assertEqual(cfgmod.validate(self.cfg), [])

    def test_nenhum_canal(self):
        self.cfg["alerts"]["popup"] = False
        self.cfg["alerts"]["email"] = False
        self.assertTrue(cfgmod.validate(self.cfg))


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = Storage(os.path.join(self.tmp, "t.db"))
        self.cfg = cfgmod.load("/nonexistent.json")
        self.cfg["alerts"].update(popup=True, email=False, traffic_in_bps=1000,
                                  traffic_out_bps=1000, cpu_percent=50, cooldown_seconds=60)
        self.q = queue.Queue()
        self.am = AlertManager(self.cfg, self.st, self.q)

    def s(self, i=0, o=0, c=0, ts=None):
        return {"ts": ts or time.time(), "in_bps": i, "out_bps": o, "cpu": c}

    def test_transicao_e_cooldown(self):
        self.assertEqual(len(self.am.evaluate(self.s(c=90), now=0)), 1)     # normal -> excedido
        self.assertEqual(len(self.am.evaluate(self.s(c=95), now=10)), 0)    # ainda excedido
        self.assertEqual(len(self.am.evaluate(self.s(c=95), now=61)), 1)    # passou a espera
        self.assertEqual(len(self.am.evaluate(self.s(c=10), now=62)), 0)    # recuperou
        self.assertEqual(len(self.am.evaluate(self.s(c=90), now=63)), 1)    # novo alerta imediato

    def test_in_out_separados(self):
        fired = self.am.evaluate(self.s(i=5000, o=10), now=0)
        self.assertEqual([f["metric"] for f in fired], ["traffic_in"])

    def test_none_ignorado(self):
        self.assertEqual(self.am.evaluate(self.s(i=None, o=None, c=None), now=0), [])

    def test_popup_mensagem(self):
        self.am.evaluate(self.s(c=90), now=0)
        kind, data = self.q.get_nowait()
        self.assertEqual(kind, "popup")
        for part in ("CPU", "90.0", "50.0", "Horário"):
            self.assertIn(part, data["body"])


class CollectorMockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = Storage(os.path.join(self.tmp, "t.db"))
        self.cfg = cfgmod.load("/nonexistent.json")
        self.cfg["collection"]["interval"] = 0.3
        self.cfg["alerts"]["cpu_percent"] = 1      # dispara fácil
        self.q = queue.Queue()
        self.am = AlertManager(self.cfg, self.st, self.q)
        self.col = Collector(self.cfg, self.st, self.am, self.q, mock=True)

    def drain(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out

    def test_coleta_pausa_persistencia_e_falha(self):
        self.col.start(); self.col.start(); self.col.start()    # sem coletas duplicadas
        time.sleep(1.8)
        self.col.pause()
        ev = self.drain()
        samples = [d for k, d in ev if k == "sample"]
        self.assertGreaterEqual(len(samples), 3)
        self.assertLessEqual(len(samples), 7)                    # ~1 amostra / 0,3 s, não 3x
        with_traffic = [s for s in samples if s["in_bps"] is not None]
        self.assertTrue(with_traffic)
        self.assertTrue(all(s["in_bps"] >= 0 and s["out_bps"] >= 0 for s in with_traffic))
        self.assertIsNone(samples[0]["in_bps"])                  # 1ª leitura sem baseline
        self.assertTrue(any(k == "popup" for k, _ in ev))

        # histórico persistido
        rows = self.st.query(0, time.time() + 1)
        self.assertEqual(len(rows), len(samples))

        # falha SNMP não derruba a thread e é sinalizada; depois recupera
        n = len(rows)
        self.col._client.fail = True
        self.col.start(); time.sleep(1.0)
        ev = self.drain()
        self.assertTrue(any(k == "error" for k, _ in ev))
        self.assertEqual(sum(1 for k, _ in ev if k == "error"), 1)   # mensagem não repetida
        self.col._client.fail = False
        time.sleep(1.0)
        ev = self.drain()
        self.assertTrue(any(k == "info" and "restabelecida" in d for k, d in ev))
        self.col.stop()
        self.assertGreater(len(self.st.query(0, time.time() + 1)), n)


if __name__ == "__main__":
    unittest.main()
