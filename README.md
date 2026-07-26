# Prediction Market Arbitrage: Kalshi × Polymarket

> Estudo de arbitragem entre dois mercados de previsão sobre o mesmo evento:
> o contrato de 15 minutos do Bitcoin. Foco: identificar divergências de
> precificação implícita e avaliar se o *edge* teórico sobrevive a custos de
> transação, execução e latência.

> **Status:** protótipo de pesquisa, com execução real limitada. A oportunidade
> teórica é identificável e aparece com frequência, mas **não se realiza como
> estratégia lucrável**, e o motivo principal não é custo, é que os dois
> mercados não medem o mesmo evento. Ver [Por que o arb não fecha](#por-que-o-arb-não-fecha).
> Este repositório é deliberadamente um estudo, não um bot de produção.

> **Aviso legal.** Kalshi e Polymarket estão atualmente proibidos no Brasil.
> O projeto foi desenvolvido e testado em abril de 2026, antes da vigência da
> restrição. As credenciais no código foram removidas e a execução está travada.

---

## Por que publicar um projeto que não funciona

Esse projeto foi desenvolvido em abril de 2026 e só agora estou publicando.

E por que estou publicando, você me pergunta? Porque ele não funciona.
Fundamentalmente não funciona. Mas ele me trouxe muito aprendizado e muita
reflexão, e a parte mais valiosa não é o código que ficou de pé, é o
entendimento de *por que* ele nunca poderia funcionar.

Tem uma diferença grande entre "meu bot deu prejuízo" e "meu bot estava medindo
a coisa errada". O primeiro é um problema de engenharia. O segundo é um problema
de premissa, e nenhuma quantidade de otimização resolve. Eu passei semanas
resolvendo o primeiro antes de perceber que o segundo existia.

Documentar isso *é* o resultado do projeto.

---

## Tese

Kalshi e Polymarket negociam contratos sobre eventos parecidos ou idênticos.
Cada preço é uma **probabilidade implícita**. Quando as duas plataformas
discordam além de um limiar, existe, em tese, uma posição casada (comprar a
ponta barata em um book, a ponta oposta no outro) com retorno independente do
resultado do evento.

A pergunta do projeto não é "dá pra ganhar dinheiro?", e sim:
**quão grande precisa ser a divergência para o arb sobreviver aos custos reais
de execução?**

A resposta que encontrei foi mais interessante que a pergunta: nenhum tamanho de
divergência resolve, porque a premissa de que os dois contratos cobrem o mesmo
evento é falsa.

## O que o projeto faz

- **Coleta em tempo real** dos order books das duas plataformas via WebSocket
  (Kalshi com snapshot + deltas incrementais; Polymarket via CLOB).
- **Normalização** dos contratos para probabilidade comparável. Na Kalshi não
  existe "ask" no book: o ask de YES é derivado dos bids de NO (`ask_YES = 1 − melhor_bid_NO`).
- **Cálculo do spread** caminhando o book nível a nível, em vez de assumir que o
  topo aguenta o tamanho inteiro, com preço médio real de execução.
- **Modelagem de custos**: taxa de cada plataforma (dinâmica por mercado no CLOB
  V2 da Polymarket), spread bid-ask, slippage estimado pela profundidade real, e
  capital travado até a resolução.
- **Execução casada** com ordens *fill-or-kill* nas duas pernas, mais uma cascata
  de recuperação para quando uma perna preenche e a outra falha.
- **Modo paper** que registra as oportunidades sem enviar ordens, usado para
  medir a estratégia sem arriscar capital.

## Por que o arb não fecha

Esta é a parte mais importante do repositório. A oportunidade teórica aparece,
mas é consumida por cinco coisas, em ordem de gravidade.

**1. Casamento imperfeito dos contratos.** O motivo estrutural, e o que mata o
projeto. Os dois mercados perguntam coisas diferentes:

| | Kalshi (`KXBTC15M`) | Polymarket (`btc-updown-15m`) |
|---|---|---|
| Pergunta | "BTC está acima do strike X?" | "O preço no fim é ≥ o preço no início?" |
| Referência | Strike fixo sobre o índice próprio da Kalshi | Oráculo Chainlink BTC/USD |
| Tipo | Nível absoluto | Variação relativa |

As duas réguas diferem por uma **mediana de US\$ 7,25** (p90 de US\$ 19,28). Numa
janela de 15 minutos, o Bitcoin frequentemente se move menos que isso. Quando o
preço fecha entre as duas referências, os mercados resolvem em direções opostas.
Medido sobre ~30 dias: **12% das rodadas divergiram.**

Pior: a divergência não é aleatória. Sejam $s_K$ o strike da Kalshi, $s_P$ o
`priceToBeat` da Polymarket e $\Delta = s_P - s_K$ o gap entre as réguas. A
Kalshi resolve YES se o preço final passar de $s_K$; a Polymarket resolve UP se
passar de $s_P$. Há divergência se e somente se o preço fecha **entre** as duas,
e o sinal de $\Delta$ determina qual das duas divergências é possível:

$$
\Delta < 0 \;\Rightarrow\; \text{Kalshi NO} + \text{Poly UP}
\qquad
\Delta > 0 \;\Rightarrow\; \text{Kalshi YES} + \text{Poly DOWN}
$$

A outra é **geometricamente impossível**, não apenas improvável. Nos dados, o
sinal de $\Delta$ acertou a direção em 7 das 9 divergências observadas. E como
**79% dos $\Delta$ são negativos**, um dos dois lados da operação cai
sistematicamente no lado ruim: fica **negativo em valor esperado**
(−\$0,05/contrato), enquanto o outro vira uma aposta direcional disfarçada. O bot
não distinguia os dois.

**2. Taxas no pior ponto possível.** Ambas as plataformas cobram taxa
proporcional a `p × (1 − p)`, uma parábola com máximo exato em p = 0,50. Um
mercado de "BTC sobe ou desce em 15 minutos" vive permanentemente em torno de
50/50, ou seja, no pico da curva:

```
Polymarket : 0,10 × 0,5 × 0,5 = 2,50%
Kalshi     : 0,07 × 0,5 × 0,5 = 1,75%
                               ─────
                        total ≈ 4,25%  do notional
```

O spread bruto precisa passar de 4,25% só para empatar.

**3. Granularidade incompatível entre as plataformas.** A Kalshi negocia
contratos inteiros. A Polymarket cobra a taxa em *shares*, não em dinheiro, e
entrega frações. Eu pedia 7 contratos e recebia **6,7 ou 7,1, quase nunca 7**.
Cada operação terminava com uma fração descoberta, que é exposição direcional
pura num ativo que se move US\$ 30 por minuto. Não existe configuração que feche
exato: dá para escolher de que lado errar, não para não errar.

**4. Slippage e profundidade do book.** O tamanho executável ao preço-alvo é
pequeno, e atravessar níveis apaga o edge. Das 25 execuções reais tentadas,
**16 (64%) foram rejeitadas** pela Polymarket porque o preço moveu entre o
snapshot do WebSocket e a ordem chegar.

**5. Latência.** Rodando de uma conexão residencial no Brasil, a mediana de
execução ficou em **887 ms**, com p90 de 3,1 s. Boa parte disso é tempo de
viagem: numa instância AWS próxima às exchanges o número cai para a casa dos
**80 ms**. Foi a maior otimização disponível no projeto, e não está no código.

> **Conclusão honesta:** o que parece arbitragem no papel é, na prática,
> **remuneração por assumir risco de casamento e de execução**. Não é almoço
> grátis, e num dos dois lados nem é remuneração: é prejuízo esperado.

## Resultados

**Divergência entre as plataformas** (~30 dias de rodadas liquidadas):

| Métrica | Valor |
|---|---|
| Rodadas que divergiram | **12%** |
| Delta mediano entre as réguas | US\$ 7,25 |
| Delta p90 | US\$ 19,28 |
| Deltas negativos | 79% |
| Direção prevista pelo sinal do delta | 7 de 9 casos |

**Valor esperado.** Numa arbitragem de verdade o payoff é constante: exatamente
uma das duas pernas paga US\$ 1, então o retorno é $(1 - C)$ e a variância é zero.
Com divergência o payoff passa a assumir três valores (0, 1 ou 2, quando as duas
pernas perdem, uma paga, ou ambas pagam), e o valor esperado vira:

$$
\mathbb{E}[\Pi] = \underbrace{(1 - C)}_{\text{edge nominal}} + \underbrace{\delta\,(2\pi - 1)}_{\text{tilt de divergência}}
$$

onde $C$ é o custo total com taxas, $\delta$ a taxa de divergência e $\pi$ a
probabilidade de a divergência ser favorável ao lado em que você está.

Repare no que acontece se a divergência for **simétrica** ($\pi = 1/2$): o tilt
zera e o EV não depende de $\delta$. Foi essa invariância que me fez concluir,
na primeira análise, que o problema era só variância. Mas $\pi \neq 1/2$, pelo
argumento geométrico acima. Com $\delta = 12\%$ e 79% dos $\Delta$ negativos:

| Posição | $\pi$ | EV por contrato |
|---|---|---|
| arb #1 (YES Kalshi + NO Poly) | 0,21 | **−\$0,0496** |
| arb #2 (NO Kalshi + YES Poly) | 0,79 | +\$0,0896 |

Para o lado desfavorável o EV é linear em $\delta$, o que dá um break-even
explícito:

$$
\mathbb{E}[\Pi] = 0{,}02 - 0{,}58\,\delta \;\Rightarrow\; \delta^{*} = 3{,}45\%
$$

A estratégia precisaria de divergência abaixo de 3,45%. O observado é 12%, ou
**3,5× o break-even**. O bot executou a arb #1 em 36 das 82 entradas, sem nunca
distinguir os casos.

**Arbitragem composta de 4 pernas** (20 h de paper trading, 81 rodadas).
Comprar os dois lados nas duas plataformas paga exatamente \$2 em qualquer
cenário, o que elimina o risco de casamento por construção:

| Métrica | Valor |
|---|---|
| Pares fechados | 59 |
| Resultados negativos | **0** |
| PnL total | \$19,06 |
| Média por par | \$0,32 |
| **Entradas que não acharam o par** | **26,8%** |

A versão de 4 pernas é a única teoricamente correta, e funcionou. Mas depende de
a operação oposta aparecer dentro da janela de 15 minutos, o que só aconteceu em
73% das vezes. O restante volta ao problema 1.

## Método e decisões

**Definição de "mesmo evento".** Foi aqui que errei, e a correção virou o
resultado do projeto. Eu casei os mercados pelo título ("BTC em 15 minutos") em
vez da documentação de resolução. Só depois escrevi um script que compara, rodada
a rodada, o strike da Kalshi contra o `priceToBeat` da Polymarket, cruzando com o
resultado real de cada uma. Foi o que revelou os 12%.

**Estimativa de slippage.** Estática, baseada na profundidade real do book no
instante do sinal, caminhando os níveis até cobrir o tamanho desejado. Se o book
não cobre, a oportunidade é descartada em vez de executada parcialmente. O
slippage enviado na ordem Kalshi é adaptativo à profundidade: calcula o mínimo
necessário para preencher, mais 1¢ de folga, com teto de 4¢.

**Premissas de custo.** Taxa da Polymarket consultada dinamicamente por mercado
(CLOB V2) com cache, e um fallback conservador multiplicado por uma margem de
segurança de 1,10. O viés é deliberado: sobrar shares custa centavos, faltar
shares deixa a posição descoberta. Quando o erro é assimétrico, a estimativa
também deve ser.

**Ordem de execução.** Sequencial, Polymarket primeiro. Ela é a perna mais lenta
e a que mais rejeita, então falhar nela é a falha barata: nada foi comprado,
nada precisa ser desfeito. Em paralelo, uma falha parcial dos dois lados deixaria
duas posições desbalanceadas sem ponto de decisão.

**O que simplifiquei.** Sem modelo de impacto de mercado, sem risco de
contraparte, sem custo de oportunidade explícito do capital travado. O horizonte
de 15 minutos torna o último pequeno, mas ele existe.

## Estrutura do repositório

```
README.md          # este arquivo: tese, resultados, por que não fecha
POST-MORTEM.md     # análise técnica completa (~8.000 palavras)
arbitrage_bot.py   # o bot, arquivo único, ~3.400 linhas
```

**[→ Leia o POST-MORTEM.md](POST-MORTEM.md)** para o aprofundamento: derivação
completa do valor esperado, microestrutura das duas plataformas, o trabalho de
latência medido otimização por otimização, a cascata de recuperação de *leg
risk*, e os bugs que custaram dinheiro (incluindo um erro de ponto flutuante que
pagava 1¢ a mais por operação num negócio que ganhava 2¢).


## Aviso

Projeto de estudo pessoal. Não é recomendação de investimento. Não contém
credenciais: os campos de API foram substituídos por placeholders e a execução
está travada por uma flag (`CREDENTIALS_REDACTED`). Verifique os termos de uso
das APIs antes de reutilizar o código de coleta. Kalshi é uma exchange regulada
(CFTC) e Polymarket tem restrições de jurisdição; o código não contorna nenhuma
delas. **Kalshi e Polymarket estão atualmente proibidos no Brasil.**
