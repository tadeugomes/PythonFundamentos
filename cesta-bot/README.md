# cesta-bot

Bot de cesta de ações da B3 com decisão semanal, backtest contra Ibovespa e CDI, sinais no Telegram e execução opcional no MetaTrader 5 da XP.

## Como funciona

Toda sexta após o fechamento o bot:

1. Baixa 18 meses de preços do Yahoo Finance para o universo em `config.yaml`.
2. Calcula momentum 12-1, SMA50 e volatilidade de cada ativo. Exclui ROE negativo e alavancagem alta (fundamentos via yfinance, cache mensal).
3. Verifica o regime: Ibovespa abaixo da SMA200 reduz a exposição para 50%. Na troca de regime, as posições já abertas são reduzidas ou recompostas ao novo tamanho (`strategy.resize_on_regime`).
4. Vende posições que acionaram o stop móvel (12% do pico), caíram abaixo da SMA50 ou saíram do top 2n de momentum. Compra as melhores ranqueadas até completar a cesta (8 ativos).
5. Registra a carteira em `state/portfolio.json`, envia o resumo no Telegram e, no modo `mt5`, envia as ordens.

O backtest usa só fatores de preço porque não há histórico gratuito confiável de fundamentos. O filtro fundamentalista atua apenas ao vivo.

O sinal usa o fechamento de sexta e, no backtest, as ordens são executadas na abertura do pregão seguinte (`backtest.execution: next_open`), como acontece ao vivo. O caixa parado rende CDI (`backtest.cash_yield`). O CDI vem da série 12 do SGS/BCB, consultada em janelas de 5 anos porque o BCB limita séries diárias a 10 anos por consulta. Se o BCB falhar, o backtest avisa e usa `cdi_annual` fixo.

Se a rodada acontecer antes das 18h de um dia de pregão, a barra do dia é descartada, porque ainda não é o fechamento.

## Instalação

```bash
pip install -r requirements.txt
python backtest.py            # últimos 5 anos (backtest.start: "5y")
```

Saída em `output/backtest.csv`, `output/trades.csv` e `output/backtest.png`. Rode variações: `python backtest.py --start 2015-01-01` (aceita data ou `Ny`). Para medir o efeito de cada premissa: `--execution close`, `--no-cash-yield`, `--no-resize` e `--out output/variante`. Mude parâmetros em `config.yaml` e compare. Desconfie de qualquer combinação que só funciona com um conjunto estreito de valores.

O cache de preços (`cache/prices_close.parquet` e `cache/prices_open.parquet`) só é usado se cobrir a data de início pedida. A rodada semanal baixa os dados sem ler nem gravar esse cache.

### Checagem diária

Por padrão tudo acontece na sexta. Três opções em `strategy` mudam isso:

- `check: daily` checa stop móvel e SMA50 em todo pregão. Sair do top momentum continua sendo critério só da sexta.
- `refill: daily` preenche no dia seguinte as vagas abertas por uma saída, sem esperar a sexta.
- `stop_type: intraday` simula uma ordem stop no pregão, executada no nível do stop ou na abertura se o papel abrir abaixo dele. Só existe no backtest.

Em todos os casos, ranking, regime e ajuste de exposição continuam semanais. Com `check` ou `refill` diários, a rodada ao vivo deve ser agendada para todo dia útil.

Teste feito em 26/10/2026 (CAGR, drawdown máximo, Sharpe sobre o CDI):

| variante | últimos 5 anos | desde 2015 |
|---|---|---|
| semanal (padrão) | 4,0% / -16,9% / -0,38 | 14,1% / -23,9% / 0,29 |
| `check: daily` | 5,4% / -14,6% / -0,34 | 14,0% / -22,9% / 0,30 |
| `check` e `refill: daily` | 6,1% / -19,4% / -0,29 | 16,2% / -19,4% / 0,39 |
| semanal + `stop_type: intraday` | 4,5% / -21,4% / -0,35 | 14,2% / -22,8% / 0,30 |
| `check: daily` + `intraday` | 4,6% / -18,2% / -0,37 | 12,2% / -25,8% / 0,20 |
| Ibovespa | 11,3% / -20,6% | 11,6% / -43,4% |
| CDI | 12,7% | 10,0% |

A reposição diária ficou cerca de 2 pontos ao ano acima da versão semanal nas duas janelas e com stops de 10% a 20%, mas ganhou em só 6 de 12 anos. O stop no pregão não melhorou nenhuma variante. Nos últimos 5 anos nenhuma variante superou o Ibovespa ou o CDI.

### Viés de sobrevivência

O universo é a lista atual de ações do Ibovespa aplicada ao passado. Empresas que saíram do índice ou da bolsa desde 2015 não entram no teste, e empresas que só entraram no índice depois de subir entram desde o início. Isso favorece a cesta na comparação com o Ibovespa. O relatório mostra quantos ativos tinham cotação na data inicial. Não há fonte gratuita da composição histórica do índice. Trate o excesso sobre o Ibovespa como limite superior.

Tickers que mudam de código somem do Yahoo com todo o histórico. O bot avisa quando um ativo do universo vem sem cotação. Em 2025 e 2026 mudaram BRFS3 (MBRF3), CPLE6 (CPLE3), ELET3 (AXIA3), EMBR3 (EMBJ3) e NTCO3 (NATU3).

## Modos de execução

Defina `execution.mode` em `config.yaml`:

| modo | o que faz |
|---|---|
| `paper` | carteira simulada, só imprime no terminal |
| `telegram` | carteira simulada + mensagem no Telegram |
| `mt5` | envia ordens pelo MetaTrader 5 (com `dry_run: true` só simula) |

Ordem recomendada: `paper` por duas semanas para validar o fluxo, `telegram` por dois ou três meses, depois `mt5` com `dry_run: true` por duas semanas, e só então `dry_run: false` com `capital_per_position` baixo (R$ 500 por ativo usa o mercado fracionário).

## Telegram

1. No Telegram, fale com `@BotFather`, envie `/newbot` e guarde o token.
2. Envie qualquer mensagem ao seu bot, depois abra `https://api.telegram.org/bot<TOKEN>/getUpdates` e copie o `chat.id`.
3. Defina as variáveis de ambiente `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.

## MetaTrader 5 na XP

1. No portal XP: Minha Conta > Ferramentas e Serviços > Assinaturas de plataformas > MetaTrader 5. Peça a conta demo primeiro (servidor `XPMT5-DEMO`).
2. Instale o terminal MT5 no Windows (ou VPS Windows), faça login uma vez manualmente e habilite "Permitir trading algorítmico" em Ferramentas > Opções > Expert Advisors.
3. `pip install MetaTrader5` e defina `MT5_LOGIN` e `MT5_PASSWORD`. Ajuste `mt5.server` e `mt5.terminal_path` no config.
4. Teste: `python run_weekly.py --mode mt5` com `dry_run: true`.

Com `dry_run: false`, o bot só envia ordens entre 10:05 e 16:50 (Brasília) em dia útil e para sem registrar nada fora desse horário. Ordens a mercado com o pregão fechado são rejeitadas. Quantidades são divididas em lote padrão (múltiplos de 100) e fracionário (resto, ticker + F). Na venda, o bot lê a posição real de cada símbolo no MT5. A carteira registrada guarda só o que a corretora confirmou; ordens rejeitadas aparecem na mensagem como não executadas.

Limitações conhecidas da XP: o MT5 é unicast, então posições abertas pelo home broker não aparecem nele. Mantenha a cesta inteira dentro do MT5 e não misture com operações manuais na mesma conta, ou use a sub-conta demo para o teste. O fracionário no MT5 pode exigir habilitação; se `PETR4F` não aparecer no Observatório de Mercado, peça à XP ou suba `capital_per_position` para cobrir lotes de 100.

## Agendamento no VPS Windows

Agendador de Tarefas: criar tarefa semanal, segunda 10:15, ação `python C:\cesta-bot\run_weekly.py`, "Iniciar em" `C:\cesta-bot`. Marque "Executar estando o usuário conectado ou não" para o MT5 abrir em sessão. Às 10:15 a barra de segunda ainda é parcial e é descartada, então o sinal usa o fechamento de sexta, como no backtest. Se a segunda for feriado, rode na terça.

Para só sinais (sem MT5), `run_weekly.py --mode telegram` roda sexta após as 18h em qualquer Linux barato ou no GitHub Actions com `cron: "30 21 * * 5"` e os dois segredos do Telegram. No GitHub Actions o container é descartado a cada execução: o workflow precisa fazer commit de `state/portfolio.json` (ou guardá-lo como artefato) ou a carteira recomeça vazia toda semana.

A rodada se recusa a processar duas vezes os mesmos dados de preço, porque isso contaria uma semana extra de posse. Use `--force` para repetir de propósito.

## Estrutura

```
config.yaml      universo, parâmetros, custos, modo
data.py          preços (Yahoo), CDI (BCB), fundamentos (yfinance)
strategy.py      regras de entrada, saída e regime
backtest.py      simulação semanal com custos
run_weekly.py    rodada ao vivo
broker_mt5.py    ordens no MetaTrader 5
notify.py        Telegram
state/           carteira registrada
output/          resultados do backtest
```

## Aviso

Nada aqui é recomendação de investimento. Momentum funciona em média e falha em reversões bruscas, com drawdowns de 30% ou mais em anos como 2015, 2020 e 2021. O regime trocou 54 vezes em 618 semanas de 2015 a 2026, muitas vezes ida e volta em semanas seguidas, e cada troca com `resize_on_regime: true` gira a carteira inteira. O stop móvel é verificado só no fechamento semanal, então a perda por ativo pode passar de 12%. Ele limita perdas por ativo, não da cesta. Use capital que pode perder durante o teste.
