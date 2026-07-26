# Dados

Saídas de análise que sustentam os números do [README](../README.md) e do
[POST-MORTEM](../POST-MORTEM.md). São dados de mercado e resultados de
processamento, não registros de conta.

| arquivo | linhas | o que é |
|---|---|---|
| `divergence_report.csv` | 96 | Uma linha por rodada liquidada. Compara o strike da Kalshi com o `priceToBeat` da Polymarket e o resultado real de cada plataforma. **É a evidência da taxa de divergência.** |
| `divergence_vs_naked.csv` | 96 | O mesmo conjunto, cruzado com a posição que o bot carregava em cada rodada. Mostra em quais divergências eu estava exposto. |
| `paper_compound_ops.csv` | 163 | Log do paper trading da arbitragem de 4 pernas: entradas, hedges, e as que ficaram sem par. Sustenta os 59 pares fechados e os 26,8% naked. |
| `operations.csv` | 25 | Execuções reais do bot, com preços alvo, preços enviados, duração e resultado. |
| `arb_timeseries_report.csv` | 275 | Varredura histórica rodada a rodada: quantos pontos alinhados, melhor edge no meio do book e no executável. |

## Colunas que importam

**`divergence_report.csv`** — o arquivo central.

| coluna | significado |
|---|---|
| `kalshi_strike` | Strike da rodada, sobre o índice próprio da Kalshi |
| `poly_price_to_beat` | Preço Chainlink na abertura da janela, referência da Polymarket |
| `delta_poly_minus_kalshi` | O gap entre as duas réguas. O sinal determina qual divergência é possível |
| `kalshi_result` / `poly_outcome` | Como cada plataforma efetivamente resolveu |
| `classification` | `AGREE` ou `DIVERGE` |

Para reproduzir a taxa de divergência:

```python
import csv
rows = list(csv.DictReader(open("divergence_report.csv", encoding="utf-8")))
div = [r for r in rows if r["classification"] == "DIVERGE"]
print(len(div) / len(rows))          # fração de rodadas divergentes
neg = [r for r in rows if float(r["delta_poly_minus_kalshi"]) < 0]
print(len(neg) / len(rows))          # fração de deltas negativos
```

## Nota sobre amostragem

O `divergence_report.csv` publicado aqui cobre **96 rodadas** (cerca de um dia).
A taxa de 12% citada no README vem de uma janela maior, de ~30 dias, que não
está neste repositório porque depende de consultas autenticadas às duas APIs
para reconstruir os resultados de liquidação. Nesta amostra menor a divergência
fica em 9,4%. A diferença entre as duas medições está discutida no README, e
nenhuma conclusão muda entre 9% e 12%.

## Redações

O `operations.csv` teve as colunas `kalshi_order_id` e `poly_order_id`
substituídas por `REDACTED`. Os IDs de ordem da Polymarket são hashes que
aparecem nos eventos `OrderFilled` do contrato na Polygon, o que permitiria
recuperar o endereço da carteira. Eles não têm valor analítico.

Registros de conta (depósitos, saques, PnL realizado, transações on-chain) não
fazem parte deste repositório.
