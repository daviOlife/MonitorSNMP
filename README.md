# Monitor SNMP de Roteador

Coleta via SNMP o tráfego IN/OUT (bps) de uma interface e a utilização da CPU de um
roteador (testado para o cenário **MikroTik RouterOS CHR**), exibe gráficos em tempo real e
históricos e dispara alertas por pop-up e e-mail quando limites são ultrapassados.

## Pré-requisitos

- Python 3.10+
- Tkinter (no Windows/macOS já vem com o Python; no Linux: `sudo apt install python3-tk`)
- Dependências: `pip install -r requirements.txt` (pysnmp e matplotlib)

## Executar

```bash
python app.py          # roteador real (usa config.json)
python app.py --mock   # roteador simulado: demonstra a interface sem equipamento
python -m unittest discover -s tests -v   # testes da lógica
```

Na primeira execução copie `config.example.json` para `config.json` (ou configure tudo pela aba
**Configurações**, que grava o `config.json`). **Não publique o `config.json`** (contém
community/senhas); ele já está no `.gitignore`. A senha SMTP também pode vir da variável de
ambiente `SMTP_PASSWORD`.

## Configurar o MikroTik CHR (RouterOS)

No terminal do CHR:

```
/snmp set enabled=yes
# v2c somente leitura, restrito à rede do laboratório (ajuste o endereço):
/snmp community add name=monitor addresses=192.168.56.0/24 read-access=yes write-access=no
```

Opcional, SNMPv3 (preferível — o nome da "community" é o usuário v3):

```
/snmp community add name=admin_snmp security=private \
    authentication-protocol=SHA1 authentication-password=SENHA_AUTH \
    encryption-protocol=AES encryption-password=SENHA_PRIV read-access=yes write-access=no
```

Na aba **Configurações** informe: IP do CHR, versão (2c/3), community (ou usuário/chaves v3) e
clique **Buscar interfaces** para escolher a interface (o `ifIndex` do SNMP pode diferir do
número mostrado em `/interface print`). Use **Testar SNMP** para validar a conexão.
O PC precisa alcançar o CHR na porta UDP 161 (VirtualBox/VMware: use rede *host-only* ou *bridge*).

## Como usar

1. **Tempo real**: *Iniciar* começa a coleta; *Pausar* interrompe preservando o que está na
   tela (*Retomar* continua); *Resetar* limpa só a visualização da sessão — **o histórico
   gravado no banco não é apagado**. Clicar em Iniciar várias vezes não cria coletas duplicadas.
2. **Histórico**: informe início e fim (`AAAA-MM-DD HH:MM:SS` ou `DD/MM/AAAA HH:MM`) e
   *Consultar*. Sem amostras no período, a tela informa isso. Os dados ficam em `monitor.db`
   (SQLite) e permanecem após fechar/abrir a aplicação.
3. **Configurações**: intervalo de coleta (>0), thresholds (tráfego IN e OUT, separados, e CPU),
   tipo de alerta (pop-up e/ou e-mail), e-mail de destino (formato validado), SMTP e dados do
   equipamento.
4. **Eventos**: erros de coleta/e-mail e alertas disparados.

### E-mail (SMTP)

Para Gmail use `smtp.gmail.com`, porta 587, TLS, usuário = seu e-mail e uma **senha de app**
(não a senha normal). Use o botão **Testar e-mail**. Falhas de envio são mostradas na barra de
status e registradas em *Eventos*, sem derrubar a aplicação.

## Roteiro de testes (evidências)

| Teste | Como fazer |
|---|---|
| Coleta SNMP | Configure o CHR, *Testar SNMP*, depois *Iniciar* |
| Controles | Iniciar → Pausar → Retomar → Resetar |
| Histórico | Aba Histórico, filtrar período com e sem dados |
| Alerta pop-up | Threshold de CPU baixo (ex.: 1%) ou tráfego baixo e gerar tráfego (`ping -f`/`ping -t -l 1400` para o CHR) |
| Alerta e-mail | Marque e-mail, configure SMTP e provoque alerta |
| Falha SNMP | Community errada ou desligue a VM: a barra mostra o erro e a coleta se recupera sozinha |
| Persistência | Feche e reabra; consulte o histórico |

Veja `DOCUMENTACAO.md` para arquitetura, OIDs, cálculo de bps e estratégia de alertas.
