"""
═══════════════════════════════════════════════════════════════════════════════
 ARBITRAGEM KALSHI × POLYMARKET — BTC 15min (v20)
═══════════════════════════════════════════════════════════════════════════════

MUDANÇAS EM RELAÇÃO À v19 (CLOB V2 + FIX TAXA POLY):

  CONTEXTO V1→V2 (cutover 2026-04-28):
    - Taxas agora são DINÂMICAS por mercado (não mais embutidas no order).
      Consultadas via client.get_clob_market_info(condition_id) → fd.{r,e,to}.
    - Order V2: removidos feeRateBps, nonce, taker.
      Adicionados: timestamp (ms), metadata, builder (bytes32).
    - Builder codes substituem o fluxo HMAC. Use POLY_BUILDER_CODE (env var)
      para attribuir as ordens — opcional.
    - Colateral migra de USDC.e → pUSD (lastreado em USDC, wrap pela onramp).
    - URL de produção continua: https://clob.polymarket.com.
    - SDK package: py_clob_client_v2.

  PROBLEMA "estou colocando um pouco menos" (FIX):
    v19 usava POLY_FEE_COEF=0.072 fixo. Se a taxa real do mercado fosse
    maior, o gross-up de _align_size_and_price comprava menos do que o
    necessário e a perna Poly ficava descoberta pela diferença do fee.
    v20 corrige em 3 camadas:
      (1) FALLBACK ELEVADO: POLY_FEE_COEF 0.072 → 0.10.
      (2) MARGEM DE SEGURANÇA: POLY_FEE_SAFETY_MULT=1.10 multiplica todas
          as estimativas — força comprar bruto extra.
      (3) LOOKUP DINÂMICO: na primeira ordem em cada token, o bot consulta
          client.get_clob_market_info() e armazena o coef real em cache.
          Cálculos passam a usar o valor exato do mercado quando disponível.

MUDANÇAS HISTÓRICAS DA v17 (mantidas):
  Bug contabilidade de round_exec_count permitia arbs DUPLICADAS na mesma
  rodada depois de um hedge bem-sucedido. Causa da perda de ~$9,85 na
  sessão 22-Apr (várias rodadas com 14 shares Poly em vez de 7).

  Fluxo bugado (v16):
    1. ARB2 → DESCOBERTO_CRITICO    (round_exec_count NÃO incrementado)
    2. uncovered_hedge_loop compra Kalshi → sucesso, limpa uncovered=None
    3. Próximo tick WS: round_exec_count ainda é 0, uncovered é None
       → execute_arb() passa pelas guardas e dispara ARB2 DE NOVO
    4. Resultado: 14 shares Poly (2x exposição) quando mercado vai contra

  Fix em duas camadas:
    (a) Incremento defensivo no finally do execute_arb — qualquer resultado
        que TOCOU a Poly consome slot da rodada, não só SUCESSO.
    (b) _register_uncovered força round_exec_count = MAX_EXECS_PER_ROUND
        imediatamente (belt-and-suspenders) — a rodada fica travada mesmo
        que o hedge limpe uncovered=None no meio.

MELHORIAS DA v16 (mantidas):
  - Guarda de posição Poly descoberta + uncovered_hedge_loop.
  - CSV async não-bloqueante, httpx com HTTP/2, cache de tick_size, etc.

AVISOS RESIDUAIS DE RISCO:
  - Poly→Kalshi sequencial: se Poly preencher e Kalshi falhar, a posição
    Poly fica descoberta. O hedge_loop tenta fechar até o fim da rodada,
    aceitando até UNCOVERED_HEDGE_MAX_LOSS_CENTS¢ de perda.
  - FOK pode ser rejeitada se o preço mover entre o snapshot e o envio.
  - Bots profissionais rodam em <50ms. Este código roda em ~300-800ms.

═══════════════════════════════════════════════════════════════════════════════
 VERSÃO PÚBLICA — NÃO OPERACIONAL
═══════════════════════════════════════════════════════════════════════════════

  Este arquivo é publicado como documentação técnica. O bloco de credenciais
  foi neutralizado (placeholders "XXXXXXXXXXXX") e há uma trava que impede a
  execução. O bot não autentica e não envia ordens.

  A estratégia NÃO FUNCIONA, e não por bug: Kalshi e Polymarket resolvem seus
  contratos sobre referências de preço diferentes — strike absoluto sobre o
  índice da Kalshi versus variação relativa sobre o oráculo Chainlink. As duas
  réguas divergem em ~12% das rodadas, o que quebra a premissa de que as duas
  pernas cobrem o mesmo evento. O README traz a análise completa.

  Kalshi e Polymarket estão atualmente proibidos no Brasil. Este projeto foi
  desenvolvido e testado antes da vigência da restrição.
"""

import asyncio
import websockets
import json
import requests
import re
import time
import os
import sys
import uuid
import datetime
import math
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
import base64

# ─── Dependência httpx (HTTP async com connection pooling) ──────────────────
#   httpx é usado para Kalshi REST API porque:
#   1) connection pooling: reaproveita conexões TCP/TLS entre requisições
#      (economiza ~30-100ms no handshake por request)
#   2) HTTP/2: multiplexação permite múltiplos requests em paralelo sobre
#      a mesma conexão (crítico quando place_kalshi_order + fetch_kalshi_order
#      acontecem em sequência rápida)
#   3) async nativo: evita o overhead do run_in_executor que o 'requests' exige
try:
    import httpx
    HTTPX_OK = True
except ImportError:
    HTTPX_OK = False
    print("[WARN] httpx não instalado. Rodando com 'requests' (mais lento).")
    print("       Execute: pip install 'httpx[http2]'")


# ─── Dependência Polymarket ────────────────────────────────────────────────────
try:
    from py_clob_client_v2 import (
        ApiCreds, ClobClient, OrderArgs, MarketOrderArgs,
        OrderType, Side, PartialCreateOrderOptions,
    )
    from py_clob_client_v2.clob_types import BalanceAllowanceParams
    POLY_SDK_OK = True
except ImportError:
    POLY_SDK_OK = False
    print("[WARN] py_clob_client_v2 não instalado. Ordens Poly desabilitadas.")
    print("       Execute: pip install py_clob_client_v2")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
# ─── CREDENCIAIS REMOVIDAS ─────────────────────────────────────────────────
#   Esta é a versão pública do projeto, publicada como documentação técnica.
#   O bloco de credenciais foi neutralizado de propósito: os valores abaixo são
#   placeholders literais, não leitura de ambiente. O código NÃO autentica e
#   NÃO envia ordens como está.
#
#   Isso é intencional. Veja o README: a estratégia é inviável por um motivo
#   estrutural (Kalshi e Polymarket resolvem sobre referências de preço
#   diferentes, divergindo em ~12% das rodadas). O repositório existe para
#   documentar o raciocínio e a engenharia, não para ser operado.
#
#   Kalshi e Polymarket estão atualmente proibidos no Brasil.
# ───────────────────────────────────────────────────────────────────────────
KALSHI_KEY_FILE = "XXXXXXXXXXXX"        # caminho do .pem da chave privada Kalshi
KALSHI_KEY_ID   = "XXXXXXXXXXXX"        # UUID da API key Kalshi
KALSHI_BASE_URL = "https://api.elections.kalshi.com"

POLY_PRIVATE_KEY  = "XXXXXXXXXXXX"      # chave privada da carteira (0x...)
POLY_CHAIN_ID     = 137
POLY_CLOB_HOST    = "https://clob.polymarket.com"
POLY_PROXY_WALLET = "XXXXXXXXXXXX"      # endereço da proxy wallet (0x...)
POLY_API_KEY      = "XXXXXXXXXXXX"
POLY_API_SECRET   = "XXXXXXXXXXXX"
POLY_API_PASS     = "XXXXXXXXXXXX"

# Trava de segurança: impede execução real mesmo se alguém preencher os campos
# acima sem ler o README. Remover isto é uma decisão consciente de quem o fizer.
CREDENTIALS_REDACTED = True

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

BUDGET              = 7.0
BOOK_DEPTH          = 5
MIN_PROFIT_PCT      = 2.5
MODE                = "REAL"
AUTO_EXECUTE        = True
EXEC_COOLDOWN       = 5
MAX_EXECS_PER_ROUND = 1
POLY_MIN_SIZE       = 5
POLY_FOK_VERIFY_MS  = 500

KALSHI_SLIPPAGE_CENTS = 2
# v17 FIX #4: reduzido de 5s pra 2s. 2s é suficiente pra Poly CLOB creditar
# shares na carteira e o unwind ser aceito. Reduzir o delay encurta a janela
# em que a posição Poly fica sem hedge (latência total do DESCOBERTO cai
# de ~6.5s pra ~3s).
UNWIND_DELAY_S        = 2

# ─── Taxas Polymarket (CLOB V2) ────────────────────────────────────────────
# CLOB V2: taxas são dinâmicas por mercado, queriáveis via
#   client.get_clob_market_info(condition_id) → {"fd": {"r": rate, "e": exp, "to": takerOnly}}
# Fórmula: fee_shares = coef * price * (1-price) * shares
#
# POLY_FEE_COEF é o FALLBACK quando o lookup dinâmico não popular o cache
# (primeiríssima ordem de um token, ou se a SDK não expor a info).
# Aumentado de 0.072 → 0.10 para cobrir mercados com taxa mais alta.
POLY_FEE_COEF   = 0.10
# Margem de segurança aplicada em TODAS as estimativas de fee Poly.
# Garante comprar bruto extra suficiente — a sobra mínima é melhor que ficar
# descoberto. 1.10 = +10% de buffer no gross-up.
POLY_FEE_SAFETY_MULT = 1.10

KALSHI_FEE_COEF = 0.07

# ─── Builder Code Polymarket (CLOB V2, opcional) ───────────────────────────
# Builder codes substituíram o fluxo HMAC POLY_BUILDER_*. Pegue o seu em
# https://polymarket.com/settings?tab=builder e exporte:
#   export POLY_BUILDER_CODE="0x..."
# Deixe vazio se não for builder. Vai como bytes32 dentro do order assinado.
POLY_BUILDER_CODE = os.environ.get("POLY_BUILDER_CODE", "")

# ═══════════════════════════════════════════════════════════════════════════════
# MODO PAPER — ARB COMPOSTA (4 PERNAS)
# ═══════════════════════════════════════════════════════════════════════════════
#   Quando PAPER_MODE=True, o bot NÃO envia ordens reais. Em vez disso:
#     1. Observa a primeira arb (1 ou 2) que cruzar profit_pct >= PAPER_ENTRY_PCT
#        e registra como "entrada virtual" (custo, preços, contratos, ts).
#     2. Continua rodando e procura a arb OPOSTA com profit_pct >= PAPER_HEDGE_PCT.
#        Quando aparece, registra "FECHADO_OK" — você teria ficado com YES+NO
#        Kalshi e UP+DOWN Poly, $2/contrato garantido independente de divergência.
#     3. Se a rodada virar SEM o hedge oposto aparecer, registra "NAKED_AT_CLOSE":
#        é o cenário ruim onde, em produção, você ficaria exposto ao risco de
#        divergência de 6%. Esse contador é o teste decisivo da estratégia.
#
#   Lucro paper por par fechado = 2 * min(c_entry, c_hedge) - (cost_entry + cost_hedge)
#
#   Tudo é gravado em paper_compound_ops.csv pra análise posterior.
PAPER_MODE         = True       # ← True desliga ordens reais e ativa paper trading
PAPER_ENTRY_PCT    = 2.0        # entrada quando profit_pct da arb >= 2%
PAPER_HEDGE_PCT    = 1.5        # hedge oposto quando profit_pct >= 1.5%
PAPER_OPS_CSV_FILE = "paper_compound_ops.csv"

# ─── Guarda de posição Poly descoberta (v16) ────────────────────────────────
# Quando DESCOBERTO_CRITICO ou PARTIAL_DESCOBERTO acontece, paramos de abrir
# novas arbitragens e focamos em comprar a perna Kalshi faltante até o fim
# da rodada. Aceitamos breakeven (ou pequena perda configurável) pra garantir
# fechamento da exposição.
UNCOVERED_BLOCK_NEW_ARBS         = True   # bloqueia novas arbs enquanto descoberto
UNCOVERED_HEDGE_RETRY_S          = 2.5    # intervalo mínimo entre tentativas de hedge
UNCOVERED_HEDGE_MAX_LOSS_CENTS   = 5      # tolerância além do breakeven, por contrato
UNCOVERED_HEDGE_LOG_EVERY_N      = 6      # frequência do log "ask alto demais"

# ─── Override: PAPER_MODE desliga qualquer execução real ────────────────────
if PAPER_MODE:
    AUTO_EXECUTE = False     # ainda assim paper_observe é chamado nos mesmos pontos
    MODE         = "PAPER"


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING — apenas console + operations.csv  (sem arquivo .log)
# ═══════════════════════════════════════════════════════════════════════════════
import csv

OPERATIONS_CSV_FILE = "operations.csv"


def log_trade(msg: str):
    """Imprime no console com timestamp."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"{ts} | {msg}", flush=True)


def log_section(title: str):
    """Separador visual no console."""
    print("=" * 70, flush=True)
    print(f" {title}", flush=True)
    print("=" * 70, flush=True)


# ─── CSV estruturado de operações ───────────────────────────────────────────
OPERATIONS_CSV_HEADER = [
    "timestamp",
    "arb_id",
    "result_tag",
    "kalshi_ticker",
    "poly_market",
    "contracts_int",
    "kalshi_side",
    "kalshi_price_target",
    "kalshi_price_sent",
    "kalshi_order_id",
    "kalshi_filled",
    "poly_token_id_head",
    "poly_price_target",
    "poly_order_id",
    "poly_matched_shares",
    "profit_estimated",
    "budget_usd",
    "exec_duration_ms",
    "warnings",
]

def _ensure_operations_csv():
    if not os.path.exists(OPERATIONS_CSV_FILE):
        with open(OPERATIONS_CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(OPERATIONS_CSV_HEADER)

def log_operation_csv(row: dict):
    """
    Versão v15: NÃO-BLOQUEANTE.
    Empurra para _csv_queue (custo ~10µs) e deixa o _csv_writer_loop
    fazer o I/O de disco em background. Se a fila não existe ainda
    (startup), faz fallback síncrono — nesse caso o overhead não
    importa porque nenhuma arb está rolando.
    """
    global _csv_queue
    if _csv_queue is not None:
        try:
            _csv_queue.put_nowait(row)
            return
        except Exception as e:
            log_trade(f"[CSV QUEUE ERRO] {e} — fallback síncrono")
    # Fallback síncrono (só no startup antes da queue existir)
    try:
        _ensure_operations_csv()
        values = [row.get(k, "") for k in OPERATIONS_CSV_HEADER]
        with open(OPERATIONS_CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(values)
            f.flush()
    except Exception as e:
        log_trade(f"[CSV LOG ERRO] {e} | row={row}")


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH KALSHI
# ═══════════════════════════════════════════════════════════════════════════════
def load_private_key_from_file(file_path):
    with open(file_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def sign_pss_text(private_key, text: str) -> str:
    sig = private_key.sign(
        text.encode('utf-8'),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode('utf-8')

def make_auth_headers(private_key, method: str, path: str) -> dict:
    ts  = str(int(datetime.datetime.now().timestamp() * 1000))
    msg = ts + method + path.split('?')[0]
    return {
        'KALSHI-ACCESS-KEY':       KALSHI_KEY_ID,
        'KALSHI-ACCESS-SIGNATURE': sign_pss_text(private_key, msg),
        'KALSHI-ACCESS-TIMESTAMP': ts,
        'Content-Type':            'application/json',
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════════════════════
state = {
    "kalshi_yes_bid":   None,
    "kalshi_yes_ask":   None,
    "poly_yes_ask":     None,
    "poly_no_ask":      None,
    "kalshi_ticker":    None,
    "poly_market_name": None,
    "poly_token_yes":   None,
    "poly_token_no":    None,
    "executing":        False,
    "last_exec_time":   0.0,
    "last_exec_result": None,
    "total_profit":     0.0,
    "round_exec_count": 0,
    "current_round_ts": 0,
    # ── histórico & métricas ─────────────────────────────────────────────
    "exec_history":     [],   # últimas 10 execs (sucesso + falha), mais recente primeiro
    "total_execs":      0,    # total de tentativas na sessão
    "total_success":    0,    # quantas viraram SUCESSO (ou SUCESSO_TOPUP)
    # ── controle de saldo ────────────────────────────────────────────────
    "balance_poly":       None,   # USD disponível na Polymarket
    "balance_kalshi":     None,   # USD disponível na Kalshi
    "balance_frozen":     False,  # True = bloqueado por saldo insuficiente
    "balance_last_check": 0.0,    # timestamp do último check
    # ── v16: guarda de posição Poly descoberta ──────────────────────────
    #   Quando não-None, novas arbs ficam bloqueadas e a task
    #   uncovered_hedge_loop tenta comprar a perna Kalshi faltante.
    #   Limpa sozinho ao hedgear com sucesso OU no fim da rodada.
    "uncovered":          None,
    # ── v17 FIX #4: coordenação entre unwind (execute_arb) e hedge_loop ──
    #   Quando True, o hedge_loop NÃO dispara ordem Kalshi porque o
    #   execute_arb ainda está tentando unwind na Poly. Evita race onde
    #   ambos agem ao mesmo tempo e criam exposição dupla no sentido
    #   contrário. Limpa em:
    #     - Fim do await unwind (sucesso OU falha)
    #     - Shutdown da rodada
    "unwind_in_progress": False,
    # ── PAPER MODE: estado da arb composta em observação ───────────────────
    #   "paper_open"        — None ou dict da entrada virtual aguardando hedge
    #   "paper_session"     — métricas acumuladas da sessão paper
    "paper_open":    None,
    "paper_session": {
        "entries":         0,   # quantas entradas viraram entrada virtual
        "closed_ok":       0,   # entradas que tiveram hedge antes do close
        "naked_at_close":  0,   # entradas que ficaram naked (RISCO real)
        "skipped_blocked": 0,   # ticks com sinal mas paper_open != None (1 entry/round)
        "total_paper_pnl": 0.0, # lucro paper acumulado (USD)
        "best_pair_pnl":   0.0, # melhor par fechado
        "worst_pair_pnl":  0.0, # pior par fechado (ainda positivo se lucro garantido)
    },
}

# Tamanho máximo do histórico exibido na UI
EXEC_HISTORY_MAX = 10

exec_lock        = asyncio.Lock()
kalshi_orderbook = {"yes": {}, "no": {}}
poly_book        = {"yes_asks": [], "no_asks": []}
last_display_time = 0

# ── Performance: UI refresh rate ─────────────────────────────────────────
#   Controla com que frequência a tela é redesenhada. 0.25s = 4Hz.
#   Valores menores = atualização mais fluida mas mais CPU consumido na UI.
#   Valores maiores = UI "lenta" mas thread principal mais livre para
#   processar eventos de WebSocket e disparar arbitragens.
UI_REFRESH_INTERVAL_S       = 0.25   # janela ativa (4Hz)
UI_REFRESH_INTERVAL_PAUSE_S = 1.0    # janela segura — pode ser mais lento

# ── Performance: cache de tick_size da Polymarket ────────────────────────
#   O tick_size de um token não muda dentro de uma rodada, então chamar
#   client.get_tick_size() a cada execução desperdiça ~80-200ms por
#   execução. Cache global (válido pelo tempo de vida do processo).
_tick_size_cache: dict = {}


# ── Performance: cliente HTTP async com pooling ──────────────────────────
#   Global compartilhado para todas as requisições Kalshi REST.
#   Por que é mais rápido que 'requests' síncrono:
#     - connection pool: reaproveita TCP+TLS entre requisições (~30-100ms)
#     - HTTP/2: multiplexação sobre 1 conexão
#     - async nativo: sem overhead de run_in_executor
#   Timeout de 5s (mesmo que o requests anterior). Limits generosos
#   o suficiente pra lidar com burst de reconciliação (~5 req em paralelo).
_http_client = None  # será inicializado no main() dentro do event loop

async def _get_http_client():
    """
    Lazy init + retorno do httpx.AsyncClient global.
    MUST ser chamado dentro do event loop. O client reaproveita a conexão
    TCP entre chamadas, economizando o handshake TLS (~50-150ms por call).
    """
    global _http_client
    if _http_client is None and HTTPX_OK:
        _http_client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(5.0, connect=3.0),
            limits=httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0,  # mantém viva por 30s
            ),
            # Envia headers genéricos; os auth headers são por-requisição
            headers={"User-Agent": "arb-scanner-v17"},
        )
    return _http_client

async def _close_http_client():
    """Fecha o client HTTP global (chamado no shutdown)."""
    global _http_client
    if _http_client is not None:
        try:
            await _http_client.aclose()
        except Exception:
            pass
        _http_client = None


# ── Performance: CSV async não-bloqueante ─────────────────────────────────
#   Antes: log_operation_csv() fazia open() + write() + flush() INLINE na
#   thread crítica do execute_arb. Isso custa 3-15ms (I/O de disco) no pior
#   momento — logo após a execução, quando estamos tentando reconciliar.
#   Agora: log_operation_csv() só empurra pra uma asyncio.Queue (~10µs).
#   Um writer task dedicado consome a fila e faz o I/O em background.
_csv_queue: "asyncio.Queue | None" = None
_csv_writer_task = None

async def _csv_writer_loop():
    """
    Consome _csv_queue e escreve no operations.csv de forma agregada.
    Usa `flush` apenas quando a fila fica vazia (batch writes).
    Se sinalizado com None, encerra gracefully.
    """
    _ensure_operations_csv()
    while True:
        try:
            row = await _csv_queue.get()
            if row is None:
                break  # shutdown
            rows_to_write = [row]
            # Drena a fila: se outros rows chegaram, escreve tudo junto
            while True:
                try:
                    nxt = _csv_queue.get_nowait()
                    if nxt is None:
                        _csv_queue.put_nowait(None)  # re-coloca sinal de shutdown
                        break
                    rows_to_write.append(nxt)
                except asyncio.QueueEmpty:
                    break

            # Escreve o batch (I/O fora do caminho crítico)
            try:
                with open(OPERATIONS_CSV_FILE, "a", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    for r in rows_to_write:
                        writer.writerow([r.get(k, "") for k in OPERATIONS_CSV_HEADER])
                    f.flush()
            except Exception as e:
                log_trade(f"[CSV WRITER ERRO] {e} | {len(rows_to_write)} rows perdidas")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_trade(f"[CSV WRITER LOOP ERRO] {e}")
            await asyncio.sleep(0.5)


def _clear_screen():
    """
    Limpa a tela usando ANSI puro (\\033[2J\\033[H), que é ~30000x mais
    rápido que os.system('clear') pois não faz fork()+exec() de um
    processo externo. Custo: ~0.0003ms vs 11ms do os.system.

    \\033[2J  = limpa a tela toda
    \\033[H   = move cursor para o canto superior esquerdo
    """
    print("\033[2J\033[H", end="", flush=False)


# ═══════════════════════════════════════════════════════════════════════════════
# JANELA SEGURA
# ═══════════════════════════════════════════════════════════════════════════════
def _safe_window() -> bool:
    s = int(time.time()) % 900
    return 20 <= s < 820

def _current_round_ts() -> int:
    return (int(time.time()) // 900) * 900

def _reset_round_if_needed():
    rts = _current_round_ts()
    if state["current_round_ts"] != rts:
        # PAPER: se há entrada aberta da rodada anterior sem hedge → naked
        if PAPER_MODE and state.get("paper_open") is not None:
            paper_close_naked()
        state["current_round_ts"] = rts
        state["round_exec_count"] = 0


# ═══════════════════════════════════════════════════════════════════════════════
# TAXAS
# ═══════════════════════════════════════════════════════════════════════════════
# Cache de coeficientes de taxa por token, populado via get_clob_market_info()
# na primeira ordem de cada token. Veja _populate_poly_fee_cache().
_poly_fee_rate_cache: dict = {}

def get_poly_fee_coef(token_id: str = None) -> float:
    """Retorna o coef da taxa Poly para um token. Cache hit → valor real do
    mercado (V2). Miss → fallback POLY_FEE_COEF. Em ambos os casos aplica
    POLY_FEE_SAFETY_MULT para criar buffer no gross-up."""
    base = POLY_FEE_COEF
    if token_id and token_id in _poly_fee_rate_cache:
        base = _poly_fee_rate_cache[token_id]
    return base * POLY_FEE_SAFETY_MULT

def calc_poly_fee_shares(price: float, gross_shares: float, token_id: str = None) -> float:
    coef = get_poly_fee_coef(token_id)
    return gross_shares * coef * price * (1.0 - price)

def calc_poly_fee_usdc(price: float, gross_shares: float, token_id: str = None) -> float:
    return calc_poly_fee_shares(price, gross_shares, token_id) * price

def calc_kalshi_fee(price: float, contracts: float) -> float:
    fee_cents = math.ceil(KALSHI_FEE_COEF * contracts * price * (1.0 - price) * 100.0)
    return fee_cents / 100.0

def poly_gross_for_net(net_shares: float, price: float, token_id: str = None) -> float:
    coef = get_poly_fee_coef(token_id)
    fee_pct = coef * price * (1.0 - price)
    return net_shares / (1.0 - fee_pct)


# ═══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK KALSHI
# ═══════════════════════════════════════════════════════════════════════════════
def apply_orderbook_snapshot(msg: dict):
    kalshi_orderbook["yes"] = {}
    kalshi_orderbook["no"]  = {}
    for side, field in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
        for entry in msg.get(field, []):
            price = round(float(entry[0]), 4)
            size  = float(entry[1])
            if size > 0:
                kalshi_orderbook[side][price] = size
    _update_kalshi_state()

def apply_orderbook_delta(msg: dict):
    side  = msg.get("side")
    price = round(float(msg.get("price_dollars", 0)), 4)
    delta = float(msg.get("delta_fp", 0))
    if side not in kalshi_orderbook:
        return
    new_size = kalshi_orderbook[side].get(price, 0.0) + delta
    if new_size < 0.001:
        kalshi_orderbook[side].pop(price, None)
    else:
        kalshi_orderbook[side][price] = new_size
    _update_kalshi_state()

def _update_kalshi_state():
    yes_book = kalshi_orderbook["yes"]
    no_book  = kalshi_orderbook["no"]
    best_yes_bid = max(yes_book.keys()) if yes_book else None
    best_no_bid  = max(no_book.keys())  if no_book  else None
    state["kalshi_yes_bid"] = best_yes_bid
    state["kalshi_yes_ask"] = round(1.0 - best_no_bid, 4) if best_no_bid is not None else None


# ═══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK POLYMARKET — via WebSocket
# ═══════════════════════════════════════════════════════════════════════════════
def _apply_poly_book_event(data: dict):
    event_type = data.get("event_type")
    asset_id   = data.get("asset_id")
    if not asset_id:
        return

    is_yes = (asset_id == state.get("poly_token_yes"))
    is_no  = (asset_id == state.get("poly_token_no"))
    if not (is_yes or is_no):
        return

    side_key = "yes_asks" if is_yes else "no_asks"

    if event_type == "book":
        asks = data.get("asks", [])
        poly_book[side_key] = sorted(
            [(float(a["price"]), float(a["size"])) for a in asks if float(a["size"]) > 0],
            key=lambda x: x[0]
        )
    elif event_type == "price_change":
        changes = data.get("price_changes", data.get("changes", []))
        current = dict(poly_book[side_key])
        for ch in changes:
            side = ch.get("side", "").upper()
            if side not in ("SELL", "ASK"):
                continue
            price = float(ch.get("price"))
            size  = float(ch.get("size", 0))
            if size <= 0:
                current.pop(price, None)
            else:
                current[price] = size
        poly_book[side_key] = sorted(current.items(), key=lambda x: x[0])

    if side_key == "yes_asks":
        state["poly_yes_ask"] = poly_book["yes_asks"][0][0] if poly_book["yes_asks"] else None
    else:
        state["poly_no_ask"]  = poly_book["no_asks"][0][0]  if poly_book["no_asks"]  else None


async def poly_ws_client(private_key):
    current_ts = None
    loop       = asyncio.get_event_loop()

    while True:
        try:
            expected_ts = _current_round_ts()

            if current_ts != expected_ts:
                slug = f"btc-updown-15m-{expected_ts}"
                try:
                    if HTTPX_OK:
                        http = await _get_http_client()
                        resp = await http.get(
                            f"https://gamma-api.polymarket.com/events/slug/{slug}",
                            timeout=5.0,
                        )
                        event = resp.json()
                    else:
                        event = await loop.run_in_executor(
                            None, lambda: requests.get(
                                f"https://gamma-api.polymarket.com/events/slug/{slug}",
                                timeout=5
                            ).json()
                        )
                except Exception as e:
                    state["poly_market_name"] = f"Gamma API erro: {e}"
                    await asyncio.sleep(2)
                    continue

                if 'markets' not in event or not event['markets']:
                    state["poly_market_name"] = f"Aguardando mercado Poly ({slug})..."
                    state.update({
                        "poly_yes_ask": None, "poly_no_ask": None,
                        "poly_token_yes": None, "poly_token_no": None,
                    })
                    poly_book["yes_asks"] = []
                    poly_book["no_asks"]  = []
                    await asyncio.sleep(2)
                    continue

                market    = event['markets'][0]
                token_ids = json.loads(market['clobTokenIds'])
                state["poly_token_yes"]   = token_ids[0]
                state["poly_token_no"]    = token_ids[1]
                state["poly_market_name"] = market.get('question')
                current_ts = expected_ts
                log_trade(f"[POLY WS] Novo mercado: {slug}  tokens={token_ids}")

                # ── Pré-aquece cache de tick_size dos dois tokens em paralelo ──
                # Fazer isso AGORA (na descoberta do mercado) economiza 80-200ms
                # na primeira execução da rodada. Não bloqueia o fluxo: se falhar,
                # a chamada cacheada no place_poly_fok tenta de novo.
                try:
                    poly_client = get_poly_client()
                    if poly_client is not None:
                        await asyncio.gather(
                            _get_tick_size_cached(poly_client, token_ids[0]),
                            _get_tick_size_cached(poly_client, token_ids[1]),
                            return_exceptions=True,
                        )
                except Exception as e:
                    log_trade(f"[TICK PRE-WARM FAIL] {e}")

            async with websockets.connect(POLY_WS_URL, ping_interval=None) as ws:
                sub = {
                    "assets_ids": [state["poly_token_yes"], state["poly_token_no"]],
                    "type":       "market",
                }
                await ws.send(json.dumps(sub))

                async def heartbeat():
                    while True:
                        await asyncio.sleep(10)
                        try:
                            await ws.send("PING")
                        except Exception:
                            break

                hb_task = asyncio.create_task(heartbeat())

                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        if raw == "PONG" or raw == "":
                            continue
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue

                        if isinstance(data, list):
                            for d in data:
                                _apply_poly_book_event(d)
                        else:
                            _apply_poly_book_event(data)

                        if (AUTO_EXECUTE and not state["executing"]
                                and _safe_window() and not _is_blocked_by_uncovered()):
                            for arb_id in (1, 2):
                                sim = simulate_arb(arb_id)
                                if (sim and sim["ok_a"] and sim["ok_b"]
                                        and sim["profit"] > 0
                                        and sim["profit"] / BUDGET * 100 >= MIN_PROFIT_PCT):
                                    asyncio.create_task(execute_arb(sim, private_key))
                                    break

                        # ── PAPER MODE: observa sem executar ────────────────
                        if PAPER_MODE and _safe_window():
                            for arb_id in (1, 2):
                                sim = simulate_arb(arb_id)
                                if sim:
                                    paper_observe(sim)

                        check_arbitrage()

                        if _current_round_ts() != current_ts:
                            break
                finally:
                    hb_task.cancel()

        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            await asyncio.sleep(1)
        except Exception as e:
            log_trade(f"[POLY WS ERRO] {e}")
            await asyncio.sleep(2)


# ═══════════════════════════════════════════════════════════════════════════════
# PAPER MODE — observação da arb composta sem ordens reais
# ═══════════════════════════════════════════════════════════════════════════════
PAPER_OPS_CSV_HEADER = [
    "timestamp_iso",          # quando o evento aconteceu
    "round_ts",               # timestamp UTC do início da rodada (15min)
    "round_clock",            # mm:ss decorrido na rodada
    "event",                  # ENTRY | HEDGE_OK | NAKED_AT_CLOSE | SKIPPED
    "arb_id",                 # 1 (YES Kalshi+NO Poly) ou 2 (NO Kalshi+YES Poly)
    "contracts",              # contratos da perna
    "cost_usd",               # custo total da perna (já com fees no real_cost)
    "profit_pct_at_event",    # profit_pct individual da arb naquele tick
    "k_yes_bid", "k_yes_ask",
    "p_yes_ask", "p_no_ask",
    # ── Preenchidos só em HEDGE_OK ─────────────────────────────────────────
    "entry_arb_id",
    "entry_cost_usd",
    "entry_contracts",
    "delay_seconds",          # entre entrada e hedge
    "min_contracts",          # min(c_entry, c_hedge) — define receita garantida
    "pair_total_cost",        # entry_cost + hedge_cost
    "pair_guaranteed_payout", # 2 * min_contracts
    "pair_pnl_usd",           # payout - total_cost
    "pair_pnl_pct_of_budget", # pnl / BUDGET * 100
]


def _ensure_paper_csv():
    if not os.path.exists(PAPER_OPS_CSV_FILE):
        with open(PAPER_OPS_CSV_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(PAPER_OPS_CSV_HEADER)


def _log_paper_csv(row: dict):
    """Síncrono mesmo — paper events são raros (≤1 por rodada de 15min)."""
    try:
        _ensure_paper_csv()
        values = [row.get(k, "") for k in PAPER_OPS_CSV_HEADER]
        with open(PAPER_OPS_CSV_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(values)
            f.flush()
    except Exception as e:
        log_trade(f"[PAPER CSV ERRO] {e} | row={row}")


def _paper_market_snapshot() -> dict:
    """Captura preços de topo do livro pro CSV — útil pra reproduzir depois."""
    return {
        "k_yes_bid": state.get("kalshi_yes_bid"),
        "k_yes_ask": state.get("kalshi_yes_ask"),
        "p_yes_ask": state.get("poly_yes_ask"),
        "p_no_ask":  state.get("poly_no_ask"),
    }


def _paper_round_clock() -> str:
    s = int(time.time()) % 900
    return f"{s//60:02d}:{s%60:02d}"


def paper_observe(sim: dict):
    """
    Análogo "sem ordem" de execute_arb. Recebe a sim que cruzou o threshold
    e decide:
      - se NÃO há entrada aberta nesta rodada e profit_pct >= PAPER_ENTRY_PCT
        → abre entrada virtual
      - se HÁ entrada aberta e a sim atual é da arb OPOSTA e profit_pct >=
        PAPER_HEDGE_PCT → fecha o par (registra HEDGE_OK)
      - caso contrário → registra SKIPPED (sinal mas bloqueado por entry aberta)
    """
    if sim is None:
        return
    if not (sim.get("ok_a") and sim.get("ok_b")):
        return
    profit_pct = (sim["profit"] / BUDGET * 100.0) if BUDGET > 0 else 0.0

    sess = state["paper_session"]
    open_pos = state.get("paper_open")
    snap = _paper_market_snapshot()
    now_iso = datetime.datetime.now().isoformat(timespec="milliseconds")
    round_ts = state.get("current_round_ts", 0)

    # ── CASO 1: já temos entrada aberta nesta rodada ────────────────────
    if open_pos is not None:
        # mesma arb que entrou? não é hedge, é só reforço — ignora
        if open_pos["arb_id"] == sim["id"]:
            return
        # arb oposta com edge suficiente → fecha o par
        if profit_pct >= PAPER_HEDGE_PCT:
            c_entry = float(open_pos["contracts"])
            c_hedge = float(sim["filled_ok"])
            min_c   = min(c_entry, c_hedge)
            entry_cost = float(open_pos["cost_usd"])
            hedge_cost = float(sim["real_cost"])
            total_cost = entry_cost + hedge_cost
            payout     = 2.0 * min_c
            pnl        = payout - total_cost
            delay_s    = int(time.time()) - int(open_pos["ts_unix"])

            sess["closed_ok"]       += 1
            sess["total_paper_pnl"] += pnl
            sess["best_pair_pnl"]    = max(sess["best_pair_pnl"], pnl)
            sess["worst_pair_pnl"]   = (pnl if sess["closed_ok"] == 1
                                        else min(sess["worst_pair_pnl"], pnl))

            log_trade(
                f"[PAPER HEDGE_OK] entrada arb{open_pos['arb_id']} + hedge arb{sim['id']} "
                f"| delay {delay_s}s | min_c {min_c:.2f} | "
                f"custo ${total_cost:.2f} → payout ${payout:.2f} → "
                f"\033[32mPnL ${pnl:+.2f}\033[0m"
            )
            _log_paper_csv({
                "timestamp_iso": now_iso,
                "round_ts": round_ts,
                "round_clock": _paper_round_clock(),
                "event": "HEDGE_OK",
                "arb_id": sim["id"],
                "contracts": round(c_hedge, 4),
                "cost_usd": round(hedge_cost, 4),
                "profit_pct_at_event": round(profit_pct, 4),
                **{k: (round(v, 4) if isinstance(v, (int, float)) else "")
                   for k, v in snap.items()},
                "entry_arb_id": open_pos["arb_id"],
                "entry_cost_usd": round(entry_cost, 4),
                "entry_contracts": round(c_entry, 4),
                "delay_seconds": delay_s,
                "min_contracts": round(min_c, 4),
                "pair_total_cost": round(total_cost, 4),
                "pair_guaranteed_payout": round(payout, 4),
                "pair_pnl_usd": round(pnl, 4),
                "pair_pnl_pct_of_budget": round(pnl / BUDGET * 100.0, 4),
            })
            state["paper_open"] = None
            return
        # arb oposta mas sem edge suficiente — não é hedge útil, ignora silencioso
        return

    # ── CASO 2: nenhuma entrada aberta ──────────────────────────────────
    # 1 entrada por rodada (mantém paridade com MAX_EXECS_PER_ROUND=1)
    if state.get("round_exec_count", 0) >= MAX_EXECS_PER_ROUND:
        return
    if profit_pct >= PAPER_ENTRY_PCT:
        c_entry = float(sim["filled_ok"])
        cost    = float(sim["real_cost"])
        state["paper_open"] = {
            "arb_id":     sim["id"],
            "contracts":  c_entry,
            "cost_usd":   cost,
            "profit_pct_at_entry": profit_pct,
            "ts_unix":    int(time.time()),
            "round_ts":   round_ts,
            "snap":       snap,
        }
        sess["entries"]            += 1
        state["round_exec_count"]  += 1   # consome o slot da rodada (1 entry/round)

        log_trade(
            f"[PAPER ENTRY] arb{sim['id']} ({sim['label_a'].strip()} + "
            f"{sim['label_b'].strip()}) | {c_entry:.2f}c @ ${cost:.2f} | "
            f"edge {profit_pct:.2f}% — aguardando hedge oposto ≥{PAPER_HEDGE_PCT:.1f}%"
        )
        _log_paper_csv({
            "timestamp_iso": now_iso,
            "round_ts": round_ts,
            "round_clock": _paper_round_clock(),
            "event": "ENTRY",
            "arb_id": sim["id"],
            "contracts": round(c_entry, 4),
            "cost_usd": round(cost, 4),
            "profit_pct_at_event": round(profit_pct, 4),
            **{k: (round(v, 4) if isinstance(v, (int, float)) else "")
               for k, v in snap.items()},
        })


def paper_close_naked():
    """
    Chamado quando a rodada vira. Se há paper_open sem hedge → registra como
    NAKED_AT_CLOSE (esse é o cenário ruim onde, em produção, você ficaria
    exposto ao risco de divergência de 6%).
    """
    open_pos = state.get("paper_open")
    if open_pos is None:
        return
    sess = state["paper_session"]
    sess["naked_at_close"] += 1
    now_iso = datetime.datetime.now().isoformat(timespec="milliseconds")
    log_trade(
        f"\033[31m[PAPER NAKED] arb{open_pos['arb_id']} entrou em "
        f"${open_pos['cost_usd']:.2f} mas hedge oposto NÃO apareceu antes do close. "
        f"Em produção: exposto a divergência (~6%).\033[0m"
    )
    _log_paper_csv({
        "timestamp_iso": now_iso,
        "round_ts": open_pos["round_ts"],
        "round_clock": "15:00",
        "event": "NAKED_AT_CLOSE",
        "arb_id": open_pos["arb_id"],
        "contracts": round(open_pos["contracts"], 4),
        "cost_usd": round(open_pos["cost_usd"], 4),
        "profit_pct_at_event": round(open_pos["profit_pct_at_entry"], 4),
        **{k: (round(v, 4) if isinstance(v, (int, float)) else "")
           for k, v in (open_pos.get("snap") or {}).items()},
    })
    state["paper_open"] = None


# ═══════════════════════════════════════════════════════════════════════════════
# SIMULAÇÃO DE LIQUIDEZ
# ═══════════════════════════════════════════════════════════════════════════════
def _walk_bids_as_ask(bid_levels, contracts_needed: float):
    filled, cost = 0.0, 0.0
    for bid_price, size in bid_levels:
        ask_price = 1.0 - bid_price
        can_fill  = min(size, contracts_needed - filled)
        filled   += can_fill
        cost     += can_fill * ask_price
        if filled >= contracts_needed - 1e-9:
            break
    sufficient = filled >= contracts_needed - 1e-9
    avg_price  = cost / filled if filled > 0 else None
    return avg_price, filled, cost, sufficient

def _walk_asks(asks_sorted: list, contracts_needed: float):
    filled, cost = 0.0, 0.0
    for price, size in asks_sorted:
        can_fill  = min(size, contracts_needed - filled)
        filled   += can_fill
        cost     += can_fill * price
        if filled >= contracts_needed - 1e-9:
            break
    sufficient = filled >= contracts_needed - 1e-9
    avg_price  = cost / filled if filled > 0 else None
    return avg_price, filled, cost, sufficient

def simulate_arb(arb_id: int):
    k_y_ask = state["kalshi_yes_ask"]
    k_y_bid = state["kalshi_yes_bid"]
    p_y_ask = state["poly_yes_ask"]
    p_n_ask = state["poly_no_ask"]

    if arb_id == 1:
        if k_y_ask is None or p_n_ask is None:
            return None
        base_cost = k_y_ask + p_n_ask
        if base_cost <= 0:
            return None
        contracts = BUDGET / base_cost
        poly_tok_no = state["poly_token_no"]
        k_fee_est = calc_kalshi_fee(k_y_ask, contracts)
        p_fee_est = calc_poly_fee_usdc(p_n_ask, contracts, poly_tok_no)
        contracts = BUDGET / (base_cost + (k_fee_est + p_fee_est) / max(contracts, 1e-9))

        no_bids_sorted = sorted(kalshi_orderbook["no"].items(), reverse=True)
        k_avg, k_filled, k_cost, k_ok = _walk_bids_as_ask(no_bids_sorted, contracts)
        p_avg, p_filled, p_cost, p_ok = _walk_asks(poly_book["no_asks"], contracts)

        real_k_p = k_avg if k_avg else k_y_ask
        real_p_p = p_avg if p_avg else p_n_ask
        filled_ok = min(k_filled, p_filled)
        real_cost = (k_cost + p_cost
                     + calc_kalshi_fee(real_k_p, k_filled)
                     + calc_poly_fee_usdc(real_p_p, p_filled, poly_tok_no))
        profit = filled_ok - real_cost

        return dict(id=1, label_a="YES Kalshi", label_b="NO  Poly  ",
                    price_a=k_y_ask, price_b=p_n_ask, avg_a=k_avg, avg_b=p_avg,
                    contracts=contracts, filled_a=k_filled, filled_b=p_filled,
                    ok_a=k_ok, ok_b=p_ok, filled_ok=filled_ok,
                    real_cost=real_cost, profit=profit,
                    kalshi_side="yes", kalshi_price=k_y_ask,
                    poly_token=state["poly_token_no"], poly_price=p_n_ask)
    else:
        k_n_ask = (1.0 - k_y_bid) if k_y_bid is not None else None
        if p_y_ask is None or k_n_ask is None:
            return None
        base_cost = p_y_ask + k_n_ask
        if base_cost <= 0:
            return None
        contracts = BUDGET / base_cost
        poly_tok_yes = state["poly_token_yes"]
        k_fee_est = calc_kalshi_fee(k_n_ask, contracts)
        p_fee_est = calc_poly_fee_usdc(p_y_ask, contracts, poly_tok_yes)
        contracts = BUDGET / (base_cost + (k_fee_est + p_fee_est) / max(contracts, 1e-9))

        yes_bids_sorted = sorted(kalshi_orderbook["yes"].items(), reverse=True)
        k_avg, k_filled, k_cost, k_ok = _walk_bids_as_ask(yes_bids_sorted, contracts)
        p_avg, p_filled, p_cost, p_ok = _walk_asks(poly_book["yes_asks"], contracts)

        real_k_p = k_avg if k_avg else k_n_ask
        real_p_p = p_avg if p_avg else p_y_ask
        filled_ok = min(k_filled, p_filled)
        real_cost = (k_cost + p_cost
                     + calc_kalshi_fee(real_k_p, k_filled)
                     + calc_poly_fee_usdc(real_p_p, p_filled, poly_tok_yes))
        profit = filled_ok - real_cost

        return dict(id=2, label_a="YES Poly  ", label_b="NO  Kalshi",
                    price_a=p_y_ask, price_b=k_n_ask, avg_a=p_avg, avg_b=k_avg,
                    contracts=contracts, filled_a=p_filled, filled_b=k_filled,
                    ok_a=p_ok, ok_b=k_ok, filled_ok=filled_ok,
                    real_cost=real_cost, profit=profit,
                    kalshi_side="no", kalshi_price=k_n_ask,
                    poly_token=state["poly_token_yes"], poly_price=p_y_ask)


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENTE POLY
# ═══════════════════════════════════════════════════════════════════════════════
_poly_client = None

def _init_poly_client():
    if not POLY_SDK_OK:
        return None
    if not POLY_PRIVATE_KEY:
        print("[WARN] PRIVATE_KEY não configurado. Ordens Poly desabilitadas.")
        return None
    try:
        creds = ApiCreds(
            api_key=POLY_API_KEY,
            api_secret=POLY_API_SECRET,
            api_passphrase=POLY_API_PASS,
        )
        client = ClobClient(
            host=POLY_CLOB_HOST,
            chain_id=POLY_CHAIN_ID,
            key=POLY_PRIVATE_KEY,
            creds=creds,
            signature_type=2,
            funder=POLY_PROXY_WALLET,
        )
        print("[INFO] Cliente Poly v2 pronto (sig_type=2)")
        return client
    except Exception as e:
        print(f"[ERRO] init Poly client: {e}")
        return None

def get_poly_client():
    global _poly_client
    if _poly_client is None:
        _poly_client = _init_poly_client()
    return _poly_client


# ═══════════════════════════════════════════════════════════════════════════════
# ORDEM KALSHI
# ═══════════════════════════════════════════════════════════════════════════════
async def place_kalshi_order(private_key, ticker: str, side: str,
                             price_float: float, contracts: int) -> dict:
    path = "/trade-api/v2/portfolio/orders"
    price_cents = max(1, min(99, round(price_float * 100)))
    client_oid  = str(uuid.uuid4())

    body = {
        "ticker":          ticker,
        "action":          "buy",
        "side":            side,
        "count":           contracts,
        "type":            "limit",
        "time_in_force":   "fill_or_kill",
        f"{side}_price":   price_cents,
        "client_order_id": client_oid,
    }

    headers = make_auth_headers(private_key, "POST", path)
    try:
        if HTTPX_OK:
            http = await _get_http_client()
            resp = await http.post(
                KALSHI_BASE_URL + path, headers=headers, json=body, timeout=5.0,
            )
            status_code = resp.status_code
            resp_text   = resp.text
            resp_json   = resp.json() if status_code == 201 else None
        else:
            loop = asyncio.get_event_loop()
            _resp = await loop.run_in_executor(
                None,
                lambda: requests.post(KALSHI_BASE_URL + path, headers=headers,
                                      json=body, timeout=5)
            )
            status_code = _resp.status_code
            resp_text   = _resp.text
            resp_json   = _resp.json() if status_code == 201 else None

        if status_code == 201:
            order  = resp_json.get("order", {})
            status = (order.get("status") or "").lower()
            filled = int(float(order.get("fill_count_fp") or order.get("filled_count") or 0))
            if status == "resting":
                return {"ok": False,
                        "error": f"FOK voltou resting (order_id={order.get('order_id')})",
                        "order_id": order.get("order_id"), "status": status, "side": side}
            # FOK é tudo-ou-nada: se filled < contracts, é falha crítica
            if status in ("executed", "filled", "matched") and filled < contracts:
                return {"ok": False,
                        "error": f"FILL PARCIAL Kalshi! pedi {contracts}, preencheu {filled}",
                        "order_id": order.get("order_id"), "status": status,
                        "filled_count": filled, "side": side, "partial": True}
            if status in ("executed", "filled", "matched") and filled >= contracts:
                return {"ok": True, "order_id": order.get("order_id"),
                        "client_id": client_oid, "status": status,
                        "filled_count": filled, "side": side,
                        "price": price_cents, "contracts": contracts}
            return {"ok": False,
                    "error": f"Status inesperado: '{status}' fill_count={filled}",
                    "order_id": order.get("order_id"), "side": side}
        return {"ok": False, "error": f"HTTP {status_code}: {resp_text[:200]}",
                "side": side}
    except Exception as e:
        return {"ok": False, "error": str(e), "side": side}


async def fetch_kalshi_order(private_key, order_id: str) -> dict:
    path    = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = make_auth_headers(private_key, "GET", path)
    try:
        if HTTPX_OK:
            http = await _get_http_client()
            resp = await http.get(
                KALSHI_BASE_URL + path, headers=headers, timeout=5.0,
            )
            status_code = resp.status_code
            resp_json   = resp.json() if status_code == 200 else None
        else:
            loop = asyncio.get_event_loop()
            _resp = await loop.run_in_executor(
                None,
                lambda: requests.get(KALSHI_BASE_URL + path, headers=headers, timeout=5)
            )
            status_code = _resp.status_code
            resp_json   = _resp.json() if status_code == 200 else None

        if status_code == 200:
            return {"ok": True, "order": resp_json.get("order", {})}
        return {"ok": False, "error": f"HTTP {status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def fetch_kalshi_balance(private_key) -> float | None:
    """Retorna saldo disponível na Kalshi em USD (cents / 100). None em caso de erro."""
    path = "/trade-api/v2/portfolio/balance"
    headers = make_auth_headers(private_key, "GET", path)
    try:
        loop = asyncio.get_event_loop()
        if HTTPX_OK:
            http = await _get_http_client()
            resp = await http.get(KALSHI_BASE_URL + path, headers=headers, timeout=5.0)
            status_code = resp.status_code
            resp_json   = resp.json() if status_code == 200 else None
        else:
            _resp = await loop.run_in_executor(
                None, lambda: requests.get(KALSHI_BASE_URL + path, headers=headers, timeout=5)
            )
            status_code = _resp.status_code
            resp_json   = _resp.json() if status_code == 200 else None

        if status_code == 200 and resp_json:
            cents = int(resp_json.get("balance", 0))
            return cents / 100.0
        return None
    except Exception as e:
        log_trade(f"[BALANCE KALSHI ERRO] {e}")
        return None


async def fetch_poly_balance() -> float | None:
    """Retorna saldo disponível na Polymarket em USD. None em caso de erro."""
    if not POLY_SDK_OK:
        return None
    client = get_poly_client()
    if client is None:
        return None
    try:
        loop = asyncio.get_event_loop()
        bal = await loop.run_in_executor(
            None,
            lambda: client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type="COLLATERAL")
            )
        )
        return int(bal.get("balance", 0)) / 1e6
    except Exception as e:
        log_trade(f"[BALANCE POLY ERRO] {e}")
        return None


async def balance_monitor_task(private_key):
    """Verifica saldos durante a janela pausada (primeiros 60s) de cada rodada.
    Congela execução se qualquer saldo estiver abaixo do BUDGET."""
    last_round_checked = -1
    while True:
        try:
            seconds_into = int(time.time()) % 900
            current_round = int(time.time()) // 900

            if seconds_into < 60 and current_round != last_round_checked:
                poly_bal   = await fetch_poly_balance()
                kalshi_bal = await fetch_kalshi_balance(private_key)

                state["balance_poly"]       = poly_bal
                state["balance_kalshi"]     = kalshi_bal
                state["balance_last_check"] = time.time()

                poly_ok   = (poly_bal   is not None and poly_bal   >= BUDGET)
                kalshi_ok = (kalshi_bal is not None and kalshi_bal >= BUDGET)
                frozen    = not (poly_ok and kalshi_ok)

                state["balance_frozen"] = frozen
                last_round_checked = current_round

                if frozen:
                    reasons = []
                    if poly_bal is None:
                        reasons.append("Poly: erro ao consultar")
                    elif not poly_ok:
                        reasons.append(f"Poly: ${poly_bal:.2f} < ${BUDGET:.2f}")
                    if kalshi_bal is None:
                        reasons.append("Kalshi: erro ao consultar")
                    elif not kalshi_ok:
                        reasons.append(f"Kalshi: ${kalshi_bal:.2f} < ${BUDGET:.2f}")
                    log_trade(f"[BALANCE] CONGELADO — {' | '.join(reasons)}")
                else:
                    log_trade(
                        f"[BALANCE] OK — Poly: ${poly_bal:.2f}  Kalshi: ${kalshi_bal:.2f}"
                    )

            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log_trade(f"[BALANCE MONITOR ERRO] {e}")
            await asyncio.sleep(10)


# ═══════════════════════════════════════════════════════════════════════════════
# ORDEM POLYMARKET — LIMIT FOK
# ═══════════════════════════════════════════════════════════════════════════════
def _decimal_places(d: Decimal) -> int:
    d = d.normalize()
    exp = d.as_tuple().exponent
    return max(0, -exp) if isinstance(exp, int) else 0


def _align_size_and_price(target_net_shares: float, price_input: float,
                          tick_size_str: str, min_size_int: int,
                          exact_int_mode: bool = False,
                          token_id: str = None):
    """
    Retorna (size, price) para ordem Polymarket respeitando TODAS as regras:
      1. price é múltiplo de tick_size
      2. size × price tem no máximo 2 casas decimais (limite USDC da Poly)
      3. size >= min_size
      4. (exact_int_mode) size INTEIRO == target_net_shares — NÃO faz gross-up.
         Isto significa que o fee Poly (~0.3 shares em 7) é cobrado "dentro"
         do target, ficando descoberto apenas pela fração do fee, não por
         1 contrato inteiro. Este é o modo correto quando a perna Kalshi é
         inteira e queremos simetria com a quantidade real da Kalshi.
      5. (exact_int_mode=False) size líquido (após fee) >= target_net_shares.
         Compra bruto suficiente para que (bruto - fee) cubra integralmente
         a perna Kalshi — usado apenas quando grain permite fração (ex: price
         0.x com 1 casa → size pode ter até 1 casa decimal).

    REGRA: no modo net-hedge (exact_int_mode=False), size é arredondado pra CIMA.
            no modo exact_int_mode, size é EXATAMENTE target (truncado se vier
            como float tipo 7.12 — chamador já deve ter passado inteiro).
    """
    tick = Decimal(tick_size_str)

    # 1) Alinha preço ao tick real (ROUND_UP = paga um tick a mais no FOK)
    price_dec = Decimal(str(price_input))
    price_aligned = (price_dec / tick).quantize(Decimal("1"), rounding=ROUND_UP) * tick
    price_places = _decimal_places(price_aligned)

    # 2) Determina granularidade de size para que size × price tenha ≤ 2 casas.
    #    Se price tem N casas, size precisa ter no máximo (2-N) casas.
    #    Se price_places >= 2 → size DEVE ser inteiro.
    if price_places >= 2:
        size_grain = Decimal("1")
    else:
        size_grain = Decimal(1).scaleb(-(2 - price_places))

    min_size = Decimal(str(min_size_int))

    # 3) Calcula size conforme o modo
    if exact_int_mode:
        # Modo "inteiro exato": size = target arredondado pra BAIXO pro inteiro.
        # Chamador tipicamente já passa inteiro (ex: contracts_int=7) — ROUND_DOWN
        # é só defesa caso venha 7.0001 ou algo assim. Força size_grain=1.
        size_grain = Decimal("1")
        target_dec = Decimal(str(target_net_shares))
        size_aligned = target_dec.quantize(Decimal("1"), rounding=ROUND_DOWN)
    else:
        # Modo "net-hedge": considera fee Poly e compra bruto suficiente
        # para que (bruto - fee) >= target. Só faz sentido quando size_grain
        # é fracionário (price com < 2 casas decimais). Usa o coef DINÂMICO
        # do mercado (cache populado por _populate_poly_fee_cache) com
        # POLY_FEE_SAFETY_MULT já embutido em get_poly_fee_coef().
        p = float(price_aligned)
        coef = get_poly_fee_coef(token_id)
        fee_pct = coef * p * (1.0 - p)
        gross_ideal = Decimal(str(target_net_shares / max(1e-9, 1.0 - fee_pct)))
        # ROUND_UP ao grain — garante cobertura da perna Kalshi
        size_aligned = (gross_ideal / size_grain).quantize(
            Decimal("1"), rounding=ROUND_UP
        ) * size_grain

    # 4) Respeita mínimo (arredondando pra cima no grain)
    if size_aligned < min_size:
        size_aligned = (min_size / size_grain).quantize(
            Decimal("1"), rounding=ROUND_UP
        ) * size_grain

    # 5) VALIDAÇÃO FINAL — maker amount (size × price) deve ter ≤ 2 casas.
    #    Se violar, AUMENTA size em 1 grain (nunca diminui — só somamos grain
    #    o que é seguro: no exact_int mode grain=1 então "aumentar 1 share" é
    #    melhor do que rejeitar a ordem; na prática price alinhado ao tick não
    #    costuma gerar violação).
    for _ in range(10):
        maker = size_aligned * price_aligned
        # Usa format() pra ver casas REAIS (sem normalize que esconde zeros)
        maker_str = format(maker, 'f')
        decimals_maker = len(maker_str.split('.')[1].rstrip('0')) if '.' in maker_str else 0
        if decimals_maker <= 2:
            break
        size_aligned += size_grain
    else:
        raise ValueError(
            f"Não consegui alinhar após 10 tentativas: "
            f"size={size_aligned} price={price_aligned} "
            f"maker={size_aligned * price_aligned}"
        )

    return size_aligned, price_aligned


async def _populate_poly_fee_cache(client, token_id: str) -> None:
    """Popula _poly_fee_rate_cache[token_id] consultando o mercado V2.

    Best-effort: se a SDK não expuser get_clob_market_info ou get_market,
    apenas loga e segue com o fallback POLY_FEE_COEF. Falha de rede também
    cai pro fallback (com POLY_FEE_SAFETY_MULT já protegendo).

    Cache hit no token: nada a fazer.
    """
    if token_id in _poly_fee_rate_cache:
        return
    loop = asyncio.get_event_loop()
    try:
        # Mapear token → condition_id. Em V2 a SDK costuma expor get_market.
        get_market_fn = getattr(client, "get_market", None)
        get_info_fn   = getattr(client, "get_clob_market_info", None)
        if not (get_market_fn and get_info_fn):
            # SDK ainda não expõe — usa fallback definitivamente
            _poly_fee_rate_cache[token_id] = POLY_FEE_COEF
            return

        market = await loop.run_in_executor(None, lambda: get_market_fn(token_id))
        cid = (market or {}).get("condition_id") or (market or {}).get("conditionId")
        if not cid:
            _poly_fee_rate_cache[token_id] = POLY_FEE_COEF
            return

        info = await loop.run_in_executor(None, lambda: get_info_fn(cid))
        fd = (info or {}).get("fd") or {}
        rate = float(fd.get("r", 0) or 0)
        exp  = int(fd.get("e", 0) or 0)
        # Documentação V2: fee = C * feeRate * p * (1-p). C costuma ser 4
        # (pico em p=0.5 normaliza a maioria das integrações). Convertendo
        # para o formato do bot: coef = 4 * rate * 10^(-exp).
        scale = (10 ** exp) if exp > 0 else 1
        coef = (4.0 * rate / scale) if scale else POLY_FEE_COEF
        if coef <= 0:
            coef = POLY_FEE_COEF
        _poly_fee_rate_cache[token_id] = coef
        log_trade(
            f"[POLY FEE V2] token={token_id[:10]}... "
            f"r={rate} e={exp} coef_real={coef:.4f} "
            f"(safety_mult={POLY_FEE_SAFETY_MULT})"
        )
    except Exception as e:
        _poly_fee_rate_cache[token_id] = POLY_FEE_COEF
        log_trade(f"[POLY FEE V2 WARN] fallback p/ {token_id[:10]}...: {e}")


async def _get_tick_size_cached(client, token_id: str) -> str:
    """
    Retorna o tick_size de um token Polymarket, com cache global.

    MOTIVO: o tick_size é uma propriedade imutável do mercado. Chamar
    client.get_tick_size() a cada execução desperdiça 80-200ms de
    latência HTTP que trava a thread principal no caminho crítico.

    Cache hit: ~0.001ms. Miss: ~100ms (só na primeira ordem do token).

    Observação: o dicionário `_tick_size_cache` é global do módulo.
    Como cada rodada tem tokens novos, o cache cresce durante a sessão,
    mas isso é <1KB mesmo após 100 rodadas — não precisa de eviction.
    """
    if token_id in _tick_size_cache:
        return _tick_size_cache[token_id]
    loop = asyncio.get_event_loop()
    tick_size_str = await loop.run_in_executor(
        None, lambda: client.get_tick_size(token_id)
    )
    _tick_size_cache[token_id] = tick_size_str
    log_trade(f"[TICK CACHE MISS] token={token_id[:10]}... tick={tick_size_str} (cached)")
    return tick_size_str


def _build_poly_order_args(token_id: str, price: float, size: float, side):
    """Cria OrderArgs com builder_code se POLY_BUILDER_CODE estiver setado.
    Defensivo: tenta múltiplos nomes de campo (builder_code / builderCode /
    builder), e se nenhum funcionar, cria sem builder code."""
    if POLY_BUILDER_CODE:
        for kw in ("builder_code", "builderCode", "builder"):
            try:
                return OrderArgs(
                    token_id=token_id, price=price, size=size, side=side,
                    **{kw: POLY_BUILDER_CODE},
                )
            except TypeError:
                continue
    return OrderArgs(token_id=token_id, price=price, size=size, side=side)


async def place_poly_fok(token_id: str, price: float,
                         target_net_shares: float,
                         exact_int_size: bool = False) -> dict:
    if not POLY_SDK_OK:
        return {"ok": False, "error": "SDK não instalado", "matched_shares": 0}

    client = get_poly_client()
    if client is None:
        return {"ok": False, "error": "Cliente Poly não inicializado", "matched_shares": 0}

    loop = asyncio.get_event_loop()
    try:
        tick_size_str = await _get_tick_size_cached(client, token_id)
        # CLOB V2: popula o coef de fee real do mercado antes de dimensionar.
        # Cache miss faz uma consulta única; subsequentes são instantâneas.
        await _populate_poly_fee_cache(client, token_id)

        size_fit, price_fit = _align_size_and_price(
            target_net_shares, price, tick_size_str, POLY_MIN_SIZE,
            exact_int_mode=exact_int_size,
            token_id=token_id,
        )

        if price_fit >= Decimal("1.0000") or price_fit <= Decimal("0"):
            return {"ok": False, "error": f"Preço inválido: {price_fit}", "matched_shares": 0}

        size_final  = float(size_fit)
        price_final = float(price_fit)
        amount_usdc = float(size_fit * price_fit)

        # Validação real — format() sem normalize para flagrar casas ocultas
        maker_str = format(size_fit * price_fit, 'f')
        maker_dec = len(maker_str.split('.')[1].rstrip('0')) if '.' in maker_str else 0
        if maker_dec > 2:
            return {"ok": False,
                    "error": f"maker amount {maker_str} tem {maker_dec} casas (>2)",
                    "matched_shares": 0}
        if _decimal_places(size_fit) > 4:
            return {"ok": False,
                    "error": f"size {size_fit} > 4 casas decimais",
                    "matched_shares": 0}

        fee_shares_est = calc_poly_fee_shares(price_final, size_final, token_id)
        net_est        = size_final - fee_shares_est
        hedge_delta    = net_est - target_net_shares
        is_exact_int   = (size_fit == size_fit.to_integral_value())

        log_trade(
            f"[POLY FOK TRY] token={token_id[:10]}... "
            f"size={size_final} price={price_final} "
            f"maker_usdc={amount_usdc:.2f} "
            f"target_net={target_net_shares} "
            f"mode={'INT_FORCED' if is_exact_int else 'DECIMAL_HEDGE'} "
            f"fee_shares={fee_shares_est:.4f} "
            f"net_est={net_est:.4f} hedge_delta={hedge_delta:+.4f}"
        )

        order_args = _build_poly_order_args(
            token_id=token_id,
            price=price_final,
            size=size_final,
            side=Side.BUY,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size_str)

        resp = await loop.run_in_executor(
            None,
            lambda: client.create_and_post_order(
                order_args, options=options, order_type=OrderType.FOK
            )
        )

        order_id = resp.get("orderID") or resp.get("id", "")
        status   = (resp.get("status") or "").lower()
        error    = resp.get("errorMsg") or resp.get("error", "")

        if error:
            return {"ok": False, "error": error, "order_id": order_id, "matched_shares": 0}

        if status in ("matched", "filled"):
            # ── v17 FIX #2: parse robusto de matched_shares ───────────────
            # Os campos que a Poly CLOB retorna variam entre versões:
            #   - size_matched  → shares preenchidas (preferido)
            #   - filled_size   → shares preenchidas (alternativo)
            #   - takingAmount  → pode ser USDC (não shares!) em algumas respostas
            #
            # Bug antigo: usar takingAmount como shares resultava em valores
            # como 7.18, 7.26, 7.54 quando preenchimento real era 7 exatos.
            # Isso levava a: (a) reconcile achar que houve sobra; (b) unwind
            # tentar vender mais shares do que tinha; (c) tolerância inflada.
            #
            # Estratégia: priorizar campos em shares. Só cair no takingAmount
            # como fallback e, se valor for maior que size_submitted, CAP.
            raw_size_matched   = resp.get("size_matched")
            raw_filled_size    = resp.get("filled_size")
            raw_taking_amount  = resp.get("takingAmount")

            if raw_size_matched is not None:
                matched_shares = float(raw_size_matched)
                matched_src    = "size_matched"
            elif raw_filled_size is not None:
                matched_shares = float(raw_filled_size)
                matched_src    = "filled_size"
            elif raw_taking_amount is not None:
                # takingAmount pode vir em USDC — se > size_submitted, é suspeito
                matched_shares = float(raw_taking_amount)
                matched_src    = "takingAmount(?)"
            else:
                matched_shares = size_final
                matched_src    = "fallback=size_final"

            # Cap defensivo: FOK com BUY não pode preencher MAIS que pedido.
            # Se algum campo retornar valor maior, trunca pra size_submitted.
            if matched_shares > size_final + 0.0001:
                log_trade(
                    f"[POLY MATCH CAP] {matched_src}={matched_shares} > "
                    f"size_submitted={size_final}. Usando size_submitted. "
                    f"Raw: sm={raw_size_matched} fs={raw_filled_size} "
                    f"ta={raw_taking_amount}"
                )
                matched_shares = size_final

            log_trade(
                f"[POLY MATCH] matched={matched_shares} src={matched_src} "
                f"(raw: sm={raw_size_matched} fs={raw_filled_size} "
                f"ta={raw_taking_amount} size_sent={size_final})"
            )

            # VALIDAÇÃO CRÍTICA: FOK é tudo-ou-nada. Se matched < target_net_shares,
            # a Poly preencheu parcial (não deveria acontecer com FOK, mas já aconteceu).
            # Não podemos aceitar ordem parcial porque descobre a perna Kalshi.
            if matched_shares < target_net_shares - 0.0001:
                log_trade(
                    f"[POLY FILL PARCIAL!] pedi {size_final} (target_net={target_net_shares}) "
                    f"preencheu {matched_shares} — TRATANDO COMO FALHA"
                )
                return {
                    "ok": False,
                    "error": f"FILL PARCIAL: matched={matched_shares} < target_net={target_net_shares}",
                    "order_id": order_id,
                    "matched_shares": matched_shares,
                    "partial": True,
                }
            return {
                "ok":             True,
                "order_id":       order_id,
                "status":         status,
                "price":          price_final,
                "amount_usdc":    amount_usdc,
                "matched_shares": matched_shares,
                "size_submitted": size_final,
            }

        if status in ("live", "open"):
            try:
                await loop.run_in_executor(None, lambda: client.cancel_order(order_id))
                log_trade(f"[POLY FOK] cancelou resting {order_id}")
            except Exception as e:
                log_trade(f"[POLY FOK] falha ao cancelar {order_id}: {e}")
            return {"ok": False,
                    "error": f"FOK virou resting (status={status}), cancelado",
                    "order_id": order_id, "matched_shares": 0}

        return {"ok": False,
                "error": f"Status inesperado: '{status}' resp={resp}",
                "order_id": order_id, "matched_shares": 0}

    except Exception as e:
        return {"ok": False, "error": str(e), "matched_shares": 0}


async def fetch_poly_order(order_id: str) -> dict:
    if not POLY_SDK_OK or not order_id:
        return {"ok": False, "error": "sem order_id"}
    client = get_poly_client()
    if client is None:
        return {"ok": False, "error": "sem cliente"}
    loop = asyncio.get_event_loop()
    try:
        order = await loop.run_in_executor(None, lambda: client.get_order(order_id))
        return {"ok": True, "order": order}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# v16: GUARDA DE POSIÇÃO DESCOBERTA + HEDGE LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def _current_kalshi_ask_for(side: str):
    """
    Retorna o melhor ask Kalshi pro lado desejado, ou None se orderbook vazio.
    - side='yes': ask YES = 1 - max_bid_NO (já calculado em state['kalshi_yes_ask'])
    - side='no' : ask NO  = 1 - max_bid_YES
    """
    if side == "yes":
        return state["kalshi_yes_ask"]
    k_y_bid = state["kalshi_yes_bid"]
    return (1.0 - k_y_bid) if k_y_bid is not None else None


# ── v17 FIX #5: slippage Kalshi adaptativo à profundidade do book ─────────
#   Antes: KALSHI_SLIPPAGE_CENTS era constante em 2¢. Em books rasos
#   (primeiro nível com pouco size), 2¢ pode atravessar níveis sem
#   necessidade e pagar caro demais. Em books profundos, 2¢ pode não
#   ser suficiente se o book mover rápido.
#
#   Nova lógica: olha o book real (do lado inverso — onde estão os bids
#   que formam nosso ask) e calcula quantos ¢ precisamos pra garantir fill
#   dos contracts_needed:
#     - Se o primeiro nível sozinho cobre a qty: 1¢ de buffer
#     - Se precisa do 2º nível: usa a diferença de preço + 1¢ buffer
#     - Se precisa do 3º+ nível: idem
#     - Piso: 1¢   (sempre paga um tick extra pra evitar ficar atrás)
#     - Teto: KALSHI_SLIPPAGE_MAX_CENTS (config)
KALSHI_SLIPPAGE_MAX_CENTS = 4  # teto — nunca paga mais que isso em slippage

def _compute_kalshi_slippage_cents(side: str, contracts_needed: int) -> int:
    """
    Retorna o slippage em ¢ ideal pra fill do `contracts_needed` no `side`.

    IMPORTANTE: o "ask" em Kalshi é derivado dos bids do LADO OPOSTO.
      - Pra comprar YES, precisamos cruzar bids de NO (NO_price = 1 - YES_price).
      - Pra comprar NO,  precisamos cruzar bids de YES.

    O livro `kalshi_orderbook[opposite_side]` tem os bids em preço NATIVO do
    opposite_side. Pra simular ask do side desejado, ordenamos bids do
    opposite DESC e convertemos (ask_price = 1 - bid_price).
    """
    opposite = "no" if side == "yes" else "yes"
    book     = kalshi_orderbook.get(opposite, {})
    if not book:
        # Sem book visível: usa o default antigo (2¢) como fallback seguro
        return KALSHI_SLIPPAGE_CENTS

    # Ordena bids do lado oposto DESC (melhor bid primeiro)
    bids_sorted = sorted(book.items(), reverse=True)

    # Converte em níveis de ask (ask = 1 - bid) e acumula size
    best_ask_price = 1.0 - bids_sorted[0][0]
    cumulative     = 0.0
    worst_needed   = best_ask_price  # pior preço necessário pra fill

    for bid_price, size in bids_sorted:
        ask_price   = 1.0 - bid_price
        cumulative += size
        worst_needed = ask_price
        if cumulative >= contracts_needed - 1e-9:
            break

    # Se não consegue cobrir com todo o book, usa o teto
    if cumulative < contracts_needed - 1e-9:
        return KALSHI_SLIPPAGE_MAX_CENTS

    # Slippage = diferença entre pior preço e melhor ask, + 1¢ buffer.
    # IMPORTANTE: preços Kalshi são em ticks de 1¢, então convertemos pra
    # cents inteiros ANTES de subtrair. Sem isso, ruído de ponto flutuante
    # (ex: 0.40 - 0.38 == 0.02000000018) inflava diff_cents via math.ceil.
    best_ask_cents = round(best_ask_price * 100)
    worst_cents    = round(worst_needed * 100)
    diff_cents     = worst_cents - best_ask_cents
    slip = max(1, diff_cents + 1)
    return min(slip, KALSHI_SLIPPAGE_MAX_CENTS)


def _register_uncovered(*, sim, p_token, p_price, k_side, ticker, shares,
                        reason_tag: str):
    """
    Registra uma posição Poly descoberta para hedge posterior.
    Deve ser chamado em DESCOBERTO_CRITICO e PARTIAL_DESCOBERTO.
    """
    if shares is None or shares < 1:
        log_trade(f"[UNCOVERED IGNORADO] shares={shares} < 1")
        return
    try:
        contracts_needed = int(math.floor(float(shares)))
    except Exception:
        contracts_needed = 0
    if contracts_needed < 1:
        log_trade(f"[UNCOVERED IGNORADO] contracts_needed={contracts_needed}")
        return

    unc = {
        "arb_id":              sim.get("id") if isinstance(sim, dict) else None,
        "reason_tag":          reason_tag,
        "round_ts":            _current_round_ts(),
        "kalshi_ticker":       ticker,
        "kalshi_side_needed":  k_side,
        "poly_token":          p_token,
        "poly_shares":         contracts_needed,
        "poly_price_paid":     float(p_price),
        "created_at":          time.time(),
        "attempts":            0,
        "last_attempt_ts":     0.0,
    }
    state["uncovered"] = unc

    # ── v17 FIX (belt-and-suspenders): trava a rodada IMEDIATAMENTE ao
    # registrar descoberta. Garante que, mesmo se o hedge_loop limpar
    # state["uncovered"] no meio da rodada (hedge bem-sucedido), nenhuma
    # nova arb seja disparada até o próximo ciclo de 15min. A checagem
    # em execute_arb é `round_exec_count >= MAX_EXECS_PER_ROUND`.
    state["round_exec_count"] = max(
        state["round_exec_count"], MAX_EXECS_PER_ROUND
    )

    breakeven = 1.0 - unc["poly_price_paid"]
    max_acc   = breakeven + UNCOVERED_HEDGE_MAX_LOSS_CENTS / 100.0
    log_trade(
        f"[UNCOVERED REGISTRADO] motivo={reason_tag} | "
        f"{unc['poly_shares']} Poly shares @ {unc['poly_price_paid']:.4f} | "
        f"precisa comprar Kalshi {k_side.upper()} | "
        f"breakeven ≤ {breakeven:.4f}, max aceitável {max_acc:.4f} "
        f"(tol={UNCOVERED_HEDGE_MAX_LOSS_CENTS}¢) | "
        "NOVAS ARBS BLOQUEADAS ATÉ HEDGE OU FIM DA RODADA | "
        f"round_exec_count travado em {state['round_exec_count']}"
    )


def _is_blocked_by_uncovered() -> bool:
    """Retorna True se há posição descoberta ativa bloqueando novas arbs."""
    return UNCOVERED_BLOCK_NEW_ARBS and state.get("uncovered") is not None


async def uncovered_hedge_loop(private_key):
    """
    Task paralela. Enquanto state['uncovered'] estiver populado:
      1. Observa o ask Kalshi do lado faltante (atualizado em tempo real por WS).
      2. Só dispara FOK quando ask ≤ breakeven + UNCOVERED_HEDGE_MAX_LOSS_CENTS.
      3. Rate-limita tentativas (UNCOVERED_HEDGE_RETRY_S) pra não sobrecarregar.
      4. Quando rodada terminar sem hedge, aceita a posição aberta e libera
         novas arbs na próxima rodada.
    """
    while True:
        try:
            await asyncio.sleep(1.0)
            unc = state.get("uncovered")
            if unc is None:
                continue

            # ── v17 FIX #4: respeita o flag de unwind em andamento ──────
            # Enquanto o execute_arb está tentando unwind a posição Poly,
            # o hedge_loop NÃO dispara compra Kalshi. Evita race onde o
            # unwind vende Poly E o hedge compra Kalshi simultaneamente,
            # resultando em posição Kalshi "solta" sem Poly pra fazer par.
            if state.get("unwind_in_progress", False):
                # Log de debug raro (a cada N segundos)
                if unc.get("attempts", 0) % 10 == 0:
                    log_trade(
                        f"[HEDGE AGUARDA UNWIND] unwind_in_progress=True — "
                        f"hedge loop em standby até unwind concluir"
                    )
                unc["attempts"] = unc.get("attempts", 0) + 1
                continue

            # ── Rodada acabou → contrato vai resolver, não dá mais pra hedgear
            if _current_round_ts() != unc["round_ts"]:
                log_trade(
                    f"[HEDGE EXPIRADO] Rodada terminou sem hedge. "
                    f"{unc['poly_shares']} Poly shares ficarão abertas até "
                    f"a resolução do contrato (BTC sobe/desce). "
                    f"Limpando flag — novas arbs LIBERADAS na próxima rodada."
                )
                state["uncovered"] = None
                continue

            # ── Rate-limit
            now = time.time()
            if now - unc["last_attempt_ts"] < UNCOVERED_HEDGE_RETRY_S:
                continue

            current_ask = _current_kalshi_ask_for(unc["kalshi_side_needed"])
            if current_ask is None:
                unc["last_attempt_ts"] = now
                continue

            breakeven      = 1.0 - unc["poly_price_paid"]
            max_acceptable = breakeven + UNCOVERED_HEDGE_MAX_LOSS_CENTS / 100.0

            if current_ask > max_acceptable:
                unc["last_attempt_ts"] = now
                unc["attempts"]       += 1
                if unc["attempts"] % UNCOVERED_HEDGE_LOG_EVERY_N == 1:
                    log_trade(
                        f"[HEDGE AGUARDA #{unc['attempts']}] "
                        f"ask={current_ask:.4f} > max={max_acceptable:.4f} "
                        f"(breakeven={breakeven:.4f})"
                    )
                continue

            # ── Preço aceitável. Envia FOK com slippage do tick Kalshi, mas
            #    SEM ultrapassar o max_acceptable (breakeven + tolerância).
            price_send = min(
                max_acceptable,
                current_ask + KALSHI_SLIPPAGE_CENTS / 100.0,
            )
            # Garante que price_send >= current_ask (senão FOK morre direto)
            if price_send < current_ask:
                price_send = current_ask

            contracts = unc["poly_shares"]
            unc["attempts"]       += 1
            unc["last_attempt_ts"] = now

            log_trade(
                f"[HEDGE TRY #{unc['attempts']}] Kalshi "
                f"{unc['kalshi_side_needed'].upper()} {contracts}c "
                f"@ {price_send:.4f} | ask={current_ask:.4f} "
                f"breakeven={breakeven:.4f} max={max_acceptable:.4f}"
            )

            result = await place_kalshi_order(
                private_key,
                unc["kalshi_ticker"],
                unc["kalshi_side_needed"],
                price_send,
                contracts,
            )

            if result.get("ok"):
                # P&L estimado: 1.00 - (poly_price + kalshi_price) por share
                # Ignora fees pra simplicidade — fees Kalshi são ~1¢ em 0.60
                total_paid_per_share = unc["poly_price_paid"] + price_send
                pnl_per_share        = 1.0 - total_paid_per_share
                pnl_total            = pnl_per_share * contracts
                log_trade(
                    f"[HEDGE ✓ CONCLUÍDO] order_id={result.get('order_id')} | "
                    f"pago total: {total_paid_per_share:.4f}/share | "
                    f"P&L/share: ${pnl_per_share:+.4f} | "
                    f"P&L total (pré-taxas): ${pnl_total:+.4f} | "
                    f"tentativas={unc['attempts']}"
                )

                # Registra no CSV pra auditoria
                try:
                    log_operation_csv({
                        "timestamp": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(timespec="milliseconds"),
                        "arb_id":              f"HEDGE_{unc.get('arb_id','?')}",
                        "result_tag":          "HEDGE_COMPLETO",
                        "kalshi_ticker":       unc["kalshi_ticker"],
                        "poly_market":         state.get("poly_market_name", ""),
                        "contracts_int":       contracts,
                        "kalshi_side":         unc["kalshi_side_needed"],
                        "kalshi_price_target": f"{current_ask:.4f}",
                        "kalshi_price_sent":   f"{price_send:.4f}",
                        "kalshi_order_id":     result.get("order_id", ""),
                        "kalshi_filled":       result.get("filled_count", ""),
                        "poly_token_id_head":  unc["poly_token"][:12] if unc.get("poly_token") else "",
                        "poly_price_target":   f"{unc['poly_price_paid']:.4f}",
                        "poly_order_id":       "",
                        "poly_matched_shares": unc["poly_shares"],
                        "profit_estimated":    f"{pnl_total:+.4f}",
                        "budget_usd":          f"{BUDGET:.2f}",
                        "exec_duration_ms":    int((time.time() - unc['created_at']) * 1000),
                        "warnings":            f"hedge pós-{unc.get('reason_tag','')} (tent={unc['attempts']})",
                    })
                except Exception:
                    pass

                state["uncovered"] = None
            else:
                err = (result.get("error") or "")[:80]
                log_trade(f"[HEDGE ✗ #{unc['attempts']}] {err}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log_trade(f"[HEDGE LOOP ERRO] {e}")
            await asyncio.sleep(2)


# ═══════════════════════════════════════════════════════════════════════════════
# UNWIND
# ═══════════════════════════════════════════════════════════════════════════════
UNWIND_SLIPPAGE = 0.06

async def unwind_poly_position(token_id: str, shares: float, purchase_price: float) -> dict:
    if not POLY_SDK_OK:
        return {"ok": False, "error": "SDK não instalado"}
    client = get_poly_client()
    if client is None:
        return {"ok": False, "error": "Cliente Poly não inicializado"}
    loop = asyncio.get_event_loop()
    try:
        tick_size_str = await _get_tick_size_cached(client, token_id)
        sell_price    = max(0.02, purchase_price - UNWIND_SLIPPAGE)
        tick          = Decimal(tick_size_str)
        price_aligned = (Decimal(str(sell_price)) / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
        if _decimal_places(price_aligned) > 2:
            price_aligned = price_aligned.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        size_grain   = Decimal(1).scaleb(-max(0, 2 - _decimal_places(price_aligned)))
        size_aligned = Decimal(str(shares)).quantize(size_grain, rounding=ROUND_DOWN)
        size_final, price_final = float(size_aligned), float(price_aligned)

        log_trade(f"[UNWIND] SELL {size_final} @ {price_final:.4f} token={token_id[:10]}...")
        order_args = _build_poly_order_args(token_id=token_id, price=price_final, size=size_final, side=Side.SELL)
        options    = PartialCreateOrderOptions(tick_size=tick_size_str)
        resp = await loop.run_in_executor(
            None, lambda: client.create_and_post_order(order_args, options=options, order_type=OrderType.GTC)
        )
        order_id = resp.get("orderID") or resp.get("id", "")
        status   = (resp.get("status") or "").lower()
        error    = resp.get("errorMsg") or resp.get("error", "")
        if error:
            return {"ok": False, "error": error, "order_id": order_id}
        if status in ("matched", "filled"):
            log_trade(f"[UNWIND OK] filled order_id={order_id}")
            return {"ok": True, "order_id": order_id, "status": status,
                    "price": price_final, "size": size_final}
        if status in ("live", "open", "resting"):
            log_trade(f"[UNWIND RESTING] {order_id} @ {price_final:.4f} — aguardando fill")
            return {"ok": True, "order_id": order_id, "status": status, "price": price_final,
                    "size": size_final, "warning": "resting — monitorar até preencher"}
        return {"ok": False, "error": f"Status inesperado: '{status}'", "order_id": order_id}
    except Exception as e:
        log_trade(f"[UNWIND ERRO] {e}")
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# TOP-UP — compra as shares que faltam para igualar perna Kalshi
# ═══════════════════════════════════════════════════════════════════════════════
TOPUP_SLIPPAGE_CENTS = 4   # paga até 4¢ a mais para garantir fill do gap

async def top_up_poly_position(token_id: str, shares_missing: float,
                               last_price: float) -> dict:
    """
    Compra `shares_missing` shares adicionais na Poly, pagando até
    TOPUP_SLIPPAGE_CENTS a mais que last_price para garantir preenchimento.
    Usa FOK para tudo-ou-nada (se falhar, não deixa resting aberta).

    Retorna dict com:
      - ok: True/False
      - matched_shares: quantas shares foram efetivamente compradas (0 se falhar)
      - price, order_id, error
    """
    if not POLY_SDK_OK:
        return {"ok": False, "error": "SDK não instalado", "matched_shares": 0}
    client = get_poly_client()
    if client is None:
        return {"ok": False, "error": "Cliente Poly não inicializado", "matched_shares": 0}
    if shares_missing <= 0:
        return {"ok": True, "matched_shares": 0, "skipped": "nada a fazer"}

    loop = asyncio.get_event_loop()
    try:
        tick_size_str = await _get_tick_size_cached(client, token_id)
        # CLOB V2: garante que o coef real do mercado esteja em cache
        await _populate_poly_fee_cache(client, token_id)

        # Preço agressivo: last_price + TOPUP_SLIPPAGE_CENTS/100
        # Quanto mais alto, maior chance de fill — mas cuidado pra não estourar 0.99.
        topup_price = min(0.99, last_price + TOPUP_SLIPPAGE_CENTS / 100.0)

        # Alinha com exact_int_mode=False: aqui queremos comprar APENAS o que falta,
        # sem overshoot exagerado (o gap costuma ser 0.3 ou assim).
        # Mas forçamos ROUND_UP no grain pra garantir >= shares_missing.
        size_fit, price_fit = _align_size_and_price(
            target_net_shares=shares_missing,
            price_input=topup_price,
            tick_size_str=tick_size_str,
            min_size_int=POLY_MIN_SIZE,
            exact_int_mode=False,  # aceitamos fracionário no gap
            token_id=token_id,
        )

        # Se por regra de min_size o gap virou POLY_MIN_SIZE (ex: 5),
        # estamos comprando MUITO mais que o necessário. Isso seria
        # uma nova posição descoberta, não top-up. Melhor abortar.
        if size_fit > Decimal(str(shares_missing)) + Decimal("1.5"):
            log_trade(
                f"[TOP-UP SKIP] gap={shares_missing} mas align forçou "
                f"size={size_fit} (>{shares_missing}+1.5). Abortando top-up."
            )
            return {
                "ok": False,
                "error": f"grain forçou size {size_fit} muito maior que gap {shares_missing}",
                "matched_shares": 0,
                "aborted_by_grain": True,
            }

        if price_fit >= Decimal("1.0000") or price_fit <= Decimal("0"):
            return {"ok": False, "error": f"Preço top-up inválido: {price_fit}",
                    "matched_shares": 0}

        size_final  = float(size_fit)
        price_final = float(price_fit)

        # Valida maker amount
        maker_str = format(size_fit * price_fit, 'f')
        maker_dec = len(maker_str.split('.')[1].rstrip('0')) if '.' in maker_str else 0
        if maker_dec > 2:
            return {"ok": False,
                    "error": f"maker top-up {maker_str} > 2 casas",
                    "matched_shares": 0}

        log_trade(
            f"[TOP-UP TRY] BUY {size_final} shares @ {price_final:.4f} "
            f"(last={last_price:.4f}, +{TOPUP_SLIPPAGE_CENTS}¢) "
            f"token={token_id[:10]}..."
        )

        order_args = _build_poly_order_args(
            token_id=token_id,
            price=price_final,
            size=size_final,
            side=Side.BUY,
        )
        options = PartialCreateOrderOptions(tick_size=tick_size_str)

        resp = await loop.run_in_executor(
            None,
            lambda: client.create_and_post_order(
                order_args, options=options, order_type=OrderType.FOK
            )
        )

        order_id = resp.get("orderID") or resp.get("id", "")
        status   = (resp.get("status") or "").lower()
        error    = resp.get("errorMsg") or resp.get("error", "")

        if error:
            log_trade(f"[TOP-UP ERRO] {error}")
            return {"ok": False, "error": error, "order_id": order_id,
                    "matched_shares": 0}

        if status in ("matched", "filled"):
            matched_shares = float(
                resp.get("takingAmount") or resp.get("filled_size")
                or resp.get("size_matched") or size_final
            )
            log_trade(
                f"[TOP-UP OK] filled {matched_shares} shares @ {price_final:.4f} "
                f"order_id={order_id}"
            )
            return {
                "ok":             True,
                "order_id":       order_id,
                "status":         status,
                "price":          price_final,
                "matched_shares": matched_shares,
                "size_submitted": size_final,
            }

        if status in ("live", "open"):
            try:
                await loop.run_in_executor(None, lambda: client.cancel_order(order_id))
                log_trade(f"[TOP-UP] cancelou resting {order_id}")
            except Exception as e:
                log_trade(f"[TOP-UP] falha ao cancelar {order_id}: {e}")
            return {"ok": False,
                    "error": f"Top-up FOK virou resting (status={status}), cancelado",
                    "order_id": order_id, "matched_shares": 0}

        return {"ok": False,
                "error": f"Top-up status inesperado: '{status}' resp={resp}",
                "order_id": order_id, "matched_shares": 0}

    except Exception as e:
        log_trade(f"[TOP-UP EXCEÇÃO] {e}")
        return {"ok": False, "error": str(e), "matched_shares": 0}


# ═══════════════════════════════════════════════════════════════════════════════
# RECONCILIAÇÃO PÓS-EXECUÇÃO
# ═══════════════════════════════════════════════════════════════════════════════
async def reconcile_execution(private_key, k_result: dict, p_result: dict,
                              expected_contracts: int) -> dict:
    report = {
        "kalshi_ok":     False,
        "poly_ok":       False,
        "kalshi_filled": 0,
        "poly_filled":   0.0,
        "delta_shares":  0.0,
        "warnings":      [],
    }

    k_order_id = k_result.get("order_id")
    if k_order_id:
        k_info = await fetch_kalshi_order(private_key, k_order_id)
        if k_info.get("ok"):
            order        = k_info["order"]
            status_k     = (order.get("status") or "").lower()
            filled       = int(float(order.get("fill_count_fp") or 0))
            remaining    = float(order.get("remaining_count_fp") or 0)
            fully_filled = (status_k == "executed" and remaining == 0.0) or (filled == expected_contracts)
            report["kalshi_filled"] = filled
            report["kalshi_ok"]     = fully_filled
            if not fully_filled:
                report["warnings"].append(
                    f"Kalshi filled {filled}/{expected_contracts} remaining={remaining} "
                    f"(status={status_k})"
                )
        else:
            report["warnings"].append(f"Falha ao consultar Kalshi: {k_info.get('error')}")

    p_order_id = p_result.get("order_id")
    if p_order_id:
        p_info = await fetch_poly_order(p_order_id)
        if p_info.get("ok"):
            order   = p_info["order"]
            matched = float(order.get("size_matched", 0) or order.get("matched", 0) or 0)
            report["poly_filled"] = matched
            report["poly_ok"]     = (abs(matched - expected_contracts) <= 0.5)
            if not report["poly_ok"]:
                report["warnings"].append(
                    f"Poly matched {matched:.4f} vs esperado {expected_contracts}"
                )
        else:
            report["poly_filled"] = p_result.get("matched_shares", 0)
            report["poly_ok"]     = True

    report["delta_shares"] = abs(report["kalshi_filled"] - report["poly_filled"])

    if report["warnings"]:
        log_trade("[RECONCILE ALERTA] " + " | ".join(report["warnings"]))

    return report


# ═══════════════════════════════════════════════════════════════════════════════
# ORQUESTRADOR DE EXECUÇÃO
# ═══════════════════════════════════════════════════════════════════════════════
async def execute_arb(sim: dict, private_key):
    if exec_lock.locked():
        return
    async with exec_lock:
        _reset_round_if_needed()

        # ── v16: bloqueio por posição Poly descoberta ────────────────────
        #   Impede abertura de nova arbitragem enquanto o hedge_loop está
        #   tentando fechar uma exposição antiga. Sem isso, ficamos com
        #   múltiplas pernas Poly órfãs empilhadas.
        if _is_blocked_by_uncovered():
            # Silencioso: não poluir log a cada tick de WS que tenta disparar.
            # Info fica visível na UI via banner.
            return

        if state["balance_frozen"]:
            log_trade("[EXEC BLOQUEADO] Saldo insuficiente — verifique as carteiras")
            return
        if state["round_exec_count"] >= MAX_EXECS_PER_ROUND:
            return
        if time.time() - state["last_exec_time"] < EXEC_COOLDOWN:
            return

        state["executing"] = True
        exec_start_ms = time.time() * 1000
        ts     = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        ts_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")

        k_result: dict  = {"ok": False}
        p_result: dict  = {"ok": False, "matched_shares": 0}
        recon            = None
        result_tag       = "INIT"
        status_str       = ""
        contracts_int    = 0
        k_price          = 0.0
        p_price          = 0.0
        k_price_slipped  = 0.0
        k_side           = ""
        p_token          = ""
        ticker           = ""

        try:
            ticker  = state["kalshi_ticker"]
            k_side  = sim["kalshi_side"]
            k_price = sim["kalshi_price"]
            p_token = sim["poly_token"]
            p_price = sim["poly_price"]

            contracts_int = max(POLY_MIN_SIZE, int(math.floor(sim["filled_ok"])))

            log_section(f"EXEC START ARB{sim['id']} @ {ts}")
            log_trade(
                f"[EXEC START] ARB{sim['id']} | "
                f"Kalshi {k_side.upper()} {contracts_int}c @{k_price:.4f} | "
                f"Poly FOK token={p_token[:10]}... N={contracts_int} @{p_price:.4f} | "
                f"Budget=${BUDGET:.2f} LucroEst=${sim['profit']:.4f}"
            )

            # ── Perna 1: Polymarket FOK ──────────────────────────────────────
            # exact_int_size=True: size = contracts_int EXATOS (sem gross-up de fee).
            # Motivo: quando o price Poly tem ≥2 casas decimais (caso comum),
            # o size precisa ser inteiro. Fazer gross-up e ROUND_UP pra 1 inteiro
            # acima (ex: target 7 → size 8) descobre a Kalshi em 1 contrato inteiro.
            # Melhor aceitar ficar descoberto pela fração do fee (~0.3 shares em 7)
            # do que por um contrato inteiro.
            p_result = await place_poly_fok(
                p_token, p_price, contracts_int, exact_int_size=True
            )

            if not p_result.get("ok"):
                # Caso especial: partial fill (shares adquiridas mas < target).
                # ESTRATÉGIA: tentar TOP-UP primeiro (comprar o gap pagando +caro).
                # Se top-up falhar, fazer UNWIND das shares parciais.
                if p_result.get("partial") and p_result.get("matched_shares", 0) > 0:
                    partial_qty = float(p_result.get("matched_shares", 0))
                    gap         = float(contracts_int) - partial_qty

                    log_trade(
                        f"[PARTIAL FILL] Poly preencheu {partial_qty}/{contracts_int}. "
                        f"Gap={gap:.4f}. Tentando TOP-UP..."
                    )

                    topup = await top_up_poly_position(p_token, gap, p_price)
                    log_trade(f"[TOP-UP RESULT] {topup}")

                    if topup.get("ok") and topup.get("matched_shares", 0) > 0:
                        # TOP-UP funcionou: agora temos partial_qty + topup_qty ≈ contracts_int
                        total_poly = partial_qty + float(topup.get("matched_shares", 0))
                        log_trade(
                            f"[TOP-UP SUCESSO] Total Poly agora: {total_poly:.4f} "
                            f"(target={contracts_int}). Prosseguindo para Kalshi..."
                        )

                        # Atualiza p_result como se fosse uma ordem só bem-sucedida
                        p_result = {
                            "ok":             True,
                            "order_id":       f"{p_result.get('order_id','?')}+{topup.get('order_id','?')}",
                            "status":         "matched",
                            "price":          p_price,
                            "matched_shares": total_poly,
                            "size_submitted": contracts_int,
                            "topup_info":     topup,
                        }

                        # ── Perna 2: Kalshi FOK (fluxo normal) ────────────
                        # v17 FIX #5: slippage adaptativo ao book
                        slip_cents = _compute_kalshi_slippage_cents(k_side, contracts_int)
                        k_price_slipped = min(0.98, k_price + slip_cents / 100)
                        log_trade(
                            f"[KALSHI pós-topup] ask={k_price:.4f} → FOK @ {k_price_slipped:.4f} "
                            f"(+{slip_cents}¢ adaptativo)"
                        )
                        k_result = await place_kalshi_order(
                            private_key, ticker, k_side, k_price_slipped, contracts_int
                        )

                        if k_result.get("ok"):
                            status_str = (
                                f"\033[32m✓ SUCESSO com TOP-UP "
                                f"(+{topup.get('matched_shares',0):.2f} Poly @ "
                                f"{topup.get('price',0):.4f})\033[0m"
                            )
                            result_tag = "SUCESSO_TOPUP"
                            state["total_profit"]     += sim["profit"]
                            state["round_exec_count"] += 1
                            recon = await reconcile_execution(
                                private_key, k_result, p_result, contracts_int
                            )
                            if recon and recon.get("warnings"):
                                status_str += f"\n     \033[33m[RECONCILIE] {'; '.join(recon['warnings'])}\033[0m"
                        else:
                            # Kalshi falhou após Poly OK (mesmo fluxo crítico de sempre)
                            log_trade(
                                f"[CRITICAL] Kalshi falhou após TOP-UP Poly OK! "
                                f"Unwind {total_poly} shares Poly..."
                            )
                            # v17 FIX #4: registra uncovered antes + flag
                            state["unwind_in_progress"] = True
                            try:
                                _register_uncovered(
                                    sim=sim,
                                    p_token=p_token,
                                    p_price=p_price,
                                    k_side=k_side,
                                    ticker=ticker,
                                    shares=total_poly,
                                    reason_tag="PRE_UNWIND_POSTOPUP",
                                )
                                await asyncio.sleep(UNWIND_DELAY_S)
                                unwind = await unwind_poly_position(p_token, total_poly, p_price)
                            finally:
                                state["unwind_in_progress"] = False

                            log_trade(f"[UNWIND RESULT] {unwind}")
                            if unwind.get("ok"):
                                status_str = f"\033[33m⚠ Kalshi falhou após topup, Poly unwound\033[0m"
                                result_tag = "UNWIND_OK_POSTOPUP"
                                state["uncovered"] = None
                                log_trade("[UNWIND OK POSTOPUP] uncovered limpo")
                            else:
                                status_str = (
                                    f"\033[31m✗ FALHA CRÍTICA após TOP-UP!\n"
                                    f"     Poly {total_poly} shares órfãs\n"
                                    "     >>> FECHE MANUAL <<<\033[0m"
                                )
                                result_tag = "DESCOBERTO_CRITICO"
                                # uncovered já registrado; atualiza reason_tag
                                if state.get("uncovered"):
                                    state["uncovered"]["reason_tag"] = "DESCOBERTO_CRITICO_POSTOPUP"

                    else:
                        # TOP-UP falhou: fazer UNWIND das shares parciais
                        log_trade(
                            f"[TOP-UP FALHOU] ({topup.get('error','?')}). "
                            f"Fazendo UNWIND de {partial_qty} shares..."
                        )
                        # v17 FIX #4: registra uncovered antes + flag
                        state["unwind_in_progress"] = True
                        try:
                            _register_uncovered(
                                sim=sim,
                                p_token=p_token,
                                p_price=p_price,
                                k_side=k_side,
                                ticker=ticker,
                                shares=partial_qty,
                                reason_tag="PRE_UNWIND_PARTIAL",
                            )
                            await asyncio.sleep(UNWIND_DELAY_S)
                            unwind = await unwind_poly_position(p_token, partial_qty, p_price)
                        finally:
                            state["unwind_in_progress"] = False

                        log_trade(f"[UNWIND PARTIAL] {unwind}")
                        if unwind.get("ok"):
                            status_str = (
                                f"\033[33m⚠ Partial fill + top-up falhou. "
                                f"Unwound @ {unwind.get('price', 0):.4f}\033[0m"
                            )
                            result_tag = "PARTIAL_UNWOUND"
                            state["uncovered"] = None
                            log_trade("[PARTIAL UNWOUND] uncovered limpo")
                        else:
                            status_str = (
                                f"\033[31m✗ PARTIAL FILL + TOP-UP FALHOU + UNWIND FALHOU!\n"
                                f"     Poly {partial_qty} shares órfãs\n"
                                f"     Erro unwind: {unwind.get('error')}\n"
                                "     >>> FECHE A POSIÇÃO POLY MANUALMENTE <<<\033[0m"
                            )
                            result_tag = "PARTIAL_DESCOBERTO"
                            # uncovered já registrado; atualiza reason_tag
                            if state.get("uncovered"):
                                state["uncovered"]["reason_tag"] = "PARTIAL_DESCOBERTO"
                        k_result = {"ok": False, "error": "Abortado (partial + topup falhou)"}
                else:
                    result_tag = "ABORTADO_POLY"
                    status_str = f"\033[33m✗ Poly FOK rejeitada ({(p_result.get('error') or '')[:80]})\033[0m"
                    k_result   = {"ok": False, "error": "Abortado (Poly não preencheu)"}
            else:
                # ── Perna 2: Kalshi FOK ──────────────────────────────────────
                # v17 FIX #5: slippage adaptativo ao book Kalshi.
                # Antes: KALSHI_SLIPPAGE_CENTS constante (=2¢), que era agressivo
                # demais em books rasos. Agora olha a profundidade real do book
                # e paga só o necessário pra garantir fill + 1¢ buffer, teto de
                # KALSHI_SLIPPAGE_MAX_CENTS. Reduz custo médio de execução e
                # diminui risco de DESCOBERTO em spreads apertados.
                slip_cents = _compute_kalshi_slippage_cents(k_side, contracts_int)
                k_price_slipped = min(0.98, k_price + slip_cents / 100)
                log_trade(
                    f"[KALSHI] ask={k_price:.4f} → FOK @ {k_price_slipped:.4f} "
                    f"(+{slip_cents}¢ adaptativo, teto {KALSHI_SLIPPAGE_MAX_CENTS}¢)"
                )
                k_result = await place_kalshi_order(
                    private_key, ticker, k_side, k_price_slipped, contracts_int
                )

                if k_result.get("ok"):
                    status_str = "\033[32m✓ SUCESSO (FOK+FOK)\033[0m"
                    result_tag = "SUCESSO"
                    state["total_profit"]     += sim["profit"]
                    state["round_exec_count"] += 1
                    recon = await reconcile_execution(private_key, k_result, p_result, contracts_int)
                    if recon and recon.get("warnings"):
                        status_str += f"\n     \033[33m[RECONCILIE] {'; '.join(recon['warnings'])}\033[0m"
                else:
                    log_trade(
                        f"[CRITICAL] Kalshi FOK falhou após Poly fill! "
                        f"poly_order={p_result.get('order_id')} "
                        f"shares={p_result.get('matched_shares')} "
                        f"erro={k_result.get('error')}"
                    )

                    # ── v17 FIX #4: REGISTRA UNCOVERED JÁ, antes do sleep ──
                    # Assim a UI mostra o estado correto imediatamente e, se
                    # o unwind falhar, o hedge_loop pula direto pra ação.
                    # O flag unwind_in_progress impede o hedge_loop de
                    # disparar enquanto o unwind ainda está tentando.
                    state["unwind_in_progress"] = True
                    try:
                        _register_uncovered(
                            sim=sim,
                            p_token=p_token,
                            p_price=p_price,
                            k_side=k_side,
                            ticker=ticker,
                            shares=p_result.get("matched_shares", contracts_int),
                            reason_tag="PRE_UNWIND",
                        )
                        log_trade(
                            f"[UNWIND] Aguardando {UNWIND_DELAY_S}s para CLOB creditar shares... "
                            f"(unwind_in_progress=True, hedge_loop em standby)"
                        )
                        await asyncio.sleep(UNWIND_DELAY_S)
                        unwind = await unwind_poly_position(
                            p_token, p_result.get("matched_shares", contracts_int), p_price
                        )
                    finally:
                        # SEMPRE limpa flag, mesmo se unwind levantar exception
                        state["unwind_in_progress"] = False

                    log_trade(f"[UNWIND RESULT] {unwind}")
                    if unwind.get("ok"):
                        if unwind.get("warning"):
                            status_str = (
                                f"\033[33m⚠ Kalshi FOK rejeitada. Unwind resting @ {unwind['price']:.4f} "
                                f"order={unwind.get('order_id')} — monitorar!\033[0m"
                            )
                            result_tag = "UNWIND_RESTING"
                            # Unwind resting ainda está vendendo — não limpar uncovered
                            # até confirmar fill. Por enquanto mantemos o uncovered
                            # registrado; se a rodada acabar sem fill, o hedge_loop
                            # limpa sozinho no próximo ciclo.
                        else:
                            status_str = f"\033[32m✓ Kalshi FOK rejeitada, Poly unwound @ {unwind['price']:.4f}\033[0m"
                            result_tag = "UNWIND_OK"
                            # Unwind completo: posição fechada, limpa uncovered
                            state["uncovered"] = None
                            log_trade("[UNWIND OK] uncovered limpo (Poly fechado)")
                    else:
                        status_str = (
                            f"\033[31m✗ KALSHI FOK FALHOU + UNWIND POLY FALHOU!\n"
                            f"     Poly order: {p_result.get('order_id')} ({p_result.get('matched_shares')} shares)\n"
                            f"     Unwind erro: {unwind.get('error')}\n"
                            "     >>> FECHE A POSIÇÃO POLY MANUALMENTE <<<\033[0m"
                        )
                        result_tag = "DESCOBERTO_CRITICO"
                        # uncovered JÁ foi registrado acima (PRE_UNWIND).
                        # Atualiza apenas o reason_tag para refletir falha no unwind.
                        if state.get("uncovered"):
                            state["uncovered"]["reason_tag"] = "DESCOBERTO_CRITICO"
                            log_trade("[DESCOBERTO] uncovered mantido; hedge_loop assume")
                    recon = None

            log_trade(f"[EXEC END {result_tag}] Kalshi={k_result} | Poly={p_result}")
            log_section(f"EXEC END ARB{sim['id']} → {result_tag}")

            state["last_exec_result"] = {
                "arb_id":     sim["id"],
                "result":     result_tag,
                "k_result":   k_result,
                "p_result":   p_result,
                "recon":      recon,
                "timestamp":  ts,
                "status_str": status_str,
            }

            # Registra no histórico da UI (tempo calculado na finalização)
            try:
                _record_exec_history(
                    arb_id=sim["id"],
                    result_tag=result_tag,
                    k_result=k_result,
                    p_result=p_result,
                    contracts_int=contracts_int,
                    profit_est=sim.get("profit", 0.0),
                    timestamp_str=ts,
                    duration_ms=int(time.time() * 1000 - exec_start_ms),
                )
            except Exception as hist_err:
                log_trade(f"[HISTORY LOG FAIL] {hist_err}")

        except Exception as e:
            log_trade(f"[EXEC EXCEPTION] {e}")

            # ── v17 FIX #1: RECONCILIAÇÃO DE EMERGÊNCIA ──────────────────
            # Antes: exception no meio do execute_arb deixava posições
            # órfãs sem ninguém reconciliar (ex: ARB2 08:16 da sessão passada).
            # Agora: se k_result OU p_result têm order_id, consulta o estado
            # real da exchange ANTES de desistir. Se Poly preencheu mas
            # Kalshi não, registra uncovered pra o hedge_loop assumir.
            try:
                k_oid_ex = k_result.get("order_id") if isinstance(k_result, dict) else None
                p_oid_ex = p_result.get("order_id") if isinstance(p_result, dict) else None

                k_filled_ex = 0
                p_filled_ex = 0.0

                if k_oid_ex:
                    try:
                        ki = await fetch_kalshi_order(private_key, k_oid_ex)
                        if ki.get("ok"):
                            ord_k = ki.get("order", {})
                            k_filled_ex = int(float(
                                ord_k.get("fill_count_fp") or
                                ord_k.get("filled_count") or 0
                            ))
                    except Exception as ke:
                        log_trade(f"[EXC-RECON] fetch Kalshi falhou: {ke}")

                if p_oid_ex:
                    try:
                        pi = await fetch_poly_order(p_oid_ex)
                        if pi.get("ok"):
                            ord_p = pi.get("order", {})
                            p_filled_ex = float(
                                ord_p.get("size_matched", 0) or
                                ord_p.get("matched", 0) or 0
                            )
                    except Exception as pe:
                        log_trade(f"[EXC-RECON] fetch Poly falhou: {pe}")

                log_trade(
                    f"[EXC-RECON] k_order={k_oid_ex} filled={k_filled_ex} | "
                    f"p_order={p_oid_ex} filled={p_filled_ex:.4f} | "
                    f"contracts_int={contracts_int}"
                )

                # Se Poly preencheu algo mas Kalshi não, há posição descoberta
                if p_filled_ex > 0.5 and k_filled_ex < contracts_int:
                    log_trade(
                        f"[EXC-RECON ⚠] POSIÇÃO ÓRFÃ DETECTADA: "
                        f"Poly {p_filled_ex:.2f} shares preenchidas, "
                        f"Kalshi {k_filled_ex}/{contracts_int} — registrando hedge"
                    )
                    try:
                        _register_uncovered(
                            sim=sim, p_token=p_token, p_price=p_price,
                            k_side=k_side, ticker=ticker,
                            shares=p_filled_ex,
                            reason_tag="EXCEPTION_RECON",
                        )
                        result_tag = "EXCEPTION_UNCOVERED"
                    except Exception as re:
                        log_trade(f"[EXC-RECON] falha ao registrar uncovered: {re}")
                        result_tag = "EXCEPTION_ORFA_MANUAL"
                elif p_filled_ex > 0.5 and k_filled_ex >= contracts_int:
                    # Ambos preencheram — foi SUCESSO mas exception na limpeza
                    log_trade(f"[EXC-RECON ✓] ambos preencheram — ignorando exception")
                    result_tag = "SUCESSO_POST_EXC"
                    state["total_profit"]     += sim.get("profit", 0.0)
                    state["round_exec_count"] += 1
                elif k_filled_ex > 0 and p_filled_ex < 0.5:
                    # Raro: Kalshi preencheu, Poly não. Dado que Poly vai
                    # PRIMEIRO, isso só acontece se a exception foi bizarra.
                    # Flag pra intervenção manual.
                    log_trade(
                        f"[EXC-RECON ⚠⚠] CASO RARO: Kalshi filled={k_filled_ex} "
                        f"mas Poly não preencheu. EXIGE INSPEÇÃO MANUAL!"
                    )
                    result_tag = "EXCEPTION_KALSHI_SO"
                else:
                    # Nada preencheu — exception benigna
                    result_tag = "EXCEPTION"
            except Exception as rec_err:
                log_trade(f"[EXC-RECON FATAL] {rec_err} — marcando EXCEPTION puro")
                result_tag = "EXCEPTION"

            state["last_exec_result"] = {
                "error":      str(e),
                "timestamp":  ts,
                "recon_tag":  result_tag,
            }
            try:
                _record_exec_history(
                    arb_id=sim.get("id", 0) if isinstance(sim, dict) else 0,
                    result_tag=result_tag,
                    k_result={"error": str(e)},
                    p_result={},
                    contracts_int=contracts_int,
                    profit_est=sim.get("profit", 0.0) if isinstance(sim, dict) else 0.0,
                    timestamp_str=ts,
                    duration_ms=int(time.time() * 1000 - exec_start_ms),
                )
            except Exception:
                pass
        finally:
            state["executing"]      = False
            state["last_exec_time"] = time.time()

            # ── v17 FIX #4: garantia extra de limpeza do flag ───────────
            # Se qualquer path acima não limpou unwind_in_progress (ex: raise
            # fora dos try/finally locais), forçamos aqui. Sem isso, o
            # hedge_loop ficaria travado esperando eternamente.
            if state.get("unwind_in_progress"):
                log_trade("[FLAG CLEANUP] unwind_in_progress True no finally — limpando")
                state["unwind_in_progress"] = False

            # ── v17 FIX: garante que QUALQUER resultado que tenha tocado a
            # Poly consuma o slot da rodada. Antes só SUCESSO incrementava,
            # então quando uma arb terminava em DESCOBERTO_CRITICO → HEDGE,
            # o contador ficava em 0 e uma nova arb disparava assim que o
            # hedge_loop limpava state["uncovered"]. Isso dobrou exposição
            # em várias rodadas (ex: 21:45-22:00, 22:00-22:15, 01:45-02:00,
            # 03:15-03:30, 03:45-04:00, 05:15-05:30) — causa direta da perda
            # de ~$9,85 na sessão de 22-Apr.
            if result_tag in _ROUND_CONSUMING_TAGS and result_tag not in _SUCCESS_TAGS:
                state["round_exec_count"] += 1
                log_trade(
                    f"[ROUND GUARD] {result_tag} consome slot da rodada "
                    f"(exec_count agora {state['round_exec_count']}/{MAX_EXECS_PER_ROUND}) "
                    f"— previne arbs duplicadas pós-hedge/unwind"
                )

            try:
                exec_duration_ms = int(time.time() * 1000 - exec_start_ms)
                warnings_joined  = ""
                if recon and recon.get("warnings"):
                    warnings_joined = " | ".join(recon["warnings"])
                log_operation_csv({
                    "timestamp":           ts_utc,
                    "arb_id":              sim.get("id", ""),
                    "result_tag":          result_tag,
                    "kalshi_ticker":       ticker,
                    "poly_market":         state.get("poly_market_name", ""),
                    "contracts_int":       contracts_int,
                    "kalshi_side":         k_side,
                    "kalshi_price_target": f"{k_price:.4f}" if k_price else "",
                    "kalshi_price_sent":   f"{k_price_slipped:.4f}" if k_price_slipped else "",
                    "kalshi_order_id":     k_result.get("order_id", "") if isinstance(k_result, dict) else "",
                    "kalshi_filled":       k_result.get("filled_count", "") if isinstance(k_result, dict) else "",
                    "poly_token_id_head":  p_token[:12] if p_token else "",
                    "poly_price_target":   f"{p_price:.4f}" if p_price else "",
                    "poly_order_id":       p_result.get("order_id", "") if isinstance(p_result, dict) else "",
                    "poly_matched_shares": p_result.get("matched_shares", "") if isinstance(p_result, dict) else "",
                    "profit_estimated":    f"{sim.get('profit', 0):.4f}",
                    "budget_usd":          f"{BUDGET:.2f}",
                    "exec_duration_ms":    exec_duration_ms,
                    "warnings":            warnings_joined,
                })
            except Exception as csv_err:
                log_trade(f"[CSV LOG FAIL] {csv_err}")


# ═══════════════════════════════════════════════════════════════════════════════
# DISPLAY E CHECK
# ═══════════════════════════════════════════════════════════════════════════════
# ── Tags que contam como "sucesso" para a métrica de taxa ──────────────────
_SUCCESS_TAGS = {"SUCESSO", "SUCESSO_TOPUP", "SUCESSO_POST_EXC"}

# ── Tags que representam FALHA da arbitragem (não usadas como sucesso) ─────
_FAILURE_TAGS_UI = {
    "ABORTADO_POLY", "PARTIAL_UNWOUND", "PARTIAL_DESCOBERTO",
    "UNWIND_OK", "UNWIND_OK_POSTOPUP", "UNWIND_RESTING",
    "DESCOBERTO_CRITICO", "EXCEPTION", "INIT",
}

# ── v17: Tags que CONSOMEM o "crédito" de execução da rodada ───────────────
# Qualquer resultado que TOCOU a Poly (enviou ordem que preencheu, mesmo que
# parcial ou unwindada depois) deve consumir o slot da rodada. Se não contar,
# depois que o hedge_loop limpar state["uncovered"], novas arbs disparam na
# mesma rodada — dobrando exposição quando o mercado vai contra (causa real
# da perda de ~$9,85 na sessão anterior).
#
# ABORTADO_POLY NÃO está aqui porque a ordem Poly foi rejeitada antes de
# qualquer fill — nenhuma posição foi aberta, podemos tentar de novo.
_ROUND_CONSUMING_TAGS = {
    "SUCESSO", "SUCESSO_TOPUP", "SUCESSO_POST_EXC",
    "DESCOBERTO_CRITICO", "PARTIAL_DESCOBERTO",
    "UNWIND_OK", "UNWIND_OK_POSTOPUP", "UNWIND_RESTING",
    "PARTIAL_UNWOUND", "EXCEPTION",
    "EXCEPTION_UNCOVERED", "EXCEPTION_ORFA_MANUAL", "EXCEPTION_KALSHI_SO",
}


def _short_failure_reason(result_tag: str, k_result: dict, p_result: dict) -> str:
    """
    Devolve um motivo CURTO e humano da falha, olhando o que cada perna
    reportou. Usado no histórico da UI.
    """
    k_err = (k_result or {}).get("error", "") if isinstance(k_result, dict) else ""
    p_err = (p_result or {}).get("error", "") if isinstance(p_result, dict) else ""

    def _classify(err: str) -> str:
        e = (err or "").lower()
        if not e:
            return ""
        if "fok voltou resting" in e or "virou resting" in e:
            return "FOK→resting (preço moveu)"
        if "fill parcial" in e or "partial" in e:
            return "fill parcial"
        if "maker amount" in e or "2 casas" in e:
            return "maker amount inválido"
        if "preço inválido" in e or "preco invalido" in e:
            return "preço inválido"
        if "tick" in e:
            return "tick size"
        if "http 4" in e or "http 5" in e:
            return f"HTTP {e.split('http ')[1][:3] if 'http ' in e else '?'}"
        if "timeout" in e or "timed out" in e:
            return "timeout"
        if "insufficient" in e or "balance" in e:
            return "saldo/allowance"
        if "invalid amount" in e:
            return "amount inválido"
        if "sdk" in e:
            return "SDK Poly"
        # fallback: primeiros 40 chars do erro
        return err[:40].strip() if err else "?"

    if result_tag == "ABORTADO_POLY":
        return f"Poly rejeitou: {_classify(p_err)}"
    if result_tag in ("PARTIAL_UNWOUND", "PARTIAL_DESCOBERTO"):
        return f"Poly partial: {_classify(p_err)}"
    if result_tag == "UNWIND_OK":
        return f"Kalshi falhou: {_classify(k_err)} (Poly unwound)"
    if result_tag == "UNWIND_OK_POSTOPUP":
        return f"Kalshi falhou após topup: {_classify(k_err)}"
    if result_tag == "UNWIND_RESTING":
        return f"Kalshi falhou, unwind resting ({_classify(k_err)})"
    if result_tag == "DESCOBERTO_CRITICO":
        return f"CRÍTICO: {_classify(k_err) or _classify(p_err)} — POSIÇÃO ABERTA"
    if result_tag == "EXCEPTION":
        return f"Exceção: {_classify(k_err or p_err)}"
    # Casos restantes: prefere erro não-vazio
    return _classify(k_err) or _classify(p_err) or result_tag


def _record_exec_history(arb_id, result_tag, k_result, p_result,
                         contracts_int, profit_est, timestamp_str,
                         duration_ms):
    """
    Insere entrada no histórico de execuções da UI. Atualiza contadores
    agregados. Mais recente primeiro, limitado a EXEC_HISTORY_MAX.
    """
    success = result_tag in _SUCCESS_TAGS
    entry = {
        "ts":         timestamp_str,
        "arb_id":     arb_id,
        "tag":        result_tag,
        "success":    success,
        "contracts":  contracts_int,
        "profit_est": profit_est,
        "duration_ms": duration_ms,
        "reason":     "" if success else _short_failure_reason(result_tag, k_result, p_result),
    }
    state["exec_history"].insert(0, entry)
    if len(state["exec_history"]) > EXEC_HISTORY_MAX:
        state["exec_history"] = state["exec_history"][:EXEC_HISTORY_MAX]
    state["total_execs"] += 1
    if success:
        state["total_success"] += 1


def print_sim_compact(sim: dict):
    """Versão compacta (1-2 linhas) da simulação de ARB."""
    ok_a, ok_b = sim["ok_a"], sim["ok_b"]
    both_ok    = ok_a and ok_b
    pct        = (sim["profit"] / BUDGET * 100) if BUDGET > 0 else 0.0

    # Status na primeira linha
    if not both_ok:
        # Mostra qual perna falhou
        liq_tag = []
        if not ok_a:
            liq_tag.append(f"{sim['label_a'].strip()}:{sim['filled_a']:.1f}c")
        if not ok_b:
            liq_tag.append(f"{sim['label_b'].strip()}:{sim['filled_b']:.1f}c")
        head = f"\033[33m[ARB {sim['id']}] SEM LIQ\033[0m ({', '.join(liq_tag)})"
    elif sim["profit"] > 0 and pct >= MIN_PROFIT_PCT:
        head = f"\033[32m[ARB {sim['id']}] ★ OPORTUNIDADE\033[0m ${sim['profit']:.2f} ({pct:.2f}%)"
    elif sim["profit"] > 0:
        head = f"[ARB {sim['id']}] lucro baixo ${sim['profit']:.2f} ({pct:.2f}% < {MIN_PROFIT_PCT}%)"
    else:
        head = f"[ARB {sim['id']}] prejuízo ${-sim['profit']:.2f}"

    # Detalhe numérico na segunda linha (compacto)
    a_p = sim['avg_a'] if sim['avg_a'] else sim['price_a']
    b_p = sim['avg_b'] if sim['avg_b'] else sim['price_b']
    detail = (f"       {sim['label_a']}@${a_p:.4f}  +  "
              f"{sim['label_b']}@${b_p:.4f}  "
              f"→ {sim['filled_ok']:.1f}c (custo ${sim['real_cost']:.2f})")

    print(head)
    print(detail)


def _colorize_tag(tag: str) -> str:
    """Retorna a tag colorida conforme seu tipo (sucesso/aviso/crítico)."""
    if tag in _SUCCESS_TAGS:
        return f"\033[32m{tag}\033[0m"
    if tag in ("UNWIND_OK", "UNWIND_OK_POSTOPUP", "PARTIAL_UNWOUND"):
        return f"\033[33m{tag}\033[0m"
    if tag in ("DESCOBERTO_CRITICO", "PARTIAL_DESCOBERTO", "EXCEPTION"):
        return f"\033[31;1m{tag}\033[0m"
    if tag == "UNWIND_RESTING":
        return f"\033[35m{tag}\033[0m"  # magenta — exige monitoramento
    if tag == "ABORTADO_POLY":
        return f"\033[33m{tag}\033[0m"
    return tag


def _render_history_block():
    """
    Renderiza o bloco de histórico de execuções. Mostra:
      - linha de métricas (total/sucessos/taxa)
      - últimas N execs (novas primeiro), sucessos e falhas intercalados
      - falhas em vermelho, sucessos em verde
    """
    hist       = state.get("exec_history", [])
    total      = state.get("total_execs", 0)
    success    = state.get("total_success", 0)
    fail       = total - success
    taxa       = (success / total * 100) if total else 0.0
    profit_ac  = state.get("total_profit", 0.0)

    # Header de métricas
    print("─" * 78)
    print(
        f" HISTÓRICO  │  execs: {total}  │  "
        f"\033[32m✓ {success}\033[0m  │  \033[31m✗ {fail}\033[0m  │  "
        f"taxa: {taxa:5.1f}%  │  lucro: \033[32m${profit_ac:+.2f}\033[0m"
    )
    print("─" * 78)

    if not hist:
        print(" (nenhuma execução ainda nesta sessão)")
        return

    # Cabeçalho das colunas
    print(f"  {'hora':<12}  {'ARB':<4}  {'c':>3}  {'ms':>5}  {'tag':<20}  motivo/profit")
    for e in hist:
        ts_short = e["ts"][-12:] if e["ts"] else "--"
        arb_lbl  = f"#{e['arb_id']}" if e["arb_id"] else "?"
        tag_col  = _colorize_tag(e["tag"])
        # Pad o tag SEM contar os códigos ANSI
        raw_len  = len(e["tag"])
        pad      = " " * max(0, 20 - raw_len)
        if e["success"]:
            right = f"\033[32m+${e['profit_est']:.2f}\033[0m"
        else:
            right = f"\033[31m{e['reason'][:48]}\033[0m"
        dur = f"{e['duration_ms']}" if e.get("duration_ms") else "--"
        print(f"  {ts_short:<12}  {arb_lbl:<4}  {e['contracts']:>3}  {dur:>5}  {tag_col}{pad}  {right}")


# ═══════════════════════════════════════════════════════════════════════════════
# (mantido) print_sim ORIGINAL — não usado no novo layout mas preservado
#           caso queira ligar detalhamento verbose no futuro.
# ═══════════════════════════════════════════════════════════════════════════════
def print_sim(sim: dict):
    ok_a, ok_b = sim["ok_a"], sim["ok_b"]
    both_ok    = ok_a and ok_b

    avg_a_str = f"${sim['avg_a']:.4f}" if sim["avg_a"] else f"${sim['price_a']:.4f}*"
    avg_b_str = f"${sim['avg_b']:.4f}" if sim["avg_b"] else f"${sim['price_b']:.4f}*"

    if not both_ok:
        print(f"\033[33m[ARB {sim['id']}] LIQUIDEZ INSUFICIENTE\033[0m")
    elif sim["profit"] > 0:
        print(f"\033[32m[ARB {sim['id']}] ★ OPORTUNIDADE ★\033[0m")
    else:
        print(f"[ARB {sim['id']}] Sem lucro")

    liq_a = "✓ OK" if ok_a else f"\033[31m✗ só {sim['filled_a']:.2f}c\033[0m"
    liq_b = "✓ OK" if ok_b else f"\033[31m✗ só {sim['filled_b']:.2f}c\033[0m"

    print(f"  Contratos alvo : {sim['contracts']:.2f}")
    print(f"  {sim['label_a']}: ask={sim['price_a']:.4f}  médio={avg_a_str}  liquidez={liq_a}")
    print(f"  {sim['label_b']}: ask={sim['price_b']:.4f}  médio={avg_b_str}  liquidez={liq_b}")
    print(f"  Executável     : {sim['filled_ok']:.2f} contratos  |  Custo: ${sim['real_cost']:.2f}")

    if sim["profit"] > 0 and both_ok:
        pct = sim["profit"] / BUDGET * 100
        print(f"  \033[32mLUCRO: ${sim['profit']:.4f}  ({pct:.2f}%)\033[0m")
        if pct >= MIN_PROFIT_PCT:
            if state["round_exec_count"] >= MAX_EXECS_PER_ROUND:
                print(f"  \033[31m→ Limite de {MAX_EXECS_PER_ROUND} exec/rodada atingido\033[0m")
            elif AUTO_EXECUTE:
                print(f"  \033[33m→ Execução automática agendada...\033[0m")
        else:
            print(f"  \033[31m→ Lucro < {MIN_PROFIT_PCT}%\033[0m")
    elif sim["profit"] > 0:
        pct = sim["profit"] / max(sim["real_cost"], 1e-9) * 100
        print(f"  Lucro parcial: ${sim['profit']:.4f}  ({pct:.2f}%)")
    else:
        print(f"  Prejuízo: ${-sim['profit']:.4f}")


def check_arbitrage():
    global last_display_time
    now = time.time()
    if now - last_display_time < UI_REFRESH_INTERVAL_S:
        return

    _reset_round_if_needed()
    seconds_into = int(now) % 900

    # ── Janela pausada ────────────────────────────────────────────────────
    if seconds_into < 20 or seconds_into >= 820:
        if now - last_display_time < UI_REFRESH_INTERVAL_PAUSE_S:
            return
        last_display_time = now
        _clear_screen()
        mm, ss = seconds_into // 60, seconds_into % 60
        print("╔" + "═" * 76 + "╗")
        print(f"║  ARB MONITOR v17 (KALSHI × POLYMARKET)  —  \033[33mPAUSADO (janela segura)\033[0m  ║")
        print("╚" + "═" * 76 + "╝")
        print(f"  Clock rodada: {mm:02d}:{ss:02d} / 15:00  |  safe-window: 01:00–12:00")

        # ── Status de saldo ──────────────────────────────────────────────
        print()
        bal_poly   = state["balance_poly"]
        bal_kalshi = state["balance_kalshi"]
        frozen     = state["balance_frozen"]
        checked    = state["balance_last_check"]

        def _bal_str(val, label):
            if val is None:
                return f"{label}: \033[33m verificando...\033[0m"
            color = "\033[32m" if val >= BUDGET else "\033[31m"
            mark  = "✓" if val >= BUDGET else "✗"
            return f"{label}: {color}{mark} ${val:.2f}\033[0m"

        print(f"  {_bal_str(bal_poly, 'Poly')}   {_bal_str(bal_kalshi, 'Kalshi')}   "
              f"Budget: ${BUDGET:.2f}")

        if checked > 0:
            age = int(now - checked)
            print(f"  Último check: há {age}s")

        if frozen:
            print()
            print("  \033[31m╔══════════════════════════════════════════════════╗\033[0m")
            print("  \033[31m║  ⛔  EXECUÇÃO CONGELADA — SALDO INSUFICIENTE   ║\033[0m")
            print("  \033[31m╚══════════════════════════════════════════════════╝\033[0m")
            print("  \033[31m  Recarregue as carteiras e aguarde o próximo check.\033[0m")

        # v16: banner uncovered também na janela pausada
        unc_p = state.get("uncovered")
        if unc_p is not None:
            age_p        = int(now - unc_p["created_at"])
            round_left_p = max(0, 900 - seconds_into)
            breakeven_p  = 1.0 - unc_p["poly_price_paid"]
            max_acc_p    = breakeven_p + UNCOVERED_HEDGE_MAX_LOSS_CENTS / 100.0
            print()
            print("  \033[31;1m" + "━" * 76 + "\033[0m")
            print(f"  \033[31;1m⚠  POSIÇÃO POLY DESCOBERTA — HEDGE LOOP ATIVO\033[0m")
            print(f"     {unc_p['poly_shares']} Poly shares @ "
                  f"${unc_p['poly_price_paid']:.4f} | precisa Kalshi "
                  f"{unc_p['kalshi_side_needed'].upper()} ≤ ${max_acc_p:.4f}")
            print(f"     Idade: {age_p}s │ Rodada resta: {round_left_p}s │ "
                  f"Tentativas: {unc_p['attempts']}")
            print("  \033[31;1m" + "━" * 76 + "\033[0m")

        print()
        _render_history_block()
        return

    last_display_time = now

    k_y_bid = state["kalshi_yes_bid"]
    k_y_ask = state["kalshi_yes_ask"]
    p_y_ask = state["poly_yes_ask"]
    p_n_ask = state["poly_no_ask"]
    k_n_ask = (1.0 - k_y_bid) if k_y_bid is not None else None

    _clear_screen()

    # ── Cabeçalho compacto ────────────────────────────────────────────────
    mm, ss = seconds_into // 60, seconds_into % 60
    modo_txt = "\033[32mAUTO\033[0m" if AUTO_EXECUTE else "\033[33mMANUAL\033[0m"
    tipo_txt = "\033[36mSIM\033[0m"  if MODE == "SIMULATION" else "\033[31mREAL\033[0m"
    execs_rd = f"{state['round_exec_count']}/{MAX_EXECS_PER_ROUND}"

    print("╔" + "═" * 76 + "╗")
    # Monta título e calcula padding descontando códigos ANSI
    title    = f"ARB MONITOR v17  —  BTC 15m  —  {mm:02d}:{ss:02d}/15:00  —  {modo_txt} {tipo_txt}"
    visible  = re.sub(r"\033\[[0-9;]*m", "", title)
    pad_need = 76 - len(visible) - 4   # -4 = "║  " + "  ║"
    print(f"║  {title}{' ' * max(0, pad_need)}  ║")
    print("╚" + "═" * 76 + "╝")

    # Linha de contexto: tickers
    kt = state.get('kalshi_ticker') or "..."
    pm = state.get('poly_market_name') or "..."
    if len(pm) > 70:
        pm = pm[:67] + "..."
    print(f"  Kalshi: {kt}")
    print(f"  Poly  : {pm}")
    print(f"  Budget ${BUDGET:.0f}  |  min-profit {MIN_PROFIT_PCT}%  |  "
          f"cooldown {EXEC_COOLDOWN}s  |  rodada {execs_rd}")

    # ── Best asks (1 linha) ───────────────────────────────────────────────
    def _f(v): return f"${v:.4f}" if v is not None else "  --  "
    print()
    print(f"  BEST ASKS │ Kalshi YES {_f(k_y_ask)}  NO {_f(k_n_ask)}   "
          f"│ Poly YES {_f(p_y_ask)}  NO {_f(p_n_ask)}")

    # ── v16: Banner de posição descoberta ────────────────────────────────
    unc = state.get("uncovered")
    if unc is not None:
        age        = int(now - unc["created_at"])
        round_left = max(0, 900 - seconds_into)
        breakeven  = 1.0 - unc["poly_price_paid"]
        max_acc    = breakeven + UNCOVERED_HEDGE_MAX_LOSS_CENTS / 100.0
        cur_ask    = _current_kalshi_ask_for(unc["kalshi_side_needed"])
        ask_str    = f"${cur_ask:.4f}" if cur_ask is not None else "  --  "
        # Pinta ask verde se está no range, vermelho se fora
        if cur_ask is not None and cur_ask <= max_acc:
            ask_colored = f"\033[32m{ask_str}\033[0m"
        else:
            ask_colored = f"\033[31m{ask_str}\033[0m"

        print()
        print("  \033[31;1m" + "━" * 76 + "\033[0m")
        print(f"  \033[31;1m⚠  POSIÇÃO POLY DESCOBERTA — ARBITRAGENS BLOQUEADAS\033[0m  "
              f"\033[90m[{unc.get('reason_tag','')}]\033[0m")
        print(f"     Poly: \033[33m{unc['poly_shares']} shares @ "
              f"${unc['poly_price_paid']:.4f}\033[0m  |  "
              f"precisa Kalshi \033[33m{unc['kalshi_side_needed'].upper()}\033[0m")
        print(f"     Kalshi ask: {ask_colored}  ≤  "
              f"max ${max_acc:.4f} (breakeven ${breakeven:.4f} + "
              f"{UNCOVERED_HEDGE_MAX_LOSS_CENTS}¢ tol)")
        print(f"     Idade: {age}s  │  Rodada resta: {round_left}s  │  "
              f"Tentativas hedge: {unc['attempts']}")
        print("  \033[31;1m" + "━" * 76 + "\033[0m")

    # ── Simulações compactas ──────────────────────────────────────────────
    print()
    sim1 = simulate_arb(1)
    if sim1: print_sim_compact(sim1)
    else:    print("  [ARB 1] aguardando dados...")

    sim2 = simulate_arb(2)
    if sim2: print_sim_compact(sim2)
    else:    print("  [ARB 2] aguardando dados...")

    # ── Status execução corrente ──────────────────────────────────────────
    if state["executing"]:
        print()
        print("  \033[33m▶ EXECUTANDO ORDENS AGORA...\033[0m")

    # ── PAPER MODE: status da arb composta em observação ─────────────────
    if PAPER_MODE:
        sess     = state["paper_session"]
        open_pos = state.get("paper_open")
        print()
        print("  \033[36m" + "─" * 78 + "\033[0m")
        if open_pos is not None:
            age   = int(time.time()) - int(open_pos["ts_unix"])
            need  = "ARB2" if open_pos["arb_id"] == 1 else "ARB1"
            print(f"  \033[36;1m▶ PAPER ENTRY ABERTA\033[0m  arb{open_pos['arb_id']} "
                  f"| {open_pos['contracts']:.2f}c @ ${open_pos['cost_usd']:.2f} "
                  f"| edge {open_pos['profit_pct_at_entry']:.2f}% | idade {age}s")
            print(f"     aguardando hedge {need} com edge ≥{PAPER_HEDGE_PCT:.1f}% "
                  f"(senão NAKED ao virar a rodada)")
        else:
            print(f"  \033[36mPAPER MODE\033[0m  sem entrada aberta — "
                  f"esperando ARB1 ou ARB2 com edge ≥{PAPER_ENTRY_PCT:.1f}%")
        print(f"     entries={sess['entries']}  "
              f"\033[32mclosed_ok={sess['closed_ok']}\033[0m  "
              f"\033[31mnaked={sess['naked_at_close']}\033[0m  "
              f"PnL paper acum.: \033[32m${sess['total_paper_pnl']:+.2f}\033[0m")
        if sess['closed_ok'] > 0:
            print(f"     melhor par: ${sess['best_pair_pnl']:+.2f}  "
                  f"pior par: ${sess['worst_pair_pnl']:+.2f}")
        print("  \033[36m" + "─" * 78 + "\033[0m")

    # ── Histórico unificado (substitui o orderbook Kalshi) ────────────────
    print()
    _render_history_block()

    print()
    print(f"  \033[90mAtualizado: {datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}\033[0m")


# ═══════════════════════════════════════════════════════════════════════════════
# TRIGGER MANUAL
# ═══════════════════════════════════════════════════════════════════════════════
_manual_trigger_queue: asyncio.Queue = None

def _keyboard_listener(loop):
    while True:
        try:
            ch = sys.stdin.read(1)
            if ch in ('1', '2') and _manual_trigger_queue is not None:
                asyncio.run_coroutine_threadsafe(
                    _manual_trigger_queue.put(int(ch)), loop
                )
        except Exception:
            break

async def manual_trigger_handler(private_key):
    while True:
        arb_id = await _manual_trigger_queue.get()

        # ── PAPER MODE: dispara entrada/hedge virtual ────────────────────
        if PAPER_MODE:
            sim = simulate_arb(arb_id)
            if sim is None:
                print(f"\n\033[31m[PAPER MANUAL] ARB{arb_id}: sem dados.\033[0m")
                continue
            if not (sim["ok_a"] and sim["ok_b"]):
                print(f"\n\033[31m[PAPER MANUAL] ARB{arb_id}: liquidez insuficiente.\033[0m")
                continue
            print(f"\n\033[33m[PAPER MANUAL] Forçando observação ARB{arb_id} "
                  f"(ignora thresholds)\033[0m")
            # Bypass de threshold: chama paper_observe com profit_pct artificialmente
            # alto pra forçar a entrada/hedge virtual
            sim_forced = dict(sim)
            sim_forced["profit"] = max(sim["profit"], BUDGET * (PAPER_ENTRY_PCT/100.0) + 0.01)
            paper_observe(sim_forced)
            continue

        if _is_blocked_by_uncovered():
            print(f"\n\033[31m[MANUAL] ARB{arb_id}: BLOQUEADO — posição Poly descoberta. "
                  f"Aguarde hedge ou fim da rodada.\033[0m")
            continue
        sim = simulate_arb(arb_id)
        if sim is None:
            print(f"\n\033[31m[MANUAL] ARB{arb_id}: sem dados.\033[0m")
        elif not (sim["ok_a"] and sim["ok_b"]):
            print(f"\n\033[31m[MANUAL] ARB{arb_id}: liquidez insuficiente.\033[0m")
        elif (sim["profit"] / BUDGET * 100) < MIN_PROFIT_PCT:
            print(f"\n\033[31m[MANUAL] ARB{arb_id}: lucro < {MIN_PROFIT_PCT}%.\033[0m")
        else:
            print(f"\n\033[33m[MANUAL] Executando ARB{arb_id}...\033[0m")
            await execute_arb(sim, private_key)


# ═══════════════════════════════════════════════════════════════════════════════
# KALSHI WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════
async def kalshi_ws(private_key):
    WS_URL  = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    WS_PATH = "/trade-api/ws/v2"

    while True:
        headers = make_auth_headers(private_key, "GET", "/trade-api/v2/markets")
        try:
            if HTTPX_OK:
                http = await _get_http_client()
                resp = await http.get(
                    "https://api.elections.kalshi.com/trade-api/v2/markets",
                    params={"series_ticker": "KXBTC15M", "status": "open"},
                    headers=headers, timeout=5.0,
                )
                markets = resp.json().get("markets", [])
            else:
                loop = asyncio.get_event_loop()
                _resp = await loop.run_in_executor(
                    None,
                    lambda: requests.get(
                        "https://api.elections.kalshi.com/trade-api/v2/markets",
                        params={"series_ticker": "KXBTC15M", "status": "open"},
                        headers=headers, timeout=5
                    )
                )
                markets = _resp.json().get("markets", [])

            expected_ts     = _current_round_ts()
            target_close_ts = expected_ts + 900
            market_ticker   = None

            for m in markets:
                close_time_str = m.get("close_time", "")
                if close_time_str:
                    try:
                        dt = datetime.datetime.strptime(
                            close_time_str.split(".")[0].replace("Z", ""),
                            '%Y-%m-%dT%H:%M:%S'
                        )
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                        if int(dt.timestamp()) == target_close_ts:
                            market_ticker = m["ticker"]
                            break
                    except Exception:
                        pass

            if not market_ticker:
                target_str = datetime.datetime.fromtimestamp(
                    target_close_ts, tz=datetime.timezone.utc
                ).strftime('%H:%M:%SZ')
                state["kalshi_ticker"]  = f"Aguardando Kalshi (alvo {target_str})..."
                state["kalshi_yes_ask"] = None
                state["kalshi_yes_bid"] = None
                await asyncio.sleep(5)
                continue

            state["kalshi_ticker"] = market_ticker
        except Exception as e:
            print(f"Erro REST Kalshi: {e}")
            await asyncio.sleep(5)
            continue

        kalshi_orderbook["yes"] = {}
        kalshi_orderbook["no"]  = {}
        state["kalshi_yes_ask"] = None
        state["kalshi_yes_bid"] = None

        try:
            ws_headers = make_auth_headers(private_key, "GET", WS_PATH)
            async with websockets.connect(WS_URL, additional_headers=ws_headers) as websocket:
                await websocket.send(json.dumps({
                    "id": 1, "cmd": "subscribe",
                    "params": {"channels": ["orderbook_delta"],
                               "market_tickers": [market_ticker]}
                }))

                async for message in websocket:
                    data     = json.loads(message)
                    msg_type = data.get("type")
                    msg      = data.get("msg", {})

                    if msg_type == "orderbook_snapshot":
                        apply_orderbook_snapshot(msg)
                        check_arbitrage()
                    elif msg_type == "orderbook_delta":
                        apply_orderbook_delta(msg)
                        check_arbitrage()
                    elif msg_type == "error":
                        error_msg = data.get('msg', {}).get('message', str(data))
                        state["kalshi_ticker"] = f"ERRO WS: {error_msg}"
                        check_arbitrage()

                    if (AUTO_EXECUTE and not state["executing"]
                            and _safe_window() and not _is_blocked_by_uncovered()):
                        for arb_id in (1, 2):
                            sim = simulate_arb(arb_id)
                            if (sim and sim["ok_a"] and sim["ok_b"]
                                    and sim["profit"] > 0
                                    and sim["profit"] / BUDGET * 100 >= MIN_PROFIT_PCT):
                                asyncio.create_task(execute_arb(sim, private_key))
                                break

                    # ── PAPER MODE: observa sem executar ────────────────────
                    if PAPER_MODE and _safe_window():
                        for arb_id in (1, 2):
                            sim = simulate_arb(arb_id)
                            if sim:
                                paper_observe(sim)

                    if _current_round_ts() != expected_ts:
                        break

        except websockets.ConnectionClosed:
            await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def _check_env():
    # ── Versão pública: credenciais neutralizadas ──────────────────────────
    #   Ver o bloco CONFIG no topo do arquivo. Esta checagem existe para que o
    #   programa pare aqui, de forma explícita, em vez de falhar de maneira
    #   confusa na primeira chamada autenticada.
    if CREDENTIALS_REDACTED:
        print("\033[33m" + "═" * 70)
        print(" VERSÃO PÚBLICA — CREDENCIAIS REMOVIDAS")
        print("═" * 70 + "\033[0m")
        print()
        print(" Este repositório é documentação técnica, não uma ferramenta")
        print(" operacional. O bloco de credenciais foi neutralizado e o bot")
        print(" não autentica nem envia ordens.")
        print()
        print(" A estratégia não funciona por um motivo estrutural: Kalshi e")
        print(" Polymarket resolvem sobre referências de preço diferentes e")
        print(" divergem em ~12% das rodadas. O README documenta a análise")
        print(" completa, a engenharia, e por que a premissa é inválida.")
        print()
        print(" Kalshi e Polymarket estão atualmente proibidos no Brasil.")
        print()
        sys.exit(0)

    required = {
        "KALSHI_KEY_ID":     KALSHI_KEY_ID,
        "PRIVATE_KEY":       POLY_PRIVATE_KEY,
        "POLY_API_KEY":      POLY_API_KEY,
        "POLY_API_SECRET":   POLY_API_SECRET,
        "POLY_API_PASS":     POLY_API_PASS,
        "POLY_PROXY_WALLET": POLY_PROXY_WALLET,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        print("\033[31m[ERRO] Credenciais não configuradas:\033[0m")
        for k in missing:
            print(f"  - {k}")
        sys.exit(1)


async def main():
    global _manual_trigger_queue

    print("═" * 70)
    print(" ARB MONITOR v17 (fix: round_exec_count vs hedge duplicate arbs)")
    if PAPER_MODE:
        print(" \033[36m▶ PAPER MODE ATIVO — NENHUMA ORDEM REAL SERÁ ENVIADA\033[0m")
        print(f"   entry_thr={PAPER_ENTRY_PCT:.1f}%  hedge_thr={PAPER_HEDGE_PCT:.1f}%  "
              f"csv={PAPER_OPS_CSV_FILE}")
    print("═" * 70)

    _check_env()
    _ensure_operations_csv()

    log_section(f"SESSÃO INICIADA — v17 — {datetime.datetime.now().isoformat()}")
    log_trade(
        f"[STARTUP] budget=${BUDGET:.2f} min_profit={MIN_PROFIT_PCT}% "
        f"cooldown={EXEC_COOLDOWN}s max_exec_round={MAX_EXECS_PER_ROUND} "
        f"auto_exec={AUTO_EXECUTE} mode={MODE}"
    )
    log_trade(
        f"[STARTUP] hedge_uncovered retry={UNCOVERED_HEDGE_RETRY_S}s "
        f"max_loss_tol={UNCOVERED_HEDGE_MAX_LOSS_CENTS}¢ "
        f"block_new_arbs={UNCOVERED_BLOCK_NEW_ARBS}"
    )

    print(f"  Poly proxy wallet: {POLY_PROXY_WALLET}")
    print(f"  Budget por exec  : ${BUDGET:.2f}")
    print(f"  Lucro mínimo     : {MIN_PROFIT_PCT}%")
    print(f"  Cooldown         : {EXEC_COOLDOWN}s")
    print(f"  Max exec/rodada  : {MAX_EXECS_PER_ROUND}")
    print(f"  Auto-execute     : {AUTO_EXECUTE}")
    print(f"  Operations CSV   : {OPERATIONS_CSV_FILE}")

    try:
        private_key = load_private_key_from_file(KALSHI_KEY_FILE)
        print("  ✓ Chave Kalshi carregada")
    except Exception as e:
        print(f"  ✗ Erro chave Kalshi: {e}")
        return

    print("  → Inicializando Poly client...")
    poly_client = get_poly_client()
    if poly_client and POLY_SDK_OK:
        try:
            bal   = poly_client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type="COLLATERAL")
            )
            saldo = int(bal.get("balance", 0)) / 1e6
            print(f"  ✓ Saldo Poly: ${saldo:.2f}")
        except Exception as e:
            print(f"  ⚠ Sem check de saldo: {e}")

    print("═" * 70)
    print(" Pressione [1] ou [2] para triggerar manualmente a arb correspondente")
    print("═" * 70)

    # ── Inicializa CSV queue + writer task (I/O assíncrono) ─────────────
    global _csv_queue, _csv_writer_task
    _csv_queue = asyncio.Queue()
    _csv_writer_task = asyncio.create_task(_csv_writer_loop())

    # ── Pré-aquece o cliente httpx (abre conexões TCP/TLS) ──────────────
    #     Isto não faz I/O real — só instancia o AsyncClient.
    if HTTPX_OK:
        await _get_http_client()
        print(f"  ✓ httpx AsyncClient pronto (HTTP/2 + keep-alive)")
    else:
        print(f"  ⚠ httpx não disponível — usando 'requests' (mais lento)")

    _manual_trigger_queue = asyncio.Queue()
    loop      = asyncio.get_event_loop()
    kb_thread = threading.Thread(target=_keyboard_listener, args=(loop,), daemon=True)
    kb_thread.start()

    try:
        # ── Em PAPER_MODE, não startamos tasks que só fazem sentido com ──
        #    posição real (saldo, hedge_loop de descoberto). Mantemos só os
        #    websockets, o handler manual e o paper observer fica embutido
        #    nos próprios websocket clients.
        tasks = [
            asyncio.create_task(poly_ws_client(private_key)),
            asyncio.create_task(kalshi_ws(private_key)),
            asyncio.create_task(manual_trigger_handler(private_key)),
        ]
        if not PAPER_MODE:
            tasks.append(asyncio.create_task(balance_monitor_task(private_key)))
            tasks.append(asyncio.create_task(uncovered_hedge_loop(private_key)))
        await asyncio.gather(*tasks)
    finally:
        # Shutdown graceful: drena CSV queue e fecha HTTP client
        log_trade("[SHUTDOWN] Drenando CSV queue e fechando conexões...")
        try:
            await _csv_queue.put(None)  # sinaliza writer pra encerrar
            await asyncio.wait_for(_csv_writer_task, timeout=3.0)
        except Exception as e:
            log_trade(f"[SHUTDOWN CSV erro] {e}")
        await _close_http_client()
        log_trade("[SHUTDOWN] ok")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor encerrado.")