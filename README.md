# Prediction Market Arbitrage: Kalshi × Polymarket

> Estudo de arbitragem entre dois mercados de previsão sobre o mesmo evento:
> o contrato de 15 minutos do Bitcoin. Foco: identificar divergências de
> precificação implícita e avaliar se o *edge* teórico sobrevive a custos de
> transação, execução e latência.

> **Status:** protótipo de pesquisa, com execução real limitada. A oportunidade
> teórica é identificável e aparece com frequência, mas **não se realiza como
> estratégia lucrável**, e o motivo principal não é custo: é que os dois
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

## Formalização

### A condição de arbitragem

Seja um evento binário $E$. Kalshi e Polymarket negociam contratos que pagam
US\$ 1 na resolução. Numa posição casada compramos YES na Kalshi (preço $p_K$) e
NO na Polymarket (preço $p_N$).

O payoff é a soma de dois indicadores de liquidação:

$$X = \mathbb{1}[S_K = \text{YES}] + \mathbb{1}[S_P = \text{NO}]$$

**Se os dois contratos liquidam sobre o mesmo evento**, então $S_K$ e $S_P$ são
perfeitamente anticorrelacionados nessa combinação: exatamente um dos dois paga.
Logo $X \equiv 1$, uma constante. O lucro é determinístico:

$$\Pi = 1 - (p_K + p_N) - \phi$$

onde $\phi$ é o custo total de taxas. Existe arbitragem se e somente se
$p_K + p_N + \phi < 1$. A variância é **exatamente zero**, e é isso que define
uma arbitragem. Guarde esse detalhe: todo o projeto morre quando
$\mathrm{Var}(X) \neq 0$.

### A função de taxa e onde ela é máxima

Ambas as plataformas cobram taxa com a mesma forma funcional:

$$\phi(p) = \kappa \cdot p\,(1-p) \cdot n$$

A parábola $p(1-p)$ tem máximo em $p = 1/2$, onde vale $1/4$. Ou seja, a taxa é
mais cara exatamente quando o mercado está mais indeciso. E um mercado de "BTC
sobe ou desce nos próximos 15 minutos" vive permanentemente em torno de 50/50:

$$\phi_{\max} = \frac{\kappa_P + \kappa_K}{4} = \frac{0{,}10 + 0{,}07}{4} = 4{,}25\%$$

**O bot operava no pico da curva de taxa por construção.** O spread bruto precisa
superar 4,25% antes de qualquer lucro.

### Quando a premissa quebra

Na prática $S_K$ e $S_P$ não são o mesmo evento. Seja
$\delta = \mathbb{P}(S_K \neq S_P)$ a taxa de divergência. Agora
$X \in \{0, 1, 2\}$:

| Cenário | $X$ | Probabilidade |
|---|---|---|
| Concordam | 1 | $1 - \delta$ |
| Divergem a favor (ambas as pernas pagam) | 2 | $\delta \cdot \pi$ |
| Divergem contra (nenhuma paga) | 0 | $\delta (1 - \pi)$ |

onde $\pi$ é a probabilidade condicional de a divergência ser favorável ao lado
em que você está posicionado. O valor esperado do payoff:

$$\mathbb{E}[X] = (1-\delta) + 2\delta\pi = 1 + \delta\,(2\pi - 1)$$

e portanto

$$\boxed{\;\mathbb{E}[\Pi] = \underbrace{(1 - C)}_{\text{edge nominal}} \;+\; \underbrace{\delta\,(2\pi - 1)}_{\text{tilt de divergência}}\;}$$

com $C = p_K + p_N + \phi$ o custo total.

**Esta é a equação central do projeto**, e ela tem uma propriedade que me levou a
uma conclusão errada por um bom tempo.

### O resultado de invariância

Se a divergência for simétrica ($\pi = 1/2$), o termo de tilt **desaparece**:

$$\mathbb{E}[\Pi] = 1 - C \quad \text{para qualquer } \delta$$

O valor esperado não depende da taxa de divergência. Você pode ter 12%, 30% ou
50% de divergência que o EV continua idêntico ao caso sem divergência nenhuma.
Isso acontece porque os payoffs $\{0, 1, 2\}$ são simétricos em torno de 1: uma
divergência simétrica tem média exatamente 1, igual ao caso em que os mercados
concordam.

Minha primeira análise parou aqui e concluiu que o problema era só variância.
Estava incompleta: **$\pi \neq 1/2$, e a assimetria é estrutural.**

### Por que $\pi \neq 1/2$: o argumento geométrico

Sejam $s_K$ o strike da Kalshi, $s_P$ o `priceToBeat` da Polymarket, e $P_T$ o
preço do BTC na liquidação. Defina o gap entre as réguas:

$$\Delta = s_P - s_K$$

A Kalshi resolve YES se $P_T > s_K$; a Polymarket resolve UP se $P_T > s_P$.
Logo há divergência se e somente se $P_T$ cai **entre** as duas referências.
E aqui está o ponto: o **sinal de $\Delta$ determina qual divergência é possível.**

$$\Delta < 0 \;\Rightarrow\; \text{banda } (s_P,\, s_K) \;\Rightarrow\; \text{Poly UP} + \text{Kalshi NO}$$
$$\Delta > 0 \;\Rightarrow\; \text{banda } (s_K,\, s_P) \;\Rightarrow\; \text{Kalshi YES} + \text{Poly DOWN}$$

O outro tipo de divergência é **geometricamente impossível**. Não é
probabilístico. Nos dados, o sinal de $\Delta$ previu corretamente a direção em
**7 das 9 divergências** observadas (as exceções vêm de o índice de liquidação da
Kalshi não ser exatamente o Chainlink, o que adiciona ruído sobre a mecânica).

Como **79% dos $\Delta$ são negativos**, temos $\pi = 0{,}21$ para um lado e
$\pi = 0{,}79$ para o outro. Substituindo na equação central com
$\delta = 0{,}12$ e $C = 0{,}98$:

$$\mathbb{E}[\Pi_{\text{arb}\#1}] = 0{,}02 + 0{,}12\,(2 \cdot 0{,}21 - 1) = \mathbf{-\$0{,}0496}$$
$$\mathbb{E}[\Pi_{\text{arb}\#2}] = 0{,}02 + 0{,}12\,(2 \cdot 0{,}79 - 1) = \mathbf{+\$0{,}0896}$$

**Um dos dois lados da operação tem valor esperado negativo.** O bot não
distinguia entre eles: executou a arb #1 em 36 das 82 entradas.

### O ponto de break-even

Reescrevendo com $\pi = 1 - q$, onde $q = 0{,}79$ é a fração de $\Delta$
negativos, o EV do lado desfavorável vira linear em $\delta$:

$$\mathbb{E}[\Pi] = (1 - C) - \delta\,(2q - 1) = 0{,}02 - 0{,}58\,\delta$$

$$\delta^* = \frac{0{,}02}{0{,}58} = \mathbf{3{,}45\%}$$

A estratégia precisaria de divergência abaixo de 3,45% para sobreviver.
**O observado é 12%, ou 3,5× o break-even.**

### Divergência como razão gap/volatilidade

A pergunta útil vira: o que determina $\delta$? A divergência ocorre quando o
movimento do BTC na janela é **menor que o gap entre as réguas**, e cai do lado
certo. Aproximadamente:

$$\delta \approx \tfrac{1}{2}\,\mathbb{P}\big(|P_T - P_0| < |\Delta|\big)$$

Estimando a distribuição de movimentos de 15 minutos a partir dos strikes
consecutivos (95 observações, $\sigma \approx$ US\$ 72, mediana de $|{\rm mov}|$
de US\$ 31):

| | Valor |
|---|---|
| Gap médio $\|\Delta\|$ | US\$ 8,96 |
| $\mathbb{P}(\|{\rm mov}\| < \|\Delta\|)$ empírico | 17,9% |
| $\delta$ previsto (metade) | ~9% |
| $\delta$ **observado** | **12%** |

O modelo explica a ordem de grandeza. O excedente (12% contra ~9%) é consistente
com os dois índices também se afastarem *durante* a janela, não só na abertura.

A leitura que interessa é a razão adimensional $|\Delta| / \sigma = 0{,}125$.
**A viabilidade da estratégia é função dessa razão.** Para levar $\delta$ abaixo
dos 3,45% de break-even, o gap entre as réguas precisaria ser ~3,5× menor, ou a
volatilidade de 15 minutos ~3,5× maior. Nenhuma das duas está sob o meu controle:
são propriedades de como as duas bolsas escreveram seus contratos.

---

## O que o projeto faz

- **Coleta em tempo real** dos order books das duas plataformas via WebSocket
  (Kalshi com snapshot + deltas incrementais; Polymarket via CLOB).
- **Normalização** dos contratos para probabilidade comparável. Na Kalshi não
  existe "ask" no book: o ask de YES é derivado dos bids de NO
  ($\text{ask}_{\text{YES}} = 1 - \text{bid}^{\max}_{\text{NO}}$).
- **Cálculo do spread** caminhando o book nível a nível, computando o preço médio
  real de execução em vez de assumir que o topo aguenta o tamanho inteiro.
- **Modelagem de custos**: taxa dinâmica por mercado (CLOB V2 da Polymarket),
  spread bid-ask, slippage estimado pela profundidade real, e capital travado até
  a resolução.
- **Execução casada** com ordens *fill-or-kill* nas duas pernas, mais uma cascata
  de recuperação para quando uma perna preenche e a outra falha.
- **Modo paper** que registra as oportunidades sem enviar ordens, usado para
  medir a estratégia sem arriscar capital.

## Por que o arb não fecha

Cinco fatores, em ordem de gravidade.

**1. Casamento imperfeito dos contratos.** O motivo estrutural, formalizado
acima. Os dois mercados perguntam coisas diferentes:

| | Kalshi (`KXBTC15M`) | Polymarket (`btc-updown-15m`) |
|---|---|---|
| Pergunta | "BTC está acima do strike X?" | "O preço no fim é ≥ o preço no início?" |
| Referência | Strike fixo sobre o índice próprio da Kalshi | Oráculo Chainlink BTC/USD |
| Tipo | Nível absoluto | Variação relativa |

Gap mediano de US\$ 7,25 entre as réguas, p90 de US\$ 19,28, contra um movimento
mediano de 15 minutos de US\$ 31. Divergência de 12%, contra break-even de 3,45%.

**2. Taxas no pico da curva.** 4,25% do notional em $p = 1/2$, como derivado
acima. O spread bruto precisa superar isso antes de qualquer lucro.

**3. Granularidade incompatível.** A Kalshi negocia contratos inteiros. A
Polymarket cobra taxa em *shares* e entrega frações: eu pedia 7 contratos e
recebia **6,7 ou 7,1, quase nunca 7**. A fração descoberta é exposição
direcional pura num ativo que se move US\$ 30 por janela. Não há configuração
que feche exato; dá para escolher de que lado errar, não para não errar.

**4. Slippage e profundidade.** Das 25 execuções reais tentadas, **16 (64%)
foram rejeitadas** pela Polymarket porque o preço moveu entre o snapshot do
WebSocket e a ordem chegar.

**5. Latência.** Mediana de **887 ms** de uma conexão residencial no Brasil, p90
de 3,1 s. Boa parte é tempo de viagem: numa instância AWS próxima às exchanges o
número cai para a casa dos **80 ms**. Foi a maior otimização disponível, e não
está no código.

> **Conclusão honesta:** o que parece arbitragem no papel é, na prática,
> **remuneração por assumir risco de casamento e de execução**. Não é almoço
> grátis, e num dos dois lados nem é remuneração: é prejuízo esperado.

## Resultados

**Divergência** (~30 dias de rodadas liquidadas):

| Métrica | Valor |
|---|---|
| Taxa de divergência $\delta$ | **12%** |
| Break-even $\delta^*$ | 3,45% |
| Gap mediano entre as réguas | US\$ 7,25 |
| Gap p90 | US\$ 19,28 |
| Fração de $\Delta$ negativos | 79% |
| Direção prevista pelo sinal de $\Delta$ | 7 de 9 |
| Razão $\|\Delta\|/\sigma_{15\rm min}$ | 0,125 |

**Valor esperado e risco por lado** ($\delta = 12\%$, $q = 79\%$):

| Posição | $\mathbb{E}[\Pi]$ / contrato | $\sigma$ / contrato |
|---|---|---|
| arb #1 (YES Kalshi + NO Poly) | **−\$0,0496** | \$0,339 |
| arb #2 (NO Kalshi + YES Poly) | +\$0,0896 | \$0,339 |
| mix executado (36 / 46) | +\$0,0285 | \$0,339 |

Com o mix real, $\sigma/\mu \approx 12$. O número de rodadas para distinguir o
edge de zero a 95% de confiança:

$$n \geq \left(\frac{z_{\alpha/2}\,\sigma}{\mu}\right)^2 = \left(\frac{1{,}96 \times 0{,}339}{0{,}0285}\right)^2 \approx 545 \text{ rodadas} \approx 5{,}7 \text{ dias}$$

Com banca de US\$ 7 e exposição de US\$ 6,86 por rodada, o risco de ruína chega
muito antes da significância estatística.

**Arbitragem composta de 4 pernas** (20 h de paper trading, 81 rodadas).
Comprar os dois lados nas duas plataformas força $X \equiv 2$ em qualquer
cenário, o que zera a variância e elimina o risco de casamento **por
construção**:

| Métrica | Valor |
|---|---|
| Pares fechados | 59 |
| Resultados negativos | **0** |
| PnL total | \$19,06 |
| Média por par | \$0,32 |
| **Entradas que não acharam o par** | **26,8%** |

É a única versão teoricamente correta, e funcionou. Mas a quarta perna depende de
a operação oposta aparecer dentro da janela, o que só ocorreu em 73% das vezes.
O restante volta ao problema 1.

## Método e decisões

**Definição de "mesmo evento".** Foi aqui que errei, e a correção virou o
resultado do projeto. Casei os mercados pelo título ("BTC em 15 minutos") em vez
da documentação de resolução. Só depois escrevi o script que compara, rodada a
rodada, $s_K$ contra $s_P$, cruzando com o resultado real de cada plataforma.
Foi o que revelou os 12%.

**Estimativa de slippage.** Estática, baseada na profundidade real do book no
instante do sinal, caminhando os níveis até cobrir o tamanho desejado. Se o book
não cobre, a oportunidade é descartada em vez de executada parcialmente. O
slippage enviado à Kalshi é adaptativo: calcula o mínimo necessário para
preencher, mais 1¢ de folga, com teto de 4¢.

**Premissas de custo.** Taxa da Polymarket consultada dinamicamente por mercado
(CLOB V2) com cache, e fallback conservador multiplicado por margem de segurança
de 1,10. O viés é deliberado: sobrar shares custa centavos, faltar shares deixa a
posição descoberta. Quando o custo do erro é assimétrico, a estimativa também
deve ser.

**Ordem de execução.** Sequencial, Polymarket primeiro. É a perna mais lenta e a
que mais rejeita, então falhar nela é a falha barata: nada foi comprado, nada
precisa ser desfeito. Em paralelo, uma falha parcial dos dois lados deixaria duas
posições desbalanceadas sem ponto de decisão.

**O que simplifiquei.** Sem modelo de impacto de mercado, sem risco de
contraparte, sem custo de oportunidade explícito do capital travado. A janela de
15 minutos torna o último pequeno, mas ele existe. A estimativa de $\sigma$ usa
strikes consecutivos como proxy do preço de abertura, o que é razoável mas não
exato.

## Estrutura do repositório

```
README.md          # este arquivo: formalização, resultados, por que não fecha
POST-MORTEM.md     # análise técnica completa (~8.000 palavras)
arbitrage_bot.py   # o bot, arquivo único, ~3.400 linhas
```

**[→ Leia o POST-MORTEM.md](POST-MORTEM.md)** para o aprofundamento:
microestrutura das duas plataformas, o trabalho de latência medido otimização por
otimização, a cascata de recuperação de *leg risk*, e os bugs que custaram
dinheiro (incluindo um erro de ponto flutuante que pagava 1¢ a mais por operação
num negócio que ganhava 2¢).

## Aviso

Projeto de estudo pessoal. Não é recomendação de investimento. Não contém
credenciais: os campos de API foram substituídos por placeholders e a execução
está travada por uma flag (`CREDENTIALS_REDACTED`). Verifique os termos de uso
das APIs antes de reutilizar o código de coleta. Kalshi é uma exchange regulada
(CFTC) e Polymarket tem restrições de jurisdição; o código não contorna nenhuma
delas. **Kalshi e Polymarket estão atualmente proibidos no Brasil.**
