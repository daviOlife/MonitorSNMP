# Documentação técnica

## Arquitetura

| Módulo | Responsabilidade |
|---|---|
| `snmp_client.py` | Consultas SNMP (pysnmp 7.x assíncrono ou 4–6.x síncrono) e `MockSnmpClient` para testes |
| `collector.py` | Thread única de coleta, cálculo de bps, detecção de reset, tratamento de falhas |
| `alerts.py` | Avaliação de thresholds, anti-repetição, pop-up (via fila) e e-mail (SMTP, em thread) |
| `storage.py` | SQLite: tabelas `samples`, `errors`, `alerts` |
| `config.py` | `config.json`, valores padrão e validação |
| `app.py` | Interface Tkinter + gráficos matplotlib (tempo real, histórico, configuração, eventos) |

A coleta roda em thread própria e se comunica com a interface por uma `queue.Queue`
(eventos `sample`, `popup`, `error`, `info`); a GUI consome a fila a cada 500 ms, então a
interface nunca trava por causa de rede ou SMTP.

## MIBs e OIDs

| Métrica | MIB::objeto | OID |
|---|---|---|
| Entrada (64 bits, preferido) | IF-MIB::ifHCInOctets.\<ifIndex\> | 1.3.6.1.2.1.31.1.1.1.6.\<i\> |
| Saída (64 bits, preferido) | IF-MIB::ifHCOutOctets.\<ifIndex\> | 1.3.6.1.2.1.31.1.1.1.10.\<i\> |
| Entrada (32 bits, fallback) | IF-MIB::ifInOctets | 1.3.6.1.2.1.2.2.1.10.\<i\> |
| Saída (32 bits, fallback) | IF-MIB::ifOutOctets | 1.3.6.1.2.1.2.2.1.16.\<i\> |
| CPU | HOST-RESOURCES-MIB::hrProcessorLoad | 1.3.6.1.2.1.25.3.3.1.2 (média dos núcleos) |
| Reinício do equipamento | SNMPv2-MIB::sysUpTime | 1.3.6.1.2.1.1.3.0 |
| Lista de interfaces | IF-MIB::ifDescr | 1.3.6.1.2.1.2.2.1.2 |

## Cálculo do tráfego (bps)

`bps = (Δbytes × 8) / Δt`, com `Δt` medido com relógio monotônico entre as duas leituras
(intervalo **real**, não o configurado). A amostra recebe `None` (lacuna no gráfico) quando:

- é a primeira leitura (sem baseline), ou após iniciar/retomar/trocar equipamento ou interface;
- `Δt ≤ 0`;
- o contador diminuiu: em Counter32 é tratado como *wrap* (soma 2³²); em Counter64 é
  reinicialização e a amostra é descartada;
- o `sysUpTime` diminuiu (reboot do roteador);
- a taxa excede 400 Gbps (valor absurdo).

Se `ifHCInOctets` não existir (`noSuchObject`), o coletor cai automaticamente para 32 bits.

## Estratégia de alertas

- Limites: tráfego IN, tráfego OUT (**avaliados separadamente**, cada um com seu limite) e CPU.
- Disparo na transição **normal → excedido**. Enquanto permanecer excedido, novo alerta só
  após o período de espera (`cooldown_seconds`, padrão 300 s). Ao voltar ao normal o estado é
  rearmado e a próxima ultrapassagem alerta imediatamente.
- Canais selecionáveis: pop-up, e-mail ou ambos. A mensagem traz métrica, valor, limite e horário.
- E-mail é enviado em thread separada; falhas viram evento de erro e não encerram a aplicação.
- Cada alerta disparado é gravado na tabela `alerts`.

## Tratamento de erros

Timeout, falha de autenticação, OID não suportado, perda de conectividade e erros inesperados
são capturados no coletor; a mensagem aparece na barra de status e em *Eventos* (sem repetir a
mesma mensagem a cada ciclo) e a coleta continua tentando; ao voltar, é informado
"Comunicação SNMP restabelecida".

## Reset e histórico

*Resetar* limpa apenas as séries da tela e o estado dos alertas. Os dados em `monitor.db`
permanecem. Consultas com mais de 5000 amostras são decimadas apenas na exibição.
