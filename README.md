# Arbitragem Kalshi × Polymarket (BTC 15 min)

Um bot de arbitragem entre dois mercados de previsão, no contrato de 15 minutos do Bitcoin.

**Status: não funciona.** Não por bug, não por lentidão, não por falta de capital. Não funciona por um motivo conceitual, na fundação da ideia. Este README é sobre esse motivo, e sobre tudo que eu construí antes de entendê-lo.

> **Aviso legal.** Kalshi e Polymarket estão atualmente proibidos no Brasil. Este projeto foi desenvolvido e testado em abril de 2026, antes da vigência da restrição. O código é publicado aqui como documentação técnica e registro de aprendizado, não como ferramenta operacional. Não rode isso.

---

## Por que publicar um projeto que não funciona

Esse projeto foi desenvolvido em abril de 2026 e só agora estou publicando.

E por que estou publicando, você me pergunta? Porque ele não funciona. Fundamentalmente não funciona. Mas ele me trouxe muito aprendizado e muita reflexão, e a parte mais valiosa não é o código que ficou de pé, é o entendimento de *por que* ele nunca poderia funcionar.

Tem uma diferença grande entre "meu bot deu prejuízo" e "meu bot estava medindo a coisa errada". O primeiro é um problema de engenharia. O segundo é um problema de premissa, e nenhuma quantidade de otimização resolve. Eu passei semanas resolvendo o primeiro antes de perceber que o segundo existia.

Este documento é a autópsia. Ele começa do zero (o que é arbitragem, por que ela funciona) e vai afunilando até microestrutura de mercado, taxas dinâmicas, e o problema de ficar preso numa perna. Se você só quer o veredito, pule para [O erro fundamental](#parte-2-o-erro-fundamental-duas-réguas-para-o-mesmo-bitcoin).

---

## Parte 1. O que é arbitragem

### O exemplo da moeda

Arbitragem é, basicamente, comprar as duas pontas. Numa analogia: fazer uma aposta em que você aposta na cara **e** na coroa, na mesma jogada. Você tira o risco de estar na ponta certa, porque você está nas duas.

A pergunta óbvia: qual a necessidade disso? Se eu apostar R$ 0,50 que vai dar cara e R$ 0,50 que vai dar coroa, eu vou ganhar R$ 1,00 de um lado e perder R$ 0,50 do outro. Empatei. Gastei R$ 1,00 e recebi R$ 1,00.

Isso é verdade **se** a compra das duas apostas sempre somar R$ 1,00, ou seja, 100%. Mas isso nem sempre é verdade.

Em mercados de apostas e mercados de previsão, as *odds* (o preço pelo qual você compra a chance) flutuam ao longo do tempo. Elas são movidas por fluxo de ordens, por notícias, por quem está com pressa de entrar ou sair. E em algum momento (se você estiver olhando) a soma das duas pontas fica abaixo de 1.

Se você consegue comprar cara a R$ 0,48 e coroa a R$ 0,50, você gastou R$ 0,98 e tem **certeza** de receber R$ 1,00. Seu lucro é R$ 0,02, e ele não depende do resultado da moeda. Não importa se dá cara ou coroa: uma das duas pontas paga R$ 1,00.

Esse é o conceito geral. E foi exatamente isso que eu tentei fazer, automatizado, no mercado de 15 minutos do Bitcoin.

### Por que a soma fica abaixo de 1

Vale entender de onde vem o dinheiro, porque isso diz onde procurar.

Num mercado só, a soma das pontas raramente fica abaixo de 1: a casa embute uma margem justamente para que fique *acima* (é assim que ela ganha). O que abre a janela é comparar **dois mercados diferentes** que negociam o mesmo evento.

Kalshi e Polymarket são duas bolsas independentes, com livros de ordens independentes, participantes diferentes, e velocidades de atualização diferentes. Quando o Bitcoin se move, os dois reprecificam, mas não no mesmo instante nem no mesmo tamanho. Nesse descompasso, o "SIM" de uma fica barato ao mesmo tempo que o "NÃO" da outra ainda não subiu.

Comprar YES na Kalshi por US$ 0,48 e NO na Polymarket por US$ 0,50 é comprar as duas pontas do mesmo evento por US$ 0,98. Uma das duas *tem* que pagar US$ 1,00.

Guarde essa última frase. É ali que tudo desmorona.

### A condição que ninguém menciona

Tem um requisito no exemplo da moeda que é tão óbvio que passa despercebido: **as duas apostas precisam ser sobre a mesma jogada.**

Se você aposta cara na jogada 1 e coroa na jogada 2, não funciona. Pode dar coroa na jogada 1 e cara na jogada 2, e você perde as duas. Os eventos não são o mesmo evento, e sem isso, você não tem uma arbitragem, você tem duas apostas independentes que por acaso apontam para lados opostos.

**Esse foi o erro deste bot.**

Kalshi e Polymarket, por mais que ambas olhem para o mesmo ativo (o Bitcoin), não estão olhando para a mesma jogada. E o Bitcoin não tem *um* preço: ele tem dezenas de cotações, em dezenas de exchanges, e cada mercado de previsão escolhe a sua régua.

---

## Parte 2. O erro fundamental: duas réguas para o mesmo Bitcoin

Aqui está a diferença que mata o projeto:

| | Kalshi (`KXBTC15M`) | Polymarket (`btc-updown-15m`) |
|---|---|---|
| **Pergunta** | "BTC está acima do strike X?" | "O preço no fim é ≥ o preço no início?" |
| **Referência** | Strike fixo, definido na abertura da rodada, sobre o índice próprio da Kalshi | Oráculo **Chainlink** BTC/USD, comparando fim vs. início |
| **Tipo** | Nível absoluto | Variação relativa |

São duas perguntas diferentes, com duas fontes de preço diferentes, sobre dois instantes de referência diferentes.

Na maior parte do tempo elas concordam, porque o Bitcoin se move mais do que a diferença entre as réguas. Mas quando o preço fecha **entre** as duas referências, os mercados resolvem em direções opostas, e a "arbitragem" deixa de ser arbitragem.

### Medindo o tamanho do problema

Eu escrevi um script para comparar, rodada a rodada, o strike da Kalshi contra o `priceToBeat` da Polymarket (o preço Chainlink no início da janela). Rodando isso sobre **~30 dias** de rodadas liquidadas:

```
delta = priceToBeat (Chainlink)  −  strike (Kalshi)

  mediana absoluta   $  7,25
  média absoluta     $  8,96
  p90                $ 19,28
  máximo             $ 36,57

  delta com sinal:   mediana −$6,08   (79% das rodadas negativas)
```

Numa janela de 15 minutos, o Bitcoin frequentemente se move **menos** de US$ 7. Ou seja: a distância entre as duas réguas é rotineiramente maior que o movimento que elas estão tentando medir.

O resultado:

```
  88% concordaram
  12% DIVERGIRAM
```

**Uma em cada oito rodadas, os dois mercados apontaram para lados opostos.**

Uma nota sobre esse número: numa amostra menor, de 96 rodadas consecutivas, eu tinha medido 9,4%. Ampliando a janela para ~30 dias, o valor sobe para 12%. Vou usar 12% daqui em diante, porque é a amostra maior, mas a ordem de grandeza é a mesma, e nenhuma das conclusões abaixo muda se for 9% ou 12%. Isso por si só já diz alguma coisa: quando a sua tese depende de um evento acontecer *menos de 1% das vezes* e ele acontece em *mais de 10%*, a precisão da medição vira detalhe.

### O que acontece numa divergência

Aqui eu preciso corrigir uma intuição que eu mesmo tinha errada no começo, e que é a parte mais interessante da história.

Divergência **não é sinônimo de perda.** Considere a arb #1: comprar YES na Kalshi + NO na Polymarket, pagando ~US$ 0,98 pelo par.

| Cenário | Kalshi YES paga | Poly NO paga | Você recebe | Resultado |
|---|---|---|---|---|
| Concordam (ambos "subiu") | $1 | $0 | **$1,00** | +$0,02 ✓ |
| Concordam (ambos "desceu") | $0 | $1 | **$1,00** | +$0,02 ✓ |
| Divergem: Kalshi YES, Poly DOWN | $1 | $1 | **$2,00** | **+$1,02** 🎉 |
| Divergem: Kalshi NO, Poly UP | $0 | $0 | **$0,00** | **−$0,98** 💀 |

Quando os mercados divergem, ou as **duas** pernas ganham, ou as **duas** perdem. É uma moeda.

E é exatamente por isso que o projeto está morto, mas não pelo motivo que parece.

### A moeda é viciada

Aqui a coisa fica interessante, e é onde eu errei na minha primeira análise.

Meu primeiro instinto foi tratar a divergência como uma moeda justa: metade das vezes as duas pernas ganham, metade das vezes as duas perdem. Se for assim, o valor esperado é:

```
EV = 88% × (+$0,02)  +  6% × (+$1,02)  +  6% × (−$0,98)  =  +$0,0200
```

E aqui tem uma armadilha matemática bonita: **sob simetria, o EV não muda, não importa quanto seja a taxa de divergência.** Os payoffs são $0, $1 e $2, simétricos em torno de $1. Uma divergência simétrica tem média de payoff exatamente $1, igual ao caso em que os mercados concordam. Você pode colocar 12%, 30%, 50% de divergência que o EV continua sendo `1 − custo`. A conta é invariante.

Isso me levou à conclusão errada de que o problema era só variância.

**Mas a divergência não é simétrica. E não é nem um pouco aleatória.**

Lembra que 79% dos deltas eram negativos, com o `priceToBeat` da Chainlink tipicamente **abaixo** do strike da Kalshi? Isso não é ruído estatístico, é estrutural. E tem uma consequência que eu demorei a enxergar: **o sinal do delta determina qual tipo de divergência pode acontecer.**

Pense na reta de preço. Você tem duas réguas: a da Kalshi (o strike) e a da Poly (o `priceToBeat`). O BTC fecha em algum lugar.

- Fecha **acima das duas** → Kalshi YES, Poly UP → concordam
- Fecha **abaixo das duas** → Kalshi NO, Poly DOWN → concordam
- Fecha **entre elas** → divergem

E aqui está o ponto: se a régua da Poly está **abaixo** da régua da Kalshi (delta negativo), a única faixa possível de divergência é "acima da Poly, abaixo da Kalshi", ou seja, **Poly UP + Kalshi NO**. O outro tipo de divergência é geometricamente impossível.

Com delta positivo, inverte: só pode dar Kalshi YES + Poly DOWN.

Não é probabilístico. É determinado pela geometria. E os dados confirmam: o sinal do delta previu corretamente a direção em **7 das 9 divergências** observadas (as duas exceções vêm de o índice de liquidação da Kalshi não ser exatamente o preço Chainlink, o que adiciona um pouco de ruído em cima da mecânica).

Como 79% dos deltas são negativos, 79% das divergências são do tipo "Kalshi NO + Poly UP". Agora olhe a tabela de payoff da seção anterior e veja o que isso faz com cada lado da operação:

```
arb #1  (YES Kalshi + NO Poly)
  ganha $2 só quando delta > 0   → 21% das divergências
  perde tudo quando delta < 0    → 79% das divergências
  EV = −$0,0496 / contrato       ← NEGATIVO
  por rodada (7 contratos): −$0,35

arb #2  (NO Kalshi + YES Poly)
  ganha $2 quando delta < 0      → 79% das divergências
  perde tudo quando delta > 0    → 21% das divergências
  EV = +$0,0896 / contrato
  por rodada (7 contratos): +$0,63
```

**A arb #1 tem valor esperado negativo.** Não "positivo com muito risco": negativo. Cada vez que o bot executava esse lado, ele estava, em média, perdendo dinheiro. E ele executou esse lado **36 vezes em 82 entradas**, sem nunca distinguir um caso do outro. Para o bot, arb #1 e arb #2 eram a mesma coisa: duas maneiras equivalentes de montar a mesma arbitragem.

Não eram. Uma era uma sangria lenta, a outra era uma aposta direcional disfarçada.

### Por que nem o lado "bom" salva

O EV combinado do mix real do bot (36 arb #1 + 46 arb #2) dá +$0,0285 por contrato. Levemente positivo. Isso poderia sugerir um conserto óbvio: filtre pelo sinal do delta, opere só a arb #2, colete os +$0,09.

Não. Por três motivos, e cada um sozinho basta.

**Primeiro: isso deixa de ser arbitragem.** O lucro da arb #2 não vem mais de comprar as duas pontas por menos de $1. Vem de apostar que o oráculo Chainlink continuará precificando o BTC sistematicamente abaixo do índice da Kalshi. Isso é uma aposta direcional sobre a relação entre duas fontes de preço. Pode ser um bom trade, mas é um trade *completamente diferente*, com risco completamente diferente, e chamá-lo de arbitragem é mentir para si mesmo sobre o que está no livro.

**Segundo: o edge repousa sobre nove observações.** Nove divergências. O intervalo de confiança disso é largo o suficiente para conter zero, o dobro, e o negativo. E o viés de 79% depende de detalhes de implementação dos dois oráculos: latência de atualização da Chainlink, composição do índice da Kalshi, qual exchange pesa mais em cada um. Nada disso é contratual. Qualquer um dos dois pode recalibrar sem aviso e o sinal inverte. Otimizar para isso seria trocar um erro conceitual por um overfit em cima de nove pontos, o que é, provavelmente, o erro pior.

**Terceiro: a variância engole o edge de qualquer forma.**

```
EV por contrato: +$0,0285
Desvio por contrato:  $0,3393        ← 12× o retorno
Pior caso: −$6,86 por rodada  (98% da banca)
```

Numa arbitragem de verdade, o desvio padrão é **zero**. É literalmente a definição. Você paga $0,98, recebe $1,00, e não existe cenário em que isso não aconteça.

Aqui o desvio é doze vezes o retorno. Quantas rodadas para ter 95% de confiança de que esse edge é diferente de zero?

```
n = (1,96 × 0,3393 / 0,0285)² ≈ 545 rodadas
                               ≈ 5,7 dias de operação ininterrupta
```

Quase seis dias operando sem parar, arriscando quase a banca inteira a cada 15 minutos, **só para descobrir se o edge existe**, e isso assumindo que o viés de 79% se mantenha estável durante todo o teste. Duas ou três divergências ruins em sequência zeram a conta antes de o teste terminar. Com banca de US$ 7 e exposição de US$ 6,86 por rodada, o risco de ruína chega muito antes da significância estatística.

Esse é o veredito da Parte 2. A operação não é uma arbitragem com risco residual: é uma aposta direcional sobre discrepância de oráculos, com um dos dois lados sendo estruturalmente perdedor, e o bot escolhendo entre eles no cara ou coroa.

---

## Parte 3. Tudo que o exemplo da moeda esconde

O exemplo lá do começo assume um mundo que existe: liquidez infinita, execução instantânea, sem taxas, e as duas pernas preenchendo simultaneamente. Cada uma dessas premissas quebra na prática, e cada quebra virou um subsistema deste código.

Vale dizer: mesmo que o problema da divergência não existisse, **estes obstáculos sozinhos já tornariam a operação marginal.** Eles não são detalhes de implementação, são o negócio em si.

### 3.1. Liquidez não é infinita

No exemplo, você compra "cara a 0,48". Quanto? A pergunta não faz sentido no exemplo, porque assume-se que dá para comprar o quanto quiser naquele preço.

No livro real, US$ 0,48 é o preço do **melhor nível**, e ele tem tamanho finito. Se você quer 7 contratos e o melhor nível tem 3, os outros 4 vêm de níveis piores. Seu preço médio de execução não é 0,48: é algo entre 0,48 e o que for necessário para preencher.

Isso importa muito quando o lucro inteiro é de 2 centavos. Um único nível a mais consumido pode apagar a operação.

O bot nunca usa o topo do livro como preço. Ele **caminha o livro** (`_walk_asks`, `_walk_bids_as_ask`), nível a nível, acumulando tamanho e custo, e calcula o preço médio real de execução para o tamanho que ele quer:

```python
def _walk_asks(asks_sorted, contracts_needed):
    filled, cost = 0.0, 0.0
    for price, size in asks_sorted:
        can_fill = min(size, contracts_needed - filled)
        filled += can_fill
        cost   += can_fill * price
        if filled >= contracts_needed - 1e-9:
            break
    sufficient = filled >= contracts_needed - 1e-9
    return cost / filled if filled else None, filled, cost, sufficient
```

O `sufficient` é a parte importante: se o livro inteiro não cobre o tamanho desejado, a oportunidade é **descartada**, não executada parcialmente. Uma arb com uma perna incompleta não é uma arb.

Um detalhe da Kalshi que confunde: **não existe "ask" no livro.** O livro tem apenas bids dos dois lados. O ask de YES é derivado dos bids de NO:

```
ask_YES = 1 − melhor_bid_NO
```

Faz sentido quando você pensa que YES e NO são complementares, quem oferece comprar NO a 0,52 está implicitamente oferecendo vender YES a 0,48. Mas significa que, para comprar YES, você atravessa o livro de **NO**, e toda a lógica de profundidade tem que ser espelhada.

### 3.2. Execução não é instantânea

No exemplo, as duas apostas acontecem no mesmo instante. Na realidade existe um intervalo entre você *ver* o preço e a ordem *chegar* na bolsa, e nesse intervalo o preço muda.

Isso é especialmente cruel numa arbitragem, porque a oportunidade existe *porque* os preços estão momentaneamente desalinhados. O mesmo mecanismo que cria a janela (o mercado reprecificando) é o que a fecha. Você está correndo contra o próprio sinal que te chamou.

O código roda em **~300–800 ms** no caminho crítico rodando da minha máquina, no Brasil, pela internet residencial. Boa parte disso é puro tempo de viagem até os servidores. Rodando numa instância AWS próxima às exchanges, isso cai para a **casa dos 80 ms**, quase uma ordem de grandeza, sem trocar uma linha de código. A Parte 4 é sobre a corrida por esses milissegundos.

### 3.3. Taxas

Talvez a parte mais subestimada. Ambas as plataformas cobram taxa com a mesma forma funcional:

```
taxa ∝ coef × preço × (1 − preço)
```

Repare no formato: `p × (1 − p)` é uma parábola com **máximo exato em p = 0,50**. A taxa é mais cara justamente quando o mercado está mais indeciso.

E onde vive um mercado binário de "BTC sobe ou desce nos próximos 15 minutos"? Em torno de 50/50, quase sempre. **O bot operava permanentemente no pico da curva de taxa.**

Concretamente, com os coeficientes usados:

```
Polymarket: 0,10 × 0,5 × 0,5 = 2,50% do notional
Kalshi: 0,07 × 0,5 × 0,5 = 1,75% do notional
                                ─────
                          total ≈ 4,25%
```

O spread bruto precisa passar de **4,25%** só para empatar. O `MIN_PROFIT_PCT` do bot era 2,5% **líquido**, o que exige um spread bruto de quase 7%. Oportunidades desse tamanho existem, mas são raras e desaparecem rápido, o que explica por que, em 25 tentativas reais, 16 foram rejeitadas por movimento de preço antes de preencher.

Um detalhe específico da Kalshi que morde em posições pequenas:

```python
def calc_kalshi_fee(price, contracts):
    fee_cents = math.ceil(0.07 * contracts * price * (1 - price) * 100.0)
    return fee_cents / 100.0
```

O `math.ceil` arredonda **para cima, em centavos inteiros**. Com 7 contratos, a taxa teórica pode ser $0,086 e você paga $0,09. Parece nada, mas o lucro alvo da operação inteira era da ordem de $0,15. O arredondamento sozinho consome uma fração relevante do resultado. Posições pequenas são estruturalmente penalizadas.

#### A taxa da Polymarket é cobrada em *shares*, não em dinheiro

Essa foi a fonte do bug mais persistente do projeto. Na Polymarket, a taxa sai da **quantidade de shares** que você recebe, não do dinheiro que você paga.

Você compra 7 shares. Chegam 6,7. As 0,3 que faltam são a taxa.

Só que a perna da Kalshi é de **7 contratos inteiros**. Se a Polymarket te entrega 6,7, você está *descoberto em 0,3 contrato*. A arbitragem tem um buraco.

A solução é comprar bruto a mais para que o líquido bata:

```python
def poly_gross_for_net(net_shares, price, token_id=None):
    coef = get_poly_fee_coef(token_id)
    fee_pct = coef * price * (1.0 - price)
    return net_shares / (1.0 - fee_pct)
```

Durante semanas eu operei com um sintoma que anotei como *"estou colocando um pouco menos"*: a perna da Poly vinha sistematicamente menor que a da Kalshi, e eu não sabia por quê. A causa: `POLY_FEE_COEF` estava fixo em `0.072`. Quando a taxa real do mercado era maior que isso, o *gross-up* comprava menos do que o necessário e a diferença virava exposição descoberta, silenciosamente, em toda operação.

A correção foi em três camadas, todas defensivas:

1. **Fallback elevado**: `0.072 → 0.10`, para errar por excesso.
2. **Margem de segurança**: `POLY_FEE_SAFETY_MULT = 1.10` multiplica toda estimativa. Comprar 10% de bruto a mais e sobrar é infinitamente melhor que faltar.
3. **Lookup dinâmico**: na primeira ordem de cada token, consulta a taxa real do mercado e guarda em cache.

```python
def get_poly_fee_coef(token_id=None):
    base = POLY_FEE_COEF                       # fallback conservador
    if token_id and token_id in _poly_fee_rate_cache:
        base = _poly_fee_rate_cache[token_id]  # valor real do mercado
    return base * POLY_FEE_SAFETY_MULT         # margem, sempre
```

O princípio geral que ficou: **quando o erro é assimétrico, enviese a estimativa para o lado barato.** Sobrar shares custa alguns centavos. Faltar shares te deixa direcionalmente exposto num mercado que se move US$ 30 por minuto. Não são erros comparáveis, e tratá-los como comparáveis foi meu equívoco original.

Vale notar que o código está migrado para o **CLOB V2** da Polymarket (cutover de 28/04/2026), onde as taxas passaram a ser dinâmicas por mercado, consultadas via `get_clob_market_info()`, e o formato de ordem mudou (saíram `feeRateBps`/`nonce`/`taker`, entraram `timestamp`/`metadata`/`builder`).

#### E aqui está o problema que eu não consegui resolver

O gross-up conserta a média, mas não conserta o caso individual. E o caso individual é o que importa.

Eu pedia 7 contratos. O que chegava era **6,7. Ou 7,1. Quase nunca 7.**

A perna da Kalshi é de 7 contratos inteiros: a Kalshi não negocia frações. A perna da Poly vem fracionada, porque a taxa é descontada em shares e o resultado depende do preço exato de execução. Então toda operação termina desbalanceada:

```
Poly entrega 6,7  →  0,3 contrato DESCOBERTO
                     exposição direcional que eu não pedi

Poly entrega 7,1  →  0,1 contrato SOBRANDO
                     custo extra, sem hedge do outro lado
```

Não existe configuração que faça isso fechar exato. As duas plataformas têm granularidades incompatíveis: uma trabalha em contratos inteiros, a outra em shares fracionárias com desconto proporcional. Você pode escolher **de que lado** errar, mas não pode escolher **não errar**.

E isso destrói o valor esperado por um caminho que não aparece em nenhuma simulação. O cálculo de arbitragem assume que as duas pernas se cancelam perfeitamente: uma paga $1, a outra paga $0, e você fica com a diferença de preço. Com 0,3 contrato descoberto, uma fração da sua posição é aposta direcional pura: sujeita à volatilidade inteira do BTC, não ao spread de 2 centavos que você foi buscar.

Numa operação cujo lucro alvo é ~$0,15, carregar 0,3 contrato exposto a um ativo que se move US$ 30 por minuto não é um detalhe de arredondamento. **É a operação inteira.** O lucro previsto vira ruído em cima de uma posição direcional minúscula, e o sinal do resultado passa a depender mais de para onde o BTC andou nos últimos segundos do que do spread que abriu a oportunidade.

Tentei três abordagens, e vale registrar por que nenhuma fecha:

| Abordagem | Por que não resolve |
|---|---|
| Arredondar o size da Poly para cima | Se o preço tem 2 casas decimais, o size tem que ser inteiro (seção 3.4). Arredondar vira **1 contrato inteiro** descoberto, muito pior que 0,3 |
| Ajustar a perna da Kalshi para casar | A Kalshi só negocia inteiros. Não há 6,7 contratos para comprar |
| Top-up do buraco na Poly | O `min_size` de 5 shares torna impossível comprar 0,3. Comprar 5 para cobrir 0,3 cria uma exposição maior que a que você está consertando |

A escolha final foi o menor dos males: mandar exatamente 7 e aceitar ficar descoberto pela fração da taxa (~0,2–0,3 share) em vez de por um contrato inteiro. É uma mitigação, não uma solução. **Toda operação carregava um pedacinho de risco direcional, e não havia como eliminá-lo.**

Esse é o tipo de obstáculo que não aparece em backtest e não aparece em papel. Ele só aparece quando você manda a ordem e conta o que voltou.

### 3.4. Conformidade de ordem: o quebra-cabeça do tamanho

Um obstáculo que eu não previ. A Polymarket impõe três regras **simultâneas** sobre uma ordem:

1. O preço tem que ser múltiplo do `tick_size` do mercado.
2. `tamanho × preço` não pode ter mais de **2 casas decimais** (limite do USDC).
3. O tamanho tem que ser ≥ `min_size`.

A regra 2 é a traiçoeira, porque acopla preço e tamanho. Se o preço tem 2 casas decimais (`0,53`), o tamanho é obrigado a ser **inteiro**, senão o produto estoura o limite. Se o preço tem 1 casa (`0,5`), o tamanho pode ter 1 casa decimal.

Agora combine isso com o gross-up da taxa da seção anterior. Você quer comprar 7,2 shares para receber 7 líquidas. Mas o preço tem 2 casas, então o tamanho tem que ser inteiro. Você arredonda para 8?

**Não.** E entender por que não levou tempo:

- Arredondar para cima (8) → você fica com ~7,7 líquidas contra 7 na Kalshi. Sobra ~0,7 share **descoberta**, direcional.
- Arredondar para baixo (7) → você recebe ~6,8 líquidas. Falta ~0,2 share.

A escolha do bot é o modo `exact_int_mode`: manda **exatamente 7**, e aceita ficar descoberto pela fração da taxa (~0,2–0,3 share) em vez de por um contrato inteiro. É a menor das duas exposições: cerca de 3% de um contrato em vez de 100%.

```python
# exact_int_mode: size = target EXATO, sem gross-up.
# O fee é absorvido "dentro" do target, deixando descoberto
# apenas a fração da taxa (~0,3 em 7) em vez de 1 contrato inteiro.
```

Existe ainda um loop de validação final que verifica o número real de casas decimais do produto e incrementa o tamanho em um *grain* até conformar, sempre para cima, nunca para baixo, pela mesma lógica de assimetria.

### 3.5. Leg risk: ficar preso numa perna

Este é o problema mais difícil do projeto inteiro, e o que gerou mais código.

Uma arbitragem só é uma arbitragem quando as **duas** pernas existem. Enquanto só uma existe, você não tem uma posição neutra: você tem uma **aposta direcional alavancada** num mercado de cripto de 15 minutos. É o pior lugar possível para estar por acidente.

E as duas pernas não podem ser executadas simultaneamente. São duas bolsas, dois protocolos, duas latências.

**Por que sequencial e não em paralelo?** Disparar as duas ao mesmo tempo parece óbvio: minimiza a janela de exposição. Mas se as duas falharem parcialmente, você fica com *duas* posições parciais desbalanceadas em bolsas diferentes, sem nenhum ponto de decisão. Sequencial dá um lugar claro para decidir: se a primeira perna não preencheu, aborta sem risco nenhum.

**Por que Polymarket primeiro?** Ela é a mais lenta e a mais propensa a rejeitar. Falhar primeiro na perna mais frágil é a falha barata: nada foi comprado, nada precisa ser desfeito. Se a ordem fosse invertida, toda rejeição da Poly deixaria uma posição Kalshi órfã.

Ambas as ordens são **FOK** (*fill-or-kill*): preenche tudo imediatamente ou cancela. Nada de preenchimento parcial, nada de ordem passiva dormindo no livro. Isso troca uma taxa de rejeição alta por uma garantia de que você nunca acorda com meia posição.

O preço dessa escolha aparece nos dados reais:

```
25 execuções reais
  16  ABORTADO_POLY        ← 64% rejeitadas: preço moveu antes de chegar
   6  SUCESSO
   1  UNWIND_OK
   1  DESCOBERTO_CRITICO
   1  HEDGE_COMPLETO
```

64% de rejeição. Toda vez, porque o preço mudou entre o snapshot do WebSocket e a ordem chegar. É a latência da seção 3.2 cobrando o pedágio.

Mas o caso que importa é o pior: **Poly preencheu, Kalshi falhou.** Agora existe uma posição real, com dinheiro real, sem hedge. Para isso existe uma cascata de quatro defesas:

**1. Top-up**, se a Poly preencheu parcialmente, compra o pedaço que falta pagando até 4¢ a mais. Melhor pagar caro pelo resto do que ficar desbalanceado.

Com uma trava: se o `min_size` forçar uma compra muito maior que o buraco, aborta. Comprar 5 shares para cobrir um buraco de 0,3 não é um conserto, é uma segunda posição descoberta.

```python
if size_fit > Decimal(str(shares_missing)) + Decimal("1.5"):
    return {"ok": False, "aborted_by_grain": True,...}
```

**2. Unwind**: vende a posição Poly de volta, aceitando 6¢ de slippage. Sai no prejuízo controlado em vez de ficar exposto. Há um delay de 2 s antes de tentar, porque o CLOB precisa creditar as shares na carteira antes que uma venda seja aceita: o delay foi reduzido de 5 s para 2 s justamente para encurtar a janela de exposição.

**3. Hedge loop**, se o unwind também falhar, uma *task* paralela assume. Ela fica observando o ask da Kalshi em tempo real e dispara a compra da perna faltante assim que o preço fica aceitável:

```python
breakeven      = 1.0 − poly_price_paid
max_acceptable = breakeven + UNCOVERED_HEDGE_MAX_LOSS_CENTS / 100
```

O conceito de *breakeven* aqui é a chave. Se você pagou $0,45 na Poly, qualquer preço abaixo de $0,55 na Kalshi ainda fecha o par com lucro. O loop espera pacientemente por esse preço, com tolerância de até 5¢ além do breakeven, até o fim da rodada. Prefere-se fechar a exposição com prejuízo pequeno e conhecido do que carregar risco direcional até a resolução.

**4. Bloqueio de novas arbs**, enquanto existe posição descoberta, nenhuma operação nova é aberta. Sem isso, você empilha pernas órfãs enquanto tenta consertar a primeira.

Há também uma **reconciliação de emergência**: se uma exceção estourar no meio da execução, o `except` não desiste. Ele consulta o estado real nas duas bolsas antes de concluir qualquer coisa. Uma exceção em Python não diz nada sobre o que aconteceu do lado da exchange, e assumir que "deu erro, então nada foi executado" é como se perde dinheiro de verdade.

#### O bug da exposição dobrada

Vale contar em detalhe, porque foi o erro mais caro e o mais instrutivo.

O contador `round_exec_count` limita a uma operação por rodada de 15 minutos. Ele só era incrementado em caso de **SUCESSO**. A sequência do desastre:

```
1. Arb executa → Poly preenche, Kalshi falha → DESCOBERTO_CRITICO
   (round_exec_count NÃO incrementa — não foi "sucesso")

2. hedge_loop compra a perna Kalshi → sucesso → limpa uncovered = None

3. Próximo tick do WebSocket:
     round_exec_count ainda é 0  ✓ passa na guarda
     uncovered é None            ✓ passa na guarda
   → dispara uma arb NOVA na mesma rodada

4. Resultado: 14 shares na Poly onde deveriam existir 7.
   Exposição dobrada, no exato momento em que o mercado virou contra.
```

O conserto foi em duas camadas, deliberadamente redundantes:

```python
# (a) qualquer resultado que TOCOU a Poly consome o slot da rodada,
#     não só SUCESSO
if result_tag in _ROUND_CONSUMING_TAGS and result_tag not in _SUCCESS_TAGS:
    state["round_exec_count"] += 1

# (b) registrar posição descoberta trava a rodada IMEDIATAMENTE
state["round_exec_count"] = max(state["round_exec_count"], MAX_EXECS_PER_ROUND)
```

A lição não é sobre o contador. É que **a recuperação de erro criou um caminho que a lógica principal não previa.** O `hedge_loop` fazia o trabalho dele corretamente (limpar a flag de posição descoberta) e ao fazê-lo destravava uma guarda que dependia daquela flag. Dois componentes corretos individualmente, um estado inconsistente na interseção.

Há também uma flag `unwind_in_progress` que existe puramente para evitar uma condição de corrida entre o `unwind` (que tenta *vender* na Poly) e o `hedge_loop` (que tenta *comprar* na Kalshi). Sem ela, os dois podiam agir simultaneamente sobre a mesma exposição e criar uma posição no sentido contrário. Toda vez que introduzi um mecanismo de recuperação, ele criou uma nova interação para vigiar.

---

## Parte 4. Engenharia: a corrida contra a latência

A oportunidade dura entre algumas centenas de milissegundos e alguns segundos. Cada milissegundo no caminho crítico é probabilidade de a janela fechar antes da ordem chegar.

Uma parte grande dessa latência é geográfica, não algorítmica: rodando de casa, no Brasil, cada ida e volta até os servidores das exchanges já consome centenas de milissegundos antes de o código fazer qualquer coisa. Numa instância AWS bem posicionada isso cai para a casa dos 80 ms. Ainda assim, cada otimização abaixo tem uma medição por trás, porque otimizar sem medir é decorar.

### 4.1. WebSocket em vez de polling

O mais importante e o mais óbvio. Os dois livros chegam por WebSocket, com push de atualizações, não por polling REST.

Na Kalshi é snapshot inicial + deltas incrementais. Cada delta atualiza um nível de preço e recalcula o topo do livro:

```python
def apply_orderbook_delta(msg):
    new_size = kalshi_orderbook[side].get(price, 0.0) + delta
    if new_size < 0.001:
        kalshi_orderbook[side].pop(price, None)
    else:
        kalshi_orderbook[side][price] = new_size
    _update_kalshi_state()
```

A avaliação da arbitragem roda **dentro do handler do WebSocket**, a cada evento de livro. Não existe loop de polling: assim que um preço muda, a oportunidade é reavaliada. É a diferença entre reagir em milissegundos e reagir no próximo tick do relógio.

### 4.2. HTTP/2 com pool de conexões

`httpx` com HTTP/2 no lugar de `requests` síncrono, para toda a API REST da Kalshi:

```python
_http_client = httpx.AsyncClient(http2=True,
    timeout=httpx.Timeout(5.0, connect=3.0),
    limits=httpx.Limits(max_keepalive_connections=10,
        max_connections=20,
        keepalive_expiry=30.0,),)
```

Três ganhos distintos:

- **Pool de conexões**: reaproveita TCP+TLS entre requisições. O handshake TLS custa 30–100 ms, e pagá-lo em toda ordem é inaceitável quando a janela inteira dura algumas centenas de milissegundos.
- **HTTP/2**: multiplexação sobre uma conexão. Importa quando `place_order` e `fetch_order` acontecem em sequência rápida.
- **Async nativo**: elimina o overhead de `run_in_executor` que `requests` exigiria dentro de um loop asyncio.

### 4.3. Cache de `tick_size` com pré-aquecimento

`client.get_tick_size()` é uma chamada HTTP de 80–200 ms. O `tick_size` é imutável dentro de um mercado. Chamá-lo em toda execução é desperdício puro no pior momento possível.

Mas o cache sozinho não resolve, porque o *primeiro* uso da rodada (quando a oportunidade aparece) ainda paga o custo. A solução é **pré-aquecer no momento em que o mercado é descoberto**, muito antes de existir qualquer oportunidade, e para os dois tokens em paralelo:

```python
await asyncio.gather(_get_tick_size_cached(poly_client, token_ids[0]),
    _get_tick_size_cached(poly_client, token_ids[1]),
    return_exceptions=True,)
```

O `return_exceptions=True` é intencional: se o pré-aquecimento falhar, não pode derrubar a descoberta do mercado. A chamada com cache tenta de novo depois. É otimização, não dependência.

O mesmo padrão vale para o cache de coeficiente de taxa da seção 3.3.

### 4.4. CSV assíncrono e não-bloqueante

Uma que só ficou óbvia depois de medir. A gravação de log fazia `open()` + `write()` + `flush()` **inline**, no meio do `execute_arb`. Isso é 3–15 ms de I/O de disco no pior instante possível: logo depois de executar, quando o bot deveria estar reconciliando.

Agora a escrita empurra para uma fila (~10 µs) e uma *task* dedicada drena em lote:

```python
row = await _csv_queue.get()
rows_to_write = [row]
while True:                        # drena o que mais chegou
    try:
        rows_to_write.append(_csv_queue.get_nowait())
    except asyncio.QueueEmpty:
        break
# uma única abertura de arquivo para o lote inteiro
```

Ganho de aproximadamente três ordens de grandeza no caminho crítico, e o `flush` acontece uma vez por lote em vez de uma vez por linha.

### 4.5. Limpar a tela com ANSI, não com `os.system`

A que mais me divertiu, porque é absurda quando você mede:

```python
def _clear_screen():
    print("\033[2J\033[H", end="", flush=False)
```

`os.system('clear')` faz `fork()` + `exec()` de um processo externo: **~11 ms**. A sequência ANSI escreve alguns bytes no stdout: **~0,0003 ms**. Cerca de 30.000× mais rápido, para o mesmo resultado visual.

O `flush=False` também é intencional: deixa o buffer acumular a tela inteira e descarregar de uma vez, em vez de fazer syscall por linha.

Uma interface de terminal atualizando a 4 Hz gastava mais tempo em `fork()` do que o bot inteiro gastava avaliando oportunidades.

### 4.6. Throttle da interface

A UI compete com o processamento de eventos pelo mesmo *event loop*. A taxa de redesenho é adaptativa:

```python
UI_REFRESH_INTERVAL_S       = 0.25   # janela ativa — 4 Hz
UI_REFRESH_INTERVAL_PAUSE_S = 1.0    # janela segura — 1 Hz
```

Fora da janela de operação, nada de importante acontece e a UI pode ser lenta. Dentro, ela ainda precisa ser útil, mas nunca à custa de um evento de livro.

### 4.7. Slippage adaptativo à profundidade

Não é otimização de latência, mas de custo, e resolve um problema real.

O slippage era uma constante de 2¢. Em livros rasos, 2¢ atravessa níveis desnecessariamente e paga caro. Em livros profundos e voláteis, 2¢ pode não bastar e a FOK morre.

A versão adaptativa olha a profundidade real e calcula o mínimo necessário para garantir o preenchimento, mais 1¢ de folga, com teto de 4¢:

```python
best_ask_cents = round(best_ask_price * 100)
worst_cents    = round(worst_needed * 100)
diff_cents     = worst_cents - best_ask_cents
slip = max(1, diff_cents + 1)
return min(slip, KALSHI_SLIPPAGE_MAX_CENTS)
```

Escondido aí tem um bug de ponto flutuante que levou tempo para achar. A versão original subtraía os preços em float **antes** de converter para centavos:

```
0.40 − 0.38  ==  0.020000000000000018
```

Passando isso por `math.ceil(x * 100)` dá **3** centavos, não 2. O bot pagava consistentemente um centavo a mais que o necessário, em toda operação. Num negócio cujo lucro alvo são 2 centavos, isso é metade do resultado evaporando por ruído de representação binária.

A correção é converter para inteiros **primeiro**, depois subtrair. Preços da Kalshi vivem em ticks de 1¢: são inteiros disfarçados de float, e tratá-los como contínuos foi o erro.

### 4.8. Janela segura

Uma guarda temporal simples que evita as duas piores fases do ciclo de 900 s:

```python
def _safe_window():
    s = int(time.time()) % 900
    return 20 <= s < 820
```

- **Primeiros 20 s**: o mercado acabou de abrir, o livro está instável e ralo, os preços não significam nada ainda.
- **Últimos 80 s**, se algo der errado, não sobra tempo para hedgear ou desfazer antes da resolução. Ficar preso numa perna aqui significa carregar a exposição até o fim, sem saída.

Aliado a isso, um monitor de saldo consulta as duas carteiras no início de cada rodada e **congela** a execução se qualquer uma estiver abaixo do orçamento. Descobrir que faltava saldo *depois* de preencher a perna da Poly é exatamente o cenário da seção 3.5.

### 4.9. O resultado

```
Latência de execução medida (25 execuções reais, rodando de casa):
  p50    887 ms
  p90  3.147 ms
  max  3.991 ms
```

**887 ms** na mediana, saindo de uma conexão residencial no Brasil. Migrando para uma instância AWS próxima às exchanges, esse número vai para a casa dos **80 ms**: a maior otimização isolada do projeto inteiro, e ela não está no código. É mudança de endereço.

O que a mediana esconde é o p90 de 3,1 s: uma em cada dez execuções demora tanto que a oportunidade certamente já morreu antes de a ordem chegar. Não por acaso, 64% das ordens da Poly foram rejeitadas por movimento de preço.

A conclusão da Parte 4 é que as otimizações foram todas reais e todas mensuráveis: e, ainda assim, o maior ganho disponível era geográfico. Vale a pena saber onde está o gargalo antes de gastar semanas no lugar errado.

---

## Parte 5. A tentativa de salvação: arbitragem composta de 4 pernas

Quando entendi o problema da divergência, restava uma saída elegante.

Se o risco vem de as duas plataformas resolverem diferente, então **compre os dois lados nas duas plataformas**:

```
YES Kalshi  +  NO Kalshi  +  UP Poly  +  DOWN Poly
```

Agora não importa o que a Kalshi decida: um dos lados dela paga $1. Não importa o que a Polymarket decida: um dos lados dela paga $1. Você recebe **exatamente $2**, em qualquer cenário, incluindo divergência total.

Isso é imune à divergência **por construção**. Não é uma estimativa de risco, é uma identidade contábil.

O lucro é o que sobra:

```
lucro = 2 × min(contratos_entrada, contratos_hedge) − (custo_entrada + custo_hedge)
```

Implementei isso como `PAPER_MODE` e rodei 20 horas contínuas, sem enviar ordens reais: entra virtualmente na primeira arb que cruza 2% de edge, depois espera a arb **oposta** aparecer com pelo menos 1,5%. Se aparecer antes do fim da rodada, o par fecha e o lucro é garantido. Se não aparecer, a posição fica *naked*, e é aí que mora o problema.

### Funcionou

```
81 rodadas · 20 horas · 25–26 de abril de 2026

  82  entradas
  59  pares fechados (HEDGE_OK)
  22  naked no fechamento

  PnL total       $19,06
  Média por par   $ 0,32
  Pior par        $ 0,14      ← positivo
  Melhor par      $ 1,12
  Negativos       0 / 59      ← zero
```

**Zero perdas em 59 pares.** O pior resultado ainda foi lucro. É o que se espera de uma arbitragem de verdade: o resultado é determinístico, a única variável é o tamanho do lucro.

### Mas não dá para forçar a quarta perna

```
Naked rate: 22 / 82 = 26,8%
Delay médio entrada → hedge: 283 s   (mín 9 s, máx 705 s)
```

Uma em cada quatro entradas nunca encontrou o par. A arb oposta simplesmente não apareceu dentro dos 15 minutos.

E é impossível forçar: a arb oposta só existe se o mercado oferecê-la. Você não controla se em algum momento da rodada o outro par vai ficar barato o suficiente. Você entra na primeira perna esperando que a segunda apareça, e em 26,8% das vezes ela não aparece.

Quando isso acontece, você está exatamente onde começou: segurando uma arb de 2 pernas, exposta à divergência. **A imunidade é condicional a completar as 4 pernas, e completar as 4 pernas não é uma decisão sua.**

Cruzando o registro do paper trading com o relatório de divergência, na janela em que os dois se sobrepõem:

```
Das 9 rodadas em que os mercados DIVERGIRAM:
    3  eu estava NAKED     ← exposto
    3  eu estava HEDGED    ← protegido, 4 pernas
    3  eu não estava operando
```

Três eventos. As 20 horas inteiras de operação renderam $19,06, e cada rodada de divergência com posição naked movimenta cerca de $7. **Todo o lucro da sessão cabe dentro de três eventos.**

E, pela mecânica da seção anterior, esses eventos não são um sorteio: quando você fica naked, o lado em que você ficou preso já determina se a divergência vai te pagar $2 ou zero. Ficar naked na arb #1 com delta negativo não é azar: é o resultado previsível de uma exposição que você não escolheu conscientemente.

E o delay médio de 283 s até o hedge revela o custo escondido: em média, quase **cinco minutos** carregando uma posição não-hedgeada. Um terço da rodada exposto direcionalmente, esperando um par que pode não vir.

A estratégia de 4 pernas está teoricamente correta e é a única versão do projeto que realmente elimina o risco de resolução. Ela só não é *executável de forma confiável*, e uma arbitragem que funciona 73% das vezes não é uma arbitragem, é uma aposta com passos extras.

---

## Parte 6. O veredito

Três conclusões independentes, cada uma suficiente para matar o projeto:

**1. Os mercados não medem o mesmo evento.** Strike absoluto sobre índice Kalshi versus variação relativa sobre oráculo Chainlink. As réguas diferem por US$ 7 na mediana, mais que o movimento típico do BTC em 15 minutos. Divergência de 12%. Isso não tem conserto do meu lado: é uma propriedade de como as duas bolsas escreveram seus contratos.

**2. A economia não fecha, mesmo se o item 1 não existisse.** Taxas de ~4,25% no ponto de operação (p ≈ 0,5, o pico exato da curva de taxa), contra spreads que raramente passam disso. E o arredondamento da Polymarket garante que a perna nunca fecha exata: sobra ou falta uma fração de contrato em toda operação, e a fração que falta é exposição direcional que você não pediu. Somado a 64% de rejeição de ordens por movimento de preço, o edge bruto necessário fica grande demais para ser comum.

**3. A versão correta não é executável.** A arb de 4 pernas elimina o risco de resolução por construção, e funcionou, com zero perdas em 59 pares. Mas depende de a arb oposta aparecer, o que aconteceu em apenas 73% das vezes. O restante volta ao problema 1.

E o fecho estatístico: a divergência não é uma moeda justa. Como o sinal do delta determina a direção dela, e 79% dos deltas são negativos, a arb #1 tem EV de **−$0,05 por contrato**: negativo. O bot a executou em 36 das 82 entradas. O que sobra de positivo vem inteiramente da arb #2, cujo edge não é arbitragem: é uma aposta direcional em discrepância de oráculos, medida sobre nove observações, com desvio padrão de doze vezes o retorno. Seriam necessárias ~545 rodadas (5,7 dias ininterruptos) só para distinguir esse edge de zero, e o risco de ruína chega bem antes da significância estatística.

Arbitragem é, por definição, retorno com desvio padrão zero. No instante em que o desvio padrão deixa de ser zero, você não tem mais uma arbitragem: tem uma posição direcional com um nome bonito. A pergunta deixa de ser "quanto eu ganho?" e passa a ser "quanto eu aguento perder até o edge aparecer?". Para esta estratégia, a resposta é: menos do que seria necessário.

---

## O que eu levo disso

**Verifique se os contratos são idênticos antes de escrever qualquer linha de código.** Dois mercados sobre "Bitcoin em 15 minutos" não são o mesmo mercado. A régua está na documentação de resolução, não no título. Eu li o título. Um script de 200 linhas comparando os históricos de resolução teria matado o projeto em uma tarde, e eu só o escrevi *depois* de construir o bot inteiro. Foi a coisa mais valiosa que fiz, e fiz por último.

**Confirme a premissa antes de otimizar a implementação.** Eu passei semanas caçando milissegundos numa estratégia que era estruturalmente inviável. As otimizações eram todas corretas e todas mensuráveis. Nenhuma delas importava. É perfeitamente possível fazer engenharia excelente sobre uma fundação errada, e a qualidade da engenharia não dá nenhum sinal de que a fundação está errada.

**Enviese estimativas para o lado do erro barato.** Sobrar shares custa centavos; faltar shares deixa você direcionalmente exposto. Quando o custo do erro é assimétrico, a estimativa também deve ser. Tratar os dois lados como equivalentes foi o que gerou o bug do "estou colocando um pouco menos", que ficou silencioso por semanas.

**O caminho de recuperação de erro é onde moram os bugs caros.** O bug da exposição dobrada não estava na lógica principal: estava na interação entre o hedge de recuperação e uma guarda que dependia do estado que o hedge limpava. Dois componentes corretos, um estado inconsistente na interseção. Todo mecanismo de recuperação que adicionei criou uma nova interação para vigiar.

**Meça antes de decidir o que é lento.** `os.system('clear')` era um dos maiores custos por iteração do programa, e é uma linha que ninguém olharia duas vezes. Enquanto isso, o gargalo real (Python + internet pública + REST) não era resolvível com nenhuma quantidade de micro-otimização.

**Precisão numérica não é detalhe acadêmico.** `0.40 - 0.38 == 0.020000000000000018` custava um centavo por operação num negócio que ganhava dois. Preços da Kalshi são inteiros em centavos disfarçados de float; tratá-los como contínuos foi um erro conceitual, não um descuido.

**Saber matar o próprio projeto é uma habilidade.** Construir o `hist_divergencie.py` sabendo que ele provavelmente provaria que meses de trabalho eram inúteis foi mais difícil que qualquer parte técnica. E foi a decisão certa. O sunk cost é o argumento mais convincente e mais perigoso que existe.

---

## Arquitetura

O bot inteiro está em [`arbitrage_bot.py`](arbitrage_bot.py): arquivo único, ~3.400 linhas, a vigésima iteração do projeto. Não é a organização que eu escolheria hoje, mas mantive a estrutura original para o registro ser honesto.

```
┌─────────────────┐        ┌─────────────────┐
│   Kalshi WS     │        │ Polymarket WS   │
│ snapshot+deltas │        │  book + price   │
└────────┬────────┘        └────────┬────────┘
         │                          │
         └───────────┬──────────────┘
                     ▼
            ┌────────────────┐
            │  simulate_arb  │  caminha os livros, aplica taxas,
            │   (arb 1 e 2)  │  calcula preço médio real
            └────────┬───────┘
                     │  edge ≥ MIN_PROFIT_PCT ?
                     ▼
            ┌────────────────┐
            │  execute_arb   │  guardas: janela segura, saldo,
            │                │  1 exec/rodada, cooldown, descoberto
            └────────┬───────┘
                     │
          ┌──────────▼──────────┐
          │  1. Poly FOK        │ ← primeiro (perna frágil)
          └──────────┬──────────┘
                     │ preencheu?
          ┌──────────▼──────────┐
          │  2. Kalshi FOK      │ ← slippage adaptativo ao livro
          └──────────┬──────────┘
                     │ falhou?
          ┌──────────▼──────────────────────────┐
          │  top-up → unwind → hedge_loop       │
          │  → bloqueia novas arbs              │
          └─────────────────────────────────────┘
```

**Parâmetros principais**

| Parâmetro | Valor | Razão |
|---|---|---|
| `BUDGET` | $7,00 | tamanho mínimo viável dado o `min_size` da Poly |
| `MIN_PROFIT_PCT` | 2,5% | líquido de taxas |
| `MAX_EXECS_PER_ROUND` | 1 | limita exposição por ciclo de 15 min |
| `POLY_FEE_COEF` | 0,10 | fallback conservador (real vem do mercado) |
| `POLY_FEE_SAFETY_MULT` | 1,10 | margem: errar por excesso é barato |
| `KALSHI_FEE_COEF` | 0,07 | fórmula publicada da Kalshi |
| `KALSHI_SLIPPAGE_MAX_CENTS` | 4 | teto do slippage adaptativo |
| `UNWIND_SLIPPAGE` | 6¢ | agressivo: sair importa mais que o preço |
| `UNCOVERED_HEDGE_MAX_LOSS_CENTS` | 5 | tolerância além do breakeven no hedge |
| Janela segura | 20 s – 820 s | evita abertura instável e fechamento sem saída |

**Dependências:** `websockets`, `httpx[http2]`, `cryptography`, `py_clob_client_v2`

### Sobre esta versão

Este repositório é **documentação técnica, não uma ferramenta operacional.** Duas coisas foram feitas de propósito:

```python
KALSHI_KEY_ID    = "XXXXXXXXXXXX"
POLY_PRIVATE_KEY = "XXXXXXXXXXXX"
#...
CREDENTIALS_REDACTED = True   # trava de execução
```

O bloco de credenciais foi substituído por placeholders literais, e uma trava em `_check_env()` encerra o programa antes de qualquer chamada autenticada. O código roda até ali, imprime a explicação, e para.

Isso não é só higiene de segredos: é coerência com a conclusão. Um repositório que documenta *por que uma estratégia não funciona* não deveria vir pronto para operá-la. Quem quiser estudar o código tem tudo; quem quiser rodá-lo precisa desfazer a trava conscientemente, e a essa altura já leu o motivo de não valer a pena.

Vale repetir o que está no topo. Kalshi e Polymarket estão atualmente proibidos no Brasil.

---

## Licença

MIT. Use como material de estudo. Não use para operar.
